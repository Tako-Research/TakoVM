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

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "oom", "cancelled"})


@dataclass
class QueuedJob:
    job_id: str

    job_data: Dict[str, Any]

    client_ip: Optional[str]

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    future: Optional[asyncio.Future] = field(default=None)

    cancelled: bool = False

    cancel_signal_sent: bool = False


class WorkerPool:
    def __init__(
        self,
        executor: "CodeExecutor",
        storage: ExecutionStorage,
        max_workers: int = 4,
        max_queue_size: int = 100,
        queue_wait_timeout: float = 1.0,
    ):
        self.executor = executor
        self.storage = storage
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        self.queue_wait_timeout = queue_wait_timeout

        self._queue: asyncio.Queue[QueuedJob] = asyncio.Queue(maxsize=max_queue_size)
        self._workers: list[asyncio.Task] = []
        self._active_jobs: Dict[str, QueuedJob] = {}
        self._running_jobs: Dict[str, QueuedJob] = {}
        self._jobs_lock: asyncio.Lock = asyncio.Lock()
        self._thread_pool: Optional[ThreadPoolExecutor] = None
        self._shutdown = False
        self._started = False

    async def start(self) -> None:
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
        if not self._started:
            return

        self._shutdown = True

        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

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

        if self._workers:
            done, pending = await asyncio.wait(
                self._workers, timeout=timeout, return_when=asyncio.ALL_COMPLETED
            )

            for task in pending:
                task.cancel()

        if self._thread_pool:
            self._thread_pool.shutdown(wait=True, cancel_futures=True)
            self._thread_pool = None

        self._workers.clear()
        self._active_jobs.clear()
        self._running_jobs.clear()
        self._started = False

        logger.info("Worker pool stopped")

    async def submit(self, job_data: Dict[str, Any], client_ip: Optional[str] = None) -> str:
        job_id = str(uuid.uuid4())

        loop = asyncio.get_running_loop()

        job = QueuedJob(
            job_id=job_id,
            job_data=job_data,
            client_ip=client_ip,
            future=loop.create_future(),
        )

        queued_record = self._build_record(job, "queued")

        await self.storage.save_record(queued_record)

        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
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
        async with self._jobs_lock:
            job = self._active_jobs.get(job_id) or self._running_jobs.get(job_id)

        if not job:
            record = await self.storage.get_record(job_id)
            if record and record.status in _TERMINAL_STATUSES:
                return record
            raise KeyError(f"Job {job_id} not found")

        if not job.future:
            raise KeyError(f"Job {job_id} has no future (internal error)")

        if timeout:
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout)
        else:
            return await job.future

    async def get_job_status(self, job_id: str) -> Optional[dict]:
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
        pending_job: Optional[QueuedJob] = None
        running_job: Optional[QueuedJob] = None
        async with self._jobs_lock:
            job = self._active_jobs.get(job_id)
            if job and job.future and not job.future.done():
                job.cancelled = True
                pending_job = job
                self._active_jobs.pop(job_id, None)

            else:
                job = self._running_jobs.get(job_id)
                if job:
                    job.cancelled = True
                    running_job = job

        if pending_job is not None:
            record = self._build_cancelled_record(pending_job)
            await self.storage.save_record(record)
            if pending_job.future and not pending_job.future.done():
                try:
                    pending_job.future.set_result(record)
                except asyncio.InvalidStateError:
                    logger.debug(f"Future already done for cancelled job {job_id}")
            logger.info(f"Job {job_id} cancelled (was pending)")
            return True

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

    @staticmethod
    def _build_record(
        job: QueuedJob,
        status: str,
        *,
        error: Optional[ExecutionError] = None,
        queued_at: Optional[datetime] = None,
    ) -> ExecutionRecord:
        job_type_name = job.job_data.get("job_type") or "default"
        return ExecutionRecord(
            execution_id=job.job_id,
            status=status,
            job_type=job_type_name,
            job_ref=f"{job_type_name}@latest",
            created_at=job.created_at,
            queued_at=queued_at if queued_at is not None else job.created_at,
            code_hash=sha256_content(job.job_data.get("code", "")),
            input_hash=sha256_json(job.job_data.get("input_data", {})),
            client_ip=job.client_ip,
            correlation_id=job.job_data.get("correlation_id"),
            error=error,
            idempotency_key=job.job_data.get("idempotency_key"),
            idempotency_fingerprint=job.job_data.get("idempotency_fingerprint"),
            parent_execution_id=job.job_data.get("parent_execution_id"),
            relationship=job.job_data.get("relationship"),
        )

    def _build_cancelled_record(
        self, job: QueuedJob, message: str = "Execution was cancelled by user"
    ) -> ExecutionRecord:
        return self._build_record(
            job, "cancelled", error=ExecutionError(type="cancelled", message=message)
        )

    def _estimate_queue_position_unlocked(self, job_id: str) -> int:
        position = 1
        for jid in self._active_jobs:
            if jid == job_id:
                break
            if jid not in self._running_jobs:
                position += 1
        return position

    async def get_stats(self) -> dict:
        async with self._jobs_lock:
            running_count = len(self._running_jobs)
        return {
            "pending": self._queue.qsize(),
            "running": running_count,
            "max_workers": self.max_workers,
            "max_queue_size": self.max_queue_size,
        }

    async def _worker_loop(self, worker_id: int) -> None:
        logger.info(f"Worker {worker_id} started")

        while not self._shutdown:
            job: Optional[QueuedJob] = None
            try:
                try:
                    job = await asyncio.wait_for(self._queue.get(), timeout=self.queue_wait_timeout)
                except asyncio.TimeoutError:
                    continue

                clear_correlation_id()

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

                async with self._jobs_lock:
                    self._running_jobs[job.job_id] = job

                try:
                    await self.storage.mark_record_running(
                        job.job_id, worker_id=f"worker-{worker_id}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to persist running state for job {job.job_id}: {e}")

                correlation_id = job.job_data.get("correlation_id")
                if correlation_id:
                    set_correlation_id(correlation_id)

                logger.info(f"Worker {worker_id} executing job {job.job_id}")

                try:
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

                    await self.storage.save_record(record)

                    if job.future:
                        try:
                            job.future.set_result(record)
                        except asyncio.InvalidStateError:
                            logger.debug(f"Future already done for job {job.job_id}")

                except asyncio.CancelledError:
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

                    from tako_vm.security import sanitize_error

                    error_type = "internal_error"
                    error_msg = sanitize_error(str(e))

                    record = self._build_record(
                        job,
                        "failed",
                        error=ExecutionError(type=error_type, message=error_msg),
                    )
                    await self.storage.save_record(record)

                    try:
                        dlq_entry = DeadLetterEntry(
                            job_id=job.job_id,
                            job_data=DeadLetterEntry.build_job_summary(job.job_data),
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

            except Exception as e:
                logger.error(f"Worker {worker_id} unexpected error: {e}", exc_info=True)
                if job is not None and job.future:
                    try:
                        from tako_vm.security import sanitize_error

                        error_record = self._build_record(
                            job,
                            "failed",
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
                await asyncio.sleep(1)
            finally:
                if job is not None:
                    async with self._jobs_lock:
                        self._active_jobs.pop(job.job_id, None)
                        self._running_jobs.pop(job.job_id, None)

        logger.info(f"Worker {worker_id} stopped")

    async def _execute_job(self, job: QueuedJob) -> ExecutionRecord:
        loop = asyncio.get_running_loop()

        job_timeout, startup_timeout = self._resolve_timeout_budget(job)
        executor_timeout = startup_timeout + job_timeout + 60

        try:
            record = await asyncio.wait_for(
                loop.run_in_executor(self._thread_pool, self._run_job_sync, job),
                timeout=executor_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return await self._handle_watchdog_timeout(job, executor_timeout)

        return record

    def _resolve_timeout_budget(self, job: QueuedJob) -> tuple:
        default_timeout: float = 30
        default_startup_timeout: float = 120

        job_type_name = job.job_data.get("job_type")
        if job_type_name:
            try:
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

        return self._build_record(
            job,
            "timeout",
            error=ExecutionError(
                type="execution_timeout",
                message=(
                    f"Execution exceeded the watchdog budget of {executor_timeout}s "
                    "(startup + execution + orchestration buffer); container was killed"
                ),
            ),
        )

    def _run_job_sync(self, job: QueuedJob) -> ExecutionRecord:
        return self.executor.execute_job_with_record(
            job_id=job.job_id,
            job=job.job_data,
            client_ip=job.client_ip,
        )
