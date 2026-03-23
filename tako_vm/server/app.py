"""
FastAPI server for secure code execution.

Provides both legacy synchronous execution and new async job queue execution.
"""

# Suppress LibreSSL warnings on macOS before any other imports
import warnings

try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from psycopg import IntegrityError
from pydantic import BaseModel, Field, field_validator

from tako_vm import __version__
from tako_vm.config import TakoVMConfig, get_config
from tako_vm.execution.health import get_circuit_breaker, startup_cleanup
from tako_vm.execution.worker import CodeExecutor, check_gvisor_available
from tako_vm.job_types import JobTypeRegistry, merge_config_job_types
from tako_vm.models import ExecutionRecord, sha256_content, sha256_json
from tako_vm.security import sanitize_error
from tako_vm.server.correlation import (
    CorrelationIdMiddleware,
    configure_logging_with_correlation,
    get_correlation_id,
)
from tako_vm.server.limits import ApiProtectionMiddleware
from tako_vm.server.queue import WorkerPool
from tako_vm.storage import ExecutionStorage

# Configure logging with correlation ID support
# Note: Log level will be reconfigured in lifespan() based on config
logging.basicConfig(level=logging.INFO)
configure_logging_with_correlation()
logger = logging.getLogger(__name__)

# Maximum wait timeout to prevent slowloris-style attacks (5 minutes)
MAX_WAIT_TIMEOUT = 300.0


def _configure_log_level(log_level: str) -> None:
    """Configure logging level from config."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)
    # Also set for uvicorn loggers
    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)


def compute_idempotency_fingerprint(
    code: str, input_data: dict, job_type: Optional[str], timeout: Optional[int]
) -> str:
    """
    Compute fingerprint of request parameters for idempotency validation.

    This fingerprint is used to detect when an idempotency key is reused
    with different parameters (which should return 409 Conflict).
    """
    return sha256_json(
        {
            "code_hash": sha256_content(code),
            "input_hash": sha256_json(input_data),
            "job_type": job_type or "default",
            "timeout": timeout,
        }
    )


# Keyed lock for idempotency (prevents race conditions under concurrent requests)
class IdempotencyLockManager:
    """
    Per-key lock manager for idempotent requests.

    Ensures that concurrent requests with the same idempotency key
    are serialized to prevent race conditions.
    """

    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, key: str):
        """Acquire lock for a specific idempotency key."""
        # Get or create lock for this key
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            lock = self._locks[key]

        # Acquire the per-key lock
        async with lock:
            yield

        # Cleanup unused locks (optional, prevents memory leak for many unique keys)
        async with self._global_lock:
            if key in self._locks and not self._locks[key].locked():
                # Only delete if no other request is waiting
                del self._locks[key]


# Application state (initialized in lifespan)
class AppState:
    config: TakoVMConfig
    registry: JobTypeRegistry
    executor: CodeExecutor
    storage: ExecutionStorage
    worker_pool: WorkerPool
    idempotency_locks: IdempotencyLockManager


state = AppState()
state.idempotency_locks = IdempotencyLockManager()


def _get_runtime_config() -> TakoVMConfig:
    """Get active config for middleware checks before/after lifespan startup."""
    if hasattr(state, "config"):
        return state.config
    return get_config()


async def _periodic_cleanup(storage: ExecutionStorage, record_ttl_days: int, dlq_ttl_days: int = 7):
    """Background task for periodic database cleanup."""
    cleanup_interval = 3600  # Run every hour
    while True:
        try:
            await asyncio.sleep(cleanup_interval)
            # Clean up old execution records
            records_deleted = await storage.cleanup_old_records(record_ttl_days)
            if records_deleted > 0:
                logger.info(f"Cleanup: deleted {records_deleted} old execution records")
            # Clean up old DLQ entries
            dlq_deleted = await storage.cleanup_old_dlq_entries(dlq_ttl_days)
            if dlq_deleted > 0:
                logger.info(f"Cleanup: deleted {dlq_deleted} old DLQ entries")
        except asyncio.CancelledError:
            logger.info("Periodic cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Periodic cleanup error: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    # Startup
    logger.info("Starting Tako VM API server...")

    # Run startup cleanup (orphaned containers, health check)
    cleanup_results = startup_cleanup()
    logger.info(f"Startup cleanup: {cleanup_results}")

    state.config = get_config()

    # Configure log level from config
    _configure_log_level(state.config.log_level)
    state.registry = JobTypeRegistry()
    merged_job_types = merge_config_job_types(state.registry, state.config.job_types)
    if merged_job_types:
        logger.info("Loaded %s job type(s) from config", merged_job_types)
    state.executor = CodeExecutor(registry=state.registry, config=state.config)
    state.storage = ExecutionStorage(state.config.database_url)
    await state.storage.init()
    state.worker_pool = WorkerPool(
        executor=state.executor,
        storage=state.storage,
        max_workers=state.config.max_workers,
        max_queue_size=state.config.max_queue_size,
        queue_wait_timeout=state.config.queue_wait_timeout,
    )
    await state.worker_pool.start()

    # Start periodic cleanup task
    cleanup_task = asyncio.create_task(
        _periodic_cleanup(state.storage, state.config.execution_record_ttl_days)
    )

    logger.info(f"Tako VM ready (production_mode={state.config.production_mode})")

    yield

    # Shutdown
    logger.info("Shutting down Tako VM API server...")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await state.worker_pool.stop()
    await state.storage.close()


app = FastAPI(
    title="Tako VM - Secure Code Executor API",
    description="Execute AI-generated Python code in isolated containers",
    version=__version__,
    lifespan=lifespan,
)

# Add API-layer protection middleware (rate limit + payload size)
app.add_middleware(ApiProtectionMiddleware, config_getter=_get_runtime_config)

# Add correlation ID middleware (must be added before other middleware)
app.add_middleware(CorrelationIdMiddleware)


# Request/Response Models
class ExecuteRequest(BaseModel):
    """Request model for code execution."""

    model_config = {"extra": "forbid"}

    code: str = Field(
        ..., min_length=1, max_length=100_000, description="Python code to execute (max 100KB)"
    )
    input_data: Dict[str, Any] = Field(
        default_factory=dict, description="Input data as JSON (max 1MB when serialized)"
    )
    timeout: Optional[int] = Field(
        default=None, ge=1, le=300, description="Timeout for code execution in seconds (1-300)"
    )
    startup_timeout: Optional[int] = Field(
        default=None,
        ge=10,
        le=600,
        description="Timeout for startup phase (container + deps) in seconds (10-600)",
    )
    job_type: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_-]+(@[a-zA-Z0-9_.@:-]+)?$",
        description="Job type name (e.g., 'data-processing' or 'data-processing@v1.0.0')",
    )
    idempotency_key: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=255,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Client-provided key for idempotent job submission (alphanumeric, 8-255 chars)",
    )
    requirements: Optional[List[str]] = Field(
        default=None,
        max_length=50,
        description="Python packages to install at runtime (e.g., ['pandas', 'numpy>=1.20'])",
    )

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Code cannot be empty")
        return v

    @field_validator("input_data")
    @classmethod
    def validate_input_data_size(cls, v):
        import json

        serialized = json.dumps(v)
        if len(serialized) > 1_048_576:  # 1MB limit
            raise ValueError("input_data exceeds 1MB limit when serialized")
        return v


class ExecuteResponse(BaseModel):
    """Response model for code execution (legacy)."""

    model_config = {"extra": "forbid"}

    success: bool
    output: Optional[Dict[str, Any]] = None
    execution_time: float
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None
    job_type: Optional[str] = None


# Status type aliases for API type safety
QueueStatus = Literal[
    "queued", "pending", "running", "succeeded", "failed", "timeout", "oom", "cancelled"
]
HealthStatus = Literal["healthy", "degraded"]
RelationshipType = Literal["rerun", "fork"]


class AsyncExecuteResponse(BaseModel):
    """Response model for async execution."""

    model_config = {"extra": "forbid"}

    job_id: str
    status: QueueStatus


class JobStatusResponse(BaseModel):
    """Response model for job status."""

    model_config = {"extra": "forbid"}

    job_id: str = Field(..., description="Unique job identifier")
    status: QueueStatus = Field(
        ...,
        description="Current status: pending, running, queued, succeeded, failed, timeout, oom, cancelled",
    )
    created_at: Optional[str] = Field(
        default=None, description="ISO 8601 timestamp when job was created"
    )
    duration_ms: Optional[int] = Field(
        default=None, description="Execution duration in milliseconds (if completed)"
    )
    queue_position: Optional[int] = Field(
        default=None, description="Position in queue (if pending)"
    )


class ExecutionTimingResponse(BaseModel):
    """Response model for execution timing breakdown."""

    model_config = {"extra": "forbid"}

    startup_ms: Optional[int] = None
    """Time spent in startup phase (container init + dep install)."""

    dep_install_ms: Optional[int] = None
    """Time spent installing dependencies (subset of startup)."""

    execution_ms: Optional[int] = None
    """Time spent executing user code."""

    total_ms: Optional[int] = None
    """Total container runtime."""

    phase_at_exit: Optional[str] = None
    """Which phase the container was in when it exited."""


class ExecutionRecordResponse(BaseModel):
    """Response model for execution record."""

    model_config = {"extra": "forbid"}

    execution_id: str
    status: QueueStatus
    job_type: str
    job_version: str
    created_at: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    output: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    timing: Optional[ExecutionTimingResponse] = None
    """Detailed timing breakdown by execution phase."""

    @classmethod
    def from_record(cls, record: ExecutionRecord) -> "ExecutionRecordResponse":
        timing_response = None
        if record.timing:
            timing_response = ExecutionTimingResponse(
                startup_ms=record.timing.startup_ms,
                dep_install_ms=record.timing.dep_install_ms,
                execution_ms=record.timing.execution_ms,
                total_ms=record.timing.total_ms,
                phase_at_exit=record.timing.phase_at_exit,
            )

        return cls(
            execution_id=record.execution_id,
            status=record.status,
            job_type=record.job_type,
            job_version=record.job_ref,
            created_at=record.created_at.isoformat(),
            started_at=record.started_at.isoformat() if record.started_at else None,
            ended_at=record.ended_at.isoformat() if record.ended_at else None,
            duration_ms=record.duration_ms,
            exit_code=record.exit_code,
            stdout=record.stdout,
            stderr=record.stderr,
            output=record.result_json,
            error=record.error.model_dump() if record.error else None,
            timing=timing_response,
        )


class JobTypeResponse(BaseModel):
    """Response model for job type info."""

    model_config = {"extra": "forbid"}

    name: str
    requirements: List[str]
    python_version: str
    memory_limit: str
    cpu_limit: float
    timeout: int
    session_enabled: bool
    gpu_enabled: bool
    gpu_vendor: Optional[str] = None
    image_exists: bool


class CircuitBreakerStatus(BaseModel):
    """Circuit breaker status for health monitoring."""

    model_config = {"extra": "forbid"}

    state: Literal["closed", "open", "half_open"] = Field(..., description="Circuit breaker state")
    failure_count: int = Field(..., description="Number of consecutive failures")
    success_count: int = Field(..., description="Number of consecutive successes")
    last_failure: Optional[float] = Field(default=None, description="Timestamp of last failure")


class QueueStatsResponse(BaseModel):
    """Worker pool queue statistics."""

    model_config = {"extra": "forbid"}

    pending: int = Field(..., description="Number of jobs waiting in queue")
    running: int = Field(..., description="Number of jobs currently executing")
    max_workers: int = Field(..., description="Maximum concurrent workers")
    max_queue_size: int = Field(..., description="Maximum queue capacity")


class HealthResponse(BaseModel):
    """Response model for health check."""

    model_config = {"extra": "forbid"}

    status: HealthStatus
    docker_available: bool
    gvisor_available: bool
    circuit_breaker: CircuitBreakerStatus
    version: str
    production_mode: bool
    queue_stats: QueueStatsResponse


class PoolStatsResponse(BaseModel):
    """Response model for worker pool stats (deprecated, use QueueStatsResponse)."""

    model_config = {"extra": "forbid"}

    pending: int = Field(..., description="Number of jobs waiting in queue")
    running: int = Field(..., description="Number of jobs currently executing")
    max_workers: int = Field(..., description="Maximum concurrent workers")
    max_queue_size: int = Field(..., description="Maximum queue capacity")


class PaginatedResponse(BaseModel):
    """Paginated list response with metadata."""

    model_config = {"extra": "forbid"}

    items: List[Any] = Field(..., description="List of items")
    limit: int = Field(..., description="Maximum items returned")
    offset: int = Field(..., description="Number of items skipped")
    has_more: bool = Field(..., description="Whether more items exist beyond this page")
    count: int = Field(..., description="Number of items in this response")


class CancelResponse(BaseModel):
    """Response model for job cancellation."""

    model_config = {"extra": "forbid"}

    status: Literal["cancelled"] = Field(..., description="Cancellation status")
    job_id: str = Field(..., description="ID of the cancelled job")


class DLQStatsResponse(BaseModel):
    """Response model for DLQ statistics."""

    model_config = {"extra": "forbid"}

    total: int = Field(..., description="Total number of entries in DLQ")
    by_error_type: Dict[str, int] = Field(..., description="Breakdown by error type")


class BuildResponse(BaseModel):
    """Response model for job type build."""

    model_config = {"extra": "forbid"}

    status: Literal["built"] = Field(..., description="Build status")
    job_type: str = Field(..., description="Name of the job type")
    version: str = Field(..., description="Full version reference")
    digest: str = Field(..., description="Short digest of the build")
    image_ref: str = Field(..., description="Docker image reference")


class DLQRemoveResponse(BaseModel):
    """Response model for DLQ entry removal."""

    model_config = {"extra": "forbid"}

    status: Literal["removed"] = Field(..., description="Removal status")
    entry_id: int = Field(..., description="ID of the removed entry")


class RerunRequest(BaseModel):
    """Request model for rerunning a previous execution."""

    model_config = {"extra": "forbid"}

    job_type: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_-]+(@[a-zA-Z0-9_.@:-]+)?$",
        description="Optional job type override",
    )
    timeout: Optional[int] = Field(
        default=None, ge=1, le=300, description="Optional timeout override (1-300s)"
    )


class ForkRequest(BaseModel):
    """Request model for forking a previous execution with modified code."""

    model_config = {"extra": "forbid"}

    code: str = Field(
        ..., min_length=1, max_length=100_000, description="New Python code to execute"
    )
    job_type: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_-]+(@[a-zA-Z0-9_.@:-]+)?$",
        description="Optional job type override",
    )
    timeout: Optional[int] = Field(
        default=None, ge=1, le=300, description="Optional timeout override (1-300s)"
    )

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Code cannot be empty")
        return v


# Full response models for ?view=full support
class ArtifactResponse(BaseModel):
    """Artifact metadata for API responses."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., description="Artifact filename")
    size_bytes: int = Field(..., description="File size in bytes")
    sha256: str = Field(..., description="SHA256 hash of file contents")
    content_type: Optional[str] = Field(default=None, description="MIME type")


class ResourceUsageResponse(BaseModel):
    """Resource consumption metrics."""

    model_config = {"extra": "forbid"}

    max_rss_mb: Optional[float] = Field(default=None, description="Peak RSS in MB")
    cpu_time_ms: Optional[int] = Field(default=None, description="CPU time in ms")
    wall_time_ms: Optional[int] = Field(default=None, description="Wall clock time in ms")


class ExecutionRecordFullResponse(ExecutionRecordResponse):
    """
    Extended execution record response with full audit trail.

    Use ?view=full query parameter to request this extended response.
    Includes artifacts, resource usage, content hashes, and lineage info.
    """

    # Pinned environment (key differentiator for reproducibility)
    job_ref: str = Field(
        ..., description="Full job reference with digest (e.g., 'data-processing@sha256:...')"
    )

    # Artifact metadata
    artifacts: List[ArtifactResponse] = Field(default_factory=list, description="Output artifacts")
    input_artifacts: List[ArtifactResponse] = Field(
        default_factory=list,
        description="Input artifacts (includes internal _code.py, _input.json)",
    )

    # Resource tracking
    resource_usage: Optional[ResourceUsageResponse] = Field(
        default=None, description="Resource consumption metrics"
    )

    # Content hashes (for replay verification)
    code_hash: Optional[str] = Field(default=None, description="SHA256 hash of submitted code")
    input_hash: Optional[str] = Field(default=None, description="SHA256 hash of input_data")

    # Lineage
    parent_execution_id: Optional[str] = Field(
        default=None, description="Parent execution ID (for rerun/fork)"
    )
    relationship: Optional[RelationshipType] = Field(
        default=None, description="Relationship to parent: 'rerun' or 'fork'"
    )

    # Truncation flags
    stdout_truncated: bool = Field(default=False, description="Whether stdout was truncated")
    stderr_truncated: bool = Field(default=False, description="Whether stderr was truncated")

    # Idempotency
    idempotency_key: Optional[str] = Field(
        default=None, description="Client-provided idempotency key"
    )

    @classmethod
    def from_record(cls, record: ExecutionRecord) -> "ExecutionRecordFullResponse":
        """Create full response from ExecutionRecord."""
        return cls(
            # Base fields
            execution_id=record.execution_id,
            status=record.status,
            job_type=record.job_type,
            job_version=record.job_ref,
            created_at=record.created_at.isoformat(),
            started_at=record.started_at.isoformat() if record.started_at else None,
            ended_at=record.ended_at.isoformat() if record.ended_at else None,
            duration_ms=record.duration_ms,
            exit_code=record.exit_code,
            stdout=record.stdout,
            stderr=record.stderr,
            output=record.result_json,
            error=record.error.model_dump() if record.error else None,
            # Extended fields
            job_ref=record.job_ref,
            artifacts=[
                ArtifactResponse(
                    name=a.name,
                    size_bytes=a.size_bytes,
                    sha256=a.sha256,
                    content_type=a.content_type,
                )
                for a in record.artifacts
            ],
            input_artifacts=[
                ArtifactResponse(
                    name=a.name,
                    size_bytes=a.size_bytes,
                    sha256=a.sha256,
                    content_type=a.content_type,
                )
                for a in record.input_artifacts
            ],
            resource_usage=ResourceUsageResponse(
                max_rss_mb=record.resource_usage.max_rss_mb,
                cpu_time_ms=record.resource_usage.cpu_time_ms,
                wall_time_ms=record.resource_usage.wall_time_ms,
            )
            if record.resource_usage
            else None,
            code_hash=record.code_hash or None,
            input_hash=record.input_hash or None,
            parent_execution_id=record.parent_execution_id,
            relationship=record.relationship,
            stdout_truncated=record.stdout_truncated,
            stderr_truncated=record.stderr_truncated,
            idempotency_key=record.idempotency_key,
        )


# Middleware for security headers
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


# Routes
@app.get("/", include_in_schema=False)
async def root():
    """Redirect to API documentation."""
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint.

    Returns:
        Health status including Docker availability, circuit breaker, and queue stats
    """
    circuit_breaker = get_circuit_breaker()
    docker_available = circuit_breaker.check_docker_health()
    gvisor_available = check_gvisor_available()
    cb_status = circuit_breaker.get_status()

    # Determine overall status
    if not docker_available:
        status = "degraded"
    elif cb_status["state"] == "open":
        status = "degraded"
    else:
        status = "healthy"

    queue_stats = await state.worker_pool.get_stats()
    return HealthResponse(
        status=status,
        docker_available=docker_available,
        gvisor_available=gvisor_available,
        circuit_breaker=CircuitBreakerStatus(**cb_status),
        version=__version__,
        production_mode=state.config.production_mode,
        queue_stats=QueueStatsResponse(**queue_stats),
    )


@app.post("/execute", response_model=ExecuteResponse)
async def execute_code(request: ExecuteRequest):
    """
    Execute Python code synchronously (legacy endpoint).

    The code runs in an isolated container with:
    - No network access by default (configurable via job type)
    - Read-only filesystem (except /output and /tmp)
    - Configurable resource limits
    - Configurable timeout

    Args:
        request: Execution request with code, input_data, and timeout

    Returns:
        Execution results including output, stdout, stderr
    """
    job_id = f"api-{int(time.time() * 1000)}"

    logger.info(f"Executing job {job_id}")
    start_time = time.time()

    try:
        job = {
            "id": job_id,
            "code": request.code,
            "input_data": request.input_data,
            "job_type": request.job_type,
        }

        if request.timeout is not None:
            job["timeout"] = request.timeout

        if request.requirements is not None:
            job["requirements"] = request.requirements

        result = state.executor.execute_job(job)
        execution_time = time.time() - start_time

        logger.info(f"Job {job_id} completed in {execution_time:.2f}s")

        return ExecuteResponse(
            success=result["success"],
            output=result.get("output"),
            execution_time=execution_time,
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            exit_code=result.get("exit_code"),
            error=result.get("error"),
            job_type=result.get("job_type"),
        )

    except Exception as e:
        logger.error("Job %s error: %s", job_id, str(e), exc_info=True)
        # Sanitize error message to avoid leaking sensitive info
        safe_message = sanitize_error(str(e))
        raise HTTPException(status_code=500, detail=f"Execution error: {safe_message}") from e


@app.post("/execute/async", response_model=AsyncExecuteResponse)
async def execute_code_async(request: ExecuteRequest, http_request: Request):
    """
    Submit code for async execution.

    Returns immediately with a job_id that can be used to poll for results.
    Supports idempotency via idempotency_key parameter.

    Args:
        request: Execution request with code, input_data, timeout, and optional idempotency_key

    Returns:
        Job ID and queued status
    """
    try:
        # For idempotent requests, use keyed lock to prevent race conditions
        if request.idempotency_key:
            async with state.idempotency_locks.acquire(request.idempotency_key):
                return await _submit_async_job(request, http_request)
        else:
            return await _submit_async_job(request, http_request)

    except HTTPException:
        raise
    except RuntimeError as e:
        # Queue full should return 503 Service Unavailable
        logger.warning("Async submit rejected: %s", str(e))
        safe_message = sanitize_error(str(e))
        raise HTTPException(status_code=503, detail=safe_message) from e
    except Exception as e:
        logger.error("Async submit error: %s", str(e), exc_info=True)
        safe_message = sanitize_error(str(e))
        raise HTTPException(status_code=500, detail=f"Submit error: {safe_message}") from e


async def _submit_async_job(request: ExecuteRequest, http_request: Request) -> AsyncExecuteResponse:
    """
    Internal helper for async job submission.

    Separated to allow idempotency locking around the entire check+submit flow.
    """
    # Check idempotency key BEFORE queueing
    if request.idempotency_key:
        existing = await state.storage.get_by_idempotency_key(request.idempotency_key)
        if existing:
            # Verify fingerprint matches (detect key reuse with different payload)
            expected_fingerprint = compute_idempotency_fingerprint(
                request.code, request.input_data, request.job_type, request.timeout
            )

            if (
                existing.idempotency_fingerprint
                and expected_fingerprint != existing.idempotency_fingerprint
            ):
                raise HTTPException(
                    status_code=409, detail="Idempotency key reused with different payload"
                )

            # Return existing job
            logger.info(f"Idempotency hit: returning existing job {existing.execution_id}")
            return AsyncExecuteResponse(job_id=existing.execution_id, status=existing.status)

    # Include correlation ID in job data for tracing
    correlation_id = get_correlation_id()

    # Compute fingerprint at submission time for storage
    idempotency_fingerprint = None
    if request.idempotency_key:
        idempotency_fingerprint = compute_idempotency_fingerprint(
            request.code, request.input_data, request.job_type, request.timeout
        )

    job_data = {
        "code": request.code,
        "input_data": request.input_data,
        "job_type": request.job_type,
        "idempotency_key": request.idempotency_key,
        "idempotency_fingerprint": idempotency_fingerprint,
        "correlation_id": correlation_id,
    }

    if request.timeout is not None:
        job_data["timeout"] = request.timeout

    if request.startup_timeout is not None:
        job_data["startup_timeout"] = request.startup_timeout

    if request.requirements is not None:
        job_data["requirements"] = request.requirements

    try:
        job_id = await state.worker_pool.submit(
            job_data=job_data, client_ip=http_request.client.host if http_request.client else None
        )
    except IntegrityError as exc:
        if request.idempotency_key:
            existing = await state.storage.get_by_idempotency_key(request.idempotency_key)
            if existing:
                if (
                    existing.idempotency_fingerprint
                    and idempotency_fingerprint
                    and existing.idempotency_fingerprint != idempotency_fingerprint
                ):
                    raise HTTPException(
                        status_code=409, detail="Idempotency key reused with different payload"
                    ) from exc
                return AsyncExecuteResponse(job_id=existing.execution_id, status=existing.status)
        raise

    logger.info(f"Job {job_id} queued (correlation_id={correlation_id})")

    return AsyncExecuteResponse(job_id=job_id, status="queued")


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """
    Get status of an async job.

    Args:
        job_id: Job ID returned from /execute/async

    Returns:
        Job status and metadata
    """
    status = await state.worker_pool.get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(**status)


@app.get("/jobs/{job_id}/result")
async def get_job_result(
    job_id: str,
    wait: bool = False,
    timeout: float = Query(
        default=30.0, ge=1.0, le=MAX_WAIT_TIMEOUT, description="Max wait time in seconds (1-300)"
    ),
    view: Optional[str] = Query(
        default=None, description="Use 'full' for extended response with artifacts, hashes, lineage"
    ),
):
    """
    Get result of an async job.

    Args:
        job_id: Job ID
        wait: If true, wait for job completion
        timeout: Max time to wait if wait=true
        view: Use 'full' for extended response with artifacts, resource usage, hashes, and lineage

    Returns:
        Execution record (slim by default, full with ?view=full)
    """
    try:
        if wait:
            record = await state.worker_pool.wait_for_result(job_id, timeout=timeout)
        else:
            record = await state.storage.get_record(job_id)
            if not record:
                # Check if still in queue
                status = await state.worker_pool.get_job_status(job_id)
                if status and status.get("status") in ("pending", "running"):
                    # Return 200 with job status instead of 202
                    return JSONResponse(
                        status_code=200,
                        content={
                            "job_id": job_id,
                            "status": status.get("status"),
                            "message": "Job still in progress",
                            "queue_position": status.get("queue_position"),
                        },
                    )
                raise HTTPException(status_code=404, detail="Job not found")

        if view == "full":
            return ExecutionRecordFullResponse.from_record(record)
        return ExecutionRecordResponse.from_record(record)

    except KeyError as e:
        raise HTTPException(status_code=404, detail="Job not found") from e
    except TimeoutError as e:
        raise HTTPException(status_code=408, detail="Timeout waiting for job") from e


@app.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
async def cancel_job(job_id: str):
    """
    Cancel a queued or running job.

    Args:
        job_id: Job ID to cancel

    Returns:
        Cancellation status
    """
    success = await state.worker_pool.cancel(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found or already completed")

    return CancelResponse(status="cancelled", job_id=job_id)


def _get_replay_data(record: ExecutionRecord) -> tuple:
    """
    Retrieve original code, input_data, and input_artifacts from stored artifacts.

    Args:
        record: ExecutionRecord to extract replay data from

    Returns:
        Tuple of (code, input_data, user_input_artifacts)

    Raises:
        HTTPException: If replay artifacts are not available
    """
    import json

    data_dir = state.config.data_dir

    # Find _code.py in input_artifacts
    code_artifact = next((a for a in record.input_artifacts if a.name == "_code.py"), None)
    if not code_artifact:
        raise HTTPException(status_code=400, detail="Original code not available for replay")

    code_path = data_dir / code_artifact.storage_key
    if not code_path.exists():
        raise HTTPException(status_code=400, detail="Original code file not found")
    code = code_path.read_text(encoding="utf-8")

    # Find _input.json in input_artifacts
    input_artifact = next((a for a in record.input_artifacts if a.name == "_input.json"), None)
    if not input_artifact:
        raise HTTPException(status_code=400, detail="Original input not available for replay")

    input_path = data_dir / input_artifact.storage_key
    if not input_path.exists():
        raise HTTPException(status_code=400, detail="Original input file not found")
    input_data = json.loads(input_path.read_text(encoding="utf-8"))

    # Get non-internal input artifacts (user-provided files, exclude _* internal artifacts)
    user_artifacts = [a.model_dump() for a in record.input_artifacts if not a.name.startswith("_")]

    return code, input_data, user_artifacts


@app.post("/jobs/{job_id}/rerun", response_model=AsyncExecuteResponse)
async def rerun_job(job_id: str, request: RerunRequest, http_request: Request):
    """
    Rerun a previous execution with the same code and inputs.

    Creates a new job with the same code and input_data as the original,
    optionally with different job_type or timeout settings. The new job
    will have a parent_execution_id pointing to the original and
    relationship='rerun'.

    Args:
        job_id: ID of the job to rerun
        request: Optional overrides for job_type and timeout

    Returns:
        New job ID and status
    """
    parent = await state.storage.get_record(job_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Job not found")

    # Can only rerun completed jobs
    if parent.status not in ("succeeded", "failed", "timeout", "oom", "cancelled"):
        raise HTTPException(status_code=400, detail="Cannot rerun job that hasn't completed")

    try:
        code, input_data, input_artifacts = _get_replay_data(parent)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get replay data for {job_id}: {e}")
        raise HTTPException(status_code=400, detail="Failed to retrieve replay data") from e

    # Build job data with parent linkage
    job_data = {
        "code": code,
        "input_data": input_data,
        "input_artifacts": input_artifacts,
        "job_type": request.job_type or parent.job_type,
        "parent_execution_id": parent.execution_id,
        "relationship": "rerun",
        "correlation_id": get_correlation_id(),
    }

    if request.timeout:
        job_data["timeout"] = request.timeout

    new_job_id = await state.worker_pool.submit(
        job_data=job_data, client_ip=http_request.client.host if http_request.client else None
    )

    logger.info(f"Job {new_job_id} created as rerun of {job_id}")

    return AsyncExecuteResponse(job_id=new_job_id, status="queued")


@app.post("/jobs/{job_id}/fork", response_model=AsyncExecuteResponse)
async def fork_job(job_id: str, request: ForkRequest, http_request: Request):
    """
    Fork a previous execution with new code but same inputs.

    Creates a new job with new code but the same input_data as the original.
    This is useful for iterating on code while keeping the same test inputs.
    The new job will have a parent_execution_id pointing to the original
    and relationship='fork'.

    Args:
        job_id: ID of the job to fork
        request: New code and optional overrides

    Returns:
        New job ID and status
    """
    parent = await state.storage.get_record(job_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        _, input_data, input_artifacts = _get_replay_data(parent)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get replay data for {job_id}: {e}")
        raise HTTPException(status_code=400, detail="Failed to retrieve replay data") from e

    # Build job data with new code and parent linkage
    job_data = {
        "code": request.code,
        "input_data": input_data,
        "input_artifacts": input_artifacts,
        "job_type": request.job_type or parent.job_type,
        "parent_execution_id": parent.execution_id,
        "relationship": "fork",
        "correlation_id": get_correlation_id(),
    }

    if request.timeout:
        job_data["timeout"] = request.timeout

    new_job_id = await state.worker_pool.submit(
        job_data=job_data, client_ip=http_request.client.host if http_request.client else None
    )

    logger.info(f"Job {new_job_id} created as fork of {job_id}")

    return AsyncExecuteResponse(job_id=new_job_id, status="queued")


@app.get("/jobs/{job_id}/artifacts/{artifact_name}")
async def download_artifact(job_id: str, artifact_name: str):
    """
    Download an artifact from a completed job.

    Args:
        job_id: Job ID
        artifact_name: Name of the artifact to download

    Returns:
        Artifact file with appropriate Content-Type and ETag header
    """
    record = await state.storage.get_record(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    # Find artifact by name (must exist in manifest)
    artifact = next((a for a in record.artifacts if a.name == artifact_name), None)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # PATH TRAVERSAL PREVENTION: resolve and verify path stays under data_dir
    # Using is_relative_to() for robust path validation (handles symlinks properly)
    data_dir = state.config.data_dir.resolve()
    artifact_path = (data_dir / artifact.storage_key).resolve()

    if not artifact_path.is_relative_to(data_dir):
        raise HTTPException(status_code=400, detail="Invalid artifact path")

    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file not found")

    return FileResponse(
        path=artifact_path,
        media_type=artifact.content_type or "application/octet-stream",
        filename=artifact_name,
        headers={"ETag": f'"{artifact.sha256}"'},
    )


@app.head("/jobs/{job_id}/artifacts/{artifact_name}")
async def artifact_metadata(job_id: str, artifact_name: str):
    """
    Get artifact metadata without downloading content.

    Args:
        job_id: Job ID
        artifact_name: Name of the artifact

    Returns:
        Response with Content-Length, Content-Type, and ETag headers
    """
    record = await state.storage.get_record(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    artifact = next((a for a in record.artifacts if a.name == artifact_name), None)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    return Response(
        content=b"",
        headers={
            "Content-Length": str(artifact.size_bytes),
            "Content-Type": artifact.content_type or "application/octet-stream",
            "ETag": f'"{artifact.sha256}"',
        },
    )


@app.get("/executions", response_model=PaginatedResponse)
async def list_executions(
    status: Optional[str] = Query(
        default=None, description="Filter by status (e.g., 'succeeded', 'failed')"
    ),
    job_type: Optional[str] = Query(default=None, description="Filter by job type name"),
    limit: int = Query(
        default=100, ge=1, le=1000, description="Maximum records to return (1-1000)"
    ),
    offset: int = Query(default=0, ge=0, description="Number of records to skip for pagination"),
    view: Optional[str] = Query(
        default=None, description="Use 'full' for extended response with artifacts, hashes, lineage"
    ),
):
    """
    List execution records with pagination.

    Returns paginated list of execution records with metadata for efficient client-side pagination.
    Use ?view=full to include artifacts, resource usage, content hashes, and lineage info.
    """
    actual_limit = min(limit, 1000)
    records = await state.storage.list_records(
        status=status,
        job_type=job_type,
        limit=actual_limit + 1,  # Fetch one extra to check has_more
        offset=offset,
    )

    has_more = len(records) > actual_limit
    if has_more:
        records = records[:actual_limit]

    if view == "full":
        items = [ExecutionRecordFullResponse.from_record(r) for r in records]
    else:
        items = [ExecutionRecordResponse.from_record(r) for r in records]

    return PaginatedResponse(
        items=items,
        limit=actual_limit,
        offset=offset,
        has_more=has_more,
        count=len(records),
    )


@app.get("/executions/{execution_id}")
async def get_execution(
    execution_id: str,
    view: Optional[str] = Query(
        default=None, description="Use 'full' for extended response with artifacts, hashes, lineage"
    ),
):
    """
    Get a specific execution record.

    Args:
        execution_id: Execution ID
        view: Use 'full' for extended response with artifacts, resource usage, hashes, and lineage

    Returns:
        Execution record (slim by default, full with ?view=full)
    """
    record = await state.storage.get_record(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Execution not found")

    if view == "full":
        return ExecutionRecordFullResponse.from_record(record)
    return ExecutionRecordResponse.from_record(record)


# Job Type Management Endpoints
@app.get("/job-types", response_model=List[JobTypeResponse])
async def list_job_types():
    """
    List all registered job types.

    Returns:
        List of job type configurations with image availability status
    """
    from tako_vm.execution.builder import ContainerBuilder

    builder = ContainerBuilder()

    result = []
    for jt in state.registry.list():
        result.append(
            JobTypeResponse(
                name=jt.name,
                requirements=jt.requirements,
                python_version=jt.python_version,
                memory_limit=jt.memory_limit,
                cpu_limit=jt.cpu_limit,
                timeout=jt.timeout,
                session_enabled=jt.session_enabled,
                gpu_enabled=jt.gpu_enabled,
                gpu_vendor=jt.gpu_vendor,
                image_exists=builder.image_exists(jt),
            )
        )
    return result


@app.get("/job-types/{name}", response_model=JobTypeResponse)
async def get_job_type(name: str):
    """
    Get a specific job type by name.

    Args:
        name: Job type name

    Returns:
        Job type configuration
    """
    from tako_vm.execution.builder import ContainerBuilder

    jt = state.registry.get(name)
    if not jt:
        raise HTTPException(status_code=404, detail=f"Job type '{name}' not found")

    builder = ContainerBuilder()
    return JobTypeResponse(
        name=jt.name,
        requirements=jt.requirements,
        python_version=jt.python_version,
        memory_limit=jt.memory_limit,
        cpu_limit=jt.cpu_limit,
        timeout=jt.timeout,
        session_enabled=jt.session_enabled,
        gpu_enabled=jt.gpu_enabled,
        gpu_vendor=jt.gpu_vendor,
        image_exists=builder.image_exists(jt),
    )


@app.post("/job-types/{name}/build", response_model=BuildResponse)
async def build_job_type(name: str, version_tag: Optional[str] = None):
    """
    Explicitly build a job type container image.

    Args:
        name: Job type name
        version_tag: Optional semantic version tag

    Returns:
        Build status and version info
    """
    from tako_vm.execution.builder import ContainerBuilder
    from tako_vm.version import VersionManager

    jt = state.registry.get(name)
    if not jt:
        raise HTTPException(status_code=404, detail=f"Job type '{name}' not found")

    try:
        builder = ContainerBuilder()
        builder.build(jt, quiet=False)

        # Register version
        version_manager = VersionManager(state.storage)
        version = await version_manager.register_version(
            job_type=jt, image_ref=jt.image_name, version_tag=version_tag, built_by="manual"
        )

        return BuildResponse(
            status="built",
            job_type=name,
            version=version.full_ref,
            digest=version.short_digest,
            image_ref=version.image_ref,
        )

    except Exception as e:
        logger.error("Build failed for %s: %s", name, e)
        raise HTTPException(
            status_code=500, detail=f"Build failed: {sanitize_error(str(e))}"
        ) from e


@app.get("/pool/stats", response_model=PoolStatsResponse)
async def get_pool_stats():
    """Get worker pool statistics."""
    stats = await state.worker_pool.get_stats()
    return PoolStatsResponse(**stats)


# Dead Letter Queue Endpoints
@app.get("/dlq/stats", response_model=DLQStatsResponse)
async def get_dlq_stats():
    """
    Get dead letter queue statistics.

    Returns:
        DLQ stats including total count and breakdown by error type
    """
    stats = await state.storage.get_dlq_stats()
    return DLQStatsResponse(**stats)


@app.get("/dlq", response_model=PaginatedResponse)
async def list_dlq_entries(
    error_type: Optional[str] = Query(
        default=None, description="Filter by error type (e.g., 'internal_error')"
    ),
    limit: int = Query(
        default=100, ge=1, le=1000, description="Maximum entries to return (1-1000)"
    ),
    offset: int = Query(default=0, ge=0, description="Number of entries to skip for pagination"),
):
    """
    List dead letter queue entries with pagination.

    Returns paginated list of failed jobs for debugging and reprocessing.
    """
    actual_limit = min(limit, 1000)
    entries = await state.storage.list_dlq_entries(
        error_type=error_type,
        limit=actual_limit + 1,  # Fetch one extra to check has_more
        offset=offset,
    )

    has_more = len(entries) > actual_limit
    if has_more:
        entries = entries[:actual_limit]

    return PaginatedResponse(
        items=[entry.model_dump() for entry in entries],
        limit=actual_limit,
        offset=offset,
        has_more=has_more,
        count=len(entries),
    )


@app.delete("/dlq/{entry_id}", response_model=DLQRemoveResponse)
async def remove_dlq_entry(entry_id: int):
    """
    Remove an entry from the dead letter queue.

    Args:
        entry_id: DLQ entry ID

    Returns:
        Removal status
    """
    if await state.storage.remove_from_dlq(entry_id):
        return DLQRemoveResponse(status="removed", entry_id=entry_id)
    raise HTTPException(status_code=404, detail="DLQ entry not found")


# Error handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle uncaught exceptions with correlation ID for tracing."""
    correlation_id = get_correlation_id()
    # Sanitize error for logging (full details only in debug logs)
    safe_error = sanitize_error(str(exc))
    logger.error(
        "Unhandled exception (correlation_id=%s): %s", correlation_id, safe_error, exc_info=True
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id} if correlation_id else {},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions with sanitized error messages."""
    correlation_id = get_correlation_id()
    # Sanitize detail if it contains a string
    detail = exc.detail
    if isinstance(detail, str):
        detail = sanitize_error(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": detail,
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id} if correlation_id else {},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
