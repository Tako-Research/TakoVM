"""Tests for security validation helpers."""

from unittest.mock import MagicMock

import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.constants import UV_CACHE_VOLUME
from tako_vm.execution import CodeExecutor
from tako_vm.job_types import JobType
from tako_vm.security import (
    classify_error,
    sanitize_error,
    validate_docker_run_args,
    validate_execution_id,
    validate_pip_requirement,
)


class TestSanitizeError:
    def test_replaces_standalone_short_container_id(self):
        result = sanitize_error("Error in container 4e5021d210f6: exited")
        assert result == "Error in container <container-id>: exited"

    def test_replaces_full_container_id(self):
        full_id = "a" * 32 + "1" * 32
        result = sanitize_error(f"container {full_id} failed")
        assert result == "container <container-id> failed"

    def test_leaves_40_char_sha_intact(self):
        sha = "2c796c3f410f0396ba541855b8421d210f6a9e7b"
        assert sanitize_error(f"commit {sha} not found") == f"commit {sha} not found"

    def test_leaves_13_char_hex_intact(self):
        token = "4e5021d210f6a"
        assert sanitize_error(f"token {token} invalid") == f"token {token} invalid"

    def test_leaves_32_char_uuid_hex_intact(self):
        uuid_hex = "550e8400e29b41d4a716446655440000"
        assert sanitize_error(f"id {uuid_hex} missing") == f"id {uuid_hex} missing"

    def test_leaves_plain_12_digit_number_intact(self):
        assert (
            sanitize_error("timestamp 170000000000 is stale") == "timestamp 170000000000 is stale"
        )

    def test_empty_input(self):
        assert sanitize_error("") == ""


class TestClassifyError:
    def test_filenotfounderror_with_could_not_find_is_not_dependency_error(self):
        error_type, message = classify_error(1, "FileNotFoundError: could not find config file")
        assert error_type == "file_not_found"
        assert "config file" in message

    def test_genuine_pip_resolution_failure_is_dependency_error(self):
        error_type, _ = classify_error(
            1, "ERROR: Could not find a version that satisfies the requirement nopkg"
        )
        assert error_type == "dependency_error"

    def test_no_matching_distribution_is_dependency_error(self):
        error_type, _ = classify_error(1, "ERROR: No matching distribution found for nopkg")
        assert error_type == "dependency_error"

    def test_user_output_containing_killed_is_not_killed(self):
        error_type, _ = classify_error(1, "ValueError: the job was killed by the user")
        assert error_type == "value_error"

    def test_shell_killed_line_is_killed(self):
        error_type, _ = classify_error(
            1, "sh: line 1:    42 Killed                  python3 code.py"
        )
        assert error_type == "killed"

    def test_bare_killed_line_is_killed(self):
        error_type, _ = classify_error(1, "Killed\n")
        assert error_type == "killed"

    def test_oom_killed_is_killed(self):
        error_type, _ = classify_error(1, "container was OOMKilled")
        assert error_type == "killed"

    def test_sigkill_exit_code_is_oom(self):
        error_type, _ = classify_error(137, "")
        assert error_type == "oom"

    def test_timeout_flag_wins(self):
        error_type, _ = classify_error(1, "MemoryError", timed_out=True)
        assert error_type == "timeout"

    def test_generic_nonzero_exit_is_runtime_error(self):
        error_type, _ = classify_error(2, "something unexpected happened")
        assert error_type == "runtime_error"


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
            config=TakoVMConfig(
                container_runtime="runsc",
                security_mode="strict",
                allow_runtime_requirements=True,
            )
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

    def test_run_container_rejects_requirements_when_policy_disabled(self, monkeypatch, tmp_path):
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="strict")
        )
        code_dir = tmp_path / "code"
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        code_dir.mkdir()
        input_dir.mkdir()
        output_dir.mkdir()

        fake_run = MagicMock()
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

        assert result["success"] is False
        assert "Runtime dependency installation is disabled" in result["error"]
        assert not (input_dir / "_requirements.txt").exists()
        fake_run.assert_not_called()

    def test_run_container_adds_dependency_proxy_only_for_runtime_deps(self, monkeypatch, tmp_path):
        executor = CodeExecutor(
            config=TakoVMConfig(
                container_runtime="runsc",
                security_mode="strict",
                allow_runtime_requirements=True,
                dependency_proxy_url="https://proxy.example:8443",
            )
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
        assert "--env=TAKO_DEPENDENCY_PROXY_URL=https://proxy.example:8443" in captured["cmd"]
        assert not any(arg.startswith("--env=HTTP_PROXY=") for arg in captured["cmd"])
        assert not any(arg.startswith("--env=HTTPS_PROXY=") for arg in captured["cmd"])
        assert not any(arg.startswith("--env=ALL_PROXY=") for arg in captured["cmd"])

        captured.clear()
        (input_dir / "_requirements.txt").unlink()
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
        assert not any(
            arg.startswith("--env=TAKO_DEPENDENCY_PROXY_URL=") for arg in captured["cmd"]
        )

    @pytest.mark.parametrize(
        ("cache_enabled", "expect_cache_mount"),
        [(False, False), (True, True)],
    )
    def test_run_container_dependency_cache_is_opt_in(
        self, monkeypatch, tmp_path, cache_enabled, expect_cache_mount
    ):
        executor = CodeExecutor(
            config=TakoVMConfig(
                container_runtime="runsc",
                security_mode="strict",
                allow_runtime_requirements=True,
                enable_runtime_dependency_cache=cache_enabled,
            )
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

        cache_mount = f"--mount=type=volume,source={UV_CACHE_VOLUME},target=/root/.cache/uv"
        expected_cache_dir = "/root/.cache/uv" if cache_enabled else "/tmp/uv-cache"
        assert result["success"] is True
        assert (cache_mount in captured["cmd"]) is expect_cache_mount
        assert f"--env=UV_CACHE_DIR={expected_cache_dir}" in captured["cmd"]
