"""
Error plumbing tests for CodeExecutor._run_container / execute_job_with_record.

Covers two bugs:

1. Docker infrastructure failures (daemon down, `docker run` exit 125) were
   recorded as circuit breaker *successes*, returned without an "error" key
   (so the retry loop never fired), and classified against the user's code.

2. Host-level subprocess timeouts returned a dict with empty output and no
   marker, so jobs killed by the host got status "failed" instead of
   "timeout" and the partial stdout/stderr captured before the kill was
   discarded.

All tests mock subprocess.run — no Docker required.
"""

import subprocess
from types import SimpleNamespace

import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution.worker import CodeExecutor
from tako_vm.job_types import JobType

DAEMON_DOWN_STDERR = (
    "docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)


class FakeCircuitBreaker:
    """Records breaker calls; always reports available."""

    def __init__(self):
        self.successes = 0
        self.failures = []

    @property
    def is_available(self):
        return True

    def record_success(self):
        self.successes += 1

    def record_failure(self, error=None):
        self.failures.append(error)


@pytest.fixture(autouse=True)
def mock_gvisor_available(monkeypatch):
    monkeypatch.setattr(worker_module, "_gvisor_available", True)


@pytest.fixture
def breaker(monkeypatch):
    fake = FakeCircuitBreaker()
    monkeypatch.setattr(worker_module, "get_circuit_breaker", lambda: fake)
    return fake


@pytest.fixture
def executor(tmp_path):
    config = TakoVMConfig(
        security_mode="permissive",
        data_dir=str(tmp_path / "data"),
        max_retry_attempts=2,
        retry_base_delay=0.1,
    )
    return CodeExecutor(config=config)


@pytest.fixture
def io_dirs(tmp_path):
    dirs = []
    for name in ("code", "input", "output"):
        d = tmp_path / name
        d.mkdir()
        dirs.append(d)
    return dirs


def _run_container(executor, io_dirs):
    code_dir, input_dir, output_dir = io_dirs
    return executor._run_container(
        code_dir=code_dir,
        input_dir=input_dir,
        output_dir=output_dir,
        timeout=30,
        startup_timeout=45,
        job_type=JobType(name="default", requirements=[]),
        job_id="job-err-test",
    )


def _patch_subprocess(monkeypatch, side_effect):
    """Replace subprocess.run in the worker module; returns the call counter."""
    calls = {"count": 0}

    def fake_run(*args, **kwargs):
        calls["count"] += 1
        result = side_effect()
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)
    return calls


class TestDockerInfraFailure:
    """`docker run` failing (exit 125 / daemon unreachable) is an infra failure."""

    def test_exit_125_records_breaker_failure_and_returns_error_key(
        self, executor, breaker, io_dirs, monkeypatch
    ):
        _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(returncode=125, stdout="", stderr=DAEMON_DOWN_STDERR),
        )

        result = _run_container(executor, io_dirs)

        assert result["success"] is False
        assert "Docker infrastructure failure" in result["error"]
        assert "Cannot connect to the Docker daemon" in result["error"]
        assert result["infra_failure"] is True
        assert breaker.failures, "infra failure must count against the circuit breaker"
        assert breaker.successes == 0, "infra failure must NOT reset the circuit breaker"

    def test_daemon_stderr_pattern_with_nonzero_exit_is_infra_failure(
        self, executor, breaker, io_dirs, monkeypatch
    ):
        _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="error during connect: this error may indicate the docker daemon is not running",
            ),
        )

        result = _run_container(executor, io_dirs)

        assert result["success"] is False
        assert "Docker infrastructure failure" in result["error"]
        assert len(breaker.failures) == 1
        assert breaker.successes == 0

    def test_daemon_down_retries_and_record_is_service_unavailable(
        self, executor, breaker, monkeypatch
    ):
        calls = _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(returncode=125, stdout="", stderr=DAEMON_DOWN_STDERR),
        )

        record = executor.execute_job_with_record(
            "job-infra-1", {"code": "print('hi')", "input_data": {}}
        )

        # Retry loop fired: both configured attempts hit Docker
        assert calls["count"] == 2
        assert len(breaker.failures) == 2
        assert breaker.successes == 0

        # Classified as an infra/service error, not the user's code
        assert record.status == "failed"
        assert record.error is not None
        assert record.error.type == "service_unavailable"
        assert "Docker infrastructure failure" in record.error.message


class TestHostTimeout:
    """Host-level subprocess timeout must become status='timeout' with partial output."""

    def test_run_container_preserves_partial_output(self, executor, breaker, io_dirs, monkeypatch):
        monkeypatch.setattr(worker_module, "kill_container", lambda name: None)
        # TimeoutExpired carries the bytes captured before the kill
        _patch_subprocess(
            monkeypatch,
            lambda: subprocess.TimeoutExpired(
                ["docker", "run"], 80, output=b"partial stdout", stderr=b"partial stderr"
            ),
        )

        result = _run_container(executor, io_dirs)

        assert result["success"] is False
        assert result["timed_out"] is True
        assert result["stdout"] == "partial stdout"
        assert result["stderr"] == "partial stderr"
        assert "timeout" in result["error"].lower()
        # Host timeout is not a Docker health problem
        assert breaker.failures == []

    def test_record_status_is_timeout_and_no_retry(self, executor, breaker, monkeypatch):
        killed = []
        monkeypatch.setattr(worker_module, "kill_container", killed.append)
        calls = _patch_subprocess(
            monkeypatch,
            lambda: subprocess.TimeoutExpired(
                ["docker", "run"], 80, output=b"got this far", stderr=None
            ),
        )

        record = executor.execute_job_with_record(
            "job-timeout-1", {"code": "while True: pass", "input_data": {}, "timeout": 30}
        )

        assert calls["count"] == 1, "host timeout must not be retried"
        assert killed, "orphaned container must be killed"
        assert record.status == "timeout"
        assert record.error is not None
        assert "timeout" in record.error.message.lower() or "time limit" in record.error.message
        assert record.stdout == "got this far"


class TestUserCodeFailure:
    """Non-zero exits from user code are not infra failures and never retry."""

    def test_nonzero_user_exit_records_success_no_retry(self, executor, breaker, monkeypatch):
        calls = _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Traceback (most recent call last):\n  ...\nValueError: boom",
            ),
        )

        record = executor.execute_job_with_record(
            "job-user-1", {"code": "raise ValueError('boom')", "input_data": {}}
        )

        assert calls["count"] == 1, "user-code failures must not retry"
        assert breaker.successes == 1, "container completed: Docker is healthy"
        assert breaker.failures == []
        assert record.status == "failed"
        assert record.error is not None
        assert record.error.type == "value_error"

    def test_successful_run_records_success(self, executor, breaker, monkeypatch):
        _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        )

        record = executor.execute_job_with_record(
            "job-ok-1", {"code": "print('ok')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert breaker.successes == 1
        assert breaker.failures == []
