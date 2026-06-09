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
    """Pending-job cancellation should not poison the waiter future."""
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

    cancelled = await pool.cancel(job.job_id)

    assert cancelled is True
    assert job.cancelled is True
    assert job.future is not None
    assert job.future.cancelled() is False


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
