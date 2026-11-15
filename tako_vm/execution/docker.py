import json
import logging
import os
import platform
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from tako_vm.security import validate_pip_requirement

logger = logging.getLogger(__name__)

_DOCKER_INFRA_STDERR_PATTERNS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "docker daemon is not running",
)

DEFAULT_EXECUTOR_IMAGE = "code-executor:latest"

EXECUTOR_ENTRYPOINT = "/entrypoint.sh"

_IMAGE_CACHE_TTL_SECONDS = 60.0

_image_exists_cache: dict[str, float] = {}
_executor_entrypoint_cache: dict[str, float] = {}


def reset_image_caches() -> None:
    _image_exists_cache.clear()
    _executor_entrypoint_cache.clear()


def decode_subprocess_stream(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def image_exists(image_name: str) -> bool:
    now = time.monotonic()
    expiry = _image_exists_cache.get(image_name)
    if expiry is not None and now < expiry:
        return True
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as e:
        logger.debug("Failed to inspect image %s: %s", image_name, e)
        return False
    if result.returncode == 0:
        _image_exists_cache[image_name] = now + _IMAGE_CACHE_TTL_SECONDS
        return True
    return False


def image_has_executor_entrypoint(image_name: str) -> Optional[bool]:
    now = time.monotonic()
    expiry = _executor_entrypoint_cache.get(image_name)
    if expiry is not None and now < expiry:
        return True
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Entrypoint}}", image_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as e:
        logger.debug("Failed to inspect entrypoint of image %s: %s", image_name, e)
        return None
    if result.returncode != 0:
        logger.debug("docker image inspect of %s failed (exit %s)", image_name, result.returncode)
        return None
    try:
        entrypoint = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError:
        logger.debug("Unparseable entrypoint for image %s: %r", image_name, result.stdout)
        return None
    if entrypoint == [EXECUTOR_ENTRYPOINT]:
        _executor_entrypoint_cache[image_name] = now + _IMAGE_CACHE_TTL_SECONDS
        return True
    return False


CONTAINER_LABEL = "tako-vm-executor"

EXECUTION_ID_LABEL = "tako-vm.execution-id"

SESSION_ID_LABEL = "tako-vm.session-id"


def is_native_linux() -> bool:
    return platform.system() == "Linux"


def generate_container_name(prefix: str, job_id: Optional[str] = None, attempt: int = 0) -> str:
    if job_id:
        if attempt > 0:
            return f"{prefix}-{job_id}-r{attempt}"
        return f"{prefix}-{job_id}"
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def kill_container(container_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "kill", container_name],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            logger.debug("Killed container %s", container_name)
            return True
        stderr = decode_subprocess_stream(result.stderr).strip()
        if "no such container" in stderr.lower():
            logger.debug("Container %s was not running", container_name)
        else:
            logger.warning(
                "Failed to kill container %s (exit %s): %s",
                container_name,
                result.returncode,
                stderr,
            )
        return False
    except Exception as e:
        logger.warning("Failed to kill container %s: %s", container_name, e)
        return False


def remove_container(container_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            logger.debug("Removed container %s", container_name)
            return True
        stderr = decode_subprocess_stream(result.stderr).strip()
        if "no such container" in stderr.lower():
            logger.debug("Container %s did not exist", container_name)
        else:
            logger.warning(
                "Failed to remove container %s (exit %s): %s; it may leak",
                container_name,
                result.returncode,
                stderr,
            )
        return False
    except Exception as e:
        logger.warning("Failed to remove container %s: %s", container_name, e)
        return False


def inspect_oom_killed(container_name: str) -> Optional[bool]:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.OOMKilled}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            logger.debug("docker inspect of %s failed (exit %s)", container_name, result.returncode)
            return None
        value = (result.stdout or "").strip().lower()
        if value == "true":
            return True
        if value == "false":
            return False
        logger.debug("Unexpected OOMKilled value for %s: %r", container_name, result.stdout)
        return None
    except Exception as e:
        logger.debug("Failed to inspect container %s: %s", container_name, e)
        return None


def container_running(container_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            logger.debug("docker inspect of %s failed (exit %s)", container_name, result.returncode)
            return False
        value = (result.stdout or "").strip().lower()
        if value == "true":
            return True
        if value == "false":
            return False
        logger.debug("Unexpected Running value for %s: %r", container_name, result.stdout)
        return False
    except Exception as e:
        logger.debug("Failed to inspect container %s: %s", container_name, e)
        return False


def stop_container(container_name: str, timeout: int = 10) -> bool:
    try:
        result = subprocess.run(
            ["docker", "stop", "-t", str(timeout), container_name],
            capture_output=True,
            timeout=timeout + 10,
            check=False,
        )
        if result.returncode == 0:
            logger.debug("Stopped container %s", container_name)
            return True
        stderr = decode_subprocess_stream(result.stderr).strip()
        if "no such container" in stderr.lower():
            logger.debug("Container %s did not exist", container_name)
        else:
            logger.warning(
                "Failed to stop container %s (exit %s): %s",
                container_name,
                result.returncode,
                stderr,
            )
        return False
    except Exception as e:
        logger.warning("Failed to stop container %s: %s", container_name, e)
        return False


def ulimit_args(limits) -> list[str]:
    return [
        f"--ulimit=nofile={limits.nofile_soft}:{limits.nofile_hard}",
        f"--ulimit=nproc={limits.nproc_soft}:{limits.nproc_hard}",
        f"--ulimit=fsize={limits.fsize}",
    ]


def base_isolation_args(
    container_name: str,
    *,
    runtime: str,
    enable_cap_restrictions: bool = True,
    execution_id: Optional[str] = None,
    auto_remove: bool = True,
) -> list[str]:
    args = [
        "docker",
        "run",
    ]
    if auto_remove:
        args.append("--rm")
    args += [
        f"--name={container_name}",
        f"--label={CONTAINER_LABEL}",
        "--init",
        "--read-only",
    ]

    if execution_id:
        args.append(f"--label={EXECUTION_ID_LABEL}={execution_id}")

    if enable_cap_restrictions:
        args.extend(
            [
                "--cap-drop=ALL",
                "--cap-add=SETUID",
                "--cap-add=SETGID",
                "--cap-add=KILL",
            ]
        )

    if runtime == "runsc":
        args.append(f"--runtime={runtime}")

    return args


_FORBIDDEN_WORKSPACE_ROOTS = (
    "/",
    "/etc",
    "/root",
    "/home",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var/run",
    "/run",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/var/lib/docker",
)

_FORBIDDEN_WORKSPACE_TREES = (
    "/etc",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var/run",
    "/run",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/var/lib/docker",
)


def _with_real_paths(roots: Sequence[str]) -> tuple[str, ...]:
    expanded = list(roots)
    for root in roots:
        try:
            real = str(Path(root).resolve())
        except OSError:
            continue
        if real not in expanded:
            expanded.append(real)
    return tuple(expanded)


def _reject_if_sensitive_host_path(path: Path, label: str) -> None:
    as_str = str(path)
    shallow_roots = _with_real_paths(_FORBIDDEN_WORKSPACE_ROOTS)
    if as_str in shallow_roots:
        raise ValueError(f"{label} a sensitive host directory: {as_str!r}")
    for root in shallow_roots:
        if root != "/" and as_str.startswith(root + "/") and as_str.count("/") <= 2:
            raise ValueError(f"{label} directly under a sensitive host directory: {as_str!r}")
    for tree in _with_real_paths(_FORBIDDEN_WORKSPACE_TREES):
        if as_str == tree or as_str.startswith(tree + "/"):
            raise ValueError(f"{label} inside a sensitive host directory: {as_str!r}")


def _require_absolute_normalized(value: str, name: str) -> Path:
    if not value or not value.strip():
        raise ValueError(f"session {name} must not be empty")
    if any(ch in value for ch in ("\x00", "\n", "\r")):
        raise ValueError(f"session {name} must not contain control characters")

    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError(f"session {name} must be an absolute path: {value!r}")

    normalized = Path(os.path.normpath(str(candidate)))
    if str(normalized) != str(candidate).rstrip("/") or ".." in candidate.parts:
        raise ValueError(f"session {name} must be normalized and contain no '..': {value!r}")
    return normalized


def validate_session_workspace_dir(workspace_dir: str, sessions_root: str) -> str:
    normalized = _require_absolute_normalized(workspace_dir, "workspace_dir")
    root = _require_absolute_normalized(sessions_root, "sessions_root")

    _reject_if_sensitive_host_path(root, "session sessions_root is")
    _reject_if_sensitive_host_path(normalized, "session workspace_dir is")

    if normalized == root or not normalized.is_relative_to(root):
        raise ValueError(
            f"session workspace_dir must be inside the sessions root {str(root)!r}: "
            f"{workspace_dir!r}"
        )

    real_root = root.resolve()
    real = normalized.resolve()
    _reject_if_sensitive_host_path(real_root, "session sessions_root resolves to")
    _reject_if_sensitive_host_path(real, "session workspace_dir resolves to")
    if real == real_root or not real.is_relative_to(real_root):
        raise ValueError(
            f"session workspace_dir resolves outside the sessions root {str(real_root)!r}: "
            f"{workspace_dir!r}"
        )

    return str(normalized)


def build_session_run_command(
    container_name: str,
    *,
    runtime: str,
    image: str,
    workspace_dir: str,
    sessions_root: str,
    session_id: str,
    memory_limit: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    enable_cap_restrictions: bool = True,
    keepalive_cmd: Optional[Sequence[str]] = None,
) -> list[str]:
    workspace_dir = validate_session_workspace_dir(workspace_dir, sessions_root)

    cmd = base_isolation_args(
        container_name,
        runtime=runtime,
        enable_cap_restrictions=enable_cap_restrictions,
        auto_remove=False,
    )

    cmd.append("-d")
    cmd.append("--network=none")
    cmd.append(f"--label={SESSION_ID_LABEL}={session_id}")
    cmd.append("-v")
    cmd.append(f"{workspace_dir}:/workspace")

    if memory_limit is not None:
        cmd.append(f"--memory={memory_limit}")
    if cpu_limit is not None:
        cmd.append(f"--cpus={cpu_limit}")

    cmd.append(image)
    cmd.extend(keepalive_cmd if keepalive_cmd is not None else ["sleep", "infinity"])

    return cmd


def build_session_exec_command(
    container_name: str,
    *,
    command: Sequence[str],
    timeout_seconds: Optional[float] = None,
    workdir: str = "/workspace",
) -> list[str]:
    cmd = [
        "docker",
        "exec",
        "-u",
        "1000:1000",
        "-w",
        workdir,
        container_name,
    ]

    if timeout_seconds is not None:
        cmd.extend(["timeout", "--signal=TERM", "--kill-after=10s", f"{timeout_seconds}s"])

    cmd.extend(command)

    return cmd


def prepare_requirements_file(
    requirements: Optional[Sequence[str]],
    input_dir: Path,
    *,
    allow_runtime_requirements: bool,
    max_requirements: int,
) -> List[str]:
    if not requirements:
        return []

    if not allow_runtime_requirements:
        raise ValueError(
            "Runtime dependency installation is disabled. "
            "Use pre-built images or set allow_runtime_requirements=True."
        )
    if len(requirements) > max_requirements:
        raise ValueError(f"Too many requirements ({len(requirements)} > {max_requirements})")

    validated_reqs: List[str] = []
    for req in requirements:
        if not validate_pip_requirement(req):
            raise ValueError(f"Invalid pip requirement: {req!r}")
        validated_reqs.append(req)

    requirements_file = input_dir / "_requirements.txt"
    requirements_file.write_text("\n".join(validated_reqs) + "\n", encoding="utf-8")
    requirements_file.chmod(0o444)
    return validated_reqs


def classify_docker_run_result(returncode: int, stderr: str) -> Tuple[bool, Optional[str]]:
    stderr_lower = (stderr or "").lower()
    is_infra_failure = returncode == 125 or (
        returncode != 0
        and any(pattern in stderr_lower for pattern in _DOCKER_INFRA_STDERR_PATTERNS)
    )
    if not is_infra_failure:
        return False, None

    stderr_lines = (stderr or "").strip().splitlines()
    detail = stderr_lines[0] if stderr_lines else f"docker run exited with code {returncode}"
    return True, detail


def classify_sigkill(container_name: str) -> Optional[bool]:
    return inspect_oom_killed(container_name)


def read_result_json(output_dir: Path, ident: str) -> Optional[dict]:
    output_file = output_dir / "result.json"
    if output_file.is_symlink():
        logger.warning("result.json for %s is a symlink, ignoring", ident)
        return None
    if not output_file.exists():
        return None
    try:
        return json.loads(output_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning("Failed to read/parse result.json for %s: %s", ident, e)
        return None
