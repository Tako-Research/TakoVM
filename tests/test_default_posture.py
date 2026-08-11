"""The shipped default configuration must actually run code, and actually
enforce the controls it advertises.

Every other Docker-backed test in this suite historically ran with
``enable_seccomp`` and ``enable_cap_restrictions`` switched off, because with
them on no container would start. That made the default posture -- the one a
new user gets, and the one the docs describe -- the single configuration
nothing exercised. Two real defects lived in that blind spot:

1. The seccomp profile denied the ``prctl`` options the OCI runtime needs
   during container init, so ``docker run`` failed before the entrypoint ran.
2. The entrypoint ``chown``'d cache dirs, which needs CAP_CHOWN;
   ``--cap-drop=ALL`` strips it, and under ``set -e`` that aborted the
   container before user code ran.

Both presented as "Docker on this host is unusual" rather than as bugs. These
tests pin the defaults ON and assert on observable in-container behavior, so a
regression shows up as a failing test instead of a workaround.
"""

import os
import textwrap

import pytest

from tako_vm.sandbox import Sandbox

from .conftest import requires_docker, requires_executor_image

# Probes the two controls that were silently absent on the library path.
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
    """All three assertions read one container run.

    Deliberately a single ``Sandbox.run`` shared across the class rather than
    one per test: each run is a container launch, and on a loaded host those
    are the flakiest thing in the suite. A flaky security test gets muted, and
    muting it would recreate the very blind spot these tests exist to close.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def probe_result(request):
        from tako_vm import config as config_mod
        from tako_vm.config import TakoVMConfig

        cfg = TakoVMConfig(data_dir=str(request.config.invocation_params.dir / ".tako-probe"))
        cfg.resolve_paths()
        assert cfg.enable_seccomp and cfg.enable_cap_restrictions
        original = config_mod._config
        config_mod._config = cfg
        os.environ.pop("TAKO_VM_ENABLE_CAP_RESTRICTIONS", None)
        try:
            return Sandbox(timeout=120).run(PROBE)
        finally:
            config_mod._config = original

    def test_user_code_executes_under_default_posture(self, probe_result):
        """With seccomp and --cap-drop=ALL on, user code must still run.

        Regression guard for the entrypoint CAP_CHOWN abort and the seccomp
        prctl denial: both made this return exit code 1 with empty stdout.
        """
        assert "CODE_RAN" in probe_result.stdout, (
            f"user code did not run under the shipped defaults "
            f"(exit={probe_result.exit_code}, stderr={probe_result.stderr[:400]!r})"
        )
        assert probe_result.exit_code == 0

    def test_seccomp_blocks_ptrace_on_library_path(self, probe_result):
        """The default-deny profile denies ptrace; it must reach the library path."""
        assert "PTRACE:BLOCKED" in probe_result.stdout, (
            f"ptrace was not blocked: the seccomp profile is not in effect "
            f"(stdout={probe_result.stdout!r})"
        )

    def test_tmp_is_noexec_on_library_path(self, probe_result):
        """/tmp is noexec when no runtime dependencies are being installed."""
        assert "TMPEXEC:BLOCKED" in probe_result.stdout, (
            f"a binary dropped in /tmp executed: /tmp is not noexec "
            f"(stdout={probe_result.stdout!r})"
        )
