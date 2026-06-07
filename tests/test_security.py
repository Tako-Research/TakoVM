"""Tests for security validation helpers."""

import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution import CodeExecutor
from tako_vm.security import validate_execution_id, validate_pip_requirement


class TestValidateExecutionId:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "api-1700000000000",
            "job_v1.2",
            "abc123-def456",
            "record-01",
            "idem-test",
            "failed-job-123",
        ],
    )
    def test_accepts_existing_repo_id_formats(self, value):
        assert validate_execution_id(value) is True

    @pytest.mark.parametrize(
        "value",
        ["", ".", "..", "../escape", "nested/path", r"windows\\path", ".hidden", "a" * 65],
    )
    def test_rejects_unsafe_ids(self, value):
        assert validate_execution_id(value) is False


class TestValidatePipRequirement:
    @pytest.mark.parametrize(
        "value",
        [
            "numpy",
            "pandas>=2.0",
            "numpy>=1.20",
            "requests[security]",
            "flask[async]",
            "pkg[one,two]!=1.0,>=0.9",
        ],
    )
    def test_accepts_supported_requirement_forms(self, value):
        assert validate_pip_requirement(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "git+https://example.com/pkg",
            "pkg @ https://example.com/pkg.whl",
            "pkg; python_version>'3.10'",
            "pkg[",
            "pkg[]",
            "pkg>=,1.0",
        ],
    )
    def test_rejects_unsafe_or_malformed_forms(self, value):
        assert validate_pip_requirement(value) is False


class TestExecutorRejectsUnsafeIds:
    @pytest.fixture(autouse=True)
    def mock_gvisor_available(self, monkeypatch):
        monkeypatch.setattr(worker_module, "_gvisor_available", True)

    def test_execute_job_rejects_path_traversal_ids(self):
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="strict")
        )

        with pytest.raises(ValueError, match="Execution ID must be"):
            executor.execute_job({"id": "../escape", "code": "print('hi')", "input_data": {}})

    def test_execute_job_with_record_accepts_existing_repo_id_formats(self):
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="strict")
        )

        record = executor.execute_job_with_record(
            "550e8400-e29b-41d4-a716-446655440000",
            {"code": "print('hi')", "input_data": {}},
        )

        assert record.execution_id == "550e8400-e29b-41d4-a716-446655440000"
        assert record.status in {"queued", "failed", "running", "succeeded"}
