import os
import tempfile

DEFAULT_IMAGE = "code-executor:latest"

UV_CACHE_VOLUME = "tako-uv-cache"

_SAFE_SCOPE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def uv_cache_volume(scope: str) -> str:
    sanitized = "".join(c if c in _SAFE_SCOPE_CHARS else "-" for c in scope) or "default"
    return f"{UV_CACHE_VOLUME}-{sanitized[:64]}"


UV_CACHE_VOLUME_DIR = "/home/sandbox/.cache/uv"
UV_CACHE_TMP_DIR = "/tmp/uv-cache"


def get_workspace_dir() -> str:
    return os.environ.get("TAKO_VM_WORKSPACE", tempfile.gettempdir())


MAX_REQUIREMENTS = 50
