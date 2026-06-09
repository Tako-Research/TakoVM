"""
Tests for orphaned-container cleanup and the executor container label.

DockerCleanup.cleanup_orphaned_containers matches containers by the
``tako-vm-executor`` label, so these tests verify both halves of the
contract:

1. Every launch path (CodeExecutor and the library Sandbox) actually applies
   the label at ``docker run`` time via ``base_isolation_args``.
2. Cleanup filters by the label only (no name-pattern matching), honors the
   ``max_age_seconds`` guard for running containers, and removes exactly the
   matched container IDs.

All tests mock subprocess.run — no Docker required.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import tako_vm.execution.health as health_module
import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution.docker import CONTAINER_LABEL, EXECUTION_ID_LABEL
from tako_vm.execution.health import DockerCleanup
from tako_vm.execution.worker import CodeExecutor
from tako_vm.job_types import JobType
from tako_vm.sandbox import Sandbox


def _created_at(age_seconds: float) -> str:
    """Render a docker ps {{.CreatedAt}} value for a container of given age."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S %z") + " UTC"


def _patch_health_subprocess(monkeypatch, ps_stdout="", ps_returncode=0):
    """Replace subprocess.run in the health module; returns recorded argv lists."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=ps_returncode, stdout=ps_stdout, stderr="boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(health_module.subprocess, "run", fake_run)
    return calls


def _removed_ids(calls):
    """Extract container IDs from recorded `docker rm -f <id>` calls."""
    return [cmd[3] for cmd in calls if cmd[:3] == ["docker", "rm", "-f"]]


class TestCleanupOrphanedContainers:
    """Tests for DockerCleanup.cleanup_orphaned_containers."""

    def test_filters_by_executor_label_only(self, monkeypatch):
        """Cleanup lists containers by label; no name-pattern filter is used."""
        calls = _patch_health_subprocess(monkeypatch, ps_stdout="")

        DockerCleanup.cleanup_orphaned_containers()

        ps_calls = [cmd for cmd in calls if cmd[:2] == ["docker", "ps"]]
        assert len(ps_calls) == 1
        assert "--filter" in ps_calls[0]
        assert f"label={CONTAINER_LABEL}" in ps_calls[0]
        # The old substring name filter matched unrelated user containers.
        assert not any("name=job-" in arg for cmd in calls for arg in cmd)

    def test_label_constant_matches_launch_paths(self):
        """health.py and docker.py must agree on the label or cleanup is a no-op."""
        assert DockerCleanup.CONTAINER_LABEL == CONTAINER_LABEL

    def test_removes_exited_and_dead_containers(self, monkeypatch):
        """Non-running labeled containers are removed regardless of age."""
        stdout = "\n".join(
            [
                f"aaa111\t{_created_at(5)}\texited",
                f"bbb222\t{_created_at(10)}\tdead",
                f"ccc333\t{_created_at(20)}\tcreated",
            ]
        )
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        assert removed == 3
        assert sorted(_removed_ids(calls)) == ["aaa111", "bbb222", "ccc333"]

    def test_running_container_younger_than_max_age_is_kept(self, monkeypatch):
        """Running containers within the age guard are never force-removed."""
        stdout = f"aaa111\t{_created_at(60)}\trunning"
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        assert removed == 0
        assert _removed_ids(calls) == []

    def test_running_container_older_than_max_age_is_removed(self, monkeypatch):
        """Running containers past max_age_seconds are treated as orphans."""
        stdout = f"aaa111\t{_created_at(7200)}\trunning"
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        assert removed == 1
        assert _removed_ids(calls) == ["aaa111"]

    def test_mixed_states_only_old_running_removed(self, monkeypatch):
        """Exited always removed; running removed only past the age guard."""
        stdout = "\n".join(
            [
                f"old-run\t{_created_at(7200)}\trunning",
                f"new-run\t{_created_at(30)}\trunning",
                f"old-exit\t{_created_at(7200)}\texited",
                f"new-exit\t{_created_at(30)}\texited",
            ]
        )
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        assert removed == 3
        assert sorted(_removed_ids(calls)) == ["new-exit", "old-exit", "old-run"]

    def test_unparseable_created_at_skips_running_container(self, monkeypatch):
        """Fail safe: never kill a running container whose age is unknown."""
        stdout = "\n".join(
            [
                "aaa111\tgarbage timestamp\trunning",
                "bbb222\tgarbage timestamp\texited",
            ]
        )
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        # Running with unknown age: skipped. Exited: removed regardless.
        assert removed == 1
        assert _removed_ids(calls) == ["bbb222"]

    def test_ps_failure_returns_zero(self, monkeypatch):
        """A failed docker ps aborts cleanup without removing anything."""
        calls = _patch_health_subprocess(monkeypatch, ps_stdout="", ps_returncode=1)

        assert DockerCleanup.cleanup_orphaned_containers() == 0
        assert _removed_ids(calls) == []


class TestParseCreatedAt:
    """Tests for DockerCleanup._parse_created_at."""

    def test_parses_docker_format(self):
        """Parses the `docker ps {{.CreatedAt}}` format (offset + zone name)."""
        ts = DockerCleanup._parse_created_at("2024-01-15 10:30:00 +0000 UTC")
        expected = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp()
        assert ts == expected

    def test_honors_non_utc_offset(self):
        """The numeric offset is respected even with an unknown zone name."""
        ts = DockerCleanup._parse_created_at("2024-01-15 19:30:00 +0900 JST")
        expected = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp()
        assert ts == expected

    def test_invalid_returns_none(self):
        """Garbage input returns None instead of raising."""
        assert DockerCleanup._parse_created_at("not a timestamp") is None
        assert DockerCleanup._parse_created_at("") is None


@pytest.fixture
def executor(tmp_path, monkeypatch):
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
    config = TakoVMConfig(security_mode="permissive", data_dir=str(tmp_path / "data"))
    return CodeExecutor(config=config)


@pytest.fixture
def io_dirs(tmp_path):
    dirs = []
    for name in ("code", "input", "output"):
        d = tmp_path / name
        d.mkdir()
        dirs.append(d)
    return dirs


class TestWorkerAppliesLabels:
    """CodeExecutor must label containers so startup cleanup can find them."""

    def test_run_container_command_includes_labels(self, executor, io_dirs, monkeypatch):
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(worker_module.subprocess, "run", fake_run)

        code_dir, input_dir, output_dir = io_dirs
        executor._run_container(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
            startup_timeout=45,
            job_type=JobType(name="default", requirements=[]),
            job_id="job-label-test",
        )

        run_cmds = [cmd for cmd in captured if cmd[:2] == ["docker", "run"]]
        assert run_cmds, "expected a docker run invocation"
        cmd = run_cmds[0]
        assert f"--label={CONTAINER_LABEL}" in cmd
        assert f"--label={EXECUTION_ID_LABEL}=job-label-test" in cmd


class TestSandboxAppliesLabels:
    """Library-mode Sandbox must label containers identically."""

    def test_build_docker_command_includes_labels(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker_module, "_gvisor_available", True)

        code_dir = tmp_path / "code"
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        for d in (code_dir, input_dir, output_dir):
            d.mkdir()

        sb = Sandbox()
        cmd, container_name = sb._build_docker_command(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
        )

        assert f"--label={CONTAINER_LABEL}" in cmd
        # No ExecutionRecord in library mode; the container name is the trace ID.
        assert f"--label={EXECUTION_ID_LABEL}={container_name}" in cmd
