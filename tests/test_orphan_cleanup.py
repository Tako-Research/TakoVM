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
    dt = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S %z") + " UTC"


def _patch_health_subprocess(monkeypatch, ps_stdout="", ps_returncode=0):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=ps_returncode, stdout=ps_stdout, stderr="boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(health_module.subprocess, "run", fake_run)
    return calls


def _removed_ids(calls):
    return [cmd[3] for cmd in calls if cmd[:3] == ["docker", "rm", "-f"]]


class TestCleanupOrphanedContainers:
    def test_filters_by_executor_label_only(self, monkeypatch):
        calls = _patch_health_subprocess(monkeypatch, ps_stdout="")

        DockerCleanup.cleanup_orphaned_containers()

        ps_calls = [cmd for cmd in calls if cmd[:2] == ["docker", "ps"]]
        assert len(ps_calls) == 1
        assert "--filter" in ps_calls[0]
        assert f"label={CONTAINER_LABEL}" in ps_calls[0]
        assert not any("name=job-" in arg for cmd in calls for arg in cmd)

    def test_label_constant_matches_launch_paths(self):
        assert DockerCleanup.CONTAINER_LABEL == CONTAINER_LABEL

    def test_removes_exited_and_dead_containers(self, monkeypatch):
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
        stdout = f"aaa111\t{_created_at(60)}\trunning"
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        assert removed == 0
        assert _removed_ids(calls) == []

    def test_running_container_older_than_max_age_is_removed(self, monkeypatch):
        stdout = f"aaa111\t{_created_at(7200)}\trunning"
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        assert removed == 1
        assert _removed_ids(calls) == ["aaa111"]

    def test_mixed_states_only_old_running_removed(self, monkeypatch):
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
        stdout = "\n".join(
            [
                "aaa111\tgarbage timestamp\trunning",
                "bbb222\tgarbage timestamp\texited",
            ]
        )
        calls = _patch_health_subprocess(monkeypatch, ps_stdout=stdout)

        removed = DockerCleanup.cleanup_orphaned_containers(max_age_seconds=3600)

        assert removed == 1
        assert _removed_ids(calls) == ["bbb222"]

    def test_ps_failure_returns_zero(self, monkeypatch):
        calls = _patch_health_subprocess(monkeypatch, ps_stdout="", ps_returncode=1)

        assert DockerCleanup.cleanup_orphaned_containers() == 0
        assert _removed_ids(calls) == []


class TestPeriodicCleanupReapsOrphans:
    @pytest.mark.asyncio
    async def test_loop_calls_reaper_with_workspace_age_guard(self, monkeypatch):
        import importlib

        app_module = importlib.import_module("tako_vm.server.app")

        recorded = {}

        def fake_reaper(max_age_seconds):
            recorded["age"] = max_age_seconds
            return 2

        monkeypatch.setattr(DockerCleanup, "cleanup_orphaned_containers", staticmethod(fake_reaper))
        monkeypatch.setattr(app_module, "prune_stale_workspaces", lambda *a, **k: 0)
        monkeypatch.setattr(app_module, "prune_old_run_dirs", lambda *a, **k: 0)

        sleeps = {"n": 0}

        async def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise __import__("asyncio").CancelledError()

        monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)

        class FakeStorage:
            async def cleanup_old_records(self, _ttl):
                return 0

            async def cleanup_old_dlq_entries(self, _ttl):
                return 0

        from pathlib import Path

        await app_module._periodic_cleanup(
            FakeStorage(),
            record_ttl_days=7,
            data_dir=Path("/tmp"),
            workspace_max_age_seconds=99999,
            dlq_ttl_days=7,
        )

        assert recorded["age"] == 99999


class TestParseCreatedAt:
    def test_parses_docker_format(self):
        ts = DockerCleanup._parse_created_at("2024-01-15 10:30:00 +0000 UTC")
        expected = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp()
        assert ts == expected

    def test_honors_non_utc_offset(self):
        ts = DockerCleanup._parse_created_at("2024-01-15 19:30:00 +0900 JST")
        expected = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp()
        assert ts == expected

    def test_invalid_returns_none(self):
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
        assert f"--label={EXECUTION_ID_LABEL}={container_name}" in cmd
