"""Regression tests for verbose, non-swallowing failure handling.

These cover failure modes that must surface loudly rather than crash opaquely
or leave stuck state:
  - an unexpected error in the API protection middleware (which runs *outside*
    FastAPI's exception handlers) -> sanitized 500 + ERROR log, not a raw crash
  - an exception in the worker's queued->running transition window -> the job
    must not be stranded in the in-memory tracking dicts, and its future must
    still resolve.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _build_app_with_failing_middleware(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tako_vm.config import TakoVMConfig
    from tako_vm.server.limits import ApiProtectionMiddleware

    cfg = TakoVMConfig(api_auth_enabled=False, api_rate_limit_enabled=True)

    app = FastAPI()

    @app.get("/execute")
    def _ep():  # non-exempt path so the protection logic runs
        return {"ok": True}

    # Inject a failure into the protection logic, which executes outside
    # FastAPI's own exception handlers.
    def boom(self, scope, identity):
        raise RuntimeError("injected: limiter identity resolution failed")

    monkeypatch.setattr(ApiProtectionMiddleware, "_get_client_identifier", boom)
    app.add_middleware(ApiProtectionMiddleware, config_getter=lambda: cfg)
    return TestClient(app, raise_server_exceptions=False)


def test_middleware_unexpected_error_returns_sanitized_500(monkeypatch, caplog):
    client = _build_app_with_failing_middleware(monkeypatch)
    with caplog.at_level("ERROR"):
        resp = client.get("/execute")

    assert resp.status_code == 500
    assert "Internal server error" in resp.text
    # Verbose-on-failure: our own correlated ERROR log, not just Starlette's.
    assert any(
        "Unhandled error in API protection middleware" in r.getMessage() for r in caplog.records
    )


def test_gvisor_probe_does_not_cache_transient_failure(monkeypatch):
    """A transient probe failure must NOT sticky-cache gVisor as unavailable —
    otherwise one flaky `docker info` degrades every later job to runc."""
    import subprocess

    import tako_vm.execution.worker as worker

    worker.reset_gvisor_check()

    # First call: probe raises (transient). Must return False but not cache.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("docker", 10)),
    )
    assert worker.check_gvisor_available() is False
    assert worker._gvisor_available is None  # not cached

    # Second call: docker recovers and reports runsc. Must now resolve True.
    class _OK:
        returncode = 0
        stdout = "map[runc:... runsc:...]"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _OK())
    assert worker.check_gvisor_available() is True
    worker.reset_gvisor_check()


def test_gvisor_probe_does_not_cache_docker_info_nonzero(monkeypatch):
    """`docker info` exiting non-zero (daemon not up) is also transient and
    must not be cached as 'gVisor unavailable'."""
    import subprocess

    import tako_vm.execution.worker as worker

    worker.reset_gvisor_check()

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "Cannot connect to the Docker daemon"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Fail())
    assert worker.check_gvisor_available() is False
    assert worker._gvisor_available is None
    worker.reset_gvisor_check()


def test_health_returns_503_before_worker_pool_ready():
    """/health must report a clear 503 'starting' before the worker pool is
    assigned, not an opaque 500 from accessing an unset attribute."""
    from fastapi.testclient import TestClient

    from tako_vm.server.app import app, state

    # Simulate pre-lifespan state: worker_pool not yet assigned.
    had_pool = hasattr(state, "worker_pool")
    saved = getattr(state, "worker_pool", None)
    if had_pool:
        del state.worker_pool
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "starting"
    finally:
        if had_pool:
            state.worker_pool = saved


def test_sdk_surfaces_deserialization_mismatch_on_result():
    """When a run succeeds but the output can't be coerced into the expected
    dataclass, the mismatch must be visible on the result object, not only logs."""
    from dataclasses import dataclass

    from tako_vm.sdk.client import TakoVM

    @dataclass
    class In:
        x: int

    @dataclass
    class Out:
        y: int

    def fn(a: In) -> Out:  # noqa: ARG001
        return Out(y=1)

    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = {
        "success": True,
        "output": {"WRONG_FIELD": 1},  # doesn't fit Out
        "execution_time": 0.1,
    }
    resp.text = ""
    session.request.return_value = resp

    client = TakoVM(session=session)
    result = client.send_raw(fn, In(x=1), timeout=5)
    assert result.success is True
    assert result.output == {"WRONG_FIELD": 1}  # raw dict preserved
    assert result.deserialization_error is not None
    assert "Out" in result.deserialization_error


@pytest.mark.asyncio
async def test_worker_exception_in_running_transition_does_not_strand_job(monkeypatch, caplog):
    import tako_vm.server.queue as qmod
    from tako_vm.server.queue import QueuedJob, WorkerPool

    pool = WorkerPool(executor=MagicMock(), storage=AsyncMock())

    # Fail AFTER the job is placed in _running_jobs but BEFORE the inner
    # try/finally that normally cleans it up (set_correlation_id).
    def boom(_cid):
        raise RuntimeError("injected: set_correlation_id failed mid-transition")

    monkeypatch.setattr(qmod, "set_correlation_id", boom)

    loop = asyncio.get_running_loop()
    job = QueuedJob(
        job_id="adv-stuck-1",
        job_data={"code": "x", "input_data": {}, "correlation_id": "cid-adv"},
        client_ip=None,
        future=loop.create_future(),
    )
    await pool._queue.put(job)
    async with pool._jobs_lock:
        pool._active_jobs[job.job_id] = job

    worker = asyncio.create_task(pool._worker_loop(0))
    try:
        with caplog.at_level("ERROR"):
            record = await asyncio.wait_for(job.future, timeout=5)
    finally:
        pool._shutdown = True
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    # Client is not left hanging, and the failure is recorded + logged.
    assert record is not None
    assert record.status == "failed"
    assert any("unexpected error" in r.getMessage().lower() for r in caplog.records)
    # The job is not stranded in either tracking dict.
    assert job.job_id not in pool._running_jobs
    assert job.job_id not in pool._active_jobs
