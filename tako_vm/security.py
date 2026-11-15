import hashlib
import logging
import re
from pathlib import Path
from typing import List, Sequence, Tuple

from .models import ErrorType

logger = logging.getLogger(__name__)

SANITIZE_PATTERNS: List[Tuple[str, str]] = [
    (r"/tmp/job-[a-zA-Z0-9_-]+", "/tmp/job-***"),
    (r"/var/folders/[^\s]+", "/var/folders/***"),
    (r"/Users/[^/\s]+", "/home/***"),
    (r"/home/[^/\s]+", "/home/***"),
    (r"/app/[^\s]+", "/app/***"),
    (r"172\.\d+\.\d+\.\d+", "172.***.***"),
    (r"10\.\d+\.\d+\.\d+", "10.***.***"),
    (r"192\.168\.\d+\.\d+", "192.168.***.***"),
    (r"(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])", "<container-id>"),
    (r"(?<![a-f0-9])(?=[0-9]*[a-f])[a-f0-9]{12}(?![a-f0-9])", "<container-id>"),
]

_KILLED_PATTERN = re.compile(
    r"\boom-?kill(?:ed|er)\b"
    r"|\bprocess killed\b"
    r"|^\s*killed\b"
    r"|\bline \d+:\s*\d+ killed\b",
    re.MULTILINE,
)

DEFAULT_MAX_STDOUT_BYTES = 65536
DEFAULT_MAX_STDERR_BYTES = 65536
DEFAULT_MAX_ARTIFACT_BYTES = 10_485_760
DEFAULT_MAX_TOTAL_ARTIFACTS_BYTES = 52_428_800


def sanitize_error(error: str) -> str:
    if not error:
        return ""

    result = error
    for pattern, replacement in SANITIZE_PATTERNS:
        result = re.sub(pattern, replacement, result)

    return result


def cap_output(output: str, max_bytes: int = DEFAULT_MAX_STDOUT_BYTES) -> Tuple[str, bool]:
    if not output:
        return "", False

    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return output, False

    truncation_msg = f"\n\n[TRUNCATED: output exceeded {max_bytes} bytes]"
    truncation_bytes = len(truncation_msg.encode("utf-8"))
    available_bytes = max_bytes - truncation_bytes

    if available_bytes <= 0:
        return truncation_msg, True

    truncated_bytes = encoded[:available_bytes]

    while truncated_bytes:
        try:
            truncated = truncated_bytes.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated_bytes = truncated_bytes[:-1]
    else:
        truncated = ""

    return truncated + truncation_msg, True


def validate_artifact_size(
    path: Path, max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES
) -> Tuple[bool, int]:
    if not path.exists():
        return False, 0

    size = path.stat().st_size
    return size <= max_bytes, size


def compute_file_hash(path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def classify_error(exit_code: int, stderr: str, timed_out: bool = False) -> Tuple[ErrorType, str]:
    if timed_out:
        return "timeout", "Execution exceeded time limit"

    if exit_code == 137:
        return "oom", "Execution exceeded memory limit"

    if exit_code == 143:
        return "cancelled", "Execution was terminated"

    if exit_code == 139:
        return "segfault", "Segmentation fault in executed code"

    if exit_code == 134:
        return "abort", "Process aborted (assertion failure or abort() called)"

    if exit_code == 136:
        return "arithmetic_error", "Floating point exception (division by zero)"

    if exit_code == 135:
        return "bus_error", "Bus error (invalid memory access)"

    if exit_code == 141:
        return "pipe_error", "Broken pipe"

    stderr_lower = stderr.lower() if stderr else ""

    if "memoryerror" in stderr_lower or "out of memory" in stderr_lower:
        return "oom", "Execution exceeded memory limit"

    if "cannot allocate memory" in stderr_lower:
        return "oom", "Cannot allocate memory"

    if _KILLED_PATTERN.search(stderr_lower):
        return "killed", "Process was killed by system"

    if "permission denied" in stderr_lower:
        return "permission", "Permission denied during execution"

    if "operation not permitted" in stderr_lower:
        return "permission", "Operation not permitted"

    if "syntaxerror" in stderr_lower:
        return "syntax_error", sanitize_error(stderr[:200] if stderr else "Syntax error")

    if "indentationerror" in stderr_lower:
        return "syntax_error", sanitize_error(stderr[:200] if stderr else "Indentation error")

    if "importerror" in stderr_lower or "modulenotfounderror" in stderr_lower:
        return "import_error", sanitize_error(stderr[:200] if stderr else "Import error")

    if "typeerror" in stderr_lower:
        return "type_error", sanitize_error(stderr[:200] if stderr else "Type error")

    if "valueerror" in stderr_lower:
        return "value_error", sanitize_error(stderr[:200] if stderr else "Value error")

    if "keyerror" in stderr_lower:
        return "key_error", sanitize_error(stderr[:200] if stderr else "Key error")

    if "indexerror" in stderr_lower:
        return "index_error", sanitize_error(stderr[:200] if stderr else "Index error")

    if "attributeerror" in stderr_lower:
        return "attribute_error", sanitize_error(stderr[:200] if stderr else "Attribute error")

    if "nameerror" in stderr_lower:
        return "name_error", sanitize_error(
            stderr[:200] if stderr else "Name error (undefined variable)"
        )

    if "filenotfounderror" in stderr_lower:
        return "file_not_found", sanitize_error(stderr[:200] if stderr else "File not found")

    if "isadirectoryerror" in stderr_lower:
        return "file_error", "Expected file but found directory"

    if "notadirectoryerror" in stderr_lower:
        return "file_error", "Expected directory but found file"

    if "oserror" in stderr_lower or "ioerror" in stderr_lower:
        return "os_error", sanitize_error(stderr[:200] if stderr else "OS/IO error")

    if "recursionerror" in stderr_lower or "maximum recursion depth" in stderr_lower:
        return "recursion_error", "Maximum recursion depth exceeded"

    if "assertionerror" in stderr_lower:
        return "assertion_error", sanitize_error(stderr[:200] if stderr else "Assertion failed")

    if "zerodivisionerror" in stderr_lower:
        return "division_error", "Division by zero"

    if "overflowerror" in stderr_lower:
        return "overflow_error", "Numeric overflow"

    if (
        "unicodeerror" in stderr_lower
        or "unicodedecodeerror" in stderr_lower
        or "unicodeencodeerror" in stderr_lower
    ):
        return "encoding_error", "Unicode encoding/decoding error"

    if "jsondecodeerror" in stderr_lower:
        return "json_error", "Invalid JSON format"

    if "connectionerror" in stderr_lower or "connectionrefusederror" in stderr_lower:
        return "network_error", "Connection error"

    if "timeouterror" in stderr_lower:
        return "network_timeout", "Network request timed out"

    if "no matching distribution" in stderr_lower or "could not find a version" in stderr_lower:
        return "dependency_error", "Failed to install required package (not found)"

    if "error: externally-managed-environment" in stderr_lower:
        return "dependency_error", "Package installation failed (environment issue)"

    if "failed to download" in stderr_lower or "failed to fetch" in stderr_lower:
        return "dependency_error", "Failed to download package (network issue)"

    if "resolving dependencies" in stderr_lower and "conflict" in stderr_lower:
        return "dependency_error", "Package dependency conflict"

    if "docker" in stderr_lower and "not found" in stderr_lower:
        return "docker_error", "Docker image or command not found"

    if "circuit breaker" in stderr_lower:
        return "service_unavailable", "Service temporarily unavailable"

    if exit_code != 0:
        msg = sanitize_error(stderr[:200]) if stderr else f"Exit code {exit_code}"
        return "runtime_error", msg

    return "unknown", "Unknown error"


def is_safe_filename(filename: str) -> bool:
    if not filename:
        return False

    if "/" in filename or "\\" in filename:
        return False

    if filename == ".." or filename.startswith(".."):
        return False

    if filename.startswith("."):
        return False

    return True


_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DANGEROUS_ENV_CHARS = re.compile(r"[\x00-\x1f\x7f`$\\]")


def validate_env_key(key: str) -> bool:
    if not key or len(key) > 256:
        return False
    return bool(_ENV_KEY_PATTERN.match(key))


def validate_env_value(value: str) -> bool:
    if value is None:
        return False
    if _DANGEROUS_ENV_CHARS.search(value):
        return False
    if len(value) > 32768:
        return False
    return True


_DOCKER_IMAGE_PATTERN = re.compile(
    r"^"
    r"(?:(?P<registry>[a-zA-Z0-9][-a-zA-Z0-9.]*(?::\d+)?)/)?"
    r"(?P<name>[a-zA-Z0-9][-a-zA-Z0-9._/]*[a-zA-Z0-9]|[a-zA-Z0-9])"
    r"(?::(?P<tag>[a-zA-Z0-9][-a-zA-Z0-9._]{0,127}))?"
    r"(?:@sha256:(?P<digest>[a-f0-9]{64}))?"
    r"$"
)


def validate_docker_image(image: str) -> bool:
    if not image or len(image) > 256:
        return False

    dangerous_patterns = [
        "\n",
        "\r",
        "`",
        "$",
        "\\",
        "#",
        ";",
        "&&",
        "||",
    ]
    for pattern in dangerous_patterns:
        if pattern in image:
            return False

    return bool(_DOCKER_IMAGE_PATTERN.match(image))


_PYTHON_VERSION_PATTERN = re.compile(r"^3\.(?:[89]|1[0-9])$")


def validate_python_version(version: str) -> bool:
    if not version or len(version) > 10:
        return False
    return bool(_PYTHON_VERSION_PATTERN.match(version))


_SAFE_EXECUTION_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_SAFE_PIP_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._"
)
_SAFE_PIP_VERSION_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.*+!-"
)
_SAFE_PIP_OPERATOR_CHARS = frozenset("<>=!~")
_SAFE_DOCKER_ARG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/=,+-[]{}@%"
)


def validate_execution_id(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    if value in (".", "..") or value.startswith("."):
        return False
    if "/" in value or "\\" in value:
        return False
    return all(char in _SAFE_EXECUTION_ID_CHARS for char in value)


def _consume_token(value: str, start: int, allowed_chars: frozenset[str]) -> int:
    index = start
    while index < len(value) and value[index] in allowed_chars:
        index += 1
    return index


def validate_pip_requirement(requirement: str) -> bool:
    if not requirement or len(requirement) > 256:
        return False

    dangerous_patterns = [
        "\n",
        "\r",
        "`",
        "$",
        "\\",
        "#",
        ";",
        "&&",
        "||",
        "/",
        "@",
    ]
    for pattern in dangerous_patterns:
        if pattern in requirement:
            return False

    index = _consume_token(requirement, 0, _SAFE_PIP_NAME_CHARS)
    if index == 0:
        return False

    if index < len(requirement) and requirement[index] == "[":
        index += 1
        while True:
            extra_end = _consume_token(requirement, index, _SAFE_PIP_NAME_CHARS)
            if extra_end == index:
                return False
            index = extra_end

            if index >= len(requirement):
                return False
            if requirement[index] == "]":
                index += 1
                break
            if requirement[index] != ",":
                return False
            index += 1

    while index < len(requirement):
        op_end = _consume_token(requirement, index, _SAFE_PIP_OPERATOR_CHARS)
        if op_end == index:
            return False
        index = op_end

        version_end = _consume_token(requirement, index, _SAFE_PIP_VERSION_CHARS)
        if version_end == index:
            return False
        index = version_end

        if index == len(requirement):
            return True
        if requirement[index] != ",":
            return False
        index += 1

    return True


def validate_docker_run_args(args: Sequence[str]) -> bool:
    if not args or args[0] != "docker":
        return False

    for arg in args:
        if not isinstance(arg, str) or not arg:
            return False
        if len(arg) > 4096:
            return False
        if any(char not in _SAFE_DOCKER_ARG_CHARS for char in arg):
            return False

    return True
