import os
import subprocess
import textwrap

import pytest

from tako_vm.sandbox import Sandbox

from .conftest import GVISOR_AVAILABLE, requires_docker, requires_executor_image

PROBE = textwrap.dedent(
    """
    import ctypes, os, subprocess
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    print("PTRACE:ALLOWED" if libc.ptrace(0, 0, 0, 0) == 0 else "PTRACE:BLOCKED")
    try:
        open("/tmp/probe.sh", "w").write("#!/bin/sh\\necho ran\\n")
        os.chmod("/tmp/probe.sh", 0o755)
        p = subprocess.run(["/tmp/probe.sh"], capture_output=True, text=True)
        print("TMPEXEC:ALLOWED" if p.returncode == 0 else "TMPEXEC:BLOCKED")
    except OSError:
        print("TMPEXEC:BLOCKED")
    print("CODE_RAN")
    """
)


@requires_docker
@requires_executor_image
class TestShippedDefaultsRunCode:
    @staticmethod
    @pytest.fixture(scope="class")
    def probe_result(request):
        from tako_vm import config as config_mod
        from tako_vm.config import TakoVMConfig

        cfg = TakoVMConfig(
            data_dir=str(request.config.invocation_params.dir / ".tako-probe"),
            **({} if GVISOR_AVAILABLE else {"security_mode": "permissive"}),
        )
        cfg.resolve_paths()
        assert cfg.enable_seccomp and cfg.enable_cap_restrictions
        if GVISOR_AVAILABLE:
            assert cfg.security_mode == "strict"
        original = config_mod._config
        config_mod._config = cfg
        os.environ.pop("TAKO_VM_ENABLE_CAP_RESTRICTIONS", None)
        try:
            return Sandbox(timeout=120).run(PROBE)
        finally:
            config_mod._config = original

    def test_user_code_executes_under_default_posture(self, probe_result):
        assert "CODE_RAN" in probe_result.stdout, (
            f"user code did not run under the shipped defaults "
            f"(exit={probe_result.exit_code}, stderr={probe_result.stderr[:400]!r})"
        )
        assert probe_result.exit_code == 0

    def test_seccomp_blocks_ptrace_on_library_path(self, probe_result):
        assert "PTRACE:BLOCKED" in probe_result.stdout, (
            f"ptrace was not blocked: the seccomp profile is not in effect "
            f"(stdout={probe_result.stdout!r})"
        )

    def test_tmp_is_noexec_on_library_path(self, probe_result):
        assert "TMPEXEC:BLOCKED" in probe_result.stdout, (
            f"a binary dropped in /tmp executed: /tmp is not noexec "
            f"(stdout={probe_result.stdout!r})"
        )


@requires_docker
@requires_executor_image
class TestExecutorImageHasNoSetuidBinaries:
    def test_no_setuid_or_setgid_files_in_image(self):
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "find",
                "code-executor:latest",
                "/",
                "-xdev",
                "-perm",
                "/6000",
                "-type",
                "f",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", (
            f"setuid/setgid binaries present in the executor image:\n{result.stdout}"
        )
