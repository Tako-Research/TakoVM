"""
Error plumbing tests for CodeExecutor._run_container / execute_job_with_record.

Covers:

1. Docker infrastructure failures (daemon down, `docker run` exit 125) were
   recorded as circuit breaker *successes*, returned without an "error" key
   (so the retry loop never fired), and classified against the user's code.

2. Host-level subprocess timeouts returned a dict with empty output and no
   marker, so jobs killed by the host got status "failed" instead of
   "timeout" and the partial stdout/stderr captured before the kill was
   discarded.

3. Retry idempotency: a retry reused the exact container name of the failed
   attempt (colliding with a not-yet-removed `--rm` container) and inherited
   the failed attempt's leftover output dir contents; record.attempt /
   record.max_attempts never reflected that retries happened.

4. Truncation flags: cap_output truncated stdout/stderr in-band but
   record.stdout_truncated / stderr_truncated were never set.

All tests mock subprocess.run — no Docker required.
"""

import subprocess
from pathlib import Path
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


def _output_mount_dir(cmd):
    """Extract the host source dir of the /output bind mount from a docker run cmd."""
    for arg in cmd:
        if arg.startswith("--mount=type=bind,") and arg.endswith(",target=/output"):
            return Path(arg.split("source=", 1)[1].split(",", 1)[0])
    raise AssertionError("docker run command has no /output bind mount")


def _patch_docker_cli(monkeypatch, run_results, on_run=None):
    """Fake subprocess.run that distinguishes `docker run` from `docker rm`.

    `docker run` invocations consume `run_results` in order (calling `on_run`
    with (attempt_index, cmd) first, e.g. to seed stale files into the mounted
    output dir); `docker rm` invocations are recorded and succeed. Any other
    docker command fails the test.

    Returns (run_cmds, rm_cmds) lists, appended to as calls happen.
    """
    run_cmds = []
    rm_cmds = []
    results = iter(run_results)

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["docker", "run"]:
            if on_run:
                on_run(len(run_cmds), cmd)
            run_cmds.append(cmd)
            return next(results)
        if cmd[:2] == ["docker", "rm"]:
            rm_cmds.append(cmd)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected docker command: {cmd}")

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)
    return run_cmds, rm_cmds


def _infra_failure():
    return SimpleNamespace(returncode=125, stdout="", stderr=DAEMON_DOWN_STDERR)


def _success(stdout="ok", stderr=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


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
        run_cmds, _ = _patch_docker_cli(monkeypatch, [_infra_failure(), _infra_failure()])

        record = executor.execute_job_with_record(
            "job-infra-1", {"code": "print('hi')", "input_data": {}}
        )

        # Retry loop fired: both configured attempts hit Docker
        assert len(run_cmds) == 2
        assert len(breaker.failures) == 2
        assert breaker.successes == 0

        # Classified as an infra/service error, not the user's code
        assert record.status == "failed"
        assert record.error is not None
        assert record.error.type == "service_unavailable"
        assert "Docker infrastructure failure" in record.error.message

        # The audit record shows that retries happened
        assert record.attempt == 1
        assert record.max_attempts == 2


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


class TestRetryIdempotency:
    """Retries must not collide on container names or report stale outputs."""

    def test_retry_uses_unique_container_name_and_removes_previous(
        self, executor, breaker, monkeypatch
    ):
        """Attempt 2 gets a -r1 name and the attempt-1 container is force-removed first."""
        run_cmds, rm_cmds = _patch_docker_cli(monkeypatch, [_infra_failure(), _success()])

        record = executor.execute_job_with_record(
            "job-retry-name", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert len(run_cmds) == 2
        names = [next(a for a in cmd if a.startswith("--name=")) for cmd in run_cmds]
        # Attempt 0 keeps the deterministic name (cancel/watchdog paths match
        # it); the retry gets a unique suffix so a lingering --rm container
        # from attempt 0 cannot cause a "name already in use" failure.
        assert names == ["--name=tako-job-retry-name", "--name=tako-job-retry-name-r1"]
        # Best-effort cleanup of the previous attempt's container fired
        assert ["docker", "rm", "-f", "tako-job-retry-name"] in rm_cmds

    def test_stale_output_cleared_between_attempts(self, executor, breaker, monkeypatch):
        """Leftovers from a failed attempt must not be reported as retry results."""
        seen_on_retry = {}

        def on_run(attempt_idx, cmd):
            out_dir = _output_mount_dir(cmd)
            if attempt_idx == 0:
                # Simulate a failed attempt that left partial output behind
                (out_dir / "result.json").write_text('{"stale": true}')
                (out_dir / ".tako_phase").write_text("phase=failed\nfailed_phase=startup\n")
                (out_dir / "stale-artifact.txt").write_text("leftover")
            else:
                seen_on_retry["entries"] = sorted(p.name for p in out_dir.iterdir())

        _patch_docker_cli(monkeypatch, [_infra_failure(), _success()], on_run=on_run)

        record = executor.execute_job_with_record(
            "job-retry-clean", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert seen_on_retry["entries"] == [], "output dir must be empty when a retry starts"
        # Nothing stale leaked into the record
        assert record.result_json is None
        assert record.artifacts == []
        assert record.timing is None

    def test_attempt_and_max_attempts_recorded_on_retry(self, executor, breaker, monkeypatch):
        """A successful retry is visible in the audit record."""
        _patch_docker_cli(monkeypatch, [_infra_failure(), _success()])

        record = executor.execute_job_with_record(
            "job-retry-attempt", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert record.attempt == 1
        assert record.max_attempts == 2

    def test_attempt_zero_when_no_retry_needed(self, executor, breaker, monkeypatch):
        """A first-attempt success records attempt 0 with the configured ceiling."""
        run_cmds, rm_cmds = _patch_docker_cli(monkeypatch, [_success()])

        record = executor.execute_job_with_record(
            "job-no-retry", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert record.attempt == 0
        assert record.max_attempts == 2
        assert len(run_cmds) == 1
        assert rm_cmds == [], "no retry means no defensive container removal"


class TestTruncationFlags:
    """cap_output truncation must be surfaced via record.stdout/stderr_truncated."""

    def test_stdout_truncation_sets_flag(self, executor, breaker, monkeypatch):
        big_stdout = "x" * (executor.config.max_stdout_bytes + 1000)
        _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(returncode=0, stdout=big_stdout, stderr=""),
        )

        record = executor.execute_job_with_record(
            "job-trunc-out", {"code": "print('x')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert record.stdout_truncated is True
        assert record.stderr_truncated is False
        assert len(record.stdout.encode("utf-8")) <= executor.config.max_stdout_bytes
        assert "[TRUNCATED" in record.stdout

    def test_stderr_truncation_sets_flag(self, executor, breaker, monkeypatch):
        big_stderr = "e" * (executor.config.max_stderr_bytes + 1000)
        _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(returncode=0, stdout="ok", stderr=big_stderr),
        )

        record = executor.execute_job_with_record(
            "job-trunc-err", {"code": "print('x')", "input_data": {}}
        )

        assert record.stdout_truncated is False
        assert record.stderr_truncated is True
        assert len(record.stderr.encode("utf-8")) <= executor.config.max_stderr_bytes
        assert "[TRUNCATED" in record.stderr

    def test_flags_false_when_output_within_limits(self, executor, breaker, monkeypatch):
        _patch_subprocess(
            monkeypatch,
            lambda: SimpleNamespace(returncode=0, stdout="small out", stderr="small err"),
        )

        record = executor.execute_job_with_record(
            "job-no-trunc", {"code": "print('x')", "input_data": {}}
        )

        assert record.stdout_truncated is False
        assert record.stderr_truncated is False
        assert record.stdout == "small out"
        assert record.stderr == "small err"
