"""
Docker utilities for container management.

Shared utilities for Docker operations across worker and sandbox.
"""

import logging
import platform
import subprocess
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Label applied to every executor container at `docker run` time. Cleanup
# (DockerCleanup.cleanup_orphaned_containers) matches on this label, so it must
# be applied by every launch path — base_isolation_args() does this centrally.
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
    removed yet (exactly the daemon-hiccup scenario that triggers a retry —
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
        logger.debug("Container %s was not running", container_name)
        return False
    except Exception as e:
        # Ignore errors - container may not exist or already be stopped
        logger.debug("Failed to kill container %s: %s", container_name, e)
        return False


def remove_container(container_name: str) -> bool:
    """
    Force-remove a container by name (best-effort).

    ``docker rm -f`` kills the container if it is running and removes it in
    one step. Used before a retry attempt to clean up the previous attempt's
    container so it cannot linger (``--rm`` removal can lag behind a daemon
    hiccup — the very condition that triggers retries).

    Silently ignores errors (the container has usually already been removed
    by ``--rm``).

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
        logger.debug("Container %s did not exist", container_name)
        return False
    except Exception as e:
        # Ignore errors - container may not exist or daemon may be unreachable
        logger.debug("Failed to remove container %s: %s", container_name, e)
        return False


def base_isolation_args(
    container_name: str,
    *,
    runtime: str,
    enable_cap_restrictions: bool = True,
    execution_id: Optional[str] = None,
) -> list[str]:
    """Leading ``docker run`` args shared by every isolated-execution path.

    Centralizes the always-on isolation posture — ``--rm``, ``--init``,
    ``--read-only``, the capability drop/add set, and the gVisor ``--runtime``
    flag — so it is assembled in exactly one place. The execution paths that
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

    Returns:
        The leading argument list, ready to have path-specific flags and the
        image name appended before execution.
    """
    args = [
        "docker",
        "run",
        "--rm",
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
