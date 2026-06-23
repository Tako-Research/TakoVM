"""
Runtime (gVisor) enforcement tests for the library Sandbox path.

The ``Sandbox`` / ``sandbox.run`` convenience entry point must apply the same
container runtime policy as the server execution path: pass ``--runtime=runsc``
when gVisor is selected, and fail closed in strict mode when gVisor is absent.
These are pure command-construction tests — no Docker required.
"""

from pathlib import Path

import pytest

import tako_vm.execution.worker as worker_module
import tako_vm.sandbox as sandbox_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution import RuntimeUnavailableError, reset_gvisor_check
from tako_vm.sandbox import Sandbox


@pytest.fixture(autouse=True)
def reset_gvisor_cache():
    reset_gvisor_check()
    yield
    reset_gvisor_check()


def _use_config(monkeypatch, *, container_runtime="runsc", security_mode="strict"):
    config = TakoVMConfig(container_runtime=container_runtime, security_mode=security_mode)
    monkeypatch.setattr(sandbox_module, "get_config", lambda: config)


def _set_gvisor(monkeypatch, available: bool):
    monkeypatch.setattr(worker_module, "_gvisor_available", available)


def _runtime_flags(tmp_path: Path) -> list[str]:
    """Build a sandbox docker command and return the --runtime=* args it carries."""
    sb = Sandbox(auto_build=False)
    cmd, _ = sb._build_docker_command(
        code_dir=tmp_path, input_dir=tmp_path, output_dir=tmp_path, timeout=10
    )
    return [arg for arg in cmd if arg.startswith("--runtime")]


class TestSandboxRuntimeEnforcement:
    def test_uses_gvisor_when_available(self, monkeypatch, tmp_path):
        _use_config(monkeypatch, container_runtime="runsc", security_mode="strict")
        _set_gvisor(monkeypatch, True)
        assert _runtime_flags(tmp_path) == ["--runtime=runsc"]

    def test_strict_fails_closed_without_gvisor(self, monkeypatch, tmp_path):
        _use_config(monkeypatch, container_runtime="runsc", security_mode="strict")
        _set_gvisor(monkeypatch, False)
        with pytest.raises(RuntimeUnavailableError):
            _runtime_flags(tmp_path)

    def test_permissive_falls_back_to_runc_without_gvisor(self, monkeypatch, tmp_path):
        _use_config(monkeypatch, container_runtime="runsc", security_mode="permissive")
        _set_gvisor(monkeypatch, False)
        # runc is docker's default, so no --runtime flag is added.
        assert _runtime_flags(tmp_path) == []

    def test_explicit_runc_in_strict_fails_closed(self, monkeypatch, tmp_path):
        _use_config(monkeypatch, container_runtime="runc", security_mode="strict")
        _set_gvisor(monkeypatch, True)
        with pytest.raises(RuntimeUnavailableError):
            _runtime_flags(tmp_path)


class TestSandboxUlimits:
    """The library Sandbox path must set the same kernel rlimits as the worker.

    Without --ulimit=fsize, RLIMIT_FSIZE is unbounded and untrusted code can
    write an arbitrarily large file to the writable /output bind-mount, a
    host-disk-exhaustion DoS gVisor does not cover (issue #97).
    """

    def _build(self, monkeypatch, tmp_path):
        _use_config(monkeypatch, container_runtime="runsc", security_mode="strict")
        _set_gvisor(monkeypatch, True)
        sb = Sandbox(auto_build=False)
        cmd, _ = sb._build_docker_command(
            code_dir=tmp_path, input_dir=tmp_path, output_dir=tmp_path, timeout=10
        )
        return cmd

    def test_sets_fsize_ulimit(self, monkeypatch, tmp_path):
        cmd = self._build(monkeypatch, tmp_path)
        from tako_vm.config import ContainerLimits

        limits = ContainerLimits()
        assert f"--ulimit=fsize={limits.fsize}" in cmd

    def test_sets_nofile_and_nproc_ulimits(self, monkeypatch, tmp_path):
        cmd = self._build(monkeypatch, tmp_path)
        from tako_vm.config import ContainerLimits

        limits = ContainerLimits()
        assert f"--ulimit=nofile={limits.nofile_soft}:{limits.nofile_hard}" in cmd
        assert f"--ulimit=nproc={limits.nproc_soft}:{limits.nproc_hard}" in cmd
