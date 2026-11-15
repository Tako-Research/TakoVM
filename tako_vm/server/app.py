import warnings

try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from psycopg import IntegrityError
from pydantic import BaseModel, Field, field_validator

from tako_vm import __version__
from tako_vm.config import TakoVMConfig, get_config
from tako_vm.execution.health import DockerCleanup, get_circuit_breaker, startup_cleanup
from tako_vm.execution.worker import (
    CodeExecutor,
    check_gvisor_available,
    prune_old_run_dirs,
    prune_stale_workspaces,
)
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

logging.basicConfig(level=logging.INFO)
configure_logging_with_correlation()
logger = logging.getLogger(__name__)

MAX_WAIT_TIMEOUT = 300.0

MAX_EXECUTION_TIMEOUT = 86_400


def _configure_log_level(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)
    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)


def compute_idempotency_fingerprint(
    code: str,
    input_data: dict,
    job_type: Optional[str],
    timeout: Optional[int],
    startup_timeout: Optional[int],
    requirements: Optional[List[str]],
) -> str:
    return sha256_json(
        {
            "code_hash": sha256_content(code),
            "input_hash": sha256_json(input_data),
            "job_type": job_type or "default",
            "timeout": timeout,
            "startup_timeout": startup_timeout,
            "requirements": requirements or [],
        }
    )


def _generate_sync_job_id() -> str:
    return f"api-{uuid.uuid4()}"


def _resolve_under_data_dir(data_dir: Path, storage_key: str) -> Path:
    candidate = (data_dir / storage_key).resolve()
    if not candidate.is_relative_to(data_dir):
        raise HTTPException(status_code=400, detail="Invalid artifact path")
    return candidate


class IdempotencyLockManager:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._waiters: Dict[str, int] = {}
        self._global_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, key: str):
        async with self._global_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._waiters[key] = self._waiters.get(key, 0) + 1

        try:
            async with lock:
                yield
        finally:
            async with self._global_lock:
                remaining = self._waiters.get(key, 0) - 1
                if remaining <= 0:
                    self._waiters.pop(key, None)
                    self._locks.pop(key, None)
                else:
                    self._waiters[key] = remaining


class AppState:
    config: TakoVMConfig
    registry: JobTypeRegistry
    executor: CodeExecutor
    storage: ExecutionStorage
    worker_pool: WorkerPool
    idempotency_locks: IdempotencyLockManager


state = AppState()
state.idempotency_locks = IdempotencyLockManager()


def _validate_execution_timeout(
    timeout: Optional[int], job_type_name: Optional[str] = None
) -> None:
    effective_timeout = timeout
    if effective_timeout is None:
        if job_type_name is None:
            effective_timeout = state.config.default_timeout
        else:
            name = job_type_name.split("@", 1)[0]
            job_type = state.registry.get(name)
            if job_type is not None:
                effective_timeout = job_type.timeout

    if effective_timeout is not None and effective_timeout > state.config.max_timeout:
        timeout_label = "timeout" if timeout is not None else "effective timeout"
        raise HTTPException(
            status_code=422,
            detail=(
                f"{timeout_label} must be less than or equal to the configured max_timeout "
                f"({state.config.max_timeout} seconds)"
            ),
        )


def _get_runtime_config() -> TakoVMConfig:
    if hasattr(state, "config"):
        return state.config
    return get_config()


async def _periodic_cleanup(
    storage: ExecutionStorage,
    record_ttl_days: int,
    data_dir: Path,
    workspace_max_age_seconds: int,
    dlq_ttl_days: int,
):
    cleanup_interval = 3600
    while True:
        try:
            await asyncio.sleep(cleanup_interval)
            records_deleted = await storage.cleanup_old_records(record_ttl_days)
            if records_deleted > 0:
                logger.info(f"Cleanup: deleted {records_deleted} old execution records")
            dlq_deleted = await storage.cleanup_old_dlq_entries(dlq_ttl_days)
            if dlq_deleted > 0:
                logger.info(f"Cleanup: deleted {dlq_deleted} old DLQ entries")
            stale_ws = prune_stale_workspaces(workspace_max_age_seconds)
            if stale_ws > 0:
                logger.info(f"Cleanup: pruned {stale_ws} stale workspace dir(s)")
            old_runs = prune_old_run_dirs(data_dir, record_ttl_days)
            if old_runs > 0:
                logger.info(f"Cleanup: pruned {old_runs} expired run artifact dir(s)")
            orphaned = DockerCleanup.cleanup_orphaned_containers(workspace_max_age_seconds)
            if orphaned > 0:
                logger.info(f"Cleanup: reaped {orphaned} orphaned container(s)")
        except asyncio.CancelledError:
            logger.info("Periodic cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Periodic cleanup error: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Tako VM API server...")

    cleanup_results = startup_cleanup()
    logger.info(f"Startup cleanup: {cleanup_results}")

    state.config = get_config()

    _configure_log_level(state.config.log_level)
    state.registry = JobTypeRegistry()
    merged_job_types = merge_config_job_types(state.registry, state.config.job_types)
    if merged_job_types:
        logger.info("Loaded %s job type(s) from config", merged_job_types)
    state.executor = CodeExecutor(registry=state.registry, config=state.config)
    state.storage = ExecutionStorage(state.config.database_url)
    await state.storage.init()

    stale_records = await state.storage.reconcile_stale_records()
    if stale_records > 0:
        logger.warning(f"Startup: marked {stale_records} interrupted execution record(s) as failed")

    state.worker_pool = WorkerPool(
        executor=state.executor,
        storage=state.storage,
        max_workers=state.config.max_workers,
        max_queue_size=state.config.max_queue_size,
        queue_wait_timeout=state.config.queue_wait_timeout,
    )
    await state.worker_pool.start()

    workspace_max_age = max(3600, state.config.max_startup_timeout + state.config.max_timeout + 300)
    stranded = prune_stale_workspaces(workspace_max_age)
    if stranded > 0:
        logger.info(f"Startup: pruned {stranded} stale workspace dir(s)")

    cleanup_task = asyncio.create_task(
        _periodic_cleanup(
            state.storage,
            state.config.execution_record_ttl_days,
            state.config.data_dir,
            workspace_max_age,
            state.config.dlq_ttl_days,
        )
    )

    logger.info(f"Tako VM ready (production_mode={state.config.production_mode})")

    yield

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

app.add_middleware(ApiProtectionMiddleware, config_getter=_get_runtime_config)

app.add_middleware(CorrelationIdMiddleware)


class ExecuteRequest(BaseModel):
    model_config = {"extra": "forbid"}

    code: str = Field(
        ..., min_length=1, max_length=100_000, description="Python code to execute (max 100KB)"
    )
    input_data: Dict[str, Any] = Field(
        default_factory=dict, description="Input data as JSON (max 1MB when serialized)"
    )
    timeout: Optional[int] = Field(
        default=None,
        ge=1,
        le=MAX_EXECUTION_TIMEOUT,
        description=(
            "Timeout for code execution in seconds. The deployment's configured "
            "max_timeout may impose a lower limit."
        ),
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
        if len(serialized) > 1_048_576:
            raise ValueError("input_data exceeds 1MB limit when serialized")
        return v


class ExecuteResponse(BaseModel):
    model_config = {"extra": "forbid"}

    success: bool
    output: Optional[Dict[str, Any]] = None
    execution_time: float
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None
    job_type: Optional[str] = None
    execution_id: Optional[str] = Field(
        default=None,
        description="Execution ID correlating this run with /executions records and logs",
    )
    runtime: Optional[str] = Field(
        default=None,
        description=(
            "Effective isolation runtime: 'runsc' (gVisor) or 'runc' (weaker "
            "fallback). Lets a caller confirm the gVisor boundary was in effect."
        ),
    )


QueueStatus = Literal[
    "queued", "pending", "running", "succeeded", "failed", "timeout", "oom", "cancelled"
]
HealthStatus = Literal["healthy", "degraded"]
RelationshipType = Literal["rerun", "fork"]


class AsyncExecuteResponse(BaseModel):
    model_config = {"extra": "forbid"}

    job_id: str
    status: QueueStatus


class JobStatusResponse(BaseModel):
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
    model_config = {"extra": "forbid"}

    startup_ms: Optional[int] = None

    dep_install_ms: Optional[int] = None

    execution_ms: Optional[int] = None

    total_ms: Optional[int] = None

    phase_at_exit: Optional[str] = None


class ExecutionRecordResponse(BaseModel):
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

    runtime: Optional[str] = None

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
            runtime=record.runtime,
        )


class JobTypeResponse(BaseModel):
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

    @classmethod
    def from_job_type(cls, jt, builder) -> "JobTypeResponse":
        return cls(
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


class CircuitBreakerStatus(BaseModel):
    model_config = {"extra": "forbid"}

    state: Literal["closed", "open", "half_open"] = Field(..., description="Circuit breaker state")
    failure_count: int = Field(..., description="Number of consecutive failures")
    success_count: int = Field(..., description="Number of consecutive successes")
    last_failure: Optional[float] = Field(default=None, description="Timestamp of last failure")


class QueueStatsResponse(BaseModel):
    model_config = {"extra": "forbid"}

    pending: int = Field(..., description="Number of jobs waiting in queue")
    running: int = Field(..., description="Number of jobs currently executing")
    max_workers: int = Field(..., description="Maximum concurrent workers")
    max_queue_size: int = Field(..., description="Maximum queue capacity")


class HealthResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: HealthStatus
    docker_available: bool
    gvisor_available: bool
    circuit_breaker: CircuitBreakerStatus
    version: str
    production_mode: bool
    queue_stats: QueueStatsResponse


PoolStatsResponse = QueueStatsResponse


class PaginatedResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: List[Any] = Field(..., description="List of items")
    limit: int = Field(..., description="Maximum items returned")
    offset: int = Field(..., description="Number of items skipped")
    has_more: bool = Field(..., description="Whether more items exist beyond this page")
    count: int = Field(..., description="Number of items in this response")


class CancelResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["cancelled"] = Field(..., description="Cancellation status")
    job_id: str = Field(..., description="ID of the cancelled job")


class DLQStatsResponse(BaseModel):
    model_config = {"extra": "forbid"}

    total: int = Field(..., description="Total number of entries in DLQ")
    by_error_type: Dict[str, int] = Field(..., description="Breakdown by error type")


class BuildResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["built"] = Field(..., description="Build status")
    job_type: str = Field(..., description="Name of the job type")
    version: str = Field(..., description="Full version reference")
    digest: str = Field(..., description="Short digest of the build")
    image_ref: str = Field(..., description="Docker image reference")


class DLQRemoveResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["removed"] = Field(..., description="Removal status")
    entry_id: int = Field(..., description="ID of the removed entry")


class RerunRequest(BaseModel):
    model_config = {"extra": "forbid"}

    job_type: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_-]+(@[a-zA-Z0-9_.@:-]+)?$",
        description="Optional job type override",
    )
    timeout: Optional[int] = Field(
        default=None,
        ge=1,
        le=MAX_EXECUTION_TIMEOUT,
        description=(
            "Optional timeout override. The deployment's configured max_timeout "
            "may impose a lower limit."
        ),
    )


class ForkRequest(BaseModel):
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
        default=None,
        ge=1,
        le=MAX_EXECUTION_TIMEOUT,
        description=(
            "Optional timeout override. The deployment's configured max_timeout "
            "may impose a lower limit."
        ),
    )

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Code cannot be empty")
        return v


class ArtifactResponse(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(..., description="Artifact filename")
    size_bytes: int = Field(..., description="File size in bytes")
    sha256: str = Field(..., description="SHA256 hash of file contents")
    content_type: Optional[str] = Field(default=None, description="MIME type")

    @classmethod
    def from_model(cls, artifact) -> "ArtifactResponse":
        return cls(
            name=artifact.name,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            content_type=artifact.content_type,
        )


class ResourceUsageResponse(BaseModel):
    model_config = {"extra": "forbid"}

    max_rss_mb: Optional[float] = Field(default=None, description="Peak RSS in MB")
    cpu_time_ms: Optional[int] = Field(default=None, description="CPU time in ms")
    wall_time_ms: Optional[int] = Field(default=None, description="Wall clock time in ms")


class ExecutionRecordFullResponse(ExecutionRecordResponse):
    job_ref: str = Field(
        ..., description="Full job reference with digest (e.g., 'data-processing@sha256:...')"
    )

    artifacts: List[ArtifactResponse] = Field(default_factory=list, description="Output artifacts")
    input_artifacts: List[ArtifactResponse] = Field(
        default_factory=list,
        description="Input artifacts (includes internal _code.py, _input.json)",
    )

    resource_usage: Optional[ResourceUsageResponse] = Field(
        default=None, description="Resource consumption metrics"
    )

    code_hash: Optional[str] = Field(default=None, description="SHA256 hash of submitted code")
    input_hash: Optional[str] = Field(default=None, description="SHA256 hash of input_data")

    parent_execution_id: Optional[str] = Field(
        default=None, description="Parent execution ID (for rerun/fork)"
    )
    relationship: Optional[RelationshipType] = Field(
        default=None, description="Relationship to parent: 'rerun' or 'fork'"
    )

    stdout_truncated: bool = Field(default=False, description="Whether stdout was truncated")
    stderr_truncated: bool = Field(default=False, description="Whether stderr was truncated")

    idempotency_key: Optional[str] = Field(
        default=None, description="Client-provided idempotency key"
    )

    @classmethod
    def from_record(cls, record: ExecutionRecord) -> "ExecutionRecordFullResponse":
        base = ExecutionRecordResponse.from_record(record).model_dump()
        return cls(
            **base,
            job_ref=record.job_ref,
            artifacts=[ArtifactResponse.from_model(a) for a in record.artifacts],
            input_artifacts=[ArtifactResponse.from_model(a) for a in record.input_artifacts],
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


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthResponse)
async def health_check():
    if not hasattr(state, "worker_pool"):
        return JSONResponse(
            status_code=503,
            content={"status": "starting", "detail": "Server is not ready yet"},
        )

    circuit_breaker = get_circuit_breaker()
    docker_available = circuit_breaker.check_docker_health()
    gvisor_available = check_gvisor_available()
    cb_status = circuit_breaker.get_status()

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
async def execute_code(request: ExecuteRequest, http_request: Request):
    _validate_execution_timeout(request.timeout, request.job_type)
    job_id = _generate_sync_job_id()

    logger.info(f"Executing job {job_id}")
    start_time = time.time()

    try:
        job = {
            "code": request.code,
            "input_data": request.input_data,
            "job_type": request.job_type,
        }

        if request.timeout is not None:
            job["timeout"] = request.timeout

        if request.startup_timeout is not None:
            job["startup_timeout"] = request.startup_timeout

        if request.requirements is not None:
            job["requirements"] = request.requirements

        client_ip = http_request.client.host if http_request.client else None

        record = await asyncio.to_thread(
            state.executor.execute_job_with_record,
            job_id=job_id,
            job=job,
            client_ip=client_ip,
        )
        execution_time = time.time() - start_time

        try:
            await state.storage.save_record(record)
        except Exception as storage_err:
            logger.warning(
                "Failed to persist execution record %s: %s",
                job_id,
                storage_err,
                exc_info=True,
            )

        logger.info(f"Job {job_id} completed in {execution_time:.2f}s")

        return ExecuteResponse(
            success=record.status == "succeeded",
            output=record.result_json,
            execution_time=execution_time,
            stdout=record.stdout,
            stderr=record.stderr,
            exit_code=record.exit_code,
            error=record.error.message if record.error else None,
            job_type=record.job_type,
            execution_id=record.execution_id,
            runtime=record.runtime,
        )

    except Exception as e:
        logger.error("Job %s error: %s", job_id, str(e), exc_info=True)
        safe_message = sanitize_error(str(e))
        raise HTTPException(status_code=500, detail=f"Execution error: {safe_message}") from e


@app.post("/execute/async", response_model=AsyncExecuteResponse)
async def execute_code_async(request: ExecuteRequest, http_request: Request):
    _validate_execution_timeout(request.timeout, request.job_type)

    try:
        if request.idempotency_key:
            async with state.idempotency_locks.acquire(request.idempotency_key):
                return await _submit_async_job(request, http_request)
        else:
            return await _submit_async_job(request, http_request)

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.warning("Async submit rejected: %s", str(e))
        safe_message = sanitize_error(str(e))
        raise HTTPException(status_code=503, detail=safe_message) from e
    except Exception as e:
        logger.error("Async submit error: %s", str(e), exc_info=True)
        safe_message = sanitize_error(str(e))
        raise HTTPException(status_code=500, detail=f"Submit error: {safe_message}") from e


async def _submit_async_job(request: ExecuteRequest, http_request: Request) -> AsyncExecuteResponse:
    if request.idempotency_key:
        existing = await state.storage.get_by_idempotency_key(request.idempotency_key)
        if existing:
            expected_fingerprint = compute_idempotency_fingerprint(
                request.code,
                request.input_data,
                request.job_type,
                request.timeout,
                request.startup_timeout,
                request.requirements,
            )

            if (
                existing.idempotency_fingerprint
                and expected_fingerprint != existing.idempotency_fingerprint
            ):
                raise HTTPException(
                    status_code=409, detail="Idempotency key reused with different payload"
                )

            logger.info(f"Idempotency hit: returning existing job {existing.execution_id}")
            return AsyncExecuteResponse(job_id=existing.execution_id, status=existing.status)

    correlation_id = get_correlation_id()

    idempotency_fingerprint = None
    if request.idempotency_key:
        idempotency_fingerprint = compute_idempotency_fingerprint(
            request.code,
            request.input_data,
            request.job_type,
            request.timeout,
            request.startup_timeout,
            request.requirements,
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
    try:
        if wait:
            record = await state.worker_pool.wait_for_result(job_id, timeout=timeout)
        else:
            record = await state.storage.get_record(job_id)
            if not record:
                status = await state.worker_pool.get_job_status(job_id)
                if status and status.get("status") in ("pending", "running"):
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
    success = await state.worker_pool.cancel(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found or already completed")

    return CancelResponse(status="cancelled", job_id=job_id)


def _get_replay_data(record: ExecutionRecord) -> tuple:
    import json

    data_dir = state.config.data_dir.resolve()

    code_artifact = next((a for a in record.input_artifacts if a.name == "_code.py"), None)
    if not code_artifact:
        raise HTTPException(status_code=400, detail="Original code not available for replay")

    code_path = _resolve_under_data_dir(data_dir, code_artifact.storage_key)
    if not code_path.exists():
        raise HTTPException(status_code=400, detail="Original code file not found")
    code = code_path.read_text(encoding="utf-8")

    input_artifact = next((a for a in record.input_artifacts if a.name == "_input.json"), None)
    if not input_artifact:
        raise HTTPException(status_code=400, detail="Original input not available for replay")

    input_path = _resolve_under_data_dir(data_dir, input_artifact.storage_key)
    if not input_path.exists():
        raise HTTPException(status_code=400, detail="Original input file not found")
    input_data = json.loads(input_path.read_text(encoding="utf-8"))

    user_artifacts = [a.model_dump() for a in record.input_artifacts if not a.name.startswith("_")]

    return code, input_data, user_artifacts


async def _submit_replay(
    parent: ExecutionRecord,
    *,
    relationship: str,
    code: str,
    input_data: dict,
    input_artifacts: list,
    job_type: Optional[str],
    timeout: Optional[int],
    http_request: Request,
) -> AsyncExecuteResponse:
    resolved_job_type = job_type or parent.job_type
    _validate_execution_timeout(timeout, resolved_job_type)

    job_data = {
        "code": code,
        "input_data": input_data,
        "input_artifacts": input_artifacts,
        "job_type": resolved_job_type,
        "parent_execution_id": parent.execution_id,
        "relationship": relationship,
        "correlation_id": get_correlation_id(),
    }

    if timeout:
        job_data["timeout"] = timeout

    new_job_id = await state.worker_pool.submit(
        job_data=job_data, client_ip=http_request.client.host if http_request.client else None
    )

    logger.info(f"Job {new_job_id} created as {relationship} of {parent.execution_id}")

    return AsyncExecuteResponse(job_id=new_job_id, status="queued")


@app.post("/jobs/{job_id}/rerun", response_model=AsyncExecuteResponse)
async def rerun_job(job_id: str, request: RerunRequest, http_request: Request):
    _validate_execution_timeout(request.timeout)
    parent = await state.storage.get_record(job_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Job not found")

    if parent.status not in ("succeeded", "failed", "timeout", "oom", "cancelled"):
        raise HTTPException(status_code=400, detail="Cannot rerun job that hasn't completed")

    try:
        code, input_data, input_artifacts = _get_replay_data(parent)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get replay data for {job_id}: {e}")
        raise HTTPException(status_code=400, detail="Failed to retrieve replay data") from e

    return await _submit_replay(
        parent,
        relationship="rerun",
        code=code,
        input_data=input_data,
        input_artifacts=input_artifacts,
        job_type=request.job_type,
        timeout=request.timeout,
        http_request=http_request,
    )


@app.post("/jobs/{job_id}/fork", response_model=AsyncExecuteResponse)
async def fork_job(job_id: str, request: ForkRequest, http_request: Request):
    _validate_execution_timeout(request.timeout)
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

    return await _submit_replay(
        parent,
        relationship="fork",
        code=request.code,
        input_data=input_data,
        input_artifacts=input_artifacts,
        job_type=request.job_type,
        timeout=request.timeout,
        http_request=http_request,
    )


@app.get("/jobs/{job_id}/artifacts/{artifact_name}")
async def download_artifact(job_id: str, artifact_name: str):
    record = await state.storage.get_record(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    artifact = next((a for a in record.artifacts if a.name == artifact_name), None)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    data_dir = state.config.data_dir.resolve()
    artifact_path = _resolve_under_data_dir(data_dir, artifact.storage_key)

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
    records = await state.storage.list_records(
        status=status,
        job_type=job_type,
        limit=limit + 1,
        offset=offset,
    )

    has_more = len(records) > limit
    if has_more:
        records = records[:limit]

    if view == "full":
        items = [ExecutionRecordFullResponse.from_record(r) for r in records]
    else:
        items = [ExecutionRecordResponse.from_record(r) for r in records]

    return PaginatedResponse(
        items=items,
        limit=limit,
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
    record = await state.storage.get_record(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Execution not found")

    if view == "full":
        return ExecutionRecordFullResponse.from_record(record)
    return ExecutionRecordResponse.from_record(record)


@app.get("/job-types", response_model=List[JobTypeResponse])
async def list_job_types():
    from tako_vm.execution.builder import ContainerBuilder

    builder = ContainerBuilder()

    return [JobTypeResponse.from_job_type(jt, builder) for jt in state.registry.list()]


@app.get("/job-types/{name}", response_model=JobTypeResponse)
async def get_job_type(name: str):
    from tako_vm.execution.builder import ContainerBuilder

    jt = state.registry.get(name)
    if not jt:
        raise HTTPException(status_code=404, detail=f"Job type '{name}' not found")

    builder = ContainerBuilder()
    return JobTypeResponse.from_job_type(jt, builder)


@app.post("/job-types/{name}/build", response_model=BuildResponse)
async def build_job_type(name: str, version_tag: Optional[str] = None):
    from tako_vm.execution.builder import ContainerBuilder
    from tako_vm.version import VersionManager

    jt = state.registry.get(name)
    if not jt:
        raise HTTPException(status_code=404, detail=f"Job type '{name}' not found")

    try:
        builder = ContainerBuilder()
        builder.build(jt, quiet=False)

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
    stats = await state.worker_pool.get_stats()
    return PoolStatsResponse(**stats)


@app.get("/dlq/stats", response_model=DLQStatsResponse)
async def get_dlq_stats():
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
    entries = await state.storage.list_dlq_entries(
        error_type=error_type,
        limit=limit + 1,
        offset=offset,
    )

    has_more = len(entries) > limit
    if has_more:
        entries = entries[:limit]

    return PaginatedResponse(
        items=[entry.model_dump() for entry in entries],
        limit=limit,
        offset=offset,
        has_more=has_more,
        count=len(entries),
    )


@app.delete("/dlq/{entry_id}", response_model=DLQRemoveResponse)
async def remove_dlq_entry(entry_id: int):
    if await state.storage.remove_from_dlq(entry_id):
        return DLQRemoveResponse(status="removed", entry_id=entry_id)
    raise HTTPException(status_code=404, detail="DLQ entry not found")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    correlation_id = get_correlation_id()
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
    correlation_id = get_correlation_id()
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
