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

5. OOM detection: exit code 137 was unconditionally reported as "oom", but
   137 is just SIGKILL — `docker kill` (cancel), pids-limit kills, and user
   sys.exit(137) looked identical, and the authoritative State.OOMKilled flag
   was unreadable because `--rm` removed the container before inspection.

6. Phase-file trust: .tako_phase lived in the 0777 /output mount, so the
   sandbox user could unlink/re-create it and forge the timing/phase data
   that feeds status determination. It now lives in the root-only /tako-meta
   mount when available.

All tests mock subprocess.run — no Docker required.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution.worker import CodeExecutor, parse_phase_file
from tako_vm.job_types import JobType, JobTypeRegistry

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


@pytest.fixture(autouse=True)
def no_prebuilt_images(monkeypatch):
    """Keep image resolution hermetic: behave as if no pre-built job-type
    image exists and no image can be entrypoint-inspected (the helpers live in
    docker.py and would otherwise hit the real daemon). TestImageResolution
    overrides these per test."""
    monkeypatch.setattr(worker_module, "image_exists", lambda name: False)
    monkeypatch.setattr(worker_module, "image_has_executor_entrypoint", lambda name: None)


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
    """Replace subprocess.run in the worker module; returns the call counter.

    Only `docker run` invocations consume `side_effect` and increment the
    counter: cleanup `docker rm -f` (every run now removes its own container,
    since --rm was dropped to allow OOM inspection) and `docker inspect`
    calls are answered with a benign failure so they stay invisible to tests
    that only care about how often the container ran.
    """
    calls = {"count": 0}

    def fake_run(cmd, *args, **kwargs):
        prefix = list(cmd[:2])
        if prefix == ["docker", "rm"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if prefix == ["docker", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
        calls["count"] += 1
        result = side_effect()
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)
    return calls


def _mount_source_dir(cmd, target):
    """Extract the host source dir of a bind mount from a docker run cmd.

    Parses the --mount key=value fields so it also matches mounts carrying
    trailing flags (e.g. the readonly /code and /input mounts).
    """
    for arg in cmd:
        if not arg.startswith("--mount=type=bind,"):
            continue
        fields = dict(
            field.split("=", 1) for field in arg[len("--mount=") :].split(",") if "=" in field
        )
        if fields.get("target") == target:
            return Path(fields["source"])
    raise AssertionError(f"docker run command has no {target} bind mount")


def _output_mount_dir(cmd):
    """Extract the host source dir of the /output bind mount from a docker run cmd."""
    return _mount_source_dir(cmd, "/output")


def _meta_mount_dir(cmd):
    """Extract the host source dir of the /tako-meta bind mount from a docker run cmd."""
    return _mount_source_dir(cmd, "/tako-meta")


def _patch_docker_cli(monkeypatch, run_results, on_run=None, inspect_result=None):
    """Fake subprocess.run that dispatches on the docker subcommand.

    `docker run` invocations consume `run_results` in order (calling `on_run`
    with (attempt_index, cmd) first, e.g. to seed stale files into the mounted
    output dir); an item that is an exception is raised instead of returned.
    `docker rm` / `docker kill` invocations are recorded and succeed.
    `docker inspect` invocations are recorded and return `inspect_result`
    (default: a "no such object" failure). Any other docker command fails
    the test.

    Returns a SimpleNamespace with `run`, `rm`, `kill`, `inspect` command
    lists plus `all` (every command, in call order).
    """
    cli = SimpleNamespace(run=[], rm=[], kill=[], inspect=[], all=[])
    results = iter(run_results)

    def fake_run(cmd, *args, **kwargs):
        cli.all.append(cmd)
        prefix = list(cmd[:2])
        if prefix == ["docker", "run"]:
            if on_run:
                on_run(len(cli.run), cmd)
            cli.run.append(cmd)
            result = next(results)
            if isinstance(result, BaseException):
                raise result
            return result
        if prefix == ["docker", "rm"]:
            cli.rm.append(cmd)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if prefix == ["docker", "kill"]:
            cli.kill.append(cmd)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if prefix == ["docker", "inspect"]:
            cli.inspect.append(cmd)
            if inspect_result is not None:
                return inspect_result
            return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
        raise AssertionError(f"unexpected docker command: {cmd}")

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)
    return cli


def _infra_failure():
    return SimpleNamespace(returncode=125, stdout="", stderr=DAEMON_DOWN_STDERR)


def _success(stdout="ok", stderr=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def _exit_137(stderr=""):
    return SimpleNamespace(returncode=137, stdout="", stderr=stderr)


def _inspect_says(value):
    """A successful `docker inspect --format {{.State.OOMKilled}}` result."""
    return SimpleNamespace(returncode=0, stdout=f"{value}\n", stderr="")


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
        cli = _patch_docker_cli(monkeypatch, [_infra_failure(), _infra_failure()])

        record = executor.execute_job_with_record(
            "job-infra-1", {"code": "print('hi')", "input_data": {}}
        )

        # Retry loop fired: both configured attempts hit Docker
        assert len(cli.run) == 2
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
        cli = _patch_docker_cli(monkeypatch, [_infra_failure(), _success()])

        record = executor.execute_job_with_record(
            "job-retry-name", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert len(cli.run) == 2
        names = [next(a for a in cmd if a.startswith("--name=")) for cmd in cli.run]
        # Attempt 0 keeps the deterministic name (cancel/watchdog paths match
        # it); the retry gets a unique suffix so a lingering container from
        # attempt 0 cannot cause a "name already in use" failure.
        assert names == ["--name=tako-job-retry-name", "--name=tako-job-retry-name-r1"]
        # Best-effort cleanup of the previous attempt's container fired
        assert ["docker", "rm", "-f", "tako-job-retry-name"] in cli.rm
        # The retry's own container is removed too (no --rm anymore)
        assert ["docker", "rm", "-f", "tako-job-retry-name-r1"] in cli.rm

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
        cli = _patch_docker_cli(monkeypatch, [_success()])

        record = executor.execute_job_with_record(
            "job-no-retry", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert record.attempt == 0
        assert record.max_attempts == 2
        assert len(cli.run) == 1
        # No --rm anymore: the run's own container is removed exactly once,
        # and no defensive pre-retry removal happened.
        assert cli.rm == [["docker", "rm", "-f", "tako-job-no-retry"]]


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


class TestOomDetection:
    """Exit 137 must be verified against State.OOMKilled, not assumed to be OOM."""

    def test_exit_137_with_oomkilled_true_is_oom(self, executor, breaker, monkeypatch):
        """A genuine OOM kill (inspect says OOMKilled=true) reports status='oom'."""
        cli = _patch_docker_cli(monkeypatch, [_exit_137()], inspect_result=_inspect_says("true"))

        record = executor.execute_job_with_record(
            "job-oom-true", {"code": "x = 'a' * 10**12", "input_data": {}}
        )

        assert record.status == "oom"
        assert record.error is not None
        assert record.error.type == "oom"
        assert "memory" in record.error.message.lower()
        # The right container was inspected
        assert cli.inspect == [
            ["docker", "inspect", "--format", "{{.State.OOMKilled}}", "tako-job-oom-true"]
        ]

    def test_exit_137_with_oomkilled_false_is_failed_killed(self, executor, breaker, monkeypatch):
        """A non-OOM SIGKILL (docker kill, pids-limit, sys.exit(137)) is NOT 'oom'."""
        cli = _patch_docker_cli(monkeypatch, [_exit_137()], inspect_result=_inspect_says("false"))

        record = executor.execute_job_with_record(
            "job-oom-false", {"code": "import sys; sys.exit(137)", "input_data": {}}
        )

        assert record.status == "failed"
        assert record.error is not None
        assert record.error.type == "killed"
        assert "SIGKILL" in record.error.message
        # The message must distinguish this from an OOM kill
        assert "not by the memory limit" in record.error.message
        assert len(cli.inspect) == 1

    def test_exit_137_with_inspect_failure_falls_back_to_oom(self, executor, breaker, monkeypatch):
        """A flaky/failed inspect (None) must not lose a true OOM: fall back to 'oom'."""
        # Default inspect_result is a "No such object" failure -> None
        _patch_docker_cli(monkeypatch, [_exit_137()])

        record = executor.execute_job_with_record(
            "job-oom-unknown", {"code": "x = 'a' * 10**12", "input_data": {}}
        )

        assert record.status == "oom"
        assert record.error is not None
        assert record.error.type == "oom"

    def test_inspect_happens_before_container_removal(self, executor, breaker, monkeypatch):
        """The container must still exist when inspected (rm -f comes after)."""
        cli = _patch_docker_cli(monkeypatch, [_exit_137()], inspect_result=_inspect_says("true"))

        executor.execute_job_with_record("job-oom-order", {"code": "x", "input_data": {}})

        inspect_idx = next(i for i, c in enumerate(cli.all) if c[:2] == ["docker", "inspect"])
        rm_idx = next(i for i, c in enumerate(cli.all) if c[:3] == ["docker", "rm", "-f"])
        assert inspect_idx < rm_idx, "inspect must run before the container is removed"

    def test_no_inspect_on_other_exit_codes(self, executor, breaker, monkeypatch):
        """The inspect is kept cheap: only exit 137 triggers it."""
        cli = _patch_docker_cli(
            monkeypatch,
            [SimpleNamespace(returncode=1, stdout="", stderr="ValueError: boom")],
        )

        record = executor.execute_job_with_record(
            "job-no-inspect", {"code": "raise ValueError('boom')", "input_data": {}}
        )

        assert record.status == "failed"
        assert cli.inspect == []

    def test_no_inspect_on_success(self, executor, breaker, monkeypatch):
        cli = _patch_docker_cli(monkeypatch, [_success()])

        record = executor.execute_job_with_record(
            "job-ok-no-inspect", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert cli.inspect == []


class TestContainerRemoval:
    """Without --rm, every exit path from _run_container must remove the container."""

    def test_docker_run_has_no_rm_flag(self, executor, breaker, monkeypatch):
        """--rm would delete the container before State.OOMKilled can be read."""
        cli = _patch_docker_cli(monkeypatch, [_success()])

        executor.execute_job_with_record("job-norm", {"code": "print('hi')", "input_data": {}})

        assert "--rm" not in cli.run[0]

    def test_container_removed_on_success(self, executor, breaker, monkeypatch):
        cli = _patch_docker_cli(monkeypatch, [_success()])

        record = executor.execute_job_with_record(
            "job-rm-ok", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert ["docker", "rm", "-f", "tako-job-rm-ok"] in cli.rm

    def test_container_removed_on_user_failure(self, executor, breaker, monkeypatch):
        cli = _patch_docker_cli(
            monkeypatch,
            [SimpleNamespace(returncode=1, stdout="", stderr="ValueError: boom")],
        )

        record = executor.execute_job_with_record(
            "job-rm-fail", {"code": "raise ValueError('boom')", "input_data": {}}
        )

        assert record.status == "failed"
        assert ["docker", "rm", "-f", "tako-job-rm-fail"] in cli.rm

    def test_container_removed_on_infra_failure(self, executor, breaker, monkeypatch):
        cli = _patch_docker_cli(monkeypatch, [_infra_failure(), _infra_failure()])

        record = executor.execute_job_with_record(
            "job-rm-infra", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "failed"
        # Both attempts' containers were removed (best-effort)
        assert ["docker", "rm", "-f", "tako-job-rm-infra"] in cli.rm
        assert ["docker", "rm", "-f", "tako-job-rm-infra-r1"] in cli.rm

    def test_container_killed_and_removed_on_host_timeout(self, executor, breaker, monkeypatch):
        """Host-level timeout still kills the running container AND removes it."""
        cli = _patch_docker_cli(
            monkeypatch,
            [subprocess.TimeoutExpired(["docker", "run"], 80, output=b"partial", stderr=None)],
        )

        record = executor.execute_job_with_record(
            "job-rm-timeout", {"code": "while True: pass", "input_data": {}, "timeout": 30}
        )

        assert record.status == "timeout"
        assert ["docker", "kill", "tako-job-rm-timeout"] in cli.kill
        assert ["docker", "rm", "-f", "tako-job-rm-timeout"] in cli.rm


class TestPhaseFileTrust:
    """.tako_phase must come from the root-only /tako-meta mount, not 0777 /output."""

    META_PHASE = (
        "container_start_ms=0\n"
        "phase=startup\n"
        "dep_install_started=false\n"
        "dep_install_ms=0\n"
        "startup_ms=11\n"
        "phase=execution\n"
        "execution_ms=222\n"
        "phase=completed\n"
        "total_ms=233\n"
    )
    FORGED_PHASE = (
        "phase=completed\nstartup_ms=1\ndep_install_ms=0\nexecution_ms=99999\ntotal_ms=99999\n"
    )

    @pytest.fixture(autouse=True)
    def run_as_root(self, monkeypatch):
        """Pretend the server runs as root so the meta dir is created/mounted."""
        monkeypatch.setattr(worker_module.os, "geteuid", lambda: 0, raising=False)

    def test_meta_dir_mounted_separately_from_output(self, executor, breaker, monkeypatch):
        seen = {}

        def on_run(attempt_idx, cmd):
            seen["meta"] = _meta_mount_dir(cmd)
            seen["output"] = _output_mount_dir(cmd)

        _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)

        executor.execute_job_with_record("job-meta-mount", {"code": "x", "input_data": {}})

        assert seen["meta"] != seen["output"]
        assert seen["meta"].name == "meta"

    def test_meta_dir_is_not_world_writable(self, executor, breaker, monkeypatch):
        """0755: the in-container sandbox user (uid 1000) must not be able to write."""
        seen = {}

        def on_run(attempt_idx, cmd):
            seen["mode"] = _meta_mount_dir(cmd).stat().st_mode & 0o777

        _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)

        executor.execute_job_with_record("job-meta-mode", {"code": "x", "input_data": {}})

        assert seen["mode"] == 0o755

    def test_phase_file_read_from_meta_dir_when_present(self, executor, breaker, monkeypatch):
        """A forged /output/.tako_phase is ignored when the meta copy exists."""

        def on_run(attempt_idx, cmd):
            # Entrypoint (container root) writes the real phase data to /tako-meta
            (_meta_mount_dir(cmd) / ".tako_phase").write_text(self.META_PHASE)
            # Sandboxed user code forges a copy in the 0777 /output mount
            (_output_mount_dir(cmd) / ".tako_phase").write_text(self.FORGED_PHASE)

        _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)

        record = executor.execute_job_with_record("job-meta-trust", {"code": "x", "input_data": {}})

        assert record.timing is not None
        assert record.timing.execution_ms == 222, "timing must come from the meta copy"
        assert record.timing.startup_ms == 11

    def test_phase_file_falls_back_to_output_dir(self, executor, breaker, monkeypatch):
        """Old executor images write only /output/.tako_phase; it must still parse."""

        def on_run(attempt_idx, cmd):
            (_output_mount_dir(cmd) / ".tako_phase").write_text(self.META_PHASE)

        _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)

        record = executor.execute_job_with_record(
            "job-meta-fallback", {"code": "x", "input_data": {}}
        )

        assert record.timing is not None
        assert record.timing.execution_ms == 222

    def test_no_meta_mount_when_host_uid_is_sandbox_uid(self, executor, breaker, monkeypatch):
        """Host uid 1000 == container sandbox uid: a 'trusted' meta dir would be
        sandbox-owned (forgeable), so the mount must be skipped entirely."""
        monkeypatch.setattr(worker_module.os, "geteuid", lambda: 1000, raising=False)

        def on_run(attempt_idx, cmd):
            (_output_mount_dir(cmd) / ".tako_phase").write_text(self.META_PHASE)

        cli = _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)

        record = executor.execute_job_with_record(
            "job-meta-collision", {"code": "x", "input_data": {}}
        )

        assert not any("target=/tako-meta" in arg for arg in cli.run[0])
        # Legacy behavior still works
        assert record.status == "succeeded"
        assert record.timing is not None

    def test_parse_phase_file_prefers_meta_dir(self, tmp_path):
        output_dir = tmp_path / "output"
        meta_dir = tmp_path / "meta"
        output_dir.mkdir()
        meta_dir.mkdir()
        (meta_dir / ".tako_phase").write_text(self.META_PHASE)
        (output_dir / ".tako_phase").write_text(self.FORGED_PHASE)

        timing = parse_phase_file(output_dir, meta_dir)

        assert timing is not None
        assert timing.execution_ms == 222

    def test_parse_phase_file_output_fallback_when_meta_empty(self, tmp_path):
        output_dir = tmp_path / "output"
        meta_dir = tmp_path / "meta"
        output_dir.mkdir()
        meta_dir.mkdir()
        (output_dir / ".tako_phase").write_text(self.META_PHASE)

        timing = parse_phase_file(output_dir, meta_dir)

        assert timing is not None
        assert timing.execution_ms == 222

    def test_parse_phase_file_without_meta_dir(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / ".tako_phase").write_text(self.META_PHASE)

        timing = parse_phase_file(output_dir, None)

        assert timing is not None
        assert timing.execution_ms == 222

    def test_parse_phase_file_ignores_symlinked_output_phase(self, tmp_path):
        """A symlink planted at /output/.tako_phase is never followed."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        target = tmp_path / "host-secret"
        target.write_text("phase=completed\nexecution_ms=1\n")
        (output_dir / ".tako_phase").symlink_to(target)

        assert parse_phase_file(output_dir, None) is None

    def test_stale_meta_phase_cleared_between_retry_attempts(self, executor, breaker, monkeypatch):
        """A failed attempt's meta phase file must not leak into the retry's record."""
        seen_on_retry = {}

        def on_run(attempt_idx, cmd):
            meta = _meta_mount_dir(cmd)
            if attempt_idx == 0:
                (meta / ".tako_phase").write_text("phase=failed\nfailed_phase=startup\n")
            else:
                seen_on_retry["entries"] = sorted(p.name for p in meta.iterdir())

        _patch_docker_cli(monkeypatch, [_infra_failure(), _success()], on_run=on_run)

        record = executor.execute_job_with_record("job-meta-retry", {"code": "x", "input_data": {}})

        assert record.status == "succeeded"
        assert seen_on_retry["entries"] == [], "meta dir must be empty when a retry starts"
        assert record.timing is None


class TestImageResolution:
    """F7: images built by `tako-vm build` must actually be executed, and raw
    base images without the executor entrypoint contract must be refused —
    running them would execute the image's default CMD instead of
    /code/main.py and record a bogus success for code that never ran."""

    def _executor(self, tmp_path, job_type):
        registry = JobTypeRegistry(config_path=tmp_path / "job_types.json")
        registry.register(job_type, persist=False)
        config = TakoVMConfig(
            security_mode="permissive",
            data_dir=str(tmp_path / "data"),
            max_retry_attempts=2,
            retry_base_delay=0.1,
            allow_runtime_requirements=True,
        )
        return CodeExecutor(config=config, registry=registry)

    def test_built_image_preferred_and_requirements_skipped(self, tmp_path, breaker, monkeypatch):
        """A pre-built job-type image with the executor contract is executed,
        and its baked-in requirements are NOT reinstalled at runtime."""
        jt = JobType(name="custom", requirements=["pandas", "numpy"])
        monkeypatch.setattr(worker_module, "image_exists", lambda name: name == jt.image_name)
        monkeypatch.setattr(
            worker_module, "image_has_executor_entrypoint", lambda name: name == jt.image_name
        )

        seen = {}

        def on_run(attempt_idx, cmd):
            input_dir = _mount_source_dir(cmd, "/input")
            seen["requirements_file_exists"] = (input_dir / "_requirements.txt").exists()

        cli = _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-built-1", {"code": "import pandas", "input_data": {}, "job_type": "custom"}
        )

        assert record.status == "succeeded"
        # The built image is what actually ran
        assert cli.run[0][-1] == "tako-vm-custom:latest"
        # Requirements are baked into the image: no runtime install file...
        assert seen["requirements_file_exists"] is False
        # ...so no bridge network was needed for dependency installation
        assert "--network=none" in cli.run[0]
        assert "--network=bridge" not in cli.run[0]

    def test_extra_requirements_still_installed_with_built_image(
        self, tmp_path, breaker, monkeypatch
    ):
        """Per-job extra requirements are not baked in and still install at
        runtime — but the job type's own requirements must not be re-listed."""
        jt = JobType(name="custom", requirements=["pandas"])
        monkeypatch.setattr(worker_module, "image_exists", lambda name: name == jt.image_name)
        monkeypatch.setattr(
            worker_module, "image_has_executor_entrypoint", lambda name: name == jt.image_name
        )

        seen = {}

        def on_run(attempt_idx, cmd):
            input_dir = _mount_source_dir(cmd, "/input")
            seen["requirements"] = (input_dir / "_requirements.txt").read_text(encoding="utf-8")

        cli = _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-built-extra",
            {
                "code": "import requests",
                "input_data": {},
                "job_type": "custom",
                "requirements": ["requests>=2.31"],
            },
        )

        assert record.status == "succeeded"
        assert cli.run[0][-1] == "tako-vm-custom:latest"
        assert seen["requirements"] == "requests>=2.31\n"

    def test_fallback_to_default_image_when_no_built_image(self, tmp_path, breaker, monkeypatch):
        """Without a built image, requirements install at runtime on the
        default executor image (the legacy behavior)."""
        jt = JobType(name="custom", requirements=["pandas"])
        # autouse fixture: image_exists -> False everywhere

        seen = {}

        def on_run(attempt_idx, cmd):
            input_dir = _mount_source_dir(cmd, "/input")
            seen["requirements"] = (input_dir / "_requirements.txt").read_text(encoding="utf-8")

        cli = _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-no-built", {"code": "import pandas", "input_data": {}, "job_type": "custom"}
        )

        assert record.status == "succeeded"
        assert cli.run[0][-1] == "code-executor:latest"
        assert seen["requirements"] == "pandas\n"

    def test_default_job_type_path_unchanged(self, executor, breaker, monkeypatch):
        """The default job type keeps running the default executor image."""
        cli = _patch_docker_cli(monkeypatch, [_success()])

        record = executor.execute_job_with_record(
            "job-default-img", {"code": "print('hi')", "input_data": {}}
        )

        assert record.status == "succeeded"
        assert cli.run[0][-1] == "code-executor:latest"

    def test_built_image_without_contract_is_ignored(self, tmp_path, breaker, monkeypatch):
        """A stale built image lacking /entrypoint.sh (old `tako-vm build`)
        must not be executed: fall back to the default executor image with
        runtime dependency installation."""
        jt = JobType(name="custom", requirements=["pandas"])
        monkeypatch.setattr(worker_module, "image_exists", lambda name: name == jt.image_name)
        monkeypatch.setattr(worker_module, "image_has_executor_entrypoint", lambda name: False)

        seen = {}

        def on_run(attempt_idx, cmd):
            input_dir = _mount_source_dir(cmd, "/input")
            seen["requirements_file_exists"] = (input_dir / "_requirements.txt").exists()

        cli = _patch_docker_cli(monkeypatch, [_success()], on_run=on_run)
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-stale-built", {"code": "import pandas", "input_data": {}, "job_type": "custom"}
        )

        assert record.status == "succeeded"
        assert cli.run[0][-1] == "code-executor:latest"
        assert seen["requirements_file_exists"] is True

    def test_executor_derived_base_image_is_allowed(self, tmp_path, breaker, monkeypatch):
        """A base_image that carries the executor entrypoint contract runs."""
        jt = JobType(name="custom", base_image="my-executor:latest")
        monkeypatch.setattr(
            worker_module,
            "image_has_executor_entrypoint",
            lambda name: name == "my-executor:latest",
        )

        cli = _patch_docker_cli(monkeypatch, [_success()])
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-exec-base", {"code": "print('hi')", "input_data": {}, "job_type": "custom"}
        )

        assert record.status == "succeeded"
        assert cli.run[0][-1] == "my-executor:latest"

    def test_raw_base_image_is_refused_not_bogus_success(self, tmp_path, breaker, monkeypatch):
        """A raw base image (no /entrypoint.sh) must fail fast with a config
        error — previously python:slim's default CMD (the REPL) ran instead of
        the user's code, exited 0 on EOF, and the job was recorded
        'succeeded' with empty output for code that NEVER ran."""
        jt = JobType(name="custom", base_image="python:3.11-slim")
        monkeypatch.setattr(worker_module, "image_has_executor_entrypoint", lambda name: False)

        cli = _patch_docker_cli(monkeypatch, [])
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-raw-base", {"code": "print('hi')", "input_data": {}, "job_type": "custom"}
        )

        assert cli.run == [], "no container may ever run a contract-less raw image"
        assert record.status == "failed"
        assert record.status != "succeeded"
        assert record.error is not None
        assert record.error.type == "config_error"
        assert "entrypoint" in record.error.message
        assert "tako-vm build custom" in record.error.message

    def test_unverifiable_base_image_is_refused(self, tmp_path, breaker, monkeypatch):
        """An inspect failure (image not local / daemon hiccup) is NOT a pass:
        the contract must be positively verified before a base image runs."""
        jt = JobType(name="custom", base_image="ghcr.io/acme/runner:1")
        # autouse fixture: image_has_executor_entrypoint -> None (inspect failed)

        cli = _patch_docker_cli(monkeypatch, [])
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-unverified-base", {"code": "print('hi')", "input_data": {}, "job_type": "custom"}
        )

        assert cli.run == []
        assert record.status == "failed"
        assert record.error is not None
        assert record.error.type == "config_error"

    def test_built_image_preferred_over_base_image(self, tmp_path, breaker, monkeypatch):
        """When a job type has both a built image and a base_image, the built
        image (requirements baked in) wins."""
        jt = JobType(name="custom", base_image="my-executor:latest", requirements=["pandas"])
        monkeypatch.setattr(worker_module, "image_exists", lambda name: name == jt.image_name)
        monkeypatch.setattr(worker_module, "image_has_executor_entrypoint", lambda name: True)

        cli = _patch_docker_cli(monkeypatch, [_success()])
        executor = self._executor(tmp_path, jt)

        record = executor.execute_job_with_record(
            "job-built-over-base", {"code": "print('hi')", "input_data": {}, "job_type": "custom"}
        )

        assert record.status == "succeeded"
        assert cli.run[0][-1] == "tako-vm-custom:latest"


class TestEntrypointScript:
    """The entrypoint must stay valid bash and keep the dual-location phase file."""

    ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"

    def test_bash_syntax_is_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(self.ENTRYPOINT)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_prefers_tako_meta_with_output_fallback(self):
        """Old hosts (no /tako-meta mount) must keep working: /output is the fallback."""
        content = self.ENTRYPOINT.read_text(encoding="utf-8")
        assert 'PHASE_FILE="/output/.tako_phase"' in content
        assert 'PHASE_FILE="/tako-meta/.tako_phase"' in content
