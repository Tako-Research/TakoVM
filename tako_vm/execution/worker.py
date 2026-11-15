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
    UV_CACHE_VOLUME_DIR,
    get_workspace_dir,
    uv_cache_volume,
)
from tako_vm.execution.docker import (
    EXECUTOR_ENTRYPOINT,
    base_isolation_args,
    classify_docker_run_result,
    classify_sigkill,
    decode_subprocess_stream,
    generate_container_name,
    image_exists,
    image_has_executor_entrypoint,
    is_native_linux,
    kill_container,
    prepare_requirements_file,
    read_result_json,
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
)

logger = logging.getLogger(__name__)

_gvisor_available: Optional[bool] = None


def _require_safe_execution_id(execution_id: str) -> str:
    if not validate_execution_id(execution_id):
        raise ValueError("Execution ID must be 1-64 chars of letters, numbers, '.', '_' or '-'")
    return execution_id


def _resolve_run_path(data_dir: Path, execution_id: str, *parts: str) -> Path:
    safe_execution_id = _require_safe_execution_id(execution_id)
    runs_root = (data_dir / "runs").resolve()
    resolved = (runs_root / safe_execution_id).joinpath(*parts).resolve()

    if not resolved.is_relative_to(runs_root):
        raise ValueError("Resolved run path escaped the configured runs directory")

    return resolved


def prune_stale_workspaces(max_age_seconds: int) -> int:
    workspace_root = Path(get_workspace_dir())
    if not workspace_root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in workspace_root.iterdir():
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
    try:
        with os.scandir(output_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path, ignore_errors=True)
                    else:
                        os.unlink(entry.path)
                except OSError as e:
                    logger.warning("Failed to clear stale output entry %s: %s", entry.path, e)
    except OSError as e:
        logger.warning("Failed to clear output dir %s: %s", output_dir, e)


_SANDBOX_UID = 1000


def _make_meta_dir(meta_dir: Path) -> Optional[Path]:
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
    global _gvisor_available
    if _gvisor_available is not None:
        return _gvisor_available

    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
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
        logger.warning("Failed to check gVisor availability (will re-check): %s", e)
        return False


def reset_gvisor_check() -> None:
    global _gvisor_available
    _gvisor_available = None


class RuntimeUnavailableError(Exception):
    pass


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
    phase_file = None
    candidates = []
    if meta_dir is not None:
        candidates.append(meta_dir / ".tako_phase")
    candidates.append(output_dir / ".tako_phase")
    for candidate in candidates:
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

        if data.get("phase") == "failed" and data.get("failed_phase"):
            timing.phase_at_exit = data.get("failed_phase")

        return timing

    except Exception as e:
        logger.warning(f"Failed to parse phase file: {e}")
        return None


def determine_timeout_phase(timing: Optional[ExecutionTiming], timed_out: bool) -> Optional[str]:
    if not timed_out:
        return None

    if timing is None:
        return None

    if timing.execution_ms is not None and timing.execution_ms > 0:
        return "execution"

    if timing.startup_ms is not None:
        return "startup"

    if timing.phase_at_exit in ("startup", "execution"):
        return timing.phase_at_exit

    return None


def resolve_runtime(config: TakoVMConfig) -> str:
    requested_runtime = config.container_runtime
    security_mode = config.security_mode

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

    if check_gvisor_available():
        logger.info("Using gVisor (runsc) runtime for strong isolation")
        return "runsc"

    if security_mode == "strict":
        raise RuntimeUnavailableError(
            "gVisor (runsc) runtime is not available but required in strict mode. "
            "Install gVisor: https://gvisor.dev/docs/user_guide/install/ "
            "Or set security_mode='permissive' to allow fallback to runc (NOT RECOMMENDED)."
        )

    logger.warning("=" * 60)
    logger.warning("WARNING: gVisor not available, falling back to runc")
    logger.warning("WARNING: This provides WEAKER ISOLATION")
    logger.warning("WARNING: DO NOT USE FOR UNTRUSTED CODE")
    logger.warning("=" * 60)
    return "runc"


class CodeExecutor:
    def __init__(
        self,
        docker_image: str = "code-executor:latest",
        default_timeout: int = 30,
        registry: Optional[JobTypeRegistry] = None,
        config: Optional[TakoVMConfig] = None,
    ):
        self.docker_image = docker_image
        self.default_timeout = default_timeout
        self.registry = registry or JobTypeRegistry()
        self.config = config or get_config()

        self._runtime = self._resolve_runtime()

    def _resolve_runtime(self) -> str:
        return resolve_runtime(self.config)

    def _get_job_type(self, job_type_name: Optional[str]) -> JobType:
        if job_type_name is None:
            return DEFAULT_JOB_TYPE

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

    def _resolve_execution_timeout(self, job: Dict[str, Any], job_type: JobType) -> int:
        timeout = job.get("timeout")
        if timeout is None:
            timeout = (
                job_type.timeout if job.get("job_type") is not None else self.config.default_timeout
            )
        if timeout > self.config.max_timeout:
            raise ValueError(
                "effective timeout must be less than or equal to the configured "
                f"max_timeout ({self.config.max_timeout} seconds)"
            )
        return timeout

    def execute_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = _require_safe_execution_id(job.get("id", uuid.uuid4().hex))

        job_type = self._get_job_type(job.get("job_type"))

        timeout = self._resolve_execution_timeout(job, job_type)
        startup_timeout = job.get("startup_timeout", job_type.startup_timeout)

        workspace = Path(tempfile.mkdtemp(prefix="job-", dir=get_workspace_dir()))

        try:
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)
            meta_dir = _make_meta_dir(workspace / "meta")

            code_file = code_dir / "main.py"
            code_file.write_text(job["code"])
            code_file.chmod(0o444)

            input_file = input_dir / "data.json"
            input_file.write_text(json.dumps(job["input_data"]))
            input_file.chmod(0o444)

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

            result["job_type"] = job_type.name

            output_file = output_dir / "result.json"
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
        job_id = _require_safe_execution_id(job_id)

        code = job.get("code", "")
        input_data = job.get("input_data", {})

        job_type_name = job.get("job_type") or "default"
        record = ExecutionRecord(
            execution_id=job_id,
            status="queued",
            job_type=job_type_name,
            job_ref=f"{job_type_name}@latest",
            code_hash=sha256_content(code),
            input_hash=sha256_json(input_data),
            client_ip=client_ip,
            runtime=self._runtime,
            idempotency_key=job.get("idempotency_key"),
            idempotency_fingerprint=job.get("idempotency_fingerprint"),
            parent_execution_id=job.get("parent_execution_id"),
            relationship=job.get("relationship"),
        )

        replay_artifacts = self._store_replay_artifacts(job_id, code, input_data)
        record.input_artifacts.extend(replay_artifacts)

        try:
            job_type = self._get_job_type(job.get("job_type"))
            record.job_type = job_type.name
        except ValueError as e:
            record.status = "failed"
            record.ended_at = datetime.now(timezone.utc)
            record.error = ExecutionError(type="config_error", message=str(e))
            return record

        try:
            timeout = self._resolve_execution_timeout(job, job_type)
        except ValueError as e:
            record.status = "failed"
            record.ended_at = datetime.now(timezone.utc)
            record.error = ExecutionError(type="config_error", message=str(e))
            return record
        startup_timeout = job.get("startup_timeout", job_type.startup_timeout)

        workspace = Path(tempfile.mkdtemp(prefix="job-", dir=get_workspace_dir()))

        try:
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)
            meta_dir = _make_meta_dir(workspace / "meta")

            code_file = code_dir / "main.py"
            code_file.write_text(code)
            code_file.chmod(0o444)

            input_file = input_dir / "data.json"
            input_file.write_text(json.dumps(input_data))
            input_file.chmod(0o444)

            record.status = "running"
            record.started_at = datetime.now(timezone.utc)

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
            record.max_attempts = self.config.max_retry_attempts

            while retry_ctx.should_retry():
                attempt = retry_ctx.attempt
                record.attempt = attempt
                if attempt > 0:
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

                    if result.get("timed_out"):
                        timed_out = True
                        break

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
                            retry_ctx.record_failure(Exception(result.get("error")))
                            if retry_ctx.should_retry():
                                continue
                    break

                except subprocess.TimeoutExpired:
                    timed_out = True
                    result = {
                        "success": False,
                        "stdout": "",
                        "stderr": "",
                        "exit_code": -1,
                        "timed_out": True,
                    }
                    break

                except Exception as e:
                    if is_transient_error(e):
                        retry_ctx.record_failure(e)
                        if retry_ctx.should_retry():
                            continue
                    raise

            end_time = time.time()
            wall_time_ms = int((end_time - start_time) * 1000)

            record.ended_at = datetime.now(timezone.utc)
            record.duration_ms = wall_time_ms
            record.exit_code = result.get("exit_code")

            raw_stdout = result.get("stdout", "")
            raw_stderr = result.get("stderr", "")
            record.stdout, record.stdout_truncated = cap_output(
                raw_stdout, self.config.max_stdout_bytes
            )
            record.stderr, record.stderr_truncated = cap_output(
                raw_stderr, self.config.max_stderr_bytes
            )

            record.resource_usage = ResourceUsage(wall_time_ms=wall_time_ms)

            record.artifacts = self._collect_artifacts(output_dir, job_id)

            record.result_json = read_result_json(output_dir, job_id)

            timing = parse_phase_file(output_dir, meta_dir)
            record.timing = timing

            internal_timeout = result.get("exit_code") == 124
            timeout_phase = determine_timeout_phase(timing, timed_out or internal_timeout)

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
                    record.error = ExecutionError(
                        type="timeout",
                        message=result.get("error")
                        or f"Execution exceeded time limit ({timeout}s)",
                        phase=timing.phase_at_exit if timing else None,
                    )
            elif result.get("exit_code") == 137:
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
                    record.error = ExecutionError(
                        type="service_unavailable",
                        message=sanitize_error(
                            result.get("error") or "Docker infrastructure failure"
                        ),
                        phase=phase,
                    )
                elif result.get("config_error"):
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
            logger.exception("Unexpected error executing job %s: %s", job_id, e)
            record.status = "failed"
            record.ended_at = datetime.now(timezone.utc)
            record.error = ExecutionError(type="internal_error", message=sanitize_error(str(e)))
            return record

        finally:
            try:
                shutil.rmtree(workspace)
            except Exception as cleanup_err:
                logger.error(
                    "Failed to cleanup workspace %s: %s. Manual cleanup may be required.",
                    workspace,
                    cleanup_err,
                )

    def _collect_artifacts(self, output_dir: Path, job_id: str) -> List[Artifact]:
        artifacts = []
        total_size = 0
        job_id = _require_safe_execution_id(job_id)

        if not output_dir.exists():
            return artifacts

        artifacts_dir = _resolve_run_path(self.config.data_dir, job_id, "artifacts")
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        for path in output_dir.iterdir():
            if path.is_symlink():
                logger.warning("Artifact %s is a symlink, skipping", path.name)
                continue
            if not path.is_file():
                continue

            if not is_safe_filename(path.name):
                logger.warning("Artifact %s has unsafe filename, skipping", path.name)
                continue

            size = path.stat().st_size

            if size > self.config.max_artifact_bytes:
                logger.warning("Artifact %s exceeds size limit, skipping", path.name)
                continue

            if total_size + size > self.config.max_total_artifacts_bytes:
                logger.warning("Total artifact size limit reached, stopping collection")
                break

            try:
                file_hash = compute_file_hash(path)
                storage_key = f"runs/{job_id}/artifacts/{path.name}"

                dest_path = _resolve_run_path(self.config.data_dir, job_id, "artifacts", path.name)
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
        replay_artifacts = []
        execution_id = _require_safe_execution_id(execution_id)
        runs_dir = _resolve_run_path(self.config.data_dir, execution_id)

        try:
            runs_dir.mkdir(parents=True, exist_ok=True)

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

        return replay_artifacts

    def _resolve_image(
        self, job_type: JobType
    ) -> tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
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
                    "Job type '%s': refusing to run base_image %s, it does not carry "
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

        image_name, image_source, image_error = self._resolve_image(job_type)
        if image_error is not None:
            return image_error

        if image_source == "built":
            all_requirements = []
        else:
            all_requirements = list(job_type.requirements) if job_type.requirements else []
        if extra_requirements:
            all_requirements.extend(extra_requirements)

        try:
            validated_reqs = prepare_requirements_file(
                all_requirements,
                input_dir,
                allow_runtime_requirements=self.config.allow_runtime_requirements,
                max_requirements=MAX_REQUIREMENTS,
            )
        except ValueError as e:
            logger.warning("Refusing job %s: %s", job_id, e)
            return {
                "success": False,
                "error": str(e),
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
            }

        has_runtime_deps = bool(validated_reqs)
        needs_network_for_deps = has_runtime_deps and not job_type.network_enabled

        if needs_network_for_deps:
            logger.warning(
                f"Job type '{job_type.name}' has requirements but network_enabled=false. "
                "Using bridge network for dependency installation. "
                "For true network isolation, use pre-built images via 'tako-vm build'."
            )

        container_name = generate_container_name("tako", job_id, attempt=attempt)

        cmd = base_isolation_args(
            container_name,
            runtime=self._runtime,
            enable_cap_restrictions=self.config.enable_cap_restrictions,
            execution_id=job_id,
            auto_remove=False,
        )

        if has_runtime_deps:
            uv_cache_dir = UV_CACHE_TMP_DIR
            if self.config.enable_runtime_dependency_cache:
                uv_cache_dir = UV_CACHE_VOLUME_DIR
                cache_volume = uv_cache_volume(job_type.name)
                cmd.append(f"--mount=type=volume,source={cache_volume},target={uv_cache_dir}")
            cmd.append(f"--env=UV_CACHE_DIR={uv_cache_dir}")

        if job_type.network_enabled:
            cmd.append("--network=bridge")
        elif needs_network_for_deps:
            cmd.append("--network=bridge")
        else:
            cmd.append("--network=none")

        if self.config.enable_userns:
            cmd.append("--user=1000:1000")

        limits = self.config.container_limits
        cmd.extend(
            [
                f"--memory={job_type.memory_limit}",
                f"--memory-swap={job_type.memory_limit}",
                f"--cpus={job_type.cpu_limit}",
                f"--pids-limit={limits.pids_limit}",
                *ulimit_args(limits),
                f"--mount=type=bind,source={code_dir.absolute()},target=/code,readonly",
                f"--mount=type=bind,source={input_dir.absolute()},target=/input,readonly",
                f"--mount=type=bind,source={output_dir.absolute()},target=/output",
                f"--tmpfs=/tmp:rw,{'exec' if has_runtime_deps else 'noexec'},nosuid,size={'300m' if has_runtime_deps else limits.tmpfs_size}",
            ]
        )

        if meta_dir is not None:
            cmd.append(f"--mount=type=bind,source={meta_dir.absolute()},target=/tako-meta")

        if self.config.enable_seccomp and self.config.seccomp_profile_path:
            if is_native_linux() and self.config.seccomp_profile_path.exists():
                cmd.append(f"--security-opt=seccomp={self.config.seccomp_profile_path}")
            elif not is_native_linux():
                logger.debug("Skipping custom seccomp profile on Docker Desktop")

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

            is_infra_failure, detail = classify_docker_run_result(
                result.returncode, result.stderr or ""
            )
            if is_infra_failure:
                circuit_breaker.record_failure(detail)
                return {
                    "success": False,
                    "error": sanitize_error(f"Docker infrastructure failure: {detail}"),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.returncode,
                    "infra_failure": True,
                }

            circuit_breaker.record_success()

            oom_killed: Optional[bool] = None
            if result.returncode == 137:
                oom_killed = classify_sigkill(container_name)

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
                "oom_killed": oom_killed,
            }

        except subprocess.TimeoutExpired as e:
            if not kill_container(container_name):
                logger.warning(
                    "Failed to kill container %s after host timeout for job %s; "
                    "it may still be running",
                    container_name,
                    job_id,
                )
            return {
                "success": False,
                "error": f"Execution timeout exceeded ({timeout}s)",
                "stdout": decode_subprocess_stream(e.stdout),
                "stderr": decode_subprocess_stream(e.stderr),
                "exit_code": -1,
                "timeout": timeout,
                "timed_out": True,
            }
        except FileNotFoundError:
            circuit_breaker.record_failure("docker command not found")
            return {
                "success": False,
                "error": "Docker command not found",
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
            }
        except Exception as e:
            logger.error(
                "Unexpected error running container for job %s: %s",
                job_id,
                e,
                exc_info=True,
            )
            kill_container(container_name)
            error_msg = str(e).lower()
            if "docker" in error_msg or "daemon" in error_msg or "connection" in error_msg:
                circuit_breaker.record_failure(str(e))
            return {
                "success": False,
                "error": sanitize_error(str(e)),
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
            }
        finally:
            remove_container(container_name)


if __name__ == "__main__":
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
