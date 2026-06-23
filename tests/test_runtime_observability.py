"""
Tests for per-job runtime (gVisor vs runc) observability (issue #99).

The effective isolation runtime each job ran under must be recorded on the
ExecutionRecord and surfaced in the API responses, so a caller can prove after
the fact whether a job had the gVisor boundary before trusting its output.

These are pure unit tests: the worker stamping is exercised via the
job-type-not-found early return (no container launch), and the response mapping
is a pure function. The storage round-trip is covered in test_storage.py (CI).
"""

from types import SimpleNamespace

import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution.worker import CodeExecutor
from tako_vm.models import ExecutionRecord
from tako_vm.server.app import ExecuteResponse, ExecutionRecordResponse


@pytest.fixture
def gvisor_executor(tmp_path, monkeypatch):
    """A CodeExecutor that resolves to the gVisor (runsc) runtime."""
    monkeypatch.setattr(worker_module, "_gvisor_available", True)
    monkeypatch.setattr(
        worker_module,
        "get_circuit_breaker",
        lambda: SimpleNamespace(
            is_available=True,
            record_success=lambda *a, **k: None,
            record_failure=lambda *a, **k: None,
        ),
    )
    config = TakoVMConfig(
        security_mode="permissive",
        production_mode=True,
        data_dir=str(tmp_path / "data"),
    )
    return CodeExecutor(config=config)


class TestWorkerStampsRuntime:
    def test_record_carries_effective_runtime(self, gvisor_executor):
        # production_mode + an unknown job type returns the record early (no
        # container launch), but the runtime is stamped at record construction,
        # so it is present on that early-return record.
        record = gvisor_executor.execute_job_with_record(
            "job-runtime-1",
            {"code": "print(1)", "job_type": "does-not-exist"},
        )

        assert gvisor_executor._runtime == "runsc"
        assert record.runtime == "runsc"
        # Sanity: this is the early-return failure path, not a real run.
        assert record.status == "failed"

    def test_runtime_matches_executor_resolution(self, gvisor_executor):
        # The recorded value must be exactly the runtime the container would use,
        # so the record can never disagree with what actually ran.
        record = gvisor_executor.execute_job_with_record(
            "job-runtime-2",
            {"code": "print(1)", "job_type": "does-not-exist"},
        )
        assert record.runtime == gvisor_executor._runtime


class TestResponsesSurfaceRuntime:
    def _record(self, runtime):
        return ExecutionRecord(
            execution_id="rec-1",
            status="succeeded",
            job_type="default",
            code_hash="a" * 64,
            input_hash="b" * 64,
            runtime=runtime,
        )

    def test_execution_record_response_includes_runtime(self):
        resp = ExecutionRecordResponse.from_record(self._record("runsc"))
        assert resp.runtime == "runsc"

    def test_execution_record_response_runtime_defaults_none(self):
        # Legacy records (no runtime) surface as None, not an error.
        resp = ExecutionRecordResponse.from_record(self._record(None))
        assert resp.runtime is None

    def test_execute_response_accepts_runtime(self):
        resp = ExecuteResponse(success=True, execution_time=1.0, runtime="runc")
        assert resp.runtime == "runc"
