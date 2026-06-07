"""Tests for worker-pool cancellation and timeout bookkeeping."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from tako_vm.models import ExecutionRecord
from tako_vm.server.queue import QueuedJob, WorkerPool


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
