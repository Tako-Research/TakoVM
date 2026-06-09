"""
Core worker module for executing code in isolated Docker containers.

Provides both legacy dict-based results and new ExecutionRecord-based results.
"""

import json
import logging
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tako_vm.config import TakoVMConfig, get_config
from tako_vm.constants import MAX_REQUIREMENTS, UV_CACHE_VOLUME, WORKSPACE_DIR
from tako_vm.execution.docker import (
    base_isolation_args,
    generate_container_name,
    is_native_linux,
    kill_container,
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
        _gvisor_available = result.returncode == 0 and "runsc" in result.stdout
        return _gvisor_available
    except Exception as e:
        logger.warning(f"Failed to check gVisor availability: {e}")
        _gvisor_available = False
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


def parse_phase_file(output_dir: Path) -> Optional[ExecutionTiming]:
    """
    Parse the phase tracking file written by entrypoint.sh.

    The phase file contains key=value pairs tracking execution phases:
    - phase: current/final phase (startup, execution, completed, failed)
    - startup_ms: time spent in startup phase
    - dep_install_ms: time spent installing dependencies
    - execution_ms: time spent executing user code
    - total_ms: total container runtime
    - failed_phase: which phase failed (if phase=failed)

    Args:
        output_dir: Path to output directory containing .tako_phase file

    Returns:
        ExecutionTiming with parsed timing info, or None if file not found
    """
    phase_file = output_dir / ".tako_phase"
    # Untrusted code could point .tako_phase at a host file; never follow it.
    if phase_file.is_symlink():
        logger.warning("Phase file is a symlink, ignoring")
        return None
    if not phase_file.exists():
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
    identically and can't drift. In strict mode (the default) gVisor must be
    available or this fails closed; in permissive mode a missing gVisor falls
    back to runc with a loud warning. An explicit ``container_runtime='runc'``
    is honored only outside strict mode.

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
        workspace = Path(tempfile.mkdtemp(prefix="job-", dir=WORKSPACE_DIR))

        try:
            # Prepare directories
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)  # Writable by container user (uid 1000)

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
        record = ExecutionRecord(
            execution_id=job_id,
            status="queued",
            job_type=job_type_name,
            job_ref=f"{job_type_name}@latest",
            created_at=datetime.now(timezone.utc),
            queued_at=datetime.now(timezone.utc),
            code_hash=sha256_content(code),
            input_hash=sha256_json(input_data),
            client_ip=client_ip,
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
        workspace = Path(tempfile.mkdtemp(prefix="job-", dir=WORKSPACE_DIR))

        try:
            # Prepare directories
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)  # Writable by container user (uid 1000)

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

            while retry_ctx.should_retry():
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
                    )

                    # Check for transient Docker errors in result
                    if not result.get("success") and result.get("error"):
                        error_msg = result.get("error", "").lower()
                        if any(
                            pattern in error_msg
                            for pattern in [
                                "circuit breaker",
                                "docker daemon",
                                "connection refused",
                            ]
                        ):
                            # Transient error, retry if possible
                            retry_ctx.record_failure(Exception(result.get("error")))
                            if retry_ctx.should_retry():
                                continue
                    break  # Success or non-transient error

                except subprocess.TimeoutExpired:
                    timed_out = True
                    result = {
                        "success": False,
                        "stdout": "",
                        "stderr": "",
                        "exit_code": -1,
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

            # Cap and sanitize outputs
            record.stdout = cap_output(result.get("stdout", ""), self.config.max_stdout_bytes)
            record.stderr = cap_output(result.get("stderr", ""), self.config.max_stderr_bytes)

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
                except json.JSONDecodeError:
                    pass

            # Parse phase timing file (written by entrypoint.sh)
            timing = parse_phase_file(output_dir)
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
                    # Fallback to generic timeout if we can't determine phase
                    record.error = ExecutionError(
                        type="timeout",
                        message=f"Execution exceeded time limit ({timeout}s)",
                        phase=timing.phase_at_exit if timing else None,
                    )
            elif result.get("exit_code") == 137:
                record.status = "oom"
                phase = timing.phase_at_exit if timing else None
                record.error = ExecutionError(
                    type="oom",
                    message="Execution exceeded memory limit",
                    phase=phase,
                )
            elif result.get("success"):
                record.status = "succeeded"
            else:
                record.status = "failed"
                error_type, error_msg = classify_error(
                    result.get("exit_code", 1), result.get("stderr", ""), timed_out
                )
                phase = timing.phase_at_exit if timing else None
                record.error = ExecutionError(type=error_type, message=error_msg, phase=phase)

            return record

        except Exception as e:
            # Unexpected error
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
    ) -> Dict[str, Any]:
        """
        Run Docker container with security restrictions.

        Args:
            code_dir: Path to directory containing code (will be mounted read-only)
            input_dir: Path to directory containing input data (will be mounted read-only)
            output_dir: Path to directory for output (will be mounted read-write)
            timeout: Execution timeout in seconds
            startup_timeout: Startup timeout in seconds
            job_type: Job type configuration for container settings
            extra_requirements: Additional requirements to install (merged with job_type)

        Returns:
            Dictionary with execution results
        """
        # Check circuit breaker before attempting Docker operation
        circuit_breaker = get_circuit_breaker()
        if not circuit_breaker.is_available:
            return {
                "success": False,
                "error": "Docker service unavailable (circuit breaker open)",
                "stdout": "",
                "stderr": "Circuit breaker is open due to repeated Docker failures. Service will retry automatically.",
                "exit_code": -1,
            }

        # Validate runtime requirements before deciding network and tmpfs policy.
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

        # Determine which image to use
        # Use custom base_image if specified, otherwise use default executor
        # Dependencies are installed at runtime from /input/_requirements.txt.
        if job_type.base_image:
            if not validate_docker_image(job_type.base_image):
                return {
                    "success": False,
                    "error": "Invalid base_image configuration",
                    "stdout": "",
                    "stderr": f"base_image '{job_type.base_image}' failed validation",
                    "exit_code": -1,
                }
            image_name = job_type.base_image
        else:
            image_name = self.docker_image
            if not validate_docker_image(image_name):
                return {
                    "success": False,
                    "error": "Invalid docker_image configuration",
                    "stdout": "",
                    "stderr": f"docker_image '{image_name}' failed validation",
                    "exit_code": -1,
                }

        # Check if runtime deps require network access
        has_runtime_deps = bool(validated_reqs)
        needs_network_for_deps = has_runtime_deps and not job_type.network_enabled

        if needs_network_for_deps:
            logger.warning(
                f"Job type '{job_type.name}' has requirements but network_enabled=false. "
                "Using bridge network for dependency installation. "
                "For true network isolation, use pre-built images via 'tako-vm build'."
            )

        # Generate container name for tracking (allows cleanup on timeout)
        container_name = generate_container_name("tako", job_id)

        cmd = base_isolation_args(
            container_name,
            runtime=self._runtime,
            enable_cap_restrictions=self.config.enable_cap_restrictions,
        )

        # Mount uv cache volume for faster repeated installs
        if has_runtime_deps:
            uv_cache_dir = "/tmp/uv-cache"
            if self.config.enable_runtime_dependency_cache:
                uv_cache_dir = "/root/.cache/uv"
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
                # Configurable ulimits
                f"--ulimit=nofile={limits.nofile_soft}:{limits.nofile_hard}",
                f"--ulimit=nproc={limits.nproc_soft}:{limits.nproc_hard}",
                f"--ulimit=fsize={limits.fsize}",
                # Mounts
                f"--mount=type=bind,source={code_dir.absolute()},target=/code,readonly",
                f"--mount=type=bind,source={input_dir.absolute()},target=/input,readonly",
                f"--mount=type=bind,source={output_dir.absolute()},target=/output",
                # Use larger /tmp and allow exec when installing packages (packages go to /tmp/site-packages)
                f"--tmpfs=/tmp:rw,{'exec' if has_runtime_deps else 'noexec'},nosuid,size={'300m' if has_runtime_deps else limits.tmpfs_size}",
            ]
        )

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

        try:
            container_timeout = startup_timeout + timeout + 5
            if not validate_docker_run_args(cmd):
                return {
                    "success": False,
                    "error": "Unsafe Docker command rejected",
                    "stdout": "",
                    "stderr": "Docker command arguments failed validation",
                    "exit_code": -1,
                }
            result = subprocess.run(
                cmd, timeout=container_timeout, capture_output=True, text=True, check=False
            )

            # Record success with circuit breaker
            circuit_breaker.record_success()

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        except subprocess.TimeoutExpired:
            # Timeout is not a Docker failure, don't record with circuit breaker
            # Kill the orphaned container (subprocess died but container keeps running)
            kill_container(container_name)
            return {
                "success": False,
                "error": f"Execution timeout exceeded ({timeout}s)",
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "timeout": timeout,
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
