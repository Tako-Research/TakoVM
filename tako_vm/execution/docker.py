"""
Docker utilities for container management.

Shared utilities for Docker operations across worker and sandbox.
"""

import json
import logging
import platform
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from tako_vm.security import validate_pip_requirement

logger = logging.getLogger(__name__)

# stderr substrings that indicate the docker CLI could not reach the daemon.
# These are infrastructure failures, never the fault of the user's code. Shared
# by classify_docker_run_result so the worker and the library Sandbox detect
# daemon/infra failures identically.
_DOCKER_INFRA_STDERR_PATTERNS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "docker daemon is not running",
)

# Default executor base image. Built from docker/Dockerfile.executor; carries
# uv, gosu, the sandbox user, and the /entrypoint.sh contract (see
# EXECUTOR_ENTRYPOINT).
DEFAULT_EXECUTOR_IMAGE = "code-executor:latest"

# The entrypoint every runnable Tako image must carry
# (`ENTRYPOINT ["/entrypoint.sh"]` in docker/Dockerfile.executor). The
# entrypoint is what enforces the in-container startup/execution timeouts,
# installs runtime requirements, writes the phase/timing file, and runs
# /code/main.py itself (as the sandbox user via gosu). An image without it
# would run its default CMD instead of the user's code. For python:slim the
# REPL exits 0 on EOF, recording a bogus "succeeded" for code that never ran.
EXECUTOR_ENTRYPOINT = "/entrypoint.sh"

# How long positive image-inspect results are cached (seconds). Only positive
# results are cached: a missing image can be built/pulled at any moment, and
# caching the miss would keep a freshly built job-type image unused.
_IMAGE_CACHE_TTL_SECONDS = 60.0

# image name -> monotonic expiry timestamp. Plain dicts: worst case under
# concurrent workers is a duplicate `docker image inspect`, which is harmless.
_image_exists_cache: dict[str, float] = {}
_executor_entrypoint_cache: dict[str, float] = {}


def reset_image_caches() -> None:
    """Clear the image-inspect caches (for tests and after image rebuilds)."""
    _image_exists_cache.clear()
    _executor_entrypoint_cache.clear()


def decode_subprocess_stream(value) -> str:
    """Coerce captured subprocess output (``str`` | ``bytes`` | ``None``) to ``str``.

    ``subprocess.TimeoutExpired.stdout``/``.stderr`` hold the raw ``bytes``
    captured before the kill even when ``subprocess.run`` was invoked with
    ``text=True`` (CPython populates them from the byte buffers), so any caller
    reading partial output on timeout must handle all three types. Shared by the
    worker and the library Sandbox so the decoding can't drift between them.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def image_exists(image_name: str) -> bool:
    """Check whether a Docker image exists locally (cached).

    Positive results are cached for a short TTL so per-run lookups (e.g. "does
    this job type have a pre-built image?") don't cost a daemon round-trip on
    every execution. Negative results are never cached: see
    ``_IMAGE_CACHE_TTL_SECONDS``.

    Args:
        image_name: Image reference to look up (e.g. "tako-vm-foo:latest")

    Returns:
        True if the image exists on the local daemon, False otherwise
        (including when the daemon is unreachable or the inspect times out).
    """
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
    """Check whether an image's ENTRYPOINT is exactly ``["/entrypoint.sh"]``.

    This is the cheap, reliable test for "was this image derived from the
    executor base image", i.e. does it honor the contract the worker depends
    on (in-container timeouts, dependency install, phase file, and running
    /code/main.py itself). Positive results are cached with a short TTL,
    mirroring ``image_exists``.

    Args:
        image_name: Image reference to inspect

    Returns:
        True if the entrypoint matches the executor contract, False if the
        image exists but has a different/absent entrypoint, or None if the
        inspect failed (image not present locally, daemon unreachable,
        timeout, unparseable output). Callers must not treat None as a pass.
    """
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


# Label applied to every executor container at `docker run` time. Cleanup
# (DockerCleanup.cleanup_orphaned_containers) matches on this label, so it must
# be applied by every launch path; base_isolation_args() does this centrally.
CONTAINER_LABEL = "tako-vm-executor"

# Label key carrying the execution/job ID, so an orphaned container can be
# mapped back to its ExecutionRecord (e.g. `docker ps --filter
# label=tako-vm.execution-id=<id>`).
EXECUTION_ID_LABEL = "tako-vm.execution-id"


def is_native_linux() -> bool:
    """
    Check if running on native Linux (not Docker Desktop).

    Docker Desktop (macOS/Windows) runs containers in a VM and has issues
    with custom seccomp profiles. Native Linux Docker works fine.

    Returns:
        True if running on native Linux, False if Docker Desktop (macOS/Windows)
    """
    return platform.system() == "Linux"


def generate_container_name(prefix: str, job_id: Optional[str] = None, attempt: int = 0) -> str:
    """
    Generate a unique container name for tracking.

    Uses job_id if provided, otherwise generates a UUID-based name
    to avoid collisions under high concurrency.

    Retry attempts (attempt > 0) get a ``-r{attempt}`` suffix so a retry can
    never collide with a previous attempt's container that ``--rm`` has not
    removed yet (exactly the daemon-hiccup scenario that triggers a retry;
    a name collision would fail the retry with docker exit 125 "name already
    in use"). Attempt 0 keeps the plain deterministic ``{prefix}-{job_id}``
    name because external kill paths (queue.py cancel()/watchdog) compute
    ``generate_container_name("tako", job_id)`` and must still match the
    first attempt's container.

    Known limitation: those cancel/watchdog paths can only kill the
    attempt-0 name, so a container from a retry attempt is not reachable by
    them. Retries are short-lived and rare; killing by the
    ``tako-vm.execution-id`` label (which every attempt carries) instead of
    by name is noted as future work.

    Args:
        prefix: Container name prefix (e.g., "tako", "tako-sandbox")
        job_id: Optional job ID to include in name
        attempt: Retry attempt index (0 = first attempt, no suffix)

    Returns:
        Unique container name like "tako-abc123", "tako-abc123-r1",
        or "tako-a1b2c3d4"
    """
    if job_id:
        if attempt > 0:
            return f"{prefix}-{job_id}-r{attempt}"
        return f"{prefix}-{job_id}"
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def kill_container(container_name: str) -> bool:
    """
    Kill and remove a container by name.

    Called on timeout or exception to clean up orphaned containers.
    When subprocess.run() times out, it kills the `docker run` CLI process
    but the container keeps running in the Docker daemon. This function
    ensures the container is properly stopped.

    Silently ignores errors (container may not exist or already be stopped).

    Args:
        container_name: Name of the container to kill

    Returns:
        True if Docker reported the container was killed, False otherwise
    """
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
        # Distinguish the benign "no such container" (already gone / never
        # started) from a genuine kill failure (a still-running untrusted
        # container we could not stop), which is a real containment concern.
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
        # A docker CLI error (daemon unreachable, timeout) leaves the container
        # state unknown; surface it rather than silently swallowing.
        logger.warning("Failed to kill container %s: %s", container_name, e)
        return False


def remove_container(container_name: str) -> bool:
    """
    Force-remove a container by name (best-effort).

    ``docker rm -f`` kills the container if it is running and removes it in
    one step. Used by the worker after every run (its containers are started
    without ``--rm`` so a 137 exit can be inspected for ``State.OOMKilled``)
    and before a retry attempt to clean up the previous attempt's container
    so it cannot linger (removal can lag behind a daemon hiccup, the very
    condition that triggers retries).

    Silently ignores errors (the container may already be gone).

    Args:
        container_name: Name of the container to remove

    Returns:
        True if Docker reported the container was removed, False otherwise
    """
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
        # ``docker rm -f`` is a no-op success for a missing container on modern
        # daemons; a non-zero exit therefore signals a genuine removal failure
        # (a leaked container), unless it is the benign "no such container".
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
        # A docker CLI error (daemon unreachable, timeout) means the container
        # may still exist and leak; surface it rather than swallowing.
        logger.warning("Failed to remove container %s: %s", container_name, e)
        return False


def inspect_oom_killed(container_name: str) -> Optional[bool]:
    """Check whether a container's process was OOM-killed via ``docker inspect``.

    ``State.OOMKilled`` is the only authoritative signal that exit code 137
    came from the kernel/cgroup OOM killer rather than some other SIGKILL
    (``docker kill`` from a cancel path, a pids-limit kill, or user code
    calling ``sys.exit(137)``). Requires the container to still exist, i.e.
    the run must not use ``--rm`` (see ``base_isolation_args(auto_remove=
    False)``); the caller is responsible for removing the container afterward.

    Args:
        container_name: Name of the container to inspect

    Returns:
        True/False from ``State.OOMKilled``, or None if the inspect failed
        (container already gone, daemon unreachable, timeout, unparseable
        output). Callers should treat None as "unknown" and fall back to
        their previous heuristic so a flaky inspect never loses a true OOM.
    """
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


def ulimit_args(limits) -> list[str]:
    """Return the ``--ulimit`` flags shared by every execution path.

    ``RLIMIT_FSIZE``/``RLIMIT_NOFILE``/``RLIMIT_NPROC`` are kernel rlimits that
    gVisor does not impose on its own; they only take effect when passed via
    ``--ulimit`` at container creation. Both builders (``CodeExecutor`` and the
    library ``Sandbox``) assemble their own ``docker run`` command, so this is
    factored out to keep them from drifting; the drift this guards against is
    exactly issue #97, where the ``Sandbox`` path shipped with no ``--ulimit``
    at all and left ``RLIMIT_FSIZE`` unbounded against the writable ``/output``
    bind-mount (a host-disk-exhaustion DoS the gVisor boundary does not cover).

    Args:
        limits: A ``ContainerLimits`` (or any object exposing ``nofile_soft``,
            ``nofile_hard``, ``nproc_soft``, ``nproc_hard``, ``fsize``).

    Returns:
        The three ``--ulimit`` flags, ready to append to a ``docker run`` command.
    """
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
    """Leading ``docker run`` args shared by every isolated-execution path.

    Centralizes the always-on isolation posture (``--rm``, ``--init``,
    ``--read-only``, the capability drop/add set, and the gVisor ``--runtime``
    flag) so it is assembled in exactly one place. The execution paths that
    each build their own command (``CodeExecutor`` and the library ``Sandbox``)
    start from this, which keeps the isolation flags from drifting between them
    (e.g. a hardening flag added to one builder and silently missed on
    another). Path-specific args (network, mounts, resource limits, env,
    seccomp) are appended by the caller.

    Args:
        container_name: Container name (``--name``).
        runtime: Resolved container runtime ('runsc' or 'runc'). Only gVisor
            ('runsc') is passed explicitly: runc is docker's default and some
            daemons reject ``--runtime=runc``.
        enable_cap_restrictions: Drop all capabilities and re-add only SETUID/
            SETGID (needed by gosu to drop to the unprivileged sandbox user).
            Can be disabled in CI where Docker can't modify capability sets.
        execution_id: Optional execution/job ID recorded as the
            ``tako-vm.execution-id`` label so orphaned containers can be traced
            back to their execution records.
        auto_remove: Pass ``--rm`` so the daemon removes the container on
            exit. Callers that need to ``docker inspect`` the exited container
            (e.g. to read ``State.OOMKilled`` and distinguish a real OOM from
            any other SIGKILL) must set this to False and remove the container
            themselves (``remove_container``) once inspection is done.

    Returns:
        The leading argument list, ready to have path-specific flags and the
        image name appended before execution.
    """
    args = [
        "docker",
        "run",
    ]
    if auto_remove:
        args.append("--rm")
    args += [
        f"--name={container_name}",
        # Identify the container as ours so startup cleanup can find orphans.
        f"--label={CONTAINER_LABEL}",
        "--init",  # Faster signal handling with tini
        "--read-only",
    ]

    if execution_id:
        args.append(f"--label={EXECUTION_ID_LABEL}={execution_id}")

    # Capability restrictions (can be disabled in CI environments where Docker
    # can't modify capability bounding sets)
    if enable_cap_restrictions:
        args.extend(
            [
                "--cap-drop=ALL",
                "--cap-add=SETUID",  # Required for gosu to switch user
                "--cap-add=SETGID",  # Required for gosu to switch user
            ]
        )
        # Security note: We don't use --security-opt=no-new-privileges because gosu requires
        # setuid to drop from root to sandbox user (uid 1000). This is a one-way privilege drop:
        # after gosu exec's the user code, the process runs as unprivileged sandbox user with
        # no capability to regain root. The container also has all other caps dropped.

    # Only specify runtime explicitly for gVisor (runsc). runc is docker's
    # default, and some daemons reject ``--runtime=runc``.
    if runtime == "runsc":
        args.append(f"--runtime={runtime}")

    return args


def prepare_requirements_file(
    requirements: Optional[Sequence[str]],
    input_dir: Path,
    *,
    allow_runtime_requirements: bool,
    max_requirements: int,
) -> List[str]:
    """Validate runtime requirements and write the ``_requirements.txt`` file.

    Single source of truth for the runtime-requirements policy shared by the
    server ``CodeExecutor`` and the library ``Sandbox``, so the two paths cannot
    drift on how they cap, validate, gate, and persist requirements.

    Policy (fail closed):

    - An empty/None list is a no-op and returns ``[]`` (no file written).
    - If requirements are present but ``allow_runtime_requirements`` is False,
      raise ``ValueError`` (runtime installs are disabled).
    - More than ``max_requirements`` entries raises ``ValueError``.
    - Any entry failing ``validate_pip_requirement`` raises ``ValueError``:
      invalid requirements are REJECTED, never silently skipped, so a typo or an
      injection attempt fails loudly instead of running with a different
      dependency set than the caller asked for.
    - The validated list is written to ``input_dir/_requirements.txt`` and
      chmod 0o444 (read-only) for the entrypoint to install.

    The policy is checked before validation so an all-invalid list cannot bypass
    the ``allow_runtime_requirements`` gate.

    Args:
        requirements: Requested pip requirements (may be None/empty).
        input_dir: Directory mounted read-only at /input where the entrypoint
            looks for ``_requirements.txt``.
        allow_runtime_requirements: Whether runtime installs are permitted.
        max_requirements: Maximum number of requirements allowed.

    Returns:
        The validated requirements list (empty when none were requested).

    Raises:
        ValueError: policy violation (installs disabled, too many, or an invalid
            requirement).
    """
    if not requirements:
        return []

    # Enforce the policy before validation so an all-invalid list cannot bypass
    # the allow_runtime_requirements check.
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
    """Decide whether a finished ``docker run`` failed in docker itself (infra).

    ``docker run`` reserves exit code 125 for failures of docker itself (daemon
    unreachable, image pull failure, bad flags); container exit codes pass
    through unchanged otherwise, and our entrypoint never exits 125 in normal
    operation. A non-zero exit whose stderr matches a known daemon-connectivity
    phrasing is likewise an infrastructure failure. Such failures must never be
    attributed to the user's code (e.g. by ``classify_error`` on daemon stderr).

    Shared by the worker (which counts these against its circuit breaker and
    marks them retriable) and the library Sandbox (which has no circuit breaker
    but still surfaces them as a distinct infra failure rather than a code bug).

    Args:
        returncode: Exit code from ``subprocess.run`` of ``docker run``.
        stderr: Captured stderr of the run.

    Returns:
        Tuple of ``(is_infra_failure, detail)``. ``detail`` is a short,
        single-line description suitable for an error message (the first stderr
        line, or a synthesized fallback), and is None when not an infra failure.
    """
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
    """Classify an exit-code-137 (SIGKILL) container as OOM or not.

    Exit 137 is SIGKILL, but not necessarily the OOM killer: ``docker kill``
    (cancel path), a pids-limit kill, or user code calling ``sys.exit(137)`` all
    look identical. Only the exited container's ``State.OOMKilled`` distinguishes
    them, so this must be called BEFORE the container is removed (the run must
    use ``auto_remove=False``). In-container timeout kills are already remapped
    to 124 by the entrypoint, so a 137 seen by callers is a genuine SIGKILL.

    Thin wrapper over ``inspect_oom_killed`` so both the worker and the library
    Sandbox apply the identical OOM policy:

    Returns:
        True if the kernel/cgroup OOM killer killed the process, False if it was
        a non-OOM SIGKILL, or None if the inspect was inconclusive (container
        already gone, daemon unreachable, timeout). Callers should treat None as
        "assume OOM" so a flaky inspect never loses a true OOM.
    """
    return inspect_oom_killed(container_name)


def read_result_json(output_dir: Path, ident: str) -> Optional[dict]:
    """Read and parse ``output_dir/result.json``, symlink-safe.

    Untrusted code in the container can point ``result.json`` at a host file to
    exfiltrate it; this never follows a symlink. A present-but-unreadable or
    unparseable file is logged and treated as "no JSON output" rather than
    silently presented as None (the caller cannot tell the difference), so the
    failure is surfaced in the log.

    Shared by the worker and the library Sandbox so both parse the result file
    identically.

    Args:
        output_dir: The job's output directory (bind-mounted at /output).
        ident: Identifier for log messages (job/execution ID or container name).

    Returns:
        The parsed JSON object, or None if absent, a symlink, or unreadable.
    """
    output_file = output_dir / "result.json"
    if output_file.is_symlink():
        logger.warning("result.json for %s is a symlink, ignoring", ident)
        return None
    if not output_file.exists():
        return None
    try:
        return json.loads(output_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # Output existed but was unreadable/unparseable (truncated, non-UTF-8,
        # device error). Surface it loudly instead of presenting None as "no
        # output produced".
        logger.warning("Failed to read/parse result.json for %s: %s", ident, e)
        return None
