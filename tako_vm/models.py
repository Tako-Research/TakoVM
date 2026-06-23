"""
Core data models for Tako VM production runtime.

This module defines the audit-grade ExecutionRecord and related models
used throughout the system. Designed for SVG/artifact-based workflows.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def sha256_json(obj: object) -> str:
    """
    Compute SHA256 hash of canonical JSON representation.

    Uses sorted keys and minimal separators for deterministic hashing
    regardless of dict ordering in caller code.

    Args:
        obj: JSON-serializable object

    Returns:
        Hex-encoded SHA256 hash
    """
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def sha256_content(content: str) -> str:
    """Compute SHA256 hash of string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ResourceUsage(BaseModel):
    """Resource consumption metrics from container execution."""

    model_config = {"extra": "forbid"}

    max_rss_mb: Optional[float] = Field(default=None, ge=0, le=1048576)  # Max 1TB
    """Peak resident set size in megabytes."""

    cpu_time_ms: Optional[int] = Field(default=None, ge=0, le=86400000)  # Max 24h
    """Total CPU time (user + system) in milliseconds."""

    wall_time_ms: Optional[int] = Field(default=None, ge=0, le=86400000)  # Max 24h
    """Wall clock duration in milliseconds."""


# Execution phases
ExecutionPhase = Literal[
    "startup",  # Container starting, deps installing
    "execution",  # User code running
    "completed",  # Finished successfully
    "failed",  # Failed during some phase
]


class ExecutionTiming(BaseModel):
    """
    Timing breakdown for execution phases.

    Separates startup time (container + dep install) from actual code execution,
    allowing users to understand where time is spent and which phase timed out.
    """

    model_config = {"extra": "forbid"}

    startup_ms: Optional[int] = Field(default=None, ge=0, le=86400000)
    """Time spent in startup phase (container init + dep install) in milliseconds."""

    dep_install_ms: Optional[int] = Field(default=None, ge=0, le=86400000)
    """Time spent installing dependencies in milliseconds (subset of startup)."""

    execution_ms: Optional[int] = Field(default=None, ge=0, le=86400000)
    """Time spent executing user code in milliseconds."""

    total_ms: Optional[int] = Field(default=None, ge=0, le=86400000)
    """Total container runtime in milliseconds."""

    phase_at_exit: Optional[ExecutionPhase] = None
    """Which phase the container was in when it exited/was killed."""

    dep_install_started: bool = False
    """Whether dependency installation was attempted."""


class InputArtifact(BaseModel):
    """Input artifact metadata for file-based inputs (e.g., SVG source files)."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=255)
    """Filename, e.g., 'input.svg'."""

    size_bytes: int = Field(..., ge=0, le=1073741824)  # Max 1GB
    """File size in bytes."""

    sha256: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    """SHA256 hash of file contents (hex-encoded)."""

    content_type: Optional[str] = Field(default=None, max_length=255)
    """MIME type, e.g., 'image/svg+xml'."""

    storage_key: str = Field(..., min_length=1, max_length=1024)
    """Storage location key, e.g., 'runs/<id>/inputs/input.svg'."""

    @field_validator("name")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        """Validate filename is safe (no path traversal)."""
        if "/" in v or "\\" in v:
            raise ValueError("Filename cannot contain path separators")
        if v in ("..", "."):
            raise ValueError("Invalid filename")
        return v


class Artifact(BaseModel):
    """Output artifact metadata."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=255)
    """Filename, e.g., 'output.png'."""

    size_bytes: int = Field(..., ge=0, le=1073741824)  # Max 1GB
    """File size in bytes."""

    sha256: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    """SHA256 hash of file contents (hex-encoded)."""

    content_type: Optional[str] = Field(default=None, max_length=255)
    """MIME type, e.g., 'image/png', 'application/pdf', 'image/svg+xml'."""

    storage_key: str = Field(..., min_length=1, max_length=1024)
    """Storage location key, e.g., 'runs/<id>/artifacts/output.png'."""

    @field_validator("name")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        """Validate filename is safe (no path traversal)."""
        if "/" in v or "\\" in v:
            raise ValueError("Filename cannot contain path separators")
        if v in ("..", "."):
            raise ValueError("Invalid filename")
        return v


# Canonical error type values (from security.classify_error)
ErrorType = Literal[
    # Timeout errors (phase-specific)
    "timeout",  # Generic execution exceeded time limit (legacy)
    "startup_timeout",  # Startup phase (dep install) exceeded time limit
    "execution_timeout",  # Code execution phase exceeded time limit
    # Signal-based errors
    "oom",  # Out of memory (SIGKILL/137)
    "cancelled",  # Cancelled by user (SIGTERM/143)
    "segfault",  # Segmentation fault (SIGSEGV/139)
    "abort",  # Process aborted (SIGABRT/134)
    "arithmetic_error",  # Floating point exception (SIGFPE/136)
    "bus_error",  # Bus error (SIGBUS/135)
    "pipe_error",  # Broken pipe (SIGPIPE/141)
    "killed",  # Process killed by system
    # Permission errors
    "permission",  # Permission denied
    # Python errors
    "syntax_error",  # SyntaxError, IndentationError
    "import_error",  # ImportError, ModuleNotFoundError
    "type_error",  # TypeError
    "value_error",  # ValueError
    "key_error",  # KeyError
    "index_error",  # IndexError
    "attribute_error",  # AttributeError
    "name_error",  # NameError (undefined variable)
    "file_not_found",  # FileNotFoundError
    "file_error",  # IsADirectoryError, NotADirectoryError
    "os_error",  # OSError, IOError
    "recursion_error",  # RecursionError
    "assertion_error",  # AssertionError
    "division_error",  # ZeroDivisionError
    "overflow_error",  # OverflowError
    "encoding_error",  # UnicodeError
    "json_error",  # JSONDecodeError
    # Dependency errors
    "dependency_error",  # Package installation failure (uv/pip)
    # Network errors
    "network_error",  # ConnectionError
    "network_timeout",  # Network request timed out
    # System errors
    "docker_error",  # Docker image/command not found
    "service_unavailable",  # Circuit breaker open
    "config_error",  # Configuration error
    "internal_error",  # Internal Tako VM error
    "interrupted",  # Job interrupted by server restart/shutdown
    "runtime_error",  # Generic runtime error
    "unknown",  # Unknown error type
]


class ExecutionError(BaseModel):
    """Sanitized error information."""

    model_config = {"extra": "forbid"}

    type: ErrorType
    """Error type classification."""

    message: str = Field(..., min_length=1, max_length=4096)
    """Sanitized error message (no sensitive paths)."""

    phase: Optional[ExecutionPhase] = None
    """Which phase the error occurred in (startup or execution)."""


# Canonical job status values
JobStatus = Literal[
    "queued",  # Submitted, waiting in queue
    "running",  # Currently executing
    "succeeded",  # Completed successfully
    "failed",  # Failed with error
    "timeout",  # Exceeded time limit
    "oom",  # Out of memory
    "cancelled",  # Cancelled by user/system
]


class ExecutionRecord(BaseModel):
    """
    Audit-grade execution record.

    This is the primary record returned from all executions, containing
    complete audit trail information for compliance and debugging.
    Designed for artifact-based workflows (SVG rendering, etc.).
    """

    model_config = {"extra": "forbid"}

    # Identity
    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=64)
    """Unique identifier for this execution."""

    # Status
    status: JobStatus = "queued"
    """Current execution status."""

    # Job reference
    job_type: str = Field(default="default", min_length=1, max_length=64)
    """Job type name, e.g., 'svg-processing'."""

    job_ref: str = Field(default="default@latest", min_length=1, max_length=128)
    """Full job reference, e.g., 'svg-processing@sha256:a1b2c3d4'."""

    # Timing
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """When the job was created/submitted."""

    queued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """When the job entered the queue."""

    dequeued_at: Optional[datetime] = None
    """When the job was picked up by a worker."""

    started_at: Optional[datetime] = None
    """When execution began (container started)."""

    ended_at: Optional[datetime] = None
    """When execution completed."""

    duration_ms: Optional[int] = Field(default=None, ge=0, le=86400000)  # Max 24h
    """Total execution duration in milliseconds."""

    # Retry/attempt tracking
    attempt: int = Field(default=0, ge=0, le=100)
    """Current attempt number (0-indexed)."""

    max_attempts: int = Field(default=1, ge=1, le=100)
    """Maximum retry attempts."""

    # Worker tracking
    worker_id: Optional[str] = Field(default=None, max_length=64)
    """ID of worker that executed this job."""

    # Effective isolation runtime the container actually ran under. 'runsc' is
    # the gVisor sandbox (the product's security boundary); 'runc' is the
    # weaker, host-kernel-sharing fallback (only reachable in permissive mode
    # when gVisor is unavailable). Persisted and surfaced in the API so a caller
    # can prove, after the fact, whether a given job had the gVisor boundary and
    # refuse to trust results from a runc run. None for legacy rows written
    # before this field existed.
    runtime: Optional[str] = Field(default=None, max_length=16)
    """Effective container runtime: 'runsc' (gVisor) or 'runc' (weaker fallback)."""

    # Idempotency
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    """Client-provided key to prevent duplicate executions."""

    idempotency_fingerprint: Optional[str] = Field(default=None, max_length=64)
    """SHA256 fingerprint of request params (for idempotency collision detection)."""

    # Input fingerprints (not raw data for security)
    code_hash: str = Field(default="", max_length=64)
    """SHA256 hash of submitted code (hex-encoded, or empty)."""

    input_hash: str = Field(default="", max_length=64)
    """SHA256 hash of canonical input_data JSON (hex-encoded, or empty)."""

    params_hash: str = Field(default="", max_length=64)
    """SHA256 hash of canonical execution params JSON (hex-encoded, or empty)."""

    input_artifacts_hash: str = Field(default="", max_length=64)
    """SHA256 hash of canonical input artifacts manifest (hex-encoded, or empty)."""

    @field_validator(
        "code_hash",
        "input_hash",
        "params_hash",
        "input_artifacts_hash",
        "idempotency_fingerprint",
        mode="after",
    )
    @classmethod
    def validate_hash_field(cls, v: Optional[str]) -> Optional[str]:
        """Validate hash fields are either empty/None or valid SHA256 hex strings."""
        import re

        if v is None or v == "":
            return v
        if len(v) != 64 or not re.match(r"^[a-f0-9]{64}$", v):
            raise ValueError("Hash must be empty or 64-character lowercase hex string")
        return v

    # Input artifacts (for file-based inputs like SVG)
    input_artifacts: List[InputArtifact] = Field(default_factory=list)
    """List of input artifacts with metadata."""

    # Results
    exit_code: Optional[int] = Field(default=None, ge=-1, le=255)
    """Container exit code (-1 for internal errors, 0-255 for normal)."""

    stdout: str = ""
    """Captured stdout (bounded to max_stdout_bytes)."""

    stderr: str = ""
    """Captured stderr (bounded to max_stderr_bytes)."""

    stdout_truncated: bool = False
    """Whether stdout was truncated due to size limit."""

    stderr_truncated: bool = False
    """Whether stderr was truncated due to size limit."""

    result_json: Optional[Dict[str, Any]] = None
    """Parsed JSON result from /output/result.json (if present)."""

    # Resources
    resource_usage: Optional[ResourceUsage] = None
    """Resource consumption metrics."""

    # Timing breakdown
    timing: Optional[ExecutionTiming] = None
    """Detailed timing breakdown by execution phase."""

    # Output artifacts
    artifacts: List[Artifact] = Field(default_factory=list)
    """List of output artifacts with metadata."""

    # Error (sanitized)
    error: Optional[ExecutionError] = None
    """Sanitized error information if failed."""

    # Audit
    client_ip: Optional[str] = Field(default=None, max_length=45)  # IPv6 max length
    """Client IP address."""

    correlation_id: Optional[str] = Field(default=None, max_length=64)
    """Correlation ID (X-Correlation-ID) of the submitting request, for request tracing."""

    # Lineage tracking
    parent_execution_id: Optional[str] = Field(default=None, max_length=64)
    """ID of parent execution (for reruns/forks)."""

    relationship: Optional[Literal["rerun", "fork"]] = None
    """Relationship to parent execution."""

    @field_validator("stdout", "stderr", mode="before")
    @classmethod
    def ensure_string(cls, v: str) -> str:
        """Ensure outputs are strings (truncation handled by capture layer)."""
        return v or ""

    def to_summary(self) -> dict:
        """Return a summary suitable for list endpoints."""
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "job_type": self.job_type,
            "job_ref": self.job_ref,
            "created_at": self.created_at.isoformat(),  # pylint: disable=no-member
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "attempt": self.attempt,
        }

    def compute_input_artifacts_hash(self) -> str:
        """Compute canonical hash of input artifacts manifest."""
        if not self.input_artifacts:
            return ""
        manifest = [
            {"name": a.name, "sha256": a.sha256, "size_bytes": a.size_bytes}
            for a in sorted(self.input_artifacts, key=lambda x: x.name)
        ]
        return sha256_json(manifest)


class JobVersion(BaseModel):
    """Immutable job type version record."""

    model_config = {"extra": "forbid"}

    job_type_name: str = Field(..., min_length=1, max_length=64)
    """Name of the job type."""

    version_tag: Optional[str] = Field(default=None, max_length=64)
    """Semantic version tag, e.g., 'v1.0.0'."""

    digest: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    """SHA256 digest of job type configuration (full hex string)."""

    # Build metadata
    built_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """When this version was built."""

    built_by: Optional[str] = Field(default=None, max_length=128)
    """API key or 'manual' that triggered the build."""

    dockerfile_hash: str = Field(default="", max_length=64)
    """SHA256 of generated Dockerfile (hex-encoded, or empty)."""

    requirements_hash: str = Field(default="", max_length=64)
    """SHA256 of requirements list (hex-encoded, or empty)."""

    # Docker image reference
    image_ref: str = Field(default="", max_length=256)
    """Full image reference, e.g., 'tako-vm-svg-processing@sha256:...'."""

    @field_validator("dockerfile_hash", "requirements_hash", mode="after")
    @classmethod
    def validate_hash_field(cls, v: str) -> str:
        """Validate hash fields are either empty or valid SHA256 hex strings."""
        import re

        if v == "":
            return v
        if len(v) != 64 or not re.match(r"^[a-f0-9]{64}$", v):
            raise ValueError("Hash must be empty or 64-character lowercase hex string")
        return v

    @property
    def full_ref(self) -> str:
        """Return job_type@sha256:digest format."""
        return f"{self.job_type_name}@sha256:{self.digest[:12]}"

    @property
    def short_digest(self) -> str:
        """Return shortened digest for display."""
        return self.digest[:12]


class DeadLetterEntry(BaseModel):
    """Entry in the dead letter queue for failed jobs.

    Redaction policy: DLQ entries are forensic records, not replay payloads.
    ``job_data`` must never contain raw code, input_data values, or
    idempotency keys -- AI-agent callers routinely embed credentials in
    input_data, and persisting them as readable JSONB would contradict
    ExecutionRecord's hash-only posture. Build ``job_data`` with
    :meth:`build_job_summary`, which keeps fingerprints (sha256 hashes),
    sizes, and non-secret diagnostic metadata (job_type, timeouts,
    requirements package list, correlation/lineage IDs) and records only the
    PRESENCE of an idempotency key.
    """

    model_config = {"extra": "forbid"}

    id: Optional[int] = Field(default=None, ge=0)
    """Database ID (set by storage)."""

    job_id: str = Field(..., min_length=1, max_length=64)
    """Original job ID."""

    job_data: Dict[str, Any]
    """Redacted forensic summary of the original job (see build_job_summary).

    Historic entries (written before redaction was introduced) may still
    contain raw fields; new entries never do.
    """

    error_type: ErrorType
    """Type of error that caused failure."""

    error_message: Optional[str] = Field(default=None, max_length=4096)
    """Error message."""

    retry_count: int = Field(default=0, ge=0, le=100)
    """Number of retry attempts made."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """When the entry was added to DLQ."""

    client_ip: Optional[str] = Field(default=None, max_length=45)  # IPv6 max length
    """Original client IP."""

    correlation_id: Optional[str] = Field(default=None, max_length=64)
    """Correlation ID for tracing."""

    @classmethod
    def build_job_summary(cls, job_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build a redacted forensic summary from a raw job payload.

        Drops raw ``code``, ``input_data``, and the ``idempotency_key`` value
        entirely; keeps sha256 fingerprints, byte sizes, and non-secret
        diagnostic metadata so internal failures remain debuggable.
        """
        code = job_data.get("code", "") or ""
        input_data = job_data.get("input_data", {})
        canonical_input = json.dumps(
            input_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        summary: Dict[str, Any] = {
            "job_type": job_data.get("job_type") or "default",
            "code_hash": sha256_content(code),
            "input_hash": sha256_json(input_data),
            "code_size_bytes": len(code.encode("utf-8")),
            "input_size_bytes": len(canonical_input.encode("utf-8")),
            "has_idempotency_key": job_data.get("idempotency_key") is not None,
        }
        # Non-secret diagnostic metadata, included only when present.
        for key in (
            "timeout",
            "startup_timeout",
            "requirements",
            "correlation_id",
            "parent_execution_id",
        ):
            value = job_data.get(key)
            if value is not None:
                summary[key] = value
        return summary
