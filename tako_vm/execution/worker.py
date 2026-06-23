"""
Core worker module for executing code in isolated Docker containers.

Provides both legacy dict-based results and new ExecutionRecord-based results.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tako_vm.config import TakoVMConfig, get_config
from tako_vm.constants import (
    MAX_REQUIREMENTS,
    UV_CACHE_TMP_DIR,
    UV_CACHE_VOLUME,
    UV_CACHE_VOLUME_DIR,
    get_workspace_dir,
)
from tako_vm.execution.docker import (
    EXECUTOR_ENTRYPOINT,
    base_isolation_args,
    decode_subprocess_stream,
    generate_container_name,
    image_exists,
    image_has_executor_entrypoint,
    inspect_oom_killed,
    is_native_linux,
    kill_container,
    remove_container,
    ulimit_args,
)
from tako_vm.execution.health import get_circuit_breaker
from tako_vm.execution.retry import RetryConfig, RetryContext, is_transient_error
from tako_vm.job_types import JobType, JobTypeRegistry
from tako_vm.models import (
    Artifact,
    ExecutionError,
    ExecutionRecord,
    ExecutionTiming,
    InputArtifact,
    ResourceUsage,
    sha256_content,
    sha256_json,
)
from tako_vm.security import (
    cap_output,
    classify_error,
    compute_file_hash,
    is_safe_filename,
    sanitize_error,
    validate_docker_image,
    validate_docker_run_args,
    validate_env_key,
    validate_env_value,
    validate_execution_id,
    validate_pip_requirement,
)

logger = logging.getLogger(__name__)

# Cache for runtime availability check
_gvisor_available: Optional[bool] = None

# stderr patterns that indicate the docker CLI could not reach the daemon.
# These are infrastructure failures, never the fault of the user's code.
_DOCKER_INFRA_STDERR_PATTERNS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "docker daemon is not running",
)


def _require_safe_execution_id(execution_id: str) -> str:
    """Reject execution IDs that are unsafe for filesystem-backed storage."""
    if not validate_execution_id(execution_id):
        raise ValueError("Execution ID must be 1-64 chars of letters, numbers, '.', '_' or '-'")
    return execution_id


def _resolve_run_path(data_dir: Path, execution_id: str, *parts: str) -> Path:
    """Resolve a path under data_dir/runs/<execution_id> and verify containment."""
    safe_execution_id = _require_safe_execution_id(execution_id)
    runs_root = (data_dir / "runs").resolve()
    resolved = (runs_root / safe_execution_id).joinpath(*parts).resolve()

    if not resolved.is_relative_to(runs_root):
        raise ValueError("Resolved run path escaped the configured runs directory")

    return resolved


def prune_stale_workspaces(max_age_seconds: int) -> int:
    """Remove stale per-run workspace dirs stranded under the workspace dir.

    Each execution creates a temp dir (``job-*`` / ``sandbox-*``) that is
    normally removed in a finally block, but a hard crash or an rmtree failure
    leaks it — and there is no other reaper, so host disk grows unbounded. This
    removes such dirs whose mtime is older than ``max_age_seconds`` (chosen to
    sit well past any run's max timeout). Fail-soft: per-dir errors are logged
    and skipped. Returns the number of dirs removed.
    """
    workspace_root = Path(get_workspace_dir())
    if not workspace_root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in workspace_root.iterdir():
        # Whole body in the try: a concurrent run can delete an entry between
        # iterdir() and these checks, and stat()/is_dir() would then raise.
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not (entry.name.startswith("job-") or entry.name.startswith("sandbox-")):
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError as e:
            logger.warning("Failed to prune stale workspace %s: %s", entry, e)
    return removed


def prune_old_run_dirs(data_dir: Path, ttl_days: int) -> int:
    """Remove on-disk run artifact dirs (``data_dir/runs/<id>``) past the TTL.

    The DB record cleanup deletes rows but not the on-disk code/input/output
    artifacts, so they survive the configured retention indefinitely — disk
    growth plus a retention/compliance gap (the stored source code and inputs
    outlive their record). This removes ``runs/<id>`` dirs whose mtime is older
    than ``ttl_days``. Fail-soft per dir. Returns the number of dirs removed.
    """
    runs_root = data_dir / "runs"
    if not runs_root.is_dir():
        return 0
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for entry in runs_root.iterdir():
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError as e:
            logger.warning("Failed to prune old run dir %s: %s", entry, e)
    return removed


def _clear_output_dir(output_dir: Path) -> None:
    """Remove the contents of the output dir between retry attempts.

    A failed attempt may have left a partial ``result.json``, ``.tako_phase``
    file, or artifacts behind; without clearing, those leftovers would be
    reported as the next attempt's results. The directory itself is preserved
    (it is bind-mounted by path and carries the 0o777 mode the container user
    needs) — only its entries are removed. Symlinks are unlinked, never
    followed: untrusted code may have planted a symlink at any name here.

    Fail-soft: per-entry errors are logged and skipped.
    """
    try:
        with os.scandir(output_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path, ignore_errors=True)
                    else:
                        # Regular files and symlinks: unlink removes the link
                        # itself without following it.
                        os.unlink(entry.path)
                except OSError as e:
                    logger.warning("Failed to clear stale output entry %s: %s", entry.path, e)
    except OSError as e:
        logger.warning("Failed to clear output dir %s: %s", output_dir, e)


# Fixed uid of the in-container sandbox user (``useradd -u 1000 sandbox`` in
# Dockerfile.executor); user code always runs as this uid via gosu.
_SANDBOX_UID = 1000


def _make_meta_dir(meta_dir: Path) -> Optional[Path]:
    """Create the control-metadata dir mounted read-write at ``/tako-meta``.

    The entrypoint writes ``.tako_phase`` (phase/timing data that feeds status
    determination) here instead of the 0777 ``/output`` mount, because the
    sandbox user (uid 1000) can unlink and re-create anything in ``/output``
    and thereby forge timing/phase data. Mode 0755 means only the directory's
    owner (the host server process uid) can write. The container runs with
    ``--cap-drop=ALL`` (no CAP_DAC_OVERRIDE), so writes inside the container
    are subject to plain permission checks against that host owner uid:

    - Server running as root (the supported production deployment,
      ``Dockerfile.server``): the dir is uid-0-owned, container root (the
      entrypoint) can write, the sandbox user (uid 1000) cannot. Secure.
    - Server running as a non-root host user: container root cannot write
      either; the entrypoint detects this and falls back to the legacy
      ``/output`` location (and ``parse_phase_file`` falls back with it) —
      no worse than the previous behavior.
    - Server running as host uid 1000 exactly: the dir would be owned by the
      sandbox user inside the container, which as owner could chmod and write
      it — the "trusted" location would be forgeable. Returns None so the
      mount is skipped entirely and the legacy behavior applies.

    Returns:
        The created directory, or None when a trusted meta dir cannot be
        provided (host uid collides with the sandbox uid).
    """
    if hasattr(os, "geteuid") and os.geteuid() == _SANDBOX_UID:
        logger.debug(
            "Server uid collides with in-container sandbox uid %s; "
            "skipping /tako-meta mount (phase file stays in /output)",
            _SANDBOX_UID,
        )
        return None
    meta_dir.mkdir()
    meta_dir.chmod(0o755)
    return meta_dir


def check_gvisor_available() -> bool:
    """
    Check if gVisor (runsc) runtime is available.

    Returns:
        True if gVisor is installed and configured as a Docker runtime
    """
    global _gvisor_available
    if _gvisor_available is not None:
        return _gvisor_available

    try:
        # Check if runsc binary exists
        result = subprocess.run(
            ["docker", "info", "--format", "{{.Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            # `docker info` failed (daemon not up yet, transient hiccup). Don't
            # cache this as "gVisor unavailable" — a one-time probe failure
            # would otherwise sticky-degrade every later job to runc in
            # permissive mode. Leave the cache unset so we re-probe next call.
            logger.warning(
                "gVisor probe inconclusive: `docker info` exited %s (%s); "
                "will re-check on next job",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return False
        _gvisor_available = "runsc" in result.stdout
        return _gvisor_available
    except Exception as e:
        # Transient failure (timeout, docker missing momentarily): log and
        # return False WITHOUT caching, so a flaky probe doesn't permanently
        # disable gVisor for the process lifetime.
        logger.warning("Failed to check gVisor availability (will re-check): %s", e)
        return False


def reset_gvisor_check() -> None:
    """Reset gVisor availability cache (for testing)."""
    global _gvisor_available
    _gvisor_available = None


class RuntimeUnavailableError(Exception):
    """Raised when the required container runtime is not available."""


# Default job type for backward compatibility
DEFAULT_JOB_TYPE = JobType(
    name="default",
    requirements=[],
    memory_limit="512m",
    cpu_limit=1.0,
    timeout=30,
)


def parse_phase_file(
    output_dir: Path, meta_dir: Optional[Path] = None
) -> Optional[ExecutionTiming]:
    """
    Parse the phase tracking file written by entrypoint.sh.

    The phase file contains key=value pairs tracking execution phases:
    - phase: current/final phase (startup, execution, completed, failed)
    - startup_ms: time spent in startup phase
    - dep_install_ms: time spent installing dependencies
    - execution_ms: time spent executing user code
    - total_ms: total container runtime
    - failed_phase: which phase failed (if phase=failed)

    Trust model: the entrypoint prefers writing the phase file to the
    root-only ``/tako-meta`` control mount (``meta_dir`` on the host), which
    sandboxed user code (uid 1000) cannot write to. When the meta copy exists
    it is authoritative and a (possibly forged) ``/output/.tako_phase`` is
    ignored. The ``output_dir`` fallback only applies when no meta copy was
    written — a stale executor image that predates ``/tako-meta``, or a host
    setup where container root could not write the mount — and that fallback
    copy lives in the 0777 output dir, so it remains untrusted legacy data.

    Args:
        output_dir: Path to output directory (legacy/fallback location)
        meta_dir: Path to the root-only control metadata directory mounted at
            /tako-meta (preferred location), or None if not mounted

    Returns:
        ExecutionTiming with parsed timing info, or None if file not found
    """
    phase_file = None
    candidates = []
    if meta_dir is not None:
        candidates.append(meta_dir / ".tako_phase")
    candidates.append(output_dir / ".tako_phase")
    for candidate in candidates:
        # Untrusted code could point .tako_phase at a host file; never follow it.
        if candidate.is_symlink():
            logger.warning("Phase file %s is a symlink, ignoring", candidate)
            continue
        if candidate.exists():
            phase_file = candidate
            break
    if phase_file is None:
        return None

    try:
        content = phase_file.read_text(encoding="utf-8")
        data = {}
        for line in content.strip().split("\n"):
            if "=" in line:
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip()

        # Parse timing values
        timing = ExecutionTiming(
            startup_ms=int(data.get("startup_ms", 0)) if data.get("startup_ms") else None,
            dep_install_ms=int(data.get("dep_install_ms", 0))
            if data.get("dep_install_ms")
            else None,
            execution_ms=int(data.get("execution_ms", 0)) if data.get("execution_ms") else None,
            total_ms=int(data.get("total_ms", 0)) if data.get("total_ms") else None,
            phase_at_exit=data.get("phase")
            if data.get("phase") in ("startup", "execution", "completed", "failed")
            else None,
            dep_install_started=data.get("dep_install_started", "false").lower() == "true",
        )

        # If phase is "failed", check which phase failed
        if data.get("phase") == "failed" and data.get("failed_phase"):
            timing.phase_at_exit = data.get("failed_phase")

        return timing

    except Exception as e:
        logger.warning(f"Failed to parse phase file: {e}")
        return None


def determine_timeout_phase(timing: Optional[ExecutionTiming], timed_out: bool) -> Optional[str]:
    """
    Determine which phase timed out based on timing data.

    Args:
        timing: ExecutionTiming from phase file
        timed_out: Whether the job timed out

    Returns:
        "startup" or "execution" or None
    """
    if not timed_out:
        return None

    if timing is None:
        return None  # Can't determine without timing data

    # If we have execution timing, we made it past startup
    if timing.execution_ms is not None and timing.execution_ms > 0:
        return "execution"

    # If we have startup timing but no execution, we timed out during startup
    if timing.startup_ms is not None:
        return "startup"

    # Check phase_at_exit
    if timing.phase_at_exit in ("startup", "execution"):
        return timing.phase_at_exit

    return None


def resolve_runtime(config: TakoVMConfig) -> str:
    """Resolve the container runtime ('runsc' or 'runc') from config.

    Single source of truth shared by every execution path — the server
    ``CodeExecutor`` and the library ``Sandbox`` — so they apply gVisor
    identically and can't drift. In strict mode gVisor must be available or this
    fails closed; in permissive mode (the current default, see
    ``TakoVMConfig.security_mode``) a missing gVisor falls back to runc with a
    loud warning. An explicit ``container_runtime='runc'`` is honored only
    outside strict mode.

    Raises:
        RuntimeUnavailableError: strict mode and gVisor unavailable, or runc
            explicitly requested under strict mode.
    """
    requested_runtime = config.container_runtime
    security_mode = config.security_mode

    # If runc is explicitly requested, allow it (user knows what they're doing)
    if requested_runtime == "runc":
        if security_mode == "strict":
            raise RuntimeUnavailableError(
                "Cannot use 'runc' runtime in strict security mode. "
                "Use container_runtime='runsc' or set security_mode='permissive'."
            )
        logger.warning(
            "Using 'runc' runtime. This provides weaker isolation than gVisor. "
            "DO NOT USE FOR UNTRUSTED CODE."
        )
        return "runc"

    # gVisor requested (default) - check availability
    if check_gvisor_available():
        logger.info("Using gVisor (runsc) runtime for strong isolation")
        return "runsc"

    # gVisor not available
    if security_mode == "strict":
        raise RuntimeUnavailableError(
            "gVisor (runsc) runtime is not available but required in strict mode. "
            "Install gVisor: https://gvisor.dev/docs/user_guide/install/ "
            "Or set security_mode='permissive' to allow fallback to runc (NOT RECOMMENDED)."
        )

    # Permissive mode - fall back to runc with loud warning
    logger.warning("=" * 60)
    logger.warning("WARNING: gVisor not available, falling back to runc")
    logger.warning("WARNING: This provides WEAKER ISOLATION")
    logger.warning("WARNING: DO NOT USE FOR UNTRUSTED CODE")
    logger.warning("=" * 60)
    return "runc"


class CodeExecutor:
    """Execute Python code in isolated Docker containers."""

    def __init__(
        self,
        docker_image: str = "code-executor:latest",
        default_timeout: int = 30,
        registry: Optional[JobTypeRegistry] = None,
        config: Optional[TakoVMConfig] = None,
    ):
        """
        Initialize the executor.

        Args:
            docker_image: Default Docker image to use (for backward compatibility)
            default_timeout: Default timeout in seconds for executions
            registry: Job type registry for looking up job types
            config: Configuration (uses global config if not provided)

        Raises:
            RuntimeUnavailableError: If gVisor is required but not available
        """
        self.docker_image = docker_image
        self.default_timeout = default_timeout
        self.registry = registry or JobTypeRegistry()
        self.config = config or get_config()

        # Check runtime availability
        self._runtime = self._resolve_runtime()

    def _resolve_runtime(self) -> str:
        """Resolve the runtime via the shared module-level ``resolve_runtime``."""
        return resolve_runtime(self.config)

    def _get_job_type(self, job_type_name: Optional[str]) -> JobType:
        """
        Get job type configuration.

        Args:
            job_type_name: Name of job type, or None for default

        Returns:
            JobType configuration
        """
        if job_type_name is None:
            return DEFAULT_JOB_TYPE

        # Handle version specifier (job_type@version)
        name = job_type_name
        if "@" in job_type_name:
            name = job_type_name.split("@")[0]

        job_type = self.registry.get(name)
        if job_type is None:
            if self.config.production_mode:
                raise ValueError(f"Job type '{name}' not found (production mode)")
            logger.warning(f"Job type '{name}' not found, using default")
            return DEFAULT_JOB_TYPE

        return job_type

    def execute_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a job in an isolated container (legacy interface).

        Args:
            job: Dictionary with keys:
                - id: Job identifier (optional)
                - code: Python code to execute (string)
                - input_data: Input data as dictionary
                - timeout: Timeout in seconds (optional, uses job type default)
                - job_type: Name of job type (optional, uses "default" if not provided)

        Returns:
            Dictionary with execution results:
                - success: Boolean indicating if execution succeeded
                - output: Parsed output data from /output/result.json (if exists)
                - stdout: Standard output from execution
                - stderr: Standard error from execution
                - exit_code: Process exit code
                - error: Error message (if failed)
                - job_type: Name of job type used
        """
        job_id = _require_safe_execution_id(job.get("id", uuid.uuid4().hex))

        # Get job type configuration
        job_type = self._get_job_type(job.get("job_type"))

        # Use job-specific timeout, or job type default
        timeout = job.get("timeout", job_type.timeout)
        startup_timeout = job.get("startup_timeout", job_type.startup_timeout)

        # Create temporary workspace
        workspace = Path(tempfile.mkdtemp(prefix="job-", dir=get_workspace_dir()))

        try:
            # Prepare directories
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)  # Writable by container user (uid 1000)
            # Control metadata (.tako_phase) mount: writable by container
            # root only, NOT by the sandbox user — see _make_meta_dir.
            # None when a trusted dir cannot be provided (mount is skipped).
            meta_dir = _make_meta_dir(workspace / "meta")

            # Write generated code to file
            code_file = code_dir / "main.py"
            code_file.write_text(job["code"])
            code_file.chmod(0o444)  # Read-only

            # Write input data
            input_file = input_dir / "data.json"
            input_file.write_text(json.dumps(job["input_data"]))
            input_file.chmod(0o444)  # Read-only

            # Execute in container
            result = self._run_container(
                code_dir=code_dir,
                input_dir=input_dir,
                output_dir=output_dir,
                timeout=timeout,
                startup_timeout=startup_timeout,
                job_type=job_type,
                extra_requirements=job.get("requirements"),
                job_id=job_id,
                meta_dir=meta_dir,
            )

            # Add job type info to result
            result["job_type"] = job_type.name

            # Read output
            output_file = output_dir / "result.json"
            # Untrusted code could point result.json at a host file; never follow it.
            if output_file.is_symlink():
                logger.warning("result.json is a symlink, ignoring")
            elif output_file.exists():
                try:
                    output_data = json.loads(output_file.read_text())
                    result["output"] = output_data
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse output JSON: {e}")
                    result["output"] = None

            return result

        finally:
            # Cleanup workspace with error logging
            try:
                shutil.rmtree(workspace)
            except Exception as cleanup_err:
                logger.error(
                    "Failed to cleanup workspace %s: %s. Manual cleanup may be required.",
                    workspace,
                    cleanup_err,
                )

    def execute_job_with_record(
        self, job_id: str, job: Dict[str, Any], client_ip: Optional[str] = None
    ) -> ExecutionRecord:
        """
        Execute a job and return an ExecutionRecord.

        This is the new production interface that provides audit-grade records.

        Args:
            job_id: Unique job identifier
            job: Dictionary with code, input_data, timeout, job_type
            client_ip: Client IP address

        Returns:
            ExecutionRecord with complete audit trail
        """
        job_id = _require_safe_execution_id(job_id)

        # Create initial record
        code = job.get("code", "")
        input_data = job.get("input_data", {})

        job_type_name = job.get("job_type") or "default"
        # created_at/queued_at are intentionally left to the model defaults
        # (now): the executor runs at execution start and does not know the
        # true submission timestamps. For async jobs, queue.submit() already
        # persisted them, and storage.save_record's upsert preserves the
        # existing row's submission-identity fields (created_at, queued_at,
        # dequeued_at set by mark_record_running, idempotency fields, ...), so
        # this record's values only matter for the fresh-insert (sync) path.
        record = ExecutionRecord(
            execution_id=job_id,
            status="queued",
            job_type=job_type_name,
            job_ref=f"{job_type_name}@latest",
            code_hash=sha256_content(code),
            input_hash=sha256_json(input_data),
            client_ip=client_ip,
            # Record the effective isolation runtime this job runs under, so the
            # gVisor-vs-runc decision is auditable per job (issue #99). This is
            # the same value passed to base_isolation_args() for the container,
            # so the record can never disagree with what actually ran.
            runtime=self._runtime,
            # Propagate idempotency and lineage fields from job data
            idempotency_key=job.get("idempotency_key"),
            idempotency_fingerprint=job.get("idempotency_fingerprint"),
            parent_execution_id=job.get("parent_execution_id"),
            relationship=job.get("relationship"),
        )

        # Store code and input as internal artifacts for replay support
        replay_artifacts = self._store_replay_artifacts(job_id, code, input_data)
        record.input_artifacts.extend(replay_artifacts)

        # Get job type configuration
        try:
            job_type = self._get_job_type(job.get("job_type"))
            record.job_type = job_type.name
        except ValueError as e:
            # Job type not found in production mode
            record.status = "failed"
            record.ended_at = datetime.now(timezone.utc)
            record.error = ExecutionError(type="config_error", message=str(e))
            return record

        timeout = job.get("timeout", job_type.timeout)
        startup_timeout = job.get("startup_timeout", job_type.startup_timeout)

        # Create temporary workspace
        workspace = Path(tempfile.mkdtemp(prefix="job-", dir=get_workspace_dir()))

        try:
            # Prepare directories
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)  # Writable by container user (uid 1000)
            # Control metadata (.tako_phase) mount: writable by container
            # root only, NOT by the sandbox user — see _make_meta_dir.
            # None when a trusted dir cannot be provided (mount is skipped).
            meta_dir = _make_meta_dir(workspace / "meta")

            # Write generated code to file
            code_file = code_dir / "main.py"
            code_file.write_text(code)
            code_file.chmod(0o444)

            # Write input data
            input_file = input_dir / "data.json"
            input_file.write_text(json.dumps(input_data))
            input_file.chmod(0o444)

            # Mark as running
            record.status = "running"
            record.started_at = datetime.now(timezone.utc)

            # Execute in container with retry for transient failures
            start_time = time.time()
            timed_out = False
            result: Dict[str, Any] = {
                "success": False,
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "error": "execution failed before container run",
            }
            retry_ctx = RetryContext(
                RetryConfig(
                    max_attempts=self.config.max_retry_attempts,
                    base_delay=self.config.retry_base_delay,
                )
            )
            # Surface retry behavior in the audit record: max_attempts is the
            # configured ceiling; attempt is updated to the index of each
            # attempt actually run (0 = no retries occurred).
            record.max_attempts = self.config.max_retry_attempts

            while retry_ctx.should_retry():
                attempt = retry_ctx.attempt
                record.attempt = attempt
                if attempt > 0:
                    # Make retries idempotent. The previous attempt's
                    # container may still exist (its removal is best-effort
                    # and can fail during the very daemon hiccup that
                    # triggered the retry), and its partial output
                    # (result.json, .tako_phase, artifacts) may still be in
                    # output_dir/meta_dir — remove all of these so the retry
                    # cannot collide on a container name or report stale
                    # results from the failed attempt.
                    remove_container(generate_container_name("tako", job_id, attempt=attempt - 1))
                    _clear_output_dir(output_dir)
                    if meta_dir is not None:
                        _clear_output_dir(meta_dir)
                try:
                    result = self._run_container(
                        code_dir=code_dir,
                        input_dir=input_dir,
                        output_dir=output_dir,
                        timeout=timeout,
                        startup_timeout=startup_timeout,
                        job_type=job_type,
                        extra_requirements=job.get("requirements"),
                        job_id=job_id,
                        attempt=attempt,
                        meta_dir=meta_dir,
                    )

                    # Host-level timeout: the job already consumed its full
                    # time budget, so never retry it.
                    if result.get("timed_out"):
                        timed_out = True
                        break

                    # Check for transient Docker errors in result
                    if not result.get("success") and result.get("error"):
                        error_msg = result.get("error", "").lower()
                        if any(
                            pattern in error_msg
                            for pattern in [
                                "circuit breaker",
                                "docker daemon",
                                "docker infrastructure failure",
                                "connection refused",
                            ]
                        ):
                            # Transient error, retry if possible
                            retry_ctx.record_failure(Exception(result.get("error")))
                            if retry_ctx.should_retry():
                                continue
                    break  # Success or non-transient error

                except subprocess.TimeoutExpired:
                    # Defensive safety net, effectively unreachable: _run_container
                    # catches TimeoutExpired itself and returns a dict with
                    # timed_out=True instead, so this branch only fires if that
                    # contract is ever broken. Kept so a future regression
                    # surfaces as a timeout rather than an unhandled exception.
                    timed_out = True
                    result = {
                        "success": False,
                        "stdout": "",
                        "stderr": "",
                        "exit_code": -1,
                        "timed_out": True,
                    }
                    break  # Timeout is not retriable

                except Exception as e:
                    if is_transient_error(e):
                        retry_ctx.record_failure(e)
                        if retry_ctx.should_retry():
                            continue
                    # Non-transient or exhausted retries
                    raise

            end_time = time.time()
            wall_time_ms = int((end_time - start_time) * 1000)

            # Update record with results
            record.ended_at = datetime.now(timezone.utc)
            record.duration_ms = wall_time_ms
            record.exit_code = result.get("exit_code")

            # Cap and sanitize outputs. cap_output truncates in-band (appends
            # a notice), so also surface truncation on the record flags — the
            # API must not report truncated output as complete. The flag
            # mirrors cap_output's own truncation condition (UTF-8 encoded
            # size vs the cap), which is robust against the notice text
            # legitimately appearing in user output.
            raw_stdout = result.get("stdout", "")
            raw_stderr = result.get("stderr", "")
            record.stdout = cap_output(raw_stdout, self.config.max_stdout_bytes)
            record.stderr = cap_output(raw_stderr, self.config.max_stderr_bytes)
            record.stdout_truncated = (
                len(raw_stdout.encode("utf-8", errors="replace")) > self.config.max_stdout_bytes
            )
            record.stderr_truncated = (
                len(raw_stderr.encode("utf-8", errors="replace")) > self.config.max_stderr_bytes
            )

            # Resource usage
            record.resource_usage = ResourceUsage(wall_time_ms=wall_time_ms)

            # Collect artifacts from output directory
            record.artifacts = self._collect_artifacts(output_dir, job_id)

            # Read main JSON result (if present)
            output_file = output_dir / "result.json"
            # Untrusted code could point result.json at a host file; never follow it.
            if output_file.is_symlink():
                logger.warning("result.json is a symlink, ignoring")
            elif output_file.exists():
                try:
                    record.result_json = json.loads(output_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError, ValueError) as e:
                    # Output existed but was unreadable/unparseable (truncated,
                    # non-UTF-8, device error). Surface it loudly instead of
                    # presenting result_json=None as "no output produced".
                    logger.warning("Failed to read/parse result.json for job %s: %s", job_id, e)

            # Parse phase timing file (written by entrypoint.sh). The
            # root-only meta_dir copy is preferred; the /output copy is a
            # legacy fallback that sandboxed code can forge.
            timing = parse_phase_file(output_dir, meta_dir)
            record.timing = timing

            # Determine which phase timed out (if applicable)
            internal_timeout = result.get("exit_code") == 124 and determine_timeout_phase(
                timing, True
            )
            timeout_phase = determine_timeout_phase(timing, timed_out) or internal_timeout

            # Determine final status with phase-aware timeout handling
            if timed_out or internal_timeout:
                record.status = "timeout"
                if timeout_phase == "startup":
                    startup_time = timing.startup_ms if timing else None
                    time_info = f" (startup took {startup_time}ms)" if startup_time else ""
                    record.error = ExecutionError(
                        type="startup_timeout",
                        message=f"Startup phase exceeded time limit ({startup_timeout}s){time_info}",
                        phase="startup",
                    )
                elif timeout_phase == "execution":
                    exec_time = timing.execution_ms if timing else None
                    startup_time = timing.startup_ms if timing else None
                    time_info = ""
                    if startup_time and exec_time:
                        time_info = f" (startup: {startup_time}ms, execution: {exec_time}ms)"
                    record.error = ExecutionError(
                        type="execution_timeout",
                        message=f"Code execution exceeded time limit ({timeout}s){time_info}",
                        phase="execution",
                    )
                else:
                    # Fallback to generic timeout if we can't determine phase.
                    # For a host-level timeout, _run_container already built
                    # the message (e.g. "Execution timeout exceeded (Ns)").
                    record.error = ExecutionError(
                        type="timeout",
                        message=result.get("error")
                        or f"Execution exceeded time limit ({timeout}s)",
                        phase=timing.phase_at_exit if timing else None,
                    )
            elif result.get("exit_code") == 137:
                # Exit 137 means SIGKILL, but not necessarily the OOM killer:
                # `docker kill` (cancel path), a pids-limit kill, or user code
                # calling sys.exit(137) all look identical. _run_container
                # inspects State.OOMKilled before removing the container:
                # True -> genuine OOM; False -> killed but NOT by the memory
                # limit; None (inspect failed) -> fall back to the historical
                # behavior of reporting OOM so a flaky inspect never loses a
                # true OOM.
                phase = timing.phase_at_exit if timing else None
                if result.get("oom_killed") is False:
                    record.status = "failed"
                    record.error = ExecutionError(
                        type="killed",
                        message=(
                            "Process was killed (SIGKILL) but not by the memory "
                            "limit (no OOM kill recorded for the container)"
                        ),
                        phase=phase,
                    )
                else:
                    if result.get("oom_killed") is None:
                        # inspect was inconclusive (daemon unreachable, container
                        # already gone). We default to OOM so a flaky inspect
                        # never loses a true OOM — but the classification is a
                        # guess, so say so loudly rather than reporting a
                        # confident "oom".
                        logger.warning(
                            "Job %s exited 137 but OOM inspect was inconclusive; "
                            "defaulting to OOM classification",
                            job_id,
                        )
                    record.status = "oom"
                    record.error = ExecutionError(
                        type="oom",
                        message="Execution exceeded memory limit",
                        phase=phase,
                    )
            elif result.get("success"):
                record.status = "succeeded"
            else:
                record.status = "failed"
                phase = timing.phase_at_exit if timing else None
                if result.get("infra_failure"):
                    # Docker-level failure (daemon down, circuit breaker open):
                    # don't run classify_error on daemon stderr — it would
                    # misattribute the failure to the user's code.
                    record.error = ExecutionError(
                        type="service_unavailable",
                        message=sanitize_error(
                            result.get("error") or "Docker infrastructure failure"
                        ),
                        phase=phase,
                    )
                elif result.get("config_error"):
                    # The job was refused before any container ran (e.g. a raw
                    # base_image without the executor entrypoint contract, or
                    # an invalid image reference). Don't run classify_error on
                    # the explanation text — this is a configuration problem,
                    # not the user's code failing.
                    record.error = ExecutionError(
                        type="config_error",
                        message=sanitize_error(
                            result.get("error") or "Invalid job type configuration"
                        ),
                        phase=phase,
                    )
                else:
                    error_type, error_msg = classify_error(
                        result.get("exit_code", 1), result.get("stderr", ""), timed_out
                    )
                    record.error = ExecutionError(type=error_type, message=error_msg, phase=phase)

            return record

        except Exception as e:
            # Unexpected error — keep the full traceback server-side; the
            # sanitized message is all the API consumer sees.
            logger.exception("Unexpected error executing job %s: %s", job_id, e)
            record.status = "failed"
            record.ended_at = datetime.now(timezone.utc)
            record.error = ExecutionError(type="internal_error", message=sanitize_error(str(e)))
            return record

        finally:
            # Cleanup workspace with error logging
            try:
                shutil.rmtree(workspace)
            except Exception as cleanup_err:
                logger.error(
                    "Failed to cleanup workspace %s: %s. Manual cleanup may be required.",
                    workspace,
                    cleanup_err,
                )

    def _collect_artifacts(self, output_dir: Path, job_id: str) -> List[Artifact]:
        """
        Collect artifacts from output directory and copy to permanent storage.

        Args:
            output_dir: Path to output directory (temp)
            job_id: Job ID for storage key generation

        Returns:
            List of Artifact objects
        """
        artifacts = []
        total_size = 0
        job_id = _require_safe_execution_id(job_id)

        if not output_dir.exists():
            return artifacts

        # Create permanent storage directory for artifacts
        artifacts_dir = _resolve_run_path(self.config.data_dir, job_id, "artifacts")
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        for path in output_dir.iterdir():
            # Reject symlinks before any stat/read/copy: untrusted code in the
            # container can point a symlink in /output at a host-readable file
            # (the config file with api_keys, /proc/self/environ, other runs'
            # data) to exfiltrate it through artifact collection. is_file(),
            # stat(), and copy2() all follow symlinks; is_symlink() does not.
            if path.is_symlink():
                logger.warning("Artifact %s is a symlink, skipping", path.name)
                continue
            if not path.is_file():
                continue

            # Validate filename is safe (no path traversal, no hidden files)
            if not is_safe_filename(path.name):
                logger.warning("Artifact %s has unsafe filename, skipping", path.name)
                continue

            size = path.stat().st_size

            # Check individual file size limit
            if size > self.config.max_artifact_bytes:
                logger.warning("Artifact %s exceeds size limit, skipping", path.name)
                continue

            # Check total size limit
            if total_size + size > self.config.max_total_artifacts_bytes:
                logger.warning("Total artifact size limit reached, stopping collection")
                break

            try:
                file_hash = compute_file_hash(path)
                storage_key = f"runs/{job_id}/artifacts/{path.name}"

                # Copy file to permanent storage
                dest_path = _resolve_run_path(self.config.data_dir, job_id, "artifacts", path.name)
                # follow_symlinks=False guards against a TOCTOU swap of a regular
                # file for a symlink after the is_symlink() check above (copy2
                # follows symlinks by default).
                shutil.copy2(path, dest_path, follow_symlinks=False)

                artifacts.append(
                    Artifact(
                        name=path.name,
                        size_bytes=size,
                        sha256=file_hash,
                        storage_key=storage_key,
                    )
                )
                total_size += size
            except Exception as e:
                logger.warning("Failed to process artifact %s: %s", path.name, e)

        return artifacts

    def _store_replay_artifacts(
        self, execution_id: str, code: str, input_data: dict
    ) -> List[InputArtifact]:
        """
        Store code and input_data as internal artifacts for replay support.

        These internal artifacts (prefixed with _) enable the rerun/fork
        functionality by preserving the exact code and inputs used.

        Args:
            execution_id: Unique execution identifier
            code: Python code that was executed
            input_data: Input data dictionary

        Returns:
            List of InputArtifact objects for the stored files
        """
        replay_artifacts = []
        execution_id = _require_safe_execution_id(execution_id)
        runs_dir = _resolve_run_path(self.config.data_dir, execution_id)

        try:
            # Ensure directory exists
            runs_dir.mkdir(parents=True, exist_ok=True)

            # Store code as _code.py
            code_bytes = code.encode("utf-8")
            code_path = runs_dir / "_code.py"
            code_path.write_text(code, encoding="utf-8")
            replay_artifacts.append(
                InputArtifact(
                    name="_code.py",
                    size_bytes=len(code_bytes),
                    sha256=sha256_content(code),
                    content_type="text/x-python",
                    storage_key=f"runs/{execution_id}/_code.py",
                )
            )

            # Store input_data as _input.json (canonical form)
            input_json = json.dumps(input_data, sort_keys=True, separators=(",", ":"))
            input_bytes = input_json.encode("utf-8")
            input_path = runs_dir / "_input.json"
            input_path.write_text(input_json, encoding="utf-8")
            replay_artifacts.append(
                InputArtifact(
                    name="_input.json",
                    size_bytes=len(input_bytes),
                    sha256=sha256_json(input_data),
                    content_type="application/json",
                    storage_key=f"runs/{execution_id}/_input.json",
                )
            )

            logger.debug("Stored replay artifacts for execution %s", execution_id)

        except Exception as e:
            logger.warning("Failed to store replay artifacts for %s: %s", execution_id, e)
            # Don't fail the execution if replay artifact storage fails

        return replay_artifacts

    def _resolve_image(
        self, job_type: JobType
    ) -> tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
        """Resolve which Docker image to run for a job type.

        Resolution order (each selection is logged for auditability):

        1. **Pre-built job-type image** (``tako-vm-<name>:latest``, produced by
           ``tako-vm build``) — preferred when it exists locally AND carries
           the executor entrypoint contract (``ENTRYPOINT ["/entrypoint.sh"]``,
           the cheap reliable marker of an executor-derived image). Its
           ``job_type.requirements`` are baked in at build time, so the caller
           skips runtime installation of those.
        2. **``job_type.base_image``** — allowed ONLY when the image itself
           carries the executor entrypoint contract (and is therefore present
           locally, since the contract is verified via ``docker image
           inspect``). A raw image (e.g. ``python:3.11-slim``) is refused with
           a config error: without ``/entrypoint.sh`` the
           TAKO_STARTUP_TIMEOUT/TAKO_EXECUTION_TIMEOUT env vars are ignored
           (no in-container timeout at all), no phase/timing file is written,
           and — worst — ``docker run`` appends only the image, so the image's
           default CMD runs instead of ``/code/main.py``. For ``python:slim``
           the REPL exits 0 on EOF, so the job would be recorded as
           "succeeded" with empty output for code that NEVER ran.
           ``ContainerBuilder`` exists precisely to wrap a base image with the
           executor contract, so the error directs users to ``tako-vm build``.
        3. **Default executor image** (``self.docker_image``) — the unchanged
           legacy path; requirements are installed at container startup by the
           entrypoint.

        Returns:
            ``(image_name, source, error)``: on success ``error`` is None and
            ``source`` is ``"built"``/``"base"``/``"default"``; on failure
            ``image_name``/``source`` are None and ``error`` is a
            ready-to-return ``_run_container`` result dict.
        """
        built_image = job_type.image_name
        if validate_docker_image(built_image) and image_exists(built_image):
            if image_has_executor_entrypoint(built_image):
                logger.info(
                    "Job type '%s': using pre-built image %s "
                    "(job type requirements baked in at build time)",
                    job_type.name,
                    built_image,
                )
                return built_image, "built", None
            logger.warning(
                "Job type '%s': pre-built image %s exists but its ENTRYPOINT is not %s "
                "(likely built by an older 'tako-vm build' or from a non-executor "
                "base_image); ignoring it. Rebuild with 'tako-vm build %s'.",
                job_type.name,
                built_image,
                EXECUTOR_ENTRYPOINT,
                job_type.name,
            )

        if job_type.base_image:
            if not validate_docker_image(job_type.base_image):
                return (
                    None,
                    None,
                    {
                        "success": False,
                        "error": "Invalid base_image configuration",
                        "stdout": "",
                        "stderr": f"base_image '{job_type.base_image}' failed validation",
                        "exit_code": -1,
                        "config_error": True,
                    },
                )
            if image_has_executor_entrypoint(job_type.base_image) is not True:
                logger.error(
                    "Job type '%s': refusing to run base_image %s — it does not carry "
                    "the executor entrypoint contract (ENTRYPOINT %s) or is not present "
                    "locally. Build an executor-derived image with 'tako-vm build %s'.",
                    job_type.name,
                    job_type.base_image,
                    EXECUTOR_ENTRYPOINT,
                    job_type.name,
                )
                return (
                    None,
                    None,
                    {
                        "success": False,
                        "error": (
                            f"base_image '{job_type.base_image}' for job type "
                            f"'{job_type.name}' does not carry the executor entrypoint "
                            f"({EXECUTOR_ENTRYPOINT}) or is not present locally; "
                            f"run 'tako-vm build {job_type.name}' to build an "
                            "executor-derived image for this job type"
                        ),
                        "stdout": "",
                        "stderr": (
                            "Raw base images cannot be run directly: without "
                            "/entrypoint.sh the in-container timeouts are not enforced "
                            "and the image's default CMD would run instead of "
                            "/code/main.py, recording a bogus success for code that "
                            f"never ran. Run 'tako-vm build {job_type.name}', or point "
                            "base_image at an executor-derived image that exists on "
                            "this host."
                        ),
                        "exit_code": -1,
                        "config_error": True,
                    },
                )
            logger.info(
                "Job type '%s': using executor-derived base image %s",
                job_type.name,
                job_type.base_image,
            )
            return job_type.base_image, "base", None

        image_name = self.docker_image
        if not validate_docker_image(image_name):
            return (
                None,
                None,
                {
                    "success": False,
                    "error": "Invalid docker_image configuration",
                    "stdout": "",
                    "stderr": f"docker_image '{image_name}' failed validation",
                    "exit_code": -1,
                    "config_error": True,
                },
            )
        logger.debug("Job type '%s': using default executor image %s", job_type.name, image_name)
        return image_name, "default", None

    def _run_container(
        self,
        code_dir: Path,
        input_dir: Path,
        output_dir: Path,
        timeout: int,
        startup_timeout: int,
        job_type: JobType,
        extra_requirements: Optional[List[str]] = None,
        job_id: Optional[str] = None,
        attempt: int = 0,
        meta_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        Run Docker container with security restrictions.

        The container is started WITHOUT ``--rm`` so that on exit code 137 the
        exited container can be inspected for ``State.OOMKilled`` (the only
        authoritative OOM signal — ``--rm`` would race the inspect). Every
        exit path from this method removes the container via a best-effort
        ``docker rm -f`` in a ``finally`` block; the labeled orphan cleanup at
        startup remains as a backstop only.

        Args:
            code_dir: Path to directory containing code (will be mounted read-only)
            input_dir: Path to directory containing input data (will be mounted read-only)
            output_dir: Path to directory for output (will be mounted read-write)
            timeout: Execution timeout in seconds
            startup_timeout: Startup timeout in seconds
            job_type: Job type configuration for container settings
            extra_requirements: Additional requirements to install (merged with job_type)
            job_id: Job/execution ID used for the container name and label
            attempt: Retry attempt index; attempts > 0 get a suffixed
                container name so they cannot collide with a previous
                attempt's not-yet-removed container
            meta_dir: Host dir mounted at /tako-meta for control metadata
                (.tako_phase) writable by container root but not the sandbox
                user, or None to skip the mount (entrypoint then falls back
                to the legacy /output location)

        Returns:
            Dictionary with execution results. On exit code 137 it includes
            ``oom_killed``: True/False from docker inspect, or None if the
            inspect failed (callers should treat None as "assume OOM").
        """
        # Check circuit breaker before attempting Docker operation
        circuit_breaker = get_circuit_breaker()
        if not circuit_breaker.is_available:
            logger.warning(
                "Refusing job %s: Docker circuit breaker is open (repeated Docker failures)",
                job_id,
            )
            return {
                "success": False,
                "error": "Docker service unavailable (circuit breaker open)",
                "stdout": "",
                "stderr": "Circuit breaker is open due to repeated Docker failures. Service will retry automatically.",
                "exit_code": -1,
                "infra_failure": True,
            }

        # Resolve which image to run: a pre-built job-type image, an
        # executor-derived base image, or the default executor image. Raw
        # base images without the executor entrypoint contract are refused
        # here — running them would execute the image's default CMD instead
        # of /code/main.py (see _resolve_image).
        image_name, image_source, image_error = self._resolve_image(job_type)
        if image_error is not None:
            return image_error

        # Validate runtime requirements before deciding network and tmpfs policy.
        # A pre-built image already has job_type.requirements baked in at build
        # time, so only per-job extra requirements still need runtime
        # installation — otherwise the entrypoint would reinstall the full set
        # on every run, defeating the point of `tako-vm build`.
        if image_source == "built":
            all_requirements = []
        else:
            all_requirements = list(job_type.requirements) if job_type.requirements else []
        if extra_requirements:
            all_requirements.extend(extra_requirements)

        if len(all_requirements) > MAX_REQUIREMENTS:
            logger.error(
                "Job has %s requirements (max %s). Use pre-built images for large dependency sets.",
                len(all_requirements),
                MAX_REQUIREMENTS,
            )
            return {
                "success": False,
                "error": f"Too many requirements ({len(all_requirements)} > {MAX_REQUIREMENTS})",
                "stdout": "",
                "stderr": "Use pre-built images for jobs with many dependencies",
                "exit_code": -1,
            }

        validated_reqs = []
        for req in all_requirements:
            if validate_pip_requirement(req):
                validated_reqs.append(req)
            else:
                logger.warning("Skipping invalid pip requirement: %s", req)

        requirements_file = input_dir / "_requirements.txt"
        if validated_reqs:
            if not self.config.allow_runtime_requirements:
                logger.warning(
                    "Refusing job %s: %d runtime requirement(s) requested but "
                    "allow_runtime_requirements=false",
                    job_id,
                    len(validated_reqs),
                )
                return {
                    "success": False,
                    "error": "Runtime dependency installation is disabled",
                    "stdout": "",
                    "stderr": (
                        "Runtime dependency installation is disabled. "
                        "Use pre-built images or set allow_runtime_requirements=true."
                    ),
                    "exit_code": -1,
                }
            requirements_file.write_text("\n".join(validated_reqs) + "\n", encoding="utf-8")
            requirements_file.chmod(0o444)

        # Check if runtime deps require network access
        has_runtime_deps = bool(validated_reqs)
        needs_network_for_deps = has_runtime_deps and not job_type.network_enabled

        if needs_network_for_deps:
            logger.warning(
                f"Job type '{job_type.name}' has requirements but network_enabled=false. "
                "Using bridge network for dependency installation. "
                "For true network isolation, use pre-built images via 'tako-vm build'."
            )

        # Generate container name for tracking (allows cleanup on timeout).
        # Retry attempts get a unique suffixed name; see
        # generate_container_name for the queue.py cancel/watchdog caveat.
        container_name = generate_container_name("tako", job_id, attempt=attempt)

        # auto_remove=False: keep the exited container so a 137 exit can be
        # checked against `docker inspect .State.OOMKilled` (with --rm the
        # daemon removes the container before it can be inspected). The
        # finally block below guarantees removal on every exit path.
        cmd = base_isolation_args(
            container_name,
            runtime=self._runtime,
            enable_cap_restrictions=self.config.enable_cap_restrictions,
            execution_id=job_id,
            auto_remove=False,
        )

        # Mount uv cache volume for faster repeated installs
        if has_runtime_deps:
            uv_cache_dir = UV_CACHE_TMP_DIR
            if self.config.enable_runtime_dependency_cache:
                uv_cache_dir = UV_CACHE_VOLUME_DIR
                cmd.append(f"--mount=type=volume,source={UV_CACHE_VOLUME},target={uv_cache_dir}")
            cmd.append(f"--env=UV_CACHE_DIR={uv_cache_dir}")

        # Network isolation (default: no network for security)
        if job_type.network_enabled:
            cmd.append("--network=bridge")
        elif needs_network_for_deps:
            # Runtime deps need network access even if job wants isolation
            cmd.append("--network=bridge")
        else:
            cmd.append("--network=none")  # Complete network isolation

        # Run as non-root user inside container (uid 1000 = sandbox user)
        # This ensures code never runs as root, even inside the container
        if self.config.enable_userns:
            cmd.append("--user=1000:1000")

        # Resource limits from job type
        limits = self.config.container_limits
        cmd.extend(
            [
                f"--memory={job_type.memory_limit}",
                f"--memory-swap={job_type.memory_limit}",
                f"--cpus={job_type.cpu_limit}",
                f"--pids-limit={limits.pids_limit}",
                # Configurable ulimits (shared with the library Sandbox path via
                # ulimit_args so the two builders can't drift; see issue #97)
                *ulimit_args(limits),
                # Mounts
                f"--mount=type=bind,source={code_dir.absolute()},target=/code,readonly",
                f"--mount=type=bind,source={input_dir.absolute()},target=/input,readonly",
                f"--mount=type=bind,source={output_dir.absolute()},target=/output",
                # Use larger /tmp and allow exec when installing packages (packages go to /tmp/site-packages)
                f"--tmpfs=/tmp:rw,{'exec' if has_runtime_deps else 'noexec'},nosuid,size={'300m' if has_runtime_deps else limits.tmpfs_size}",
            ]
        )

        # Control-metadata mount: the entrypoint (running as container root)
        # writes .tako_phase here instead of the sandbox-writable /output, so
        # user code cannot forge phase/timing data. See _make_meta_dir.
        if meta_dir is not None:
            cmd.append(f"--mount=type=bind,source={meta_dir.absolute()},target=/tako-meta")

        # Add seccomp profile if enabled and exists (native Linux only)
        # Docker Desktop (macOS/Windows) has issues with custom seccomp profiles
        # Some CI environments (GitHub Actions) may also have issues with custom seccomp
        if self.config.enable_seccomp and self.config.seccomp_profile_path:
            if is_native_linux() and self.config.seccomp_profile_path.exists():
                cmd.append(f"--security-opt=seccomp={self.config.seccomp_profile_path}")
            elif not is_native_linux():
                logger.debug("Skipping custom seccomp profile on Docker Desktop")

        # Add environment variables from job type (with validation)
        for key, value in job_type.environment.items():
            if not validate_env_key(key):
                logger.warning(f"Skipping invalid environment variable key: {key}")
                continue
            if not validate_env_value(value):
                logger.warning(f"Skipping environment variable with unsafe value: {key}")
                continue
            cmd.append(f"--env={key}={value}")

        if has_runtime_deps and self.config.dependency_proxy_url:
            cmd.append(f"--env=TAKO_DEPENDENCY_PROXY_URL={self.config.dependency_proxy_url}")

        cmd.append(f"--env=TAKO_STARTUP_TIMEOUT={startup_timeout}")
        cmd.append(f"--env=TAKO_EXECUTION_TIMEOUT={timeout}")

        # Add image name
        cmd.append(image_name)

        container_timeout = startup_timeout + timeout + 5
        if not validate_docker_run_args(cmd):
            logger.error(
                "Refusing job %s: docker run arguments failed safety validation",
                job_id,
            )
            return {
                "success": False,
                "error": "Unsafe Docker command rejected",
                "stdout": "",
                "stderr": "Docker command arguments failed validation",
                "exit_code": -1,
            }

        try:
            result = subprocess.run(
                cmd, timeout=container_timeout, capture_output=True, text=True, check=False
            )

            # `docker run` reserves exit code 125 for failures of docker itself
            # (daemon unreachable, image pull failure, bad flags); container
            # exit codes pass through unchanged otherwise, and our entrypoint
            # never exits 125 in normal operation. Treat 125 — or daemon
            # connectivity errors on stderr of a failed run — as infrastructure
            # failures: they must count against the circuit breaker, be marked
            # retriable via the "error" key, and never be attributed to the
            # user's code by classify_error.
            stderr_lower = (result.stderr or "").lower()
            is_infra_failure = result.returncode == 125 or (
                result.returncode != 0
                and any(pattern in stderr_lower for pattern in _DOCKER_INFRA_STDERR_PATTERNS)
            )
            if is_infra_failure:
                stderr_lines = (result.stderr or "").strip().splitlines()
                detail = (
                    stderr_lines[0]
                    if stderr_lines
                    else f"docker run exited with code {result.returncode}"
                )
                circuit_breaker.record_failure(detail)
                return {
                    "success": False,
                    "error": sanitize_error(f"Docker infrastructure failure: {detail}"),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.returncode,
                    "infra_failure": True,
                }

            # The container ran to completion: any exit code here (including a
            # non-zero exit from user code) means Docker itself is healthy.
            circuit_breaker.record_success()

            # Exit 137 is SIGKILL — could be the OOM killer, but also `docker
            # kill` (cancel path), a pids-limit kill, or user sys.exit(137).
            # Only the exited container's State.OOMKilled distinguishes them;
            # inspect before the finally block removes the container. Note:
            # in-container timeout kills are already remapped to 124 by the
            # entrypoint, so a 137 seen here is a genuine SIGKILL.
            oom_killed: Optional[bool] = None
            if result.returncode == 137:
                oom_killed = inspect_oom_killed(container_name)

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
                "oom_killed": oom_killed,
            }

        except subprocess.TimeoutExpired as e:
            # Timeout is not a Docker failure, don't record with circuit breaker
            # Kill the orphaned container (subprocess died but container keeps running)
            if not kill_container(container_name):
                # An untrusted container that outlived its host timeout and
                # could not be killed is a real containment concern — surface it.
                logger.warning(
                    "Failed to kill container %s after host timeout for job %s; "
                    "it may still be running",
                    container_name,
                    job_id,
                )
            return {
                "success": False,
                "error": f"Execution timeout exceeded ({timeout}s)",
                # Preserve partial output captured up to the kill so the user
                # can see how far their code got before the host-level kill.
                "stdout": decode_subprocess_stream(e.stdout),
                "stderr": decode_subprocess_stream(e.stderr),
                "exit_code": -1,
                "timeout": timeout,
                # Marker for the caller: this run hit the host-level timeout,
                # must be recorded as status="timeout", and must not retry.
                "timed_out": True,
            }
        except FileNotFoundError:
            # Docker command not found - record failure
            circuit_breaker.record_failure("docker command not found")
            return {
                "success": False,
                "error": "Docker command not found",
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
            }
        except Exception as e:
            # Other errors might be Docker-related
            # Kill container in case it was started before the error
            logger.error(
                "Unexpected error running container for job %s: %s",
                job_id,
                e,
                exc_info=True,
            )
            # Best-effort kill; the container may never have started (error
            # before `docker run`), so a False return here is often benign and
            # not worth a warning — the finally block removes it regardless.
            kill_container(container_name)
            error_msg = str(e).lower()
            if "docker" in error_msg or "daemon" in error_msg or "connection" in error_msg:
                circuit_breaker.record_failure(str(e))
            # Sanitize error message before returning to prevent info leakage
            return {
                "success": False,
                "error": sanitize_error(str(e)),
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
            }
        finally:
            # The container is started without --rm (so a 137 exit can be
            # inspected for State.OOMKilled above), which makes this method
            # responsible for removal on EVERY exit path: success, user-code
            # failure, infra failure, host timeout (kill_container above only
            # kills, it does not remove), and unexpected exceptions. Removal
            # is best-effort (`docker rm -f`, errors swallowed); the labeled
            # orphan cleanup at startup is the backstop if it fails.
            remove_container(container_name)


if __name__ == "__main__":
    # Example usage
    executor = CodeExecutor()

    job = {
        "id": "test-123",
        "code": """
import json

# Read input
with open('/input/data.json') as f:
    data = json.load(f)

# Process (example: double all numbers)
result = {k: v * 2 for k, v in data.items()}

# Write output
with open('/output/result.json', 'w') as f:
    json.dump(result, f)

print("Processing complete!")
""",
        "input_data": {"a": 1, "b": 2, "c": 3},
    }

    result = executor.execute_job(job)
    print(json.dumps(result, indent=2))
