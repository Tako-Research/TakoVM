"""Tests for worker-pool cancellation and timeout bookkeeping."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tako_vm.models import ExecutionRecord
from tako_vm.server.queue import QueuedJob, WorkerPool


def make_record(job_id: str, status: str = "succeeded", **kwargs) -> ExecutionRecord:
    """Build a minimal ExecutionRecord for tests."""
    return ExecutionRecord(
        execution_id=job_id,
        status=status,
        created_at=datetime.now(timezone.utc),
        queued_at=datetime.now(timezone.utc),
        code_hash="a" * 64,
        input_hash="b" * 64,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_cancel_pending_job_leaves_future_resolvable():
    """Pending-job cancellation resolves the waiter future synchronously."""
    storage = MagicMock()
    storage.save_record = AsyncMock()
    pool = WorkerPool(executor=MagicMock(), storage=storage)
    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-123",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )

    async with pool._jobs_lock:
        pool._active_jobs[job.job_id] = job

    cancelled = await pool.cancel(job.job_id)

    assert cancelled is True
    assert job.cancelled is True
    assert job.future is not None
    assert job.future.cancelled() is False
    # Cancellation is now observable immediately: the cancelled record is
    # persisted and the future is resolved without waiting for a worker.
    storage.save_record.assert_awaited_once()
    assert job.future.done()
    assert job.future.result().status == "cancelled"


@pytest.mark.asyncio
async def test_execute_job_timeout_includes_startup_timeout(monkeypatch):
    """Thread-pool timeout must cover startup and execution budgets."""
    pool = WorkerPool(executor=MagicMock(), storage=MagicMock())
    pool._thread_pool = MagicMock()
    job = QueuedJob(
        job_id="job-123",
        job_data={"timeout": 30, "startup_timeout": 90},
        client_ip=None,
    )

    captured = {}

    class DummyLoop:
        def run_in_executor(self, executor, fn, arg):
            record = ExecutionRecord(
                execution_id="job-123",
                status="queued",
                created_at=datetime.now(timezone.utc),
                queued_at=datetime.now(timezone.utc),
                code_hash="a" * 64,
                input_hash="b" * 64,
            )
            return asyncio.sleep(0, result=record)

    async def fake_wait_for(awaitable, timeout):
        captured["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: DummyLoop())
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    record = await pool._execute_job(job)

    assert record.execution_id == "job-123"
    assert captured["timeout"] == 180


@pytest.mark.asyncio
async def test_wait_for_result_timeout_does_not_cancel_future():
    """A wait=true poll timing out must not cancel the underlying job future."""
    pool = WorkerPool(executor=MagicMock(), storage=MagicMock())
    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-123",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )

    async with pool._jobs_lock:
        pool._active_jobs[job.job_id] = job

    with pytest.raises(asyncio.TimeoutError):
        await pool.wait_for_result("job-123", timeout=0.05)

    # The job must remain executable: future not cancelled, no cancel flag,
    # so the worker dequeue guard would not treat it as user-cancelled.
    assert job.future.cancelled() is False
    assert job.cancelled is False

    # A later result must still reach waiters.
    record = make_record("job-123")
    job.future.set_result(record)
    result = await pool.wait_for_result("job-123", timeout=1.0)
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_wait_for_result_timeout_does_not_break_concurrent_waiters():
    """One waiter timing out must not raise CancelledError in other waiters."""
    pool = WorkerPool(executor=MagicMock(), storage=MagicMock())
    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-123",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )

    async with pool._jobs_lock:
        pool._active_jobs[job.job_id] = job

    patient_waiter = asyncio.create_task(pool.wait_for_result("job-123", timeout=5.0))
    impatient_waiter = asyncio.create_task(pool.wait_for_result("job-123", timeout=0.05))

    with pytest.raises(asyncio.TimeoutError):
        await impatient_waiter

    job.future.set_result(make_record("job-123"))
    result = await patient_waiter
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_job_still_executes_after_wait_timeout():
    """A queued job survives a timed-out wait and is executed by the worker."""
    executor = MagicMock()
    storage = AsyncMock()
    pool = WorkerPool(executor=executor, storage=storage, max_workers=1, queue_wait_timeout=0.05)

    job_id = await pool.submit({"code": "print('hi')", "input_data": {}})
    executor.execute_job_with_record.return_value = make_record(job_id)

    # Wait times out before any worker has started.
    with pytest.raises(asyncio.TimeoutError):
        await pool.wait_for_result(job_id, timeout=0.05)

    # Grab the future before workers start (the job is removed from the
    # active map once a worker finishes it).
    async with pool._jobs_lock:
        future = pool._active_jobs[job_id].future

    await pool.start()
    try:
        result = await asyncio.wait_for(future, timeout=5.0)
    finally:
        await pool.stop(timeout=5.0)

    # The job ran (was not skipped as cancelled) and produced the real record.
    assert executor.execute_job_with_record.called
    assert result.execution_id == job_id
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_get_job_status_done_future_reports_record_status():
    """A done future must surface the record's terminal status, never 'completed'."""
    pool = WorkerPool(executor=MagicMock(), storage=MagicMock())
    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-123",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    async with pool._jobs_lock:
        pool._active_jobs[job.job_id] = job

    job.future.set_result(make_record("job-123", status="failed"))

    status = await pool.get_job_status("job-123")

    assert status is not None
    assert status["status"] == "failed"
    assert status["status"] != "completed"


@pytest.mark.asyncio
async def test_get_job_status_cancelled_future_reports_cancelled():
    """A cancelled future must map to the 'cancelled' API status."""
    pool = WorkerPool(executor=MagicMock(), storage=MagicMock())
    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-123",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    async with pool._jobs_lock:
        pool._active_jobs[job.job_id] = job

    job.future.cancel()

    status = await pool.get_job_status("job-123")

    assert status is not None
    assert status["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_persists_terminal_records_for_drained_jobs():
    """Graceful shutdown must persist a terminal record for each pending job."""
    storage = MagicMock()
    storage.save_record = AsyncMock()
    pool = WorkerPool(executor=MagicMock(), storage=storage)
    pool._started = True  # simulate a started pool without real worker tasks

    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-shutdown",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    pool._queue.put_nowait(job)
    pool._active_jobs[job.job_id] = job

    await pool.stop(timeout=0.1)

    storage.save_record.assert_awaited_once()
    record = storage.save_record.await_args.args[0]
    assert record.execution_id == "job-shutdown"
    assert record.status == "cancelled"
    assert record.error is not None
    assert record.error.type == "cancelled"
    assert "server shut down" in record.error.message
    # Record is persisted before the future is resolved/cancelled
    assert job.future.cancelled() is True


@pytest.mark.asyncio
async def test_stop_storage_failure_does_not_prevent_shutdown():
    """Persistence errors during shutdown are best-effort: shutdown completes."""
    storage = MagicMock()
    storage.save_record = AsyncMock(side_effect=RuntimeError("db unavailable"))
    pool = WorkerPool(executor=MagicMock(), storage=storage)
    pool._started = True

    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-db-down",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    pool._queue.put_nowait(job)

    await pool.stop(timeout=0.1)

    assert pool._started is False
    assert pool._queue.empty()
    assert job.future.cancelled() is True


@pytest.mark.asyncio
async def test_worker_loop_persists_running_transition_before_execute():
    """The worker must persist queued -> running before executing the job."""
    call_order = []

    storage = MagicMock()
    storage.save_record = AsyncMock()

    async def record_running(*args, **kwargs):
        call_order.append("mark_running")
        return True

    storage.mark_record_running = AsyncMock(side_effect=record_running)

    pool = WorkerPool(executor=MagicMock(), storage=storage, queue_wait_timeout=0.05)

    async def fake_execute(job):
        call_order.append("execute")
        return ExecutionRecord(
            execution_id=job.job_id,
            status="succeeded",
            created_at=datetime.now(timezone.utc),
            queued_at=datetime.now(timezone.utc),
            code_hash="a" * 64,
            input_hash="b" * 64,
        )

    pool._execute_job = fake_execute

    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-running",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    pool._active_jobs[job.job_id] = job
    pool._queue.put_nowait(job)

    worker = asyncio.create_task(pool._worker_loop(0))
    try:
        record = await asyncio.wait_for(job.future, timeout=2.0)
    finally:
        pool._shutdown = True
        await asyncio.wait_for(worker, timeout=2.0)

    assert record.status == "succeeded"
    assert call_order == ["mark_running", "execute"]
    storage.mark_record_running.assert_awaited_once_with("job-running", worker_id="worker-0")


@pytest.mark.asyncio
async def test_worker_loop_executes_even_if_running_persist_fails():
    """A failure persisting the running state must not block execution."""
    storage = MagicMock()
    storage.save_record = AsyncMock()
    storage.mark_record_running = AsyncMock(side_effect=RuntimeError("db unavailable"))

    pool = WorkerPool(executor=MagicMock(), storage=storage, queue_wait_timeout=0.05)

    async def fake_execute(job):
        return ExecutionRecord(
            execution_id=job.job_id,
            status="succeeded",
            created_at=datetime.now(timezone.utc),
            queued_at=datetime.now(timezone.utc),
            code_hash="a" * 64,
            input_hash="b" * 64,
        )

    pool._execute_job = fake_execute

    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-persist-fail",
        job_data={"code": "print('hi')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    pool._active_jobs[job.job_id] = job
    pool._queue.put_nowait(job)

    worker = asyncio.create_task(pool._worker_loop(0))
    try:
        record = await asyncio.wait_for(job.future, timeout=2.0)
    finally:
        pool._shutdown = True
        await asyncio.wait_for(worker, timeout=2.0)

    assert record.status == "succeeded"


def _patch_budget_probe(monkeypatch, job_id: str) -> dict:
    """Capture the watchdog timeout passed to asyncio.wait_for in _execute_job."""
    captured = {}

    class DummyLoop:
        def run_in_executor(self, executor, fn, arg):
            return asyncio.sleep(0, result=make_record(job_id))

    async def fake_wait_for(awaitable, timeout):
        captured["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: DummyLoop())
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    return captured


@pytest.mark.asyncio
async def test_watchdog_budget_uses_job_type_defaults(monkeypatch):
    """When job_data omits timeouts, the watchdog must use the job type's budget."""
    from tako_vm.job_types import JobType

    executor = MagicMock()
    executor.registry = MagicMock()
    executor.registry.get.return_value = JobType(
        name="ml-inference", timeout=120, startup_timeout=180
    )
    pool = WorkerPool(executor=executor, storage=MagicMock())
    pool._thread_pool = MagicMock()
    job = QueuedJob(
        job_id="job-jt",
        job_data={"job_type": "ml-inference", "code": "print('hi')", "input_data": {}},
        client_ip=None,
    )

    captured = _patch_budget_probe(monkeypatch, "job-jt")

    record = await pool._execute_job(job)

    assert record.execution_id == "job-jt"
    # 180 (startup) + 120 (execution) + 60 (orchestration buffer)
    assert captured["timeout"] == 360
    executor.registry.get.assert_called_once_with("ml-inference")


@pytest.mark.asyncio
async def test_watchdog_budget_strips_version_specifier(monkeypatch):
    """job_type@version must resolve the registry entry by bare name."""
    from tako_vm.job_types import JobType

    executor = MagicMock()
    executor.registry = MagicMock()
    executor.registry.get.return_value = JobType(
        name="data-processing", timeout=60, startup_timeout=180
    )
    pool = WorkerPool(executor=executor, storage=MagicMock())
    pool._thread_pool = MagicMock()
    job = QueuedJob(
        job_id="job-ver",
        job_data={"job_type": "data-processing@v2", "code": "x", "input_data": {}},
        client_ip=None,
    )

    captured = _patch_budget_probe(monkeypatch, "job-ver")

    await pool._execute_job(job)

    assert captured["timeout"] == 300
    executor.registry.get.assert_called_once_with("data-processing")


@pytest.mark.asyncio
async def test_watchdog_budget_explicit_timeouts_win(monkeypatch):
    """Explicit job_data timeouts override the job type's defaults."""
    from tako_vm.job_types import JobType

    executor = MagicMock()
    executor.registry = MagicMock()
    executor.registry.get.return_value = JobType(
        name="ml-inference", timeout=120, startup_timeout=180
    )
    pool = WorkerPool(executor=executor, storage=MagicMock())
    pool._thread_pool = MagicMock()
    job = QueuedJob(
        job_id="job-explicit",
        job_data={
            "job_type": "ml-inference",
            "timeout": 30,
            "startup_timeout": 90,
            "code": "x",
            "input_data": {},
        },
        client_ip=None,
    )

    captured = _patch_budget_probe(monkeypatch, "job-explicit")

    await pool._execute_job(job)

    assert captured["timeout"] == 180


@pytest.mark.asyncio
async def test_watchdog_budget_unknown_job_type_falls_back(monkeypatch):
    """Unknown job types fall back to the executor's built-in defaults."""
    executor = MagicMock()
    executor.registry = MagicMock()
    executor.registry.get.return_value = None
    pool = WorkerPool(executor=executor, storage=MagicMock())
    pool._thread_pool = MagicMock()
    job = QueuedJob(
        job_id="job-unknown",
        job_data={"job_type": "does-not-exist", "code": "x", "input_data": {}},
        client_ip=None,
    )

    captured = _patch_budget_probe(monkeypatch, "job-unknown")

    await pool._execute_job(job)

    # DEFAULT_JOB_TYPE budget: 120 (startup) + 30 (execution) + 60 (buffer)
    assert captured["timeout"] == 210


@pytest.mark.asyncio
async def test_watchdog_timeout_kills_container_and_returns_timeout_record(monkeypatch):
    """A watchdog timeout must kill the container and yield status='timeout'."""
    pool = WorkerPool(executor=MagicMock(), storage=MagicMock())
    pool._thread_pool = MagicMock()
    job = QueuedJob(
        job_id="job-wd",
        job_data={"timeout": 1, "startup_timeout": 1, "code": "x", "input_data": {}},
        client_ip=None,
    )

    kill_calls = []
    monkeypatch.setattr(
        "tako_vm.server.queue.kill_container", lambda name: kill_calls.append(name) or True
    )

    class DummyLoop:
        def run_in_executor(self, executor, fn, arg):
            return asyncio.sleep(0, result=make_record("job-wd"))

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()  # avoid 'coroutine never awaited' warning
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: DummyLoop())
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    record = await pool._execute_job(job)

    assert record.status == "timeout"
    assert record.error is not None
    assert record.error.type == "execution_timeout"
    assert "watchdog budget" in record.error.message
    assert kill_calls == ["tako-job-wd"]


@pytest.mark.asyncio
async def test_watchdog_timeout_saves_timeout_record_and_skips_dlq(monkeypatch):
    """End-to-end through the worker loop: watchdog timeout is not an internal error.

    The persisted record must have status 'timeout' (not failed/internal_error)
    and the job must NOT be pushed to the dead letter queue.
    """
    storage = MagicMock()
    storage.save_record = AsyncMock()
    storage.mark_record_running = AsyncMock()
    storage.add_to_dlq = AsyncMock()

    pool = WorkerPool(executor=MagicMock(), storage=storage, queue_wait_timeout=0.05)

    kill_calls = []
    monkeypatch.setattr(
        "tako_vm.server.queue.kill_container", lambda name: kill_calls.append(name) or True
    )

    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable, timeout):
        # The worker loop's queue wait and the test's own waits use small
        # timeouts; only the watchdog budget is >= 60s.
        if timeout >= 60:
            if asyncio.isfuture(awaitable):
                awaitable.cancel()
            raise asyncio.TimeoutError()
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-dlq",
        job_data={"code": "x", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    pool._active_jobs[job.job_id] = job
    pool._queue.put_nowait(job)

    worker = asyncio.create_task(pool._worker_loop(0))
    try:
        record = await asyncio.wait_for(job.future, timeout=5.0)
    finally:
        pool._shutdown = True
        await asyncio.wait_for(worker, timeout=5.0)

    assert record.status == "timeout"
    assert record.error is not None
    assert record.error.type == "execution_timeout"
    assert kill_calls == ["tako-job-dlq"]

    # Watchdog timeouts are budget overruns, not internal errors: no DLQ entry.
    storage.add_to_dlq.assert_not_awaited()
    saved = storage.save_record.await_args.args[0]
    assert saved.status == "timeout"


@pytest.mark.asyncio
async def test_submit_queue_full_race_releases_idempotency_key():
    """A QueueFull race must not strand the idempotency key on a failed record.

    The 503 the client receives is the canonical retry signal; the retry with
    the same key must not collide with the dead record.
    """
    storage = MagicMock()
    # submit() mutates and re-saves the same record object, so snapshot the
    # fields we care about at call time instead of inspecting await_args_list.
    saves = []

    async def capture_save(record):
        saves.append((record.status, record.idempotency_key, record.idempotency_fingerprint))

    storage.save_record = AsyncMock(side_effect=capture_save)
    pool = WorkerPool(executor=MagicMock(), storage=storage)

    # Simulate the TOCTOU race: full() pre-check passes, put_nowait raises.
    fake_queue = MagicMock()
    fake_queue.full.return_value = False
    fake_queue.put_nowait.side_effect = asyncio.QueueFull
    pool._queue = fake_queue

    job_data = {
        "code": "print('hi')",
        "input_data": {},
        "idempotency_key": "idem-123",
        "idempotency_fingerprint": "f" * 64,
    }

    with pytest.raises(RuntimeError, match="queue is full"):
        await pool.submit(job_data=job_data, client_ip="10.0.0.1")

    # Two saves: the preliminary queued record, then the failed rewrite.
    assert saves == [
        ("queued", "idem-123", "f" * 64),
        ("failed", None, None),
    ]

    # The final upsert keeps the failure visible for forensics.
    failed = storage.save_record.await_args_list[1].args[0]
    assert failed.error is not None
    assert failed.error.type == "service_unavailable"


@pytest.mark.asyncio
async def test_submit_queue_full_rejects_and_cleans_up_record():
    """A full queue is rejected solely by the put_nowait/QueueFull handler.

    The redundant full() pre-check was removed (it left a TOCTOU window where a
    queued record was persisted before the insert was attempted). The single
    handler still cleans up: it rewrites the preliminary record as failed and
    releases the idempotency key.
    """
    storage = MagicMock()
    saves = []

    async def capture_save(record):
        saves.append((record.status, record.idempotency_key))

    storage.save_record = AsyncMock(side_effect=capture_save)
    pool = WorkerPool(executor=MagicMock(), storage=storage, max_queue_size=1)
    loop = asyncio.get_running_loop()
    pool._queue.put_nowait(
        QueuedJob(
            job_id="job-occupies-slot",
            job_data={"code": "x", "input_data": {}},
            client_ip=None,
            future=loop.create_future(),
        )
    )

    with pytest.raises(RuntimeError, match="queue is full"):
        await pool.submit(
            job_data={"code": "y", "input_data": {}, "idempotency_key": "idem-456"},
            client_ip=None,
        )

    # Preliminary queued record, then the failed rewrite that frees the key.
    assert saves == [
        ("queued", "idem-456"),
        ("failed", None),
    ]


@pytest.mark.asyncio
async def test_submit_persists_correlation_id_on_preliminary_record():
    """submit() must persist job_data's correlation_id on the queued record."""
    storage = MagicMock()
    storage.save_record = AsyncMock()

    pool = WorkerPool(executor=MagicMock(), storage=storage)

    job_id = await pool.submit(
        {
            "code": "print('hi')",
            "input_data": {},
            "correlation_id": "corr-submit-123",
        }
    )

    storage.save_record.assert_awaited_once()
    saved = storage.save_record.await_args.args[0]
    assert saved.execution_id == job_id
    assert saved.status == "queued"
    assert saved.correlation_id == "corr-submit-123"


@pytest.mark.asyncio
async def test_worker_loop_clears_correlation_id_between_jobs():
    """A job without a correlation_id must not inherit the previous job's."""
    from tako_vm.server.correlation import get_correlation_id

    storage = MagicMock()
    storage.save_record = AsyncMock()
    storage.mark_record_running = AsyncMock()

    pool = WorkerPool(executor=MagicMock(), storage=storage, queue_wait_timeout=0.05)

    seen: dict[str, object] = {}

    async def fake_execute(job):
        seen[job.job_id] = get_correlation_id()
        return make_record(job.job_id)

    pool._execute_job = fake_execute

    loop = asyncio.get_running_loop()
    job_a = QueuedJob(
        job_id="job-with-corr",
        job_data={"code": "print('a')", "input_data": {}, "correlation_id": "corr-a"},
        client_ip=None,
        future=loop.create_future(),
    )
    job_b = QueuedJob(
        job_id="job-without-corr",
        job_data={"code": "print('b')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    for job in (job_a, job_b):
        pool._active_jobs[job.job_id] = job
        pool._queue.put_nowait(job)

    worker = asyncio.create_task(pool._worker_loop(0))
    try:
        await asyncio.wait_for(job_a.future, timeout=2.0)
        await asyncio.wait_for(job_b.future, timeout=2.0)
    finally:
        pool._shutdown = True
        await asyncio.wait_for(worker, timeout=2.0)

    assert seen["job-with-corr"] == "corr-a"
    assert seen["job-without-corr"] is None


@pytest.mark.asyncio
async def test_cancelled_skip_path_does_not_inherit_previous_correlation_id():
    """The pre-execution cancelled-skip path runs before the correlation_id is
    set for the job, so it must see a cleared context (not the previous job's
    id), and the cancelled record itself must carry the job's own (absent)
    correlation_id."""
    from tako_vm.server.correlation import get_correlation_id

    storage = MagicMock()
    storage.mark_record_running = AsyncMock()

    save_contexts: list[tuple[str, object]] = []

    async def capture_save(record):
        save_contexts.append((record.execution_id, get_correlation_id()))

    storage.save_record = AsyncMock(side_effect=capture_save)

    pool = WorkerPool(executor=MagicMock(), storage=storage, queue_wait_timeout=0.05)

    async def fake_execute(job):
        return make_record(job.job_id)

    pool._execute_job = fake_execute

    loop = asyncio.get_running_loop()
    job_a = QueuedJob(
        job_id="job-corr-first",
        job_data={"code": "print('a')", "input_data": {}, "correlation_id": "corr-a"},
        client_ip=None,
        future=loop.create_future(),
    )
    job_b = QueuedJob(
        job_id="job-cancelled-skip",
        job_data={"code": "print('b')", "input_data": {}},
        client_ip=None,
        future=loop.create_future(),
    )
    job_b.cancelled = True
    for job in (job_a, job_b):
        pool._active_jobs[job.job_id] = job
        pool._queue.put_nowait(job)

    worker = asyncio.create_task(pool._worker_loop(0))
    try:
        await asyncio.wait_for(job_a.future, timeout=2.0)
        record_b = await asyncio.wait_for(job_b.future, timeout=2.0)
    finally:
        pool._shutdown = True
        await asyncio.wait_for(worker, timeout=2.0)

    assert record_b.status == "cancelled"
    assert record_b.correlation_id is None

    contexts = dict(save_contexts)
    # Job A's terminal save happens while its correlation id is set.
    assert contexts["job-corr-first"] == "corr-a"
    # Job B's cancelled-skip save must NOT see job A's correlation id.
    assert contexts["job-cancelled-skip"] is None


@pytest.mark.asyncio
async def test_internal_error_dlq_entry_is_redacted():
    """Internal-error DLQ entries must hold a redacted forensic summary.

    The persisted entry keeps fingerprints/sizes/metadata but never the raw
    code string, input_data values, or the idempotency key itself.
    """
    from tako_vm.models import sha256_content, sha256_json

    storage = MagicMock()
    storage.save_record = AsyncMock()
    storage.mark_record_running = AsyncMock()
    storage.add_to_dlq = AsyncMock()

    pool = WorkerPool(executor=MagicMock(), storage=storage, queue_wait_timeout=0.05)

    async def boom(job):
        raise RuntimeError("executor exploded")

    pool._execute_job = boom

    secret_code = "import os; print(os.environ['AWS_SECRET_ACCESS_KEY'])"
    secret_input = {"api_key": "sk-live-supersecret", "nested": {"token": "tok-12345"}}

    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="job-redact",
        job_data={
            "code": secret_code,
            "input_data": secret_input,
            "idempotency_key": "idem-key-abc-123",
            "job_type": "default",
            "timeout": 30,
            "startup_timeout": 60,
            "requirements": ["numpy", "pandas"],
            "correlation_id": "corr-redact",
            "parent_execution_id": "parent-1",
        },
        client_ip=None,
        future=loop.create_future(),
    )
    pool._active_jobs[job.job_id] = job
    pool._queue.put_nowait(job)

    worker = asyncio.create_task(pool._worker_loop(0))
    try:
        record = await asyncio.wait_for(job.future, timeout=5.0)
    finally:
        pool._shutdown = True
        await asyncio.wait_for(worker, timeout=5.0)

    assert record.status == "failed"
    storage.add_to_dlq.assert_awaited_once()
    entry = storage.add_to_dlq.await_args.args[0]
    summary = entry.job_data

    # Forensic fingerprints, sizes, flags, and metadata are retained.
    assert summary["code_hash"] == sha256_content(secret_code)
    assert summary["input_hash"] == sha256_json(secret_input)
    assert summary["code_size_bytes"] == len(secret_code.encode("utf-8"))
    assert summary["input_size_bytes"] > 0
    assert summary["has_idempotency_key"] is True
    assert summary["job_type"] == "default"
    assert summary["timeout"] == 30
    assert summary["startup_timeout"] == 60
    assert summary["requirements"] == ["numpy", "pandas"]
    assert summary["correlation_id"] == "corr-redact"
    assert summary["parent_execution_id"] == "parent-1"

    # Raw payloads and the key itself never reach storage.
    assert "code" not in summary
    assert "input_data" not in summary
    assert "idempotency_key" not in summary
    serialized = entry.model_dump_json()
    assert secret_code not in serialized
    assert "sk-live-supersecret" not in serialized
    assert "tok-12345" not in serialized
    assert "idem-key-abc-123" not in serialized
