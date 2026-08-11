"""
Shared constants for Tako VM.

Centralized constants to avoid duplication and ensure consistency
across worker, sandbox, and other modules.
"""

import os
import tempfile

# Docker image for code execution
DEFAULT_IMAGE = "code-executor:latest"

# Base name for the uv cache volume (speeds up repeated dependency installs).
# Never mounted directly: use uv_cache_volume() so the volume is scoped.
UV_CACHE_VOLUME = "tako-uv-cache"

# Characters allowed in the scope suffix of a cache volume name. Job type names
# are already validated to alphanumerics/dash/underscore, so this is a
# belt-and-braces guard against a scope reaching the docker CLI unsanitized.
_SAFE_SCOPE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def uv_cache_volume(scope: str) -> str:
    """Return the uv cache volume name for a given sharing scope.

    The shared dependency cache is a cross-job channel: it is mounted
    read-write at a path the sandbox user owns, and it stays mounted for the
    container's whole life, not just the install phase. So one job can write a
    cache entry that a later job's ``uv pip install`` resolves and executes.

    A single host-wide volume made that boundary "every job on this host".
    Scoping the volume per job type narrows it to a group the OPERATOR defines:
    jobs of different types can no longer reach each other's cache.

    This reduces blast radius, it does not eliminate the channel. Jobs sharing
    one job type still share one cache, so if you accept untrusted submissions
    into a job type that has ``allow_runtime_requirements`` AND
    ``enable_runtime_dependency_cache`` enabled, they can still poison each
    other. Tako VM has no tenant identity to key on; job type is the only
    boundary it knows. Give mutually untrusting workloads distinct job types,
    or leave the cache disabled (the default).
    """
    sanitized = "".join(c if c in _SAFE_SCOPE_CHARS else "-" for c in scope) or "default"
    return f"{UV_CACHE_VOLUME}-{sanitized[:64]}"


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
