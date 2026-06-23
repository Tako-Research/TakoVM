"""
Shared constants for Tako VM.

Centralized constants to avoid duplication and ensure consistency
across worker, sandbox, and other modules.
"""

import os
import tempfile

# Docker image for code execution
DEFAULT_IMAGE = "code-executor:latest"

# Docker volume name for uv cache (speeds up repeated dependency installs)
UV_CACHE_VOLUME = "tako-uv-cache"

# In-container uv cache locations. The shared-volume mount point lives under the
# sandbox user's home (uid 1000), deliberately NOT under /root: the dependency
# install runs unprivileged via gosu (issue #102), and /root is mode 0700 so
# uid 1000 cannot even traverse into a cache mounted there. uv's cache is
# content-addressed and location-independent, so relocating an existing volume
# from the old /root path to here reuses its contents unchanged.
UV_CACHE_VOLUME_DIR = "/home/sandbox/.cache/uv"
# Ephemeral per-container uv cache used when the shared volume is disabled.
# Lives on the writable /tmp tmpfs, which the sandbox user can write.
UV_CACHE_TMP_DIR = "/tmp/uv-cache"


# Workspace directory for job files (can be set via TAKO_VM_WORKSPACE env var)
# When running the server in a container with Docker socket mounted, this must
# be a path that exists on the host and is mounted into the server container.
#
# Read live (not captured at import time) so a TAKO_VM_WORKSPACE set after the
# module is imported, or changed between runs, takes effect, and so tests can
# point it at a temp dir without monkeypatching a module-level constant.
def get_workspace_dir() -> str:
    """Return the configured workspace directory, reading TAKO_VM_WORKSPACE live."""
    return os.environ.get("TAKO_VM_WORKSPACE", tempfile.gettempdir())


# Maximum number of runtime requirements to prevent env var overflow and slow startups
MAX_REQUIREMENTS = 50
