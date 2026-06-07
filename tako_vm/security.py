"""
Security utilities for Tako VM.

Provides output capping, error sanitization, and artifact validation.
"""

import hashlib
import logging
import re
from typing import Sequence
from pathlib import Path
from typing import List, Tuple

from .models import ErrorType

logger = logging.getLogger(__name__)

# Patterns to sanitize from error messages (pattern, replacement)
SANITIZE_PATTERNS: List[Tuple[str, str]] = [
    # Temp directories
    (r"/tmp/job-[a-zA-Z0-9_-]+", "/tmp/job-***"),
    (r"/var/folders/[^\s]+", "/var/folders/***"),
    # User home directories
    (r"/Users/[^/\s]+", "/home/***"),
    (r"/home/[^/\s]+", "/home/***"),
    # Container internal paths that might leak info
    (r"/app/[^\s]+", "/app/***"),
    # IP addresses (internal)
    (r"172\.\d+\.\d+\.\d+", "172.***.***"),
    (r"10\.\d+\.\d+\.\d+", "10.***.***"),
    (r"192\.168\.\d+\.\d+", "192.168.***.***"),
    # Docker container IDs
    (r"[a-f0-9]{64}", "<container-id>"),
    (r"[a-f0-9]{12}(?![a-f0-9])", "<container-id>"),
]

# Default limits
DEFAULT_MAX_STDOUT_BYTES = 65536  # 64KB
DEFAULT_MAX_STDERR_BYTES = 65536  # 64KB
DEFAULT_MAX_ARTIFACT_BYTES = 10_485_760  # 10MB
DEFAULT_MAX_TOTAL_ARTIFACTS_BYTES = 52_428_800  # 50MB


def sanitize_error(error: str) -> str:
    """
    Remove sensitive information from error messages.

    Sanitizes:
    - File system paths
    - User directories
    - IP addresses
    - Container IDs

    Args:
        error: Raw error message

    Returns:
        Sanitized error message safe for external exposure
    """
    if not error:
        return ""

    result = error
    for pattern, replacement in SANITIZE_PATTERNS:
        result = re.sub(pattern, replacement, result)

    return result


def cap_output(output: str, max_bytes: int = DEFAULT_MAX_STDOUT_BYTES) -> str:
    """
    Cap output to maximum byte size.

    Truncates at UTF-8 character boundary and adds truncation notice.

    Args:
        output: Raw output string
        max_bytes: Maximum size in bytes

    Returns:
        Capped output with truncation notice if truncated
    """
    if not output:
        return ""

    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return output

    # Leave room for truncation message
    truncation_msg = f"\n\n[TRUNCATED: output exceeded {max_bytes} bytes]"
    truncation_bytes = len(truncation_msg.encode("utf-8"))
    available_bytes = max_bytes - truncation_bytes

    if available_bytes <= 0:
        return truncation_msg

    # Truncate at byte boundary, then decode
    truncated_bytes = encoded[:available_bytes]

    # Find last valid UTF-8 boundary
    while truncated_bytes:
        try:
            truncated = truncated_bytes.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated_bytes = truncated_bytes[:-1]
    else:
        truncated = ""

    return truncated + truncation_msg


def validate_artifact_size(
    path: Path, max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES
) -> Tuple[bool, int]:
    """
    Validate artifact is within size limit.

    Args:
        path: Path to artifact file
        max_bytes: Maximum allowed size

    Returns:
        Tuple of (is_valid, actual_size)
    """
    if not path.exists():
        return False, 0

    size = path.stat().st_size
    return size <= max_bytes, size


def compute_file_hash(path: Path) -> str:
    """
    Compute SHA256 hash of file.

    Args:
        path: Path to file

    Returns:
        Hex-encoded SHA256 hash
    """
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_content_hash(content: str) -> str:
    """
    Compute SHA256 hash of string content.

    Args:
        content: String to hash

    Returns:
        Hex-encoded SHA256 hash
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def classify_error(exit_code: int, stderr: str, timed_out: bool = False) -> Tuple[ErrorType, str]:
    """
    Classify error type from execution results.

    Args:
        exit_code: Container exit code
        stderr: Captured stderr
        timed_out: Whether execution timed out

    Returns:
        Tuple of (error_type, sanitized_message)
    """
    if timed_out:
        return "timeout", "Execution exceeded time limit"

    # Signal-based exit codes (128 + signal_number)
    if exit_code == 137:  # SIGKILL (9)
        return "oom", "Execution exceeded memory limit"

    if exit_code == 143:  # SIGTERM (15)
        return "cancelled", "Execution was terminated"

    if exit_code == 139:  # SIGSEGV (11)
        return "segfault", "Segmentation fault in executed code"

    if exit_code == 134:  # SIGABRT (6)
        return "abort", "Process aborted (assertion failure or abort() called)"

    if exit_code == 136:  # SIGFPE (8)
        return "arithmetic_error", "Floating point exception (division by zero)"

    if exit_code == 135:  # SIGBUS (7)
        return "bus_error", "Bus error (invalid memory access)"

    if exit_code == 141:  # SIGPIPE (13)
        return "pipe_error", "Broken pipe"

    # Check stderr for common Python error patterns
    stderr_lower = stderr.lower() if stderr else ""

    # Memory errors
    if "memoryerror" in stderr_lower or "out of memory" in stderr_lower:
        return "oom", "Execution exceeded memory limit"

    if "cannot allocate memory" in stderr_lower:
        return "oom", "Cannot allocate memory"

    # Process termination
    if "killed" in stderr_lower:
        return "killed", "Process was killed by system"

    # Permission errors
    if "permission denied" in stderr_lower:
        return "permission", "Permission denied during execution"

    if "operation not permitted" in stderr_lower:
        return "permission", "Operation not permitted"

    # Syntax errors
    if "syntaxerror" in stderr_lower:
        return "syntax_error", sanitize_error(stderr[:200] if stderr else "Syntax error")

    if "indentationerror" in stderr_lower:
        return "syntax_error", sanitize_error(stderr[:200] if stderr else "Indentation error")

    # Dependency installation errors (uv/pip failures)
    if "no matching distribution" in stderr_lower or "could not find" in stderr_lower:
        return "dependency_error", "Failed to install required package (not found)"

    if "error: externally-managed-environment" in stderr_lower:
        return "dependency_error", "Package installation failed (environment issue)"

    if "failed to download" in stderr_lower or "failed to fetch" in stderr_lower:
        return "dependency_error", "Failed to download package (network issue)"

    if "resolving dependencies" in stderr_lower and "conflict" in stderr_lower:
        return "dependency_error", "Package dependency conflict"

    # Import errors
    if "importerror" in stderr_lower or "modulenotfounderror" in stderr_lower:
        return "import_error", sanitize_error(stderr[:200] if stderr else "Import error")

    # Type errors
    if "typeerror" in stderr_lower:
        return "type_error", sanitize_error(stderr[:200] if stderr else "Type error")

    # Value errors
    if "valueerror" in stderr_lower:
        return "value_error", sanitize_error(stderr[:200] if stderr else "Value error")

    # Key/Index errors
    if "keyerror" in stderr_lower:
        return "key_error", sanitize_error(stderr[:200] if stderr else "Key error")

    if "indexerror" in stderr_lower:
        return "index_error", sanitize_error(stderr[:200] if stderr else "Index error")

    # Attribute errors
    if "attributeerror" in stderr_lower:
        return "attribute_error", sanitize_error(stderr[:200] if stderr else "Attribute error")

    # Name errors
    if "nameerror" in stderr_lower:
        return "name_error", sanitize_error(
            stderr[:200] if stderr else "Name error (undefined variable)"
        )

    # File errors
    if "filenotfounderror" in stderr_lower:
        return "file_not_found", sanitize_error(stderr[:200] if stderr else "File not found")

    if "isadirectoryerror" in stderr_lower:
        return "file_error", "Expected file but found directory"

    if "notadirectoryerror" in stderr_lower:
        return "file_error", "Expected directory but found file"

    # OS errors
    if "oserror" in stderr_lower or "ioerror" in stderr_lower:
        return "os_error", sanitize_error(stderr[:200] if stderr else "OS/IO error")

    # Recursion errors
    if "recursionerror" in stderr_lower or "maximum recursion depth" in stderr_lower:
        return "recursion_error", "Maximum recursion depth exceeded"

    # Assertion errors
    if "assertionerror" in stderr_lower:
        return "assertion_error", sanitize_error(stderr[:200] if stderr else "Assertion failed")

    # Zero division
    if "zerodivisionerror" in stderr_lower:
        return "division_error", "Division by zero"

    # Overflow errors
    if "overflowerror" in stderr_lower:
        return "overflow_error", "Numeric overflow"

    # Unicode errors
    if (
        "unicodeerror" in stderr_lower
        or "unicodedecodeerror" in stderr_lower
        or "unicodeencodeerror" in stderr_lower
    ):
        return "encoding_error", "Unicode encoding/decoding error"

    # JSON errors
    if "jsondecodeerror" in stderr_lower:
        return "json_error", "Invalid JSON format"

    # Network errors (if network is enabled)
    if "connectionerror" in stderr_lower or "connectionrefusederror" in stderr_lower:
        return "network_error", "Connection error"

    if "timeouterror" in stderr_lower:
        return "network_timeout", "Network request timed out"

    # Docker/container specific
    if "docker" in stderr_lower and "not found" in stderr_lower:
        return "docker_error", "Docker image or command not found"

    if "circuit breaker" in stderr_lower:
        return "service_unavailable", "Service temporarily unavailable"

    # Generic error
    if exit_code != 0:
        msg = sanitize_error(stderr[:200]) if stderr else f"Exit code {exit_code}"
        return "runtime_error", msg

    return "unknown", "Unknown error"


def is_safe_filename(filename: str) -> bool:
    """
    Check if filename is safe (no path traversal).

    Args:
        filename: Filename to check

    Returns:
        True if filename is safe
    """
    if not filename:
        return False

    # No path separators
    if "/" in filename or "\\" in filename:
        return False

    # No parent directory references
    if filename == ".." or filename.startswith(".."):
        return False

    # No hidden files (optional, can be relaxed)
    if filename.startswith("."):
        return False

    return True


# Pattern for valid environment variable names (POSIX)
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Characters that could be dangerous in Docker env values
_DANGEROUS_ENV_CHARS = re.compile(r"[\x00-\x1f\x7f`$\\]")


def validate_env_key(key: str) -> bool:
    """
    Validate environment variable key is safe for Docker.

    Args:
        key: Environment variable name

    Returns:
        True if key is valid
    """
    if not key or len(key) > 256:
        return False
    return bool(_ENV_KEY_PATTERN.match(key))


def validate_env_value(value: str) -> bool:
    """
    Validate environment variable value is safe for Docker.

    Args:
        value: Environment variable value

    Returns:
        True if value is safe
    """
    if value is None:
        return False
    # Reject control characters, backticks, dollar signs, backslashes
    if _DANGEROUS_ENV_CHARS.search(value):
        return False
    # Reasonable length limit
    if len(value) > 32768:  # 32KB max
        return False
    return True


# Pattern for valid Docker image names
# Format: [registry/]name[:tag][@sha256:digest]
# Registry: hostname[:port]/
# Name: alphanumeric, dash, underscore, period, forward slash
# Tag: alphanumeric, dash, underscore, period
_DOCKER_IMAGE_PATTERN = re.compile(
    r"^"
    r"(?:(?P<registry>[a-zA-Z0-9][-a-zA-Z0-9.]*(?::\d+)?)/)?"  # Optional registry
    r"(?P<name>[a-zA-Z0-9][-a-zA-Z0-9._/]*[a-zA-Z0-9]|[a-zA-Z0-9])"  # Image name
    r"(?::(?P<tag>[a-zA-Z0-9][-a-zA-Z0-9._]{0,127}))?"  # Optional tag
    r"(?:@sha256:(?P<digest>[a-f0-9]{64}))?"  # Optional digest
    r"$"
)


def validate_docker_image(image: str) -> bool:
    """
    Validate Docker image name is safe.

    Validates format to prevent injection attacks in Dockerfile FROM statements.

    Args:
        image: Docker image name (e.g., 'python:3.11-slim', 'myregistry.io/app:v1')

    Returns:
        True if image name is valid and safe
    """
    if not image or len(image) > 256:
        return False

    # Reject dangerous patterns that could be used for injection
    dangerous_patterns = [
        "\n",
        "\r",  # Newlines could inject additional Dockerfile commands
        "`",
        "$",
        "\\",  # Shell injection
        "#",  # Comments
        ";",
        "&&",
        "||",  # Command chaining
    ]
    for pattern in dangerous_patterns:
        if pattern in image:
            return False

    return bool(_DOCKER_IMAGE_PATTERN.match(image))


# Pattern for valid Python versions
_PYTHON_VERSION_PATTERN = re.compile(r"^3\.(?:[89]|1[0-9])$")


def validate_python_version(version: str) -> bool:
    """
    Validate Python version string.

    Accepts versions like '3.8', '3.9', '3.10', '3.11', '3.12', etc.

    Args:
        version: Python version string

    Returns:
        True if version is valid
    """
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
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "._:/=,+-[]{}@%"
)


def validate_execution_id(value: str) -> bool:
    """
    Validate an execution/job ID before using it in filesystem paths.

    The same identifier is also reused in Docker container names, so the
    character set stays intentionally conservative.
    """
    if not value or len(value) > 64:
        return False
    if value in (".", "..") or value.startswith("."):
        return False
    if "/" in value or "\\" in value:
        return False
    return all(char in _SAFE_EXECUTION_ID_CHARS for char in value)


def _consume_token(value: str, start: int, allowed_chars: frozenset[str]) -> int:
    """Consume a non-empty token made of allowed characters."""
    index = start
    while index < len(value) and value[index] in allowed_chars:
        index += 1
    return index


def validate_pip_requirement(requirement: str) -> bool:
    """
    Validate pip requirement string is safe.

    Validates against simplified PEP 508 to prevent injection.

    Args:
        requirement: Pip requirement (e.g., 'numpy', 'pandas>=1.0', 'flask[async]')

    Returns:
        True if requirement is valid and safe
    """
    if not requirement or len(requirement) > 256:
        return False

    # Reject dangerous patterns
    dangerous_patterns = [
        "\n",
        "\r",  # Newlines
        "`",
        "$",
        "\\",  # Shell injection
        "#",  # Comments
        ";",
        "&&",
        "||",  # Command chaining
        "/",  # Path separators (could be URLs or local paths)
        "@",  # URL specifiers
    ]
    for pattern in dangerous_patterns:
        if pattern in requirement:
            return False

    # Parse a conservative subset of PEP 508 in linear time:
    # package-name
    # package-name[extra,extra2]
    # package-name>=1.0,!=2.0
    index = _consume_token(requirement, 0, _SAFE_PIP_NAME_CHARS)
    if index == 0:
        return False

    # Optional extras block: [extra,extra2]
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

    # Optional version clauses: >=1.0,!=2.0
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
    """
    Validate a Docker CLI argv list before execution.

    This is a defense-in-depth check for commands built from validated user and
    config inputs. It assumes list-style subprocess execution (no shell).

    Args:
        args: Complete argv list passed to subprocess.run()

    Returns:
        True if every argument is safe for direct Docker CLI execution
    """
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
