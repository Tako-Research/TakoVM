"""Tests for security validation helpers."""

from unittest.mock import MagicMock

import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution import CodeExecutor
from tako_vm.job_types import JobType
from tako_vm.security import (
    validate_docker_run_args,
    validate_execution_id,
    validate_pip_requirement,
)


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


class TestValidateDockerRunArgs:
    def test_accepts_safe_docker_run_argv(self, tmp_path):
        code_dir = tmp_path / "code"
        code_dir.mkdir()

        assert (
            validate_docker_run_args(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name=tako-job-123",
                    "--network=none",
                    f"--mount=type=bind,source={code_dir.resolve()},target=/code,readonly",
                    "--env=TAKO_EXECUTION_TIMEOUT=30",
                    "code-executor:latest",
                ]
            )
            is True
        )

    @pytest.mark.parametrize(
        "args",
        [
            ["podman", "run", "code-executor:latest"],
            ["docker", "run", "bad\narg", "code-executor:latest"],
            ["docker", "run", "--env=NAME=value with spaces", "code-executor:latest"],
            ["docker", "run", "", "code-executor:latest"],
        ],
    )
    def test_rejects_unsafe_docker_run_argv(self, args):
        assert validate_docker_run_args(args) is False


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

    def test_run_container_receives_startup_and_execution_timeouts(self, monkeypatch, tmp_path):
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="strict")
        )
        code_dir = tmp_path / "code"
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        code_dir.mkdir()
        input_dir.mkdir()
        output_dir.mkdir()

        captured = {}

        def fake_run(cmd, timeout, capture_output, text, check):
            captured["cmd"] = cmd
            captured["timeout"] = timeout
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(worker_module.subprocess, "run", fake_run)

        result = executor._run_container(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
            startup_timeout=45,
            job_type=JobType(name="default", requirements=[]),
            job_id="job-123",
        )

        assert result["success"] is True
        assert captured["timeout"] == 80
        assert "--env=TAKO_STARTUP_TIMEOUT=45" in captured["cmd"]
        assert "--env=TAKO_EXECUTION_TIMEOUT=30" in captured["cmd"]

    def test_run_container_writes_requirements_file_not_env(self, monkeypatch, tmp_path):
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="strict")
        )
        code_dir = tmp_path / "code"
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        code_dir.mkdir()
        input_dir.mkdir()
        output_dir.mkdir()

        captured = {}

        def fake_run(cmd, timeout, capture_output, text, check):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(worker_module.subprocess, "run", fake_run)

        result = executor._run_container(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
            startup_timeout=45,
            job_type=JobType(name="default", requirements=[]),
            extra_requirements=["requests>=2.31"],
            job_id="job-123",
        )

        assert result["success"] is True
        assert not any(arg.startswith("--env=TAKO_REQUIREMENTS=") for arg in captured["cmd"])
        assert "--network=bridge" in captured["cmd"]
        assert (input_dir / "_requirements.txt").read_text(encoding="utf-8") == "requests>=2.31\n"
