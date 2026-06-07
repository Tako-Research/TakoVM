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


def is_native_linux() -> bool:
    """
    Check if running on native Linux (not Docker Desktop).

    Docker Desktop (macOS/Windows) runs containers in a VM and has issues
    with custom seccomp profiles. Native Linux Docker works fine.

    Returns:
        True if running on native Linux, False if Docker Desktop (macOS/Windows)
    """
    return platform.system() == "Linux"


def generate_container_name(prefix: str, job_id: Optional[str] = None) -> str:
    """
    Generate a unique container name for tracking.

    Uses job_id if provided, otherwise generates a UUID-based name
    to avoid collisions under high concurrency.

    Args:
        prefix: Container name prefix (e.g., "tako", "tako-sandbox")
        job_id: Optional job ID to include in name

    Returns:
        Unique container name like "tako-abc123" or "tako-a1b2c3d4"
    """
    if job_id:
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
