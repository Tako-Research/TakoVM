import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _validate_optional_sha256_hex(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return v
    if not _SHA256_RE.match(v):
        raise ValueError("Hash must be empty or 64-character lowercase hex string")
    return v


def _validate_safe_filename(v: str) -> str:
    if "/" in v or "\\" in v:
        raise ValueError("Filename cannot contain path separators")
    if v in ("..", "."):
        raise ValueError("Invalid filename")
    return v


def sha256_json(obj: object) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def sha256_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ResourceUsage(BaseModel):
    model_config = {"extra": "forbid"}

    max_rss_mb: Optional[float] = Field(default=None, ge=0, le=1048576)

    cpu_time_ms: Optional[int] = Field(default=None, ge=0, le=86400000)

    wall_time_ms: Optional[int] = Field(default=None, ge=0, le=86400000)


ExecutionPhase = Literal[
    "startup",
    "execution",
    "completed",
    "failed",
]


class ExecutionTiming(BaseModel):
    model_config = {"extra": "forbid"}

    startup_ms: Optional[int] = Field(default=None, ge=0, le=86400000)

    dep_install_ms: Optional[int] = Field(default=None, ge=0, le=86400000)

    execution_ms: Optional[int] = Field(default=None, ge=0, le=86400000)

    total_ms: Optional[int] = Field(default=None, ge=0, le=86400000)

    phase_at_exit: Optional[ExecutionPhase] = None

    dep_install_started: bool = False


class InputArtifact(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=255)

    size_bytes: int = Field(..., ge=0, le=1073741824)

    sha256: str = Field(..., min_length=64, max_length=64, pattern=_SHA256_RE.pattern)

    content_type: Optional[str] = Field(default=None, max_length=255)

    storage_key: str = Field(..., min_length=1, max_length=1024)

    @field_validator("name")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        return _validate_safe_filename(v)


class Artifact(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=255)

    size_bytes: int = Field(..., ge=0, le=1073741824)

    sha256: str = Field(..., min_length=64, max_length=64, pattern=_SHA256_RE.pattern)

    content_type: Optional[str] = Field(default=None, max_length=255)

    storage_key: str = Field(..., min_length=1, max_length=1024)

    @field_validator("name")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        return _validate_safe_filename(v)


ErrorType = Literal[
    "timeout",
    "startup_timeout",
    "execution_timeout",
    "oom",
    "cancelled",
    "segfault",
    "abort",
    "arithmetic_error",
    "bus_error",
    "pipe_error",
    "killed",
    "permission",
    "syntax_error",
    "import_error",
    "type_error",
    "value_error",
    "key_error",
    "index_error",
    "attribute_error",
    "name_error",
    "file_not_found",
    "file_error",
    "os_error",
    "recursion_error",
    "assertion_error",
    "division_error",
    "overflow_error",
    "encoding_error",
    "json_error",
    "dependency_error",
    "network_error",
    "network_timeout",
    "docker_error",
    "service_unavailable",
    "config_error",
    "internal_error",
    "interrupted",
    "runtime_error",
    "unknown",
]


class ExecutionError(BaseModel):
    model_config = {"extra": "forbid"}

    type: ErrorType

    message: str = Field(..., min_length=1, max_length=4096)

    phase: Optional[ExecutionPhase] = None


JobStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "timeout",
    "oom",
    "cancelled",
]


class ExecutionRecord(BaseModel):
    model_config = {"extra": "forbid"}

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=64)

    status: JobStatus = "queued"

    job_type: str = Field(default="default", min_length=1, max_length=64)

    job_ref: str = Field(default="default@latest", min_length=1, max_length=128)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    queued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    dequeued_at: Optional[datetime] = None

    started_at: Optional[datetime] = None

    ended_at: Optional[datetime] = None

    duration_ms: Optional[int] = Field(default=None, ge=0, le=86400000)

    attempt: int = Field(default=0, ge=0, le=100)

    max_attempts: int = Field(default=1, ge=1, le=100)

    worker_id: Optional[str] = Field(default=None, max_length=64)

    runtime: Optional[str] = Field(default=None, max_length=16)

    idempotency_key: Optional[str] = Field(default=None, max_length=128)

    idempotency_fingerprint: Optional[str] = Field(default=None, max_length=64)

    code_hash: str = Field(default="", max_length=64)

    input_hash: str = Field(default="", max_length=64)

    params_hash: str = Field(default="", max_length=64)

    input_artifacts_hash: str = Field(default="", max_length=64)

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
        return _validate_optional_sha256_hex(v)

    input_artifacts: List[InputArtifact] = Field(default_factory=list)

    exit_code: Optional[int] = Field(default=None, ge=-1, le=255)

    stdout: str = ""

    stderr: str = ""

    stdout_truncated: bool = False

    stderr_truncated: bool = False

    result_json: Optional[Dict[str, Any]] = None

    resource_usage: Optional[ResourceUsage] = None

    timing: Optional[ExecutionTiming] = None

    artifacts: List[Artifact] = Field(default_factory=list)

    error: Optional[ExecutionError] = None

    client_ip: Optional[str] = Field(default=None, max_length=45)

    correlation_id: Optional[str] = Field(default=None, max_length=64)

    parent_execution_id: Optional[str] = Field(default=None, max_length=64)

    relationship: Optional[Literal["rerun", "fork"]] = None

    session_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("stdout", "stderr", mode="before")
    @classmethod
    def ensure_string(cls, v: str) -> str:
        return v or ""

    def to_summary(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "job_type": self.job_type,
            "job_ref": self.job_ref,
            "created_at": self.created_at.isoformat(),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "attempt": self.attempt,
        }

    def compute_input_artifacts_hash(self) -> str:
        if not self.input_artifacts:
            return ""
        manifest = [
            {"name": a.name, "sha256": a.sha256, "size_bytes": a.size_bytes}
            for a in sorted(self.input_artifacts, key=lambda x: x.name)
        ]
        return sha256_json(manifest)


class JobVersion(BaseModel):
    model_config = {"extra": "forbid"}

    job_type_name: str = Field(..., min_length=1, max_length=64)

    version_tag: Optional[str] = Field(default=None, max_length=64)

    digest: str = Field(..., min_length=64, max_length=64, pattern=_SHA256_RE.pattern)

    built_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    built_by: Optional[str] = Field(default=None, max_length=128)

    dockerfile_hash: str = Field(default="", max_length=64)

    requirements_hash: str = Field(default="", max_length=64)

    image_ref: str = Field(default="", max_length=256)

    @field_validator("dockerfile_hash", "requirements_hash", mode="after")
    @classmethod
    def validate_hash_field(cls, v: str) -> str:
        return _validate_optional_sha256_hex(v) or ""

    @property
    def full_ref(self) -> str:
        return f"{self.job_type_name}@sha256:{self.digest[:12]}"

    @property
    def short_digest(self) -> str:
        return self.digest[:12]


class DeadLetterEntry(BaseModel):
    model_config = {"extra": "forbid"}

    id: Optional[int] = Field(default=None, ge=0)

    job_id: str = Field(..., min_length=1, max_length=64)

    job_data: Dict[str, Any]

    error_type: ErrorType

    error_message: Optional[str] = Field(default=None, max_length=4096)

    retry_count: int = Field(default=0, ge=0, le=100)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    client_ip: Optional[str] = Field(default=None, max_length=45)

    correlation_id: Optional[str] = Field(default=None, max_length=64)

    @classmethod
    def build_job_summary(cls, job_data: Dict[str, Any]) -> Dict[str, Any]:
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


SessionStatus = Literal[
    "creating",
    "running",
    "idle",
    "suspended",
    "terminated",
    "failed",
    "expired",
]

SessionEventDirection = Literal[
    "in",
    "out",
    "system",
]


class SessionRecord(BaseModel):
    model_config = {"extra": "forbid"}

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=64)

    status: SessionStatus = "creating"

    container_name: str = Field(..., min_length=1, max_length=128)

    container_id: Optional[str] = Field(default=None, max_length=128)

    image_name: Optional[str] = Field(default=None, max_length=255)

    runtime: Optional[str] = Field(default=None, max_length=16)

    workspace_dir: str = Field(..., min_length=1, max_length=1024)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    started_at: Optional[datetime] = None

    last_activity_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    expires_at: Optional[datetime] = None

    ended_at: Optional[datetime] = None

    idle_timeout_seconds: int = Field(default=1800, ge=30, le=86400)

    ttl_seconds: int = Field(default=86400, ge=60, le=604800)

    metadata: Dict[str, Any] = Field(default_factory=dict)


class SessionEvent(BaseModel):
    model_config = {"extra": "forbid"}

    id: Optional[int] = Field(default=None, ge=0)

    session_id: str = Field(..., min_length=1, max_length=64)

    direction: SessionEventDirection

    event_type: str = Field(default="message", min_length=1, max_length=64)

    payload: Any = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
