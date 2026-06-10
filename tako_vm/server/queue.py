"""
Job queue and worker pool for Tako VM.

Provides async job submission, execution, and cancellation.
"""

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from tako_vm.execution.docker import generate_container_name, kill_container
from tako_vm.models import (
    DeadLetterEntry,
    ExecutionError,
    ExecutionRecord,
    sha256_content,
    sha256_json,
)
from tako_vm.server.correlation import (
    clear_correlation_id,
    get_correlation_id,
    set_correlation_id,
)
from tako_vm.storage import ExecutionStorage

if TYPE_CHECKING:
    from tako_vm.execution.worker import CodeExecutor

logger = logging.getLogger(__name__)


@dataclass
class QueuedJob:
    """Job waiting in or being processed by the queue."""

    job_id: str
    """Unique job identifier."""

    job_data: Dict[str, Any]
    """Job data including code, input_data, etc."""

    client_ip: Optional[str]
    """Client IP address."""

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    """When the job was queued."""

    future: Optional[asyncio.Future] = field(default=None)
    """Future that will hold the result (set by WorkerPool.submit)."""

    cancelled: bool = False
    """Whether this job has been cancelled."""

    cancel_signal_sent: bool = False
    """Whether a kill signal was successfully sent to a running container."""


class WorkerPool:
    """
    Async worker pool for job execution.

    Uses asyncio.Queue for pending jobs and ThreadPoolExecutor
    for running Docker operations.
    """

    def __init__(
        self,
        executor: "CodeExecutor",
        storage: ExecutionStorage,
        max_workers: int = 4,
        max_queue_size: int = 100,
        queue_wait_timeout: float = 1.0,
    ):
        """
        Initialize worker pool.

        Args:
            executor: CodeExecutor instance for running jobs
            storage: ExecutionStorage for persisting records
            max_workers: Maximum concurrent workers
            max_queue_size: Maximum pending jobs in queue
            queue_wait_timeout: Timeout for queue wait operations in seconds
        """
        self.executor = executor
        self.storage = storage
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        self.queue_wait_timeout = queue_wait_timeout

        self._queue: asyncio.Queue[QueuedJob] = asyncio.Queue(maxsize=max_queue_size)
        self._workers: list[asyncio.Task] = []
        self._active_jobs: Dict[str, QueuedJob] = {}
        self._running_jobs: Dict[str, QueuedJob] = {}
        self._jobs_lock: asyncio.Lock = asyncio.Lock()  # Protect job dictionary access
        self._thread_pool: Optional[ThreadPoolExecutor] = None
        self._shutdown = False
        self._started = False

    async def start(self) -> None:
        """Start worker tasks."""
        if self._started:
            return

        self._shutdown = False
        self._thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        for i in range(self.max_workers):
            worker = asyncio.create_task(self._worker_loop(i), name=f"worker-{i}")
            self._workers.append(worker)

        self._started = True
        logger.info(f"Started worker pool with {self.max_workers} workers")

    async def stop(self, timeout: float = 30.0) -> None:
        """
        Graceful shutdown.

        Args:
            timeout: Maximum time to wait for workers to finish
        """
        if not self._started:
            return

        self._shutdown = True

        # Cancel all pending jobs, persisting a terminal record first so their
        # DB rows don't stay stuck at 'queued' after a graceful restart/deploy.
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Best-effort: a storage failure during shutdown must not
            # prevent shutdown (startup reconciliation covers the gap).
            try:
                record = self._build_cancelled_record(
                    job, message="Job cancelled: server shut down before execution started"
                )
                await self.storage.save_record(record)
            except Exception as e:
                logger.warning(
                    f"Failed to persist shutdown record for pending job {job.job_id}: {e}"
                )

            if job.future and not job.future.done():
                job.future.cancel()

        # Wait for workers with timeout
        if self._workers:
            done, pending = await asyncio.wait(
                self._workers, timeout=timeout, return_when=asyncio.ALL_COMPLETED
            )

            # Cancel any still running
            for task in pending:
                task.cancel()

        # Shutdown thread pool - wait for running jobs to complete
        if self._thread_pool:
            self._thread_pool.shutdown(wait=True, cancel_futures=True)
            self._thread_pool = None

        self._workers.clear()
        self._active_jobs.clear()
        self._running_jobs.clear()
        self._started = False

        logger.info("Worker pool stopped")

    async def submit(self, job_data: Dict[str, Any], client_ip: Optional[str] = None) -> str:
        """
        Submit job to queue.

        Args:
            job_data: Job data dictionary
            client_ip: Client IP address

        Returns:
            Job ID

        Raises:
            RuntimeError: If queue is full
        """
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # Create future in async context where event loop is running
        loop = asyncio.get_running_loop()

        job = QueuedJob(
            job_id=job_id,
            job_data=job_data,
            client_ip=client_ip,
            future=loop.create_future(),
        )

        # Create preliminary "queued" record for idempotency tracking
        # This ensures concurrent requests with same idempotency_key see the record
        job_type_name = job_data.get("job_type") or "default"
        queued_record = ExecutionRecord(
            execution_id=job_id,
            status="queued",
            job_type=job_type_name,
            job_ref=f"{job_type_name}@latest",
            created_at=now,
            queued_at=now,
            code_hash=sha256_content(job_data.get("code", "")),
            input_hash=sha256_json(job_data.get("input_data", {})),
            client_ip=client_ip,
            correlation_id=job_data.get("correlation_id"),
            idempotency_key=job_data.get("idempotency_key"),
            idempotency_fingerprint=job_data.get("idempotency_fingerprint"),
            parent_execution_id=job_data.get("parent_execution_id"),
            relationship=job_data.get("relationship"),
        )
        if self._queue.full():
            raise RuntimeError("Job queue is full, try again later")

        await self.storage.save_record(queued_record)

        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            # Release the idempotency key: a 503 tells the client to retry,
            # and the retry must not collide with this dead record. save_record
            # is an upsert that overwrites all columns, so re-saving with the
            # key cleared frees it while keeping the failure visible.
            queued_record.status = "failed"
            queued_record.idempotency_key = None
            queued_record.idempotency_fingerprint = None
            queued_record.error = ExecutionError(
                type="service_unavailable", message="Job queue is full, try again later"
            )
            await self.storage.save_record(queued_record)
            raise RuntimeError("Job queue is full, try again later") from exc

        async with self._jobs_lock:
            self._active_jobs[job_id] = job

        logger.info(f"Job {job_id} queued (queue size: {self._queue.qsize()})")

        return job_id

    async def wait_for_result(
        self, job_id: str, timeout: Optional[float] = None
    ) -> ExecutionRecord:
        """
        Wait for job completion.

        Args:
            job_id: Job ID to wait for
            timeout: Maximum time to wait (None = no timeout)

        Returns:
            ExecutionRecord with results

        Raises:
            KeyError: If job not found
            asyncio.TimeoutError: If timeout exceeded
            asyncio.CancelledError: If job was cancelled
        """
        # Get job reference with lock protection
        async with self._jobs_lock:
            job = self._active_jobs.get(job_id) or self._running_jobs.get(job_id)

        if not job:
            # Check if already completed in storage
            record = await self.storage.get_record(job_id)
            if record:
                return record
            raise KeyError(f"Job {job_id} not found")

        if not job.future:
            raise KeyError(f"Job {job_id} has no future (internal error)")

        if timeout:
            # Shield the job future: asyncio.wait_for cancels the awaited
            # future on timeout, which would make the worker loop treat the
            # job as user-cancelled (never executing it) and surface
            # CancelledError to any concurrent waiters. A read-only wait
            # timing out must never affect the job itself.
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout)
        else:
            return await job.future

    async def get_job_status(self, job_id: str) -> Optional[dict]:
        """
        Get current job status.

        Args:
            job_id: Job ID to check

        Returns:
            Status dict or None if not found
        """
        # Check active/running jobs with lock protection
        async with self._jobs_lock:
            if job_id in self._running_jobs:
                return {
                    "job_id": job_id,
                    "status": "running",
                    "created_at": self._running_jobs[job_id].created_at.isoformat(),
                }

            if job_id in self._active_jobs:
                job = self._active_jobs[job_id]
                if job.future and job.future.done():
                    # Derive a real terminal status from the finished future.
                    # "completed" is not a valid QueueStatus and would fail
                    # response validation in the API layer.
                    if job.future.cancelled():
                        status = "cancelled"
                        duration_ms = None
                    elif job.future.exception() is not None:
                        status = "failed"
                        duration_ms = None
                    else:
                        record = job.future.result()
                        status = record.status
                        duration_ms = record.duration_ms
                    return {
                        "job_id": job_id,
                        "status": status,
                        "created_at": job.created_at.isoformat(),
                        "duration_ms": duration_ms,
                    }
                queue_position = self._estimate_queue_position_unlocked(job_id)
                return {
                    "job_id": job_id,
                    "status": "pending",
                    "created_at": job.created_at.isoformat(),
                    "queue_position": queue_position,
                }

        # Check storage for completed jobs (outside lock - storage has its own lock)
        record = await self.storage.get_record(job_id)
        if record:
            return {
                "job_id": job_id,
                "status": record.status,
                "created_at": record.created_at.isoformat(),
                "duration_ms": record.duration_ms,
            }

        return None

    async def cancel(self, job_id: str) -> bool:
        """
        Cancel a queued or running job.

        Args:
            job_id: Job ID to cancel

        Returns:
            True if job was found and cancelled
        """
        running_job: Optional[QueuedJob] = None
        async with self._jobs_lock:
            # Check pending jobs
            job = self._active_jobs.get(job_id)
            if job and job.future and not job.future.done():
                job.cancelled = True
                logger.info(f"Job {job_id} cancelled (was pending)")
                return True

            # Check running jobs - send a kill to the tracked container.
            job = self._running_jobs.get(job_id)
            if job:
                job.cancelled = True
                running_job = job

        if not running_job:
            return False

        container_name = generate_container_name("tako", job_id)
        signal_sent = await asyncio.to_thread(kill_container, container_name)

        async with self._jobs_lock:
            current = self._running_jobs.get(job_id)
            if current is running_job:
                current.cancel_signal_sent = signal_sent

        if signal_sent:
            logger.info(f"Job {job_id} cancellation signal sent to container {container_name}")
        else:
            logger.info(f"Job {job_id} cancellation requested, but container was not running")

        return True

    def _build_cancelled_record(
        self, job: QueuedJob, message: str = "Execution was cancelled by user"
    ) -> ExecutionRecord:
        """Create a final cancelled record for a job."""
        job_type_name = job.job_data.get("job_type") or "default"
        return ExecutionRecord(
            execution_id=job.job_id,
            status="cancelled",
            job_type=job_type_name,
            job_ref=f"{job_type_name}@latest",
            created_at=job.created_at,
            queued_at=job.created_at,
            code_hash=sha256_content(job.job_data.get("code", "")),
            input_hash=sha256_json(job.job_data.get("input_data", {})),
            client_ip=job.client_ip,
            correlation_id=job.job_data.get("correlation_id"),
            error=ExecutionError(type="cancelled", message=message),
            idempotency_key=job.job_data.get("idempotency_key"),
            idempotency_fingerprint=job.job_data.get("idempotency_fingerprint"),
            parent_execution_id=job.job_data.get("parent_execution_id"),
            relationship=job.job_data.get("relationship"),
        )

    def _estimate_queue_position_unlocked(self, job_id: str) -> int:
        """Estimate position in queue (rough estimate). Must be called with _jobs_lock held."""
        position = 1
        for jid in self._active_jobs:
            if jid == job_id:
                break
            if jid not in self._running_jobs:
                position += 1
        return position

    async def get_stats(self) -> dict:
        """Get current pool statistics."""
        async with self._jobs_lock:
            running_count = len(self._running_jobs)
        return {
            "pending": self._queue.qsize(),
            "running": running_count,
            "max_workers": self.max_workers,
            "max_queue_size": self.max_queue_size,
        }

    @property
    def stats(self) -> dict:
        """Get current pool statistics (sync version for backward compatibility).

        DEPRECATED: This property doesn't use lock protection for running count,
        which can cause race conditions. Use get_stats() for async code paths.

        Returns:
            Dict with pending, running, max_workers, max_queue_size
        """
        import warnings

        warnings.warn(
            "WorkerPool.stats property is deprecated due to race condition. "
            "Use 'await worker_pool.get_stats()' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return {
            "pending": self._queue.qsize(),
            "running": len(self._running_jobs),
            "max_workers": self.max_workers,
            "max_queue_size": self.max_queue_size,
        }

    async def _worker_loop(self, worker_id: int) -> None:
        """
        Worker coroutine that processes jobs.

        Args:
            worker_id: Worker identifier for logging
        """
        logger.info(f"Worker {worker_id} started")

        while not self._shutdown:
            # Initialize job to None for safe error recovery
            job: Optional[QueuedJob] = None
            try:
                # Wait for job with timeout so we can check shutdown flag
                try:
                    job = await asyncio.wait_for(self._queue.get(), timeout=self.queue_wait_timeout)
                except asyncio.TimeoutError:
                    continue

                # Reset correlation context before any per-job work (including
                # the cancelled-skip path below) so logs and DLQ entries never
                # inherit the previous job's correlation ID.
                clear_correlation_id()

                # Skip if already cancelled
                if job.cancelled or (job.future and job.future.cancelled()):
                    await self.storage.save_record(self._build_cancelled_record(job))
                    if job.future and not job.future.done():
                        try:
                            job.future.set_result(self._build_cancelled_record(job))
                        except asyncio.InvalidStateError:
                            logger.debug(f"Future already done for cancelled job {job.job_id}")
                    async with self._jobs_lock:
                        self._active_jobs.pop(job.job_id, None)
                    continue

                # Move to running
                async with self._jobs_lock:
                    self._running_jobs[job.job_id] = job

                # Persist the queued -> running transition before executing so
                # the DB reflects in-flight work: if the server crashes mid-run,
                # forensics see 'running' (not a misleading 'queued') and startup
                # reconciliation marks it failed. Best-effort: execution proceeds
                # even if this write fails.
                try:
                    await self.storage.mark_record_running(
                        job.job_id, worker_id=f"worker-{worker_id}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to persist running state for job {job.job_id}: {e}")

                # Set correlation ID from job data for logging
                correlation_id = job.job_data.get("correlation_id")
                if correlation_id:
                    set_correlation_id(correlation_id)

                logger.info(f"Worker {worker_id} executing job {job.job_id}")

                try:
                    # Run execution in thread pool
                    record = await self._execute_job(job)

                    if job.cancelled and (
                        job.cancel_signal_sent or record.exit_code in {124, 137, 143}
                    ):
                        phase = record.timing.phase_at_exit if record.timing else None
                        record.status = "cancelled"
                        record.error = ExecutionError(
                            type="cancelled",
                            message="Execution was cancelled by user",
                            phase=phase,
                        )

                    # Save to storage
                    await self.storage.save_record(record)

                    # Set result (with race condition protection)
                    if job.future:
                        try:
                            job.future.set_result(record)
                        except asyncio.InvalidStateError:
                            logger.debug(f"Future already done for job {job.job_id}")

                except asyncio.CancelledError:
                    # Job was cancelled
                    record = self._build_cancelled_record(job)
                    await self.storage.save_record(record)

                    if job.future:
                        try:
                            job.future.set_result(record)
                        except asyncio.InvalidStateError:
                            logger.debug(f"Future already done for cancelled job {job.job_id}")

                except Exception as e:
                    logger.error(
                        f"Worker {worker_id} error on job {job.job_id}: {e}", exc_info=True
                    )

                    # Always create an ExecutionRecord for audit trail (P0 fix)
                    from tako_vm.security import sanitize_error

                    error_type = "internal_error"
                    error_msg = sanitize_error(str(e))
                    job_type_name = job.job_data.get("job_type") or "default"

                    record = ExecutionRecord(
                        execution_id=job.job_id,
                        status="failed",
                        job_type=job_type_name,
                        job_ref=f"{job_type_name}@latest",
                        created_at=job.created_at,
                        queued_at=job.created_at,
                        code_hash=sha256_content(job.job_data.get("code", "")),
                        input_hash=sha256_json(job.job_data.get("input_data", {})),
                        client_ip=job.client_ip,
                        correlation_id=job.job_data.get("correlation_id"),
                        error=ExecutionError(type=error_type, message=error_msg),
                        # Propagate idempotency and lineage fields
                        idempotency_key=job.job_data.get("idempotency_key"),
                        idempotency_fingerprint=job.job_data.get("idempotency_fingerprint"),
                        parent_execution_id=job.job_data.get("parent_execution_id"),
                        relationship=job.job_data.get("relationship"),
                    )
                    await self.storage.save_record(record)

                    # Add to dead letter queue for internal errors (P3 fix)
                    try:
                        dlq_entry = DeadLetterEntry(
                            job_id=job.job_id,
                            job_data=job.job_data,
                            error_type=error_type,
                            error_message=error_msg,
                            retry_count=0,
                            client_ip=job.client_ip,
                            correlation_id=get_correlation_id(),
                        )
                        await self.storage.add_to_dlq(dlq_entry)
                        logger.info(f"Job {job.job_id} added to dead letter queue")
                    except Exception as dlq_err:
                        logger.warning(f"Failed to add job {job.job_id} to DLQ: {dlq_err}")

                    if job.future:
                        try:
                            job.future.set_result(record)
                        except asyncio.InvalidStateError:
                            logger.debug(f"Future already done for failed job {job.job_id}")

                finally:
                    # Cleanup with lock protection
                    async with self._jobs_lock:
                        self._active_jobs.pop(job.job_id, None)
                        self._running_jobs.pop(job.job_id, None)

            except Exception as e:
                logger.error(f"Worker {worker_id} unexpected error: {e}", exc_info=True)
                # Ensure job future is resolved even on unexpected errors
                # to prevent clients from hanging indefinitely
                if job is not None and job.future:
                    try:
                        from tako_vm.security import sanitize_error

                        error_record = ExecutionRecord(
                            execution_id=job.job_id,
                            status="failed",
                            job_type=job.job_data.get("job_type") or "default",
                            job_ref=f"{job.job_data.get('job_type') or 'default'}@latest",
                            created_at=job.created_at,
                            queued_at=job.created_at,
                            code_hash=sha256_content(job.job_data.get("code", "")),
                            input_hash=sha256_json(job.job_data.get("input_data", {})),
                            client_ip=job.client_ip,
                            correlation_id=job.job_data.get("correlation_id"),
                            error=ExecutionError(
                                type="internal_error", message=sanitize_error(str(e))
                            ),
                        )
                        await self.storage.save_record(error_record)
                        job.future.set_result(error_record)
                    except Exception as recovery_err:
                        logger.error(
                            f"Failed to recover from error for job {job.job_id}: {recovery_err}"
                        )
                        try:
                            job.future.cancel()
                        except Exception:
                            pass
                await asyncio.sleep(1)  # Prevent tight loop on errors

        logger.info(f"Worker {worker_id} stopped")

    async def _execute_job(self, job: QueuedJob) -> ExecutionRecord:
        """
        Execute job in thread pool with timeout protection.

        Args:
            job: Job to execute

        Returns:
            ExecutionRecord with results (including a terminal "timeout" record
            if the watchdog budget is exceeded)
        """
        loop = asyncio.get_running_loop()

        # Include both startup and execution budgets, plus buffer for Docker orchestration.
        job_timeout, startup_timeout = self._resolve_timeout_budget(job)
        executor_timeout = startup_timeout + job_timeout + 60

        # Run synchronous execution in thread pool with timeout protection
        try:
            record = await asyncio.wait_for(
                loop.run_in_executor(self._thread_pool, self._run_job_sync, job),
                timeout=executor_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return await self._handle_watchdog_timeout(job, executor_timeout)

        return record

    def _resolve_timeout_budget(self, job: QueuedJob) -> tuple:
        """
        Resolve the (job_timeout, startup_timeout) budget for the watchdog.

        Mirrors the executor's own resolution (CodeExecutor falls back to the
        job type's timeout/startup_timeout when the job omits them), so the
        watchdog never undercuts a legitimate job-type budget. Falls back to
        the executor's built-in defaults when the job type is missing or the
        registry lookup fails.

        Args:
            job: Job whose budget to resolve

        Returns:
            Tuple of (job_timeout, startup_timeout) in seconds
        """
        # Matches CodeExecutor's DEFAULT_JOB_TYPE (timeout=30, startup_timeout=120).
        default_timeout: float = 30
        default_startup_timeout: float = 120

        job_type_name = job.job_data.get("job_type")
        if job_type_name:
            try:
                # Strip version specifier (job_type@version), like the executor does.
                name = job_type_name.split("@", 1)[0]
                registry = getattr(self.executor, "registry", None)
                job_type = registry.get(name) if registry is not None else None
                if job_type is not None:
                    default_timeout = float(job_type.timeout)
                    default_startup_timeout = float(job_type.startup_timeout)
            except Exception as e:
                logger.warning(
                    f"Could not resolve job-type budget for job {job.job_id} "
                    f"(job_type={job_type_name!r}): {e}; using built-in defaults"
                )

        job_timeout = job.job_data.get("timeout", default_timeout)
        startup_timeout = job.job_data.get("startup_timeout", default_startup_timeout)
        return job_timeout, startup_timeout

    async def _handle_watchdog_timeout(
        self, job: QueuedJob, executor_timeout: float
    ) -> ExecutionRecord:
        """
        Handle a watchdog (wait_for) timeout for a job.

        The asyncio timeout only cancels the awaiting wrapper -- the executor
        thread keeps running. Kill the container immediately so the sandboxed
        workload stops now instead of when the thread's subprocess backstop
        eventually fires, and return a terminal "timeout" record (this is a
        budget overrun, not an internal error, so it must not be DLQ'd).

        Args:
            job: Job that exceeded the watchdog budget
            executor_timeout: Budget in seconds that was exceeded

        Returns:
            ExecutionRecord with status "timeout"
        """
        container_name = generate_container_name("tako", job.job_id)
        killed = await asyncio.to_thread(kill_container, container_name)
        if killed:
            logger.warning(
                f"Job {job.job_id} exceeded watchdog budget of {executor_timeout}s; "
                f"killed container {container_name}"
            )
        else:
            logger.warning(
                f"Job {job.job_id} exceeded watchdog budget of {executor_timeout}s; "
                f"container {container_name} was not running (already exited?)"
            )
        logger.warning(
            f"Executor thread for job {job.job_id} is stranded until its subprocess "
            "timeout backstop fires; the worker slot is freed now"
        )

        job_type_name = job.job_data.get("job_type") or "default"
        return ExecutionRecord(
            execution_id=job.job_id,
            status="timeout",
            job_type=job_type_name,
            job_ref=f"{job_type_name}@latest",
            created_at=job.created_at,
            queued_at=job.created_at,
            code_hash=sha256_content(job.job_data.get("code", "")),
            input_hash=sha256_json(job.job_data.get("input_data", {})),
            client_ip=job.client_ip,
            correlation_id=job.job_data.get("correlation_id"),
            error=ExecutionError(
                type="execution_timeout",
                message=(
                    f"Execution exceeded the watchdog budget of {executor_timeout}s "
                    "(startup + execution + orchestration buffer); container was killed"
                ),
            ),
            idempotency_key=job.job_data.get("idempotency_key"),
            idempotency_fingerprint=job.job_data.get("idempotency_fingerprint"),
            parent_execution_id=job.job_data.get("parent_execution_id"),
            relationship=job.job_data.get("relationship"),
        )

    def _run_job_sync(self, job: QueuedJob) -> ExecutionRecord:
        """
        Synchronously execute job (runs in thread pool).

        Args:
            job: Job to execute

        Returns:
            ExecutionRecord with results
        """
        return self.executor.execute_job_with_record(
            job_id=job.job_id,
            job=job.job_data,
            client_ip=job.client_ip,
        )
