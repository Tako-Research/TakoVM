"""Executable specification of Tako VM's isolation promise.

These tests pin the always-on container hardening posture so it cannot silently
regress. gVisor is Tako VM's sole isolation boundary; the flags assembled by
``base_isolation_args`` (the single source of truth, ``execution/docker.py``)
and the fail-closed runtime resolution in ``resolve_runtime``
(``execution/worker.py``) are what keep untrusted code contained.

This is the security tripwire the durable-session roadmap leans on: every later
phase (long-lived containers, egress, credentials, BYO agents, snapshots) must
keep these green. A change that drops ``--read-only``, re-adds a dangerous
capability, weakens the runtime, or lets strict mode fall back to runc should
break here loudly, in CI, with no Docker required.

Deliberately Docker-free: these assert on the *assembled command* and the
*resolution logic*, not on a running container, so they run everywhere and fast
and can gate every PR.
"""

import pytest

from tako_vm.config import TakoVMConfig
from tako_vm.execution import worker
from tako_vm.execution.docker import (
    CONTAINER_LABEL,
    EXECUTION_ID_LABEL,
    base_isolation_args,
)
from tako_vm.execution.worker import RuntimeUnavailableError, resolve_runtime

# Flags that would silently break out of, or substantially weaken, the sandbox.
# base_isolation_args must never emit any of these, under any argument combo.
DANGEROUS_FLAG_PREFIXES = (
    "--privileged",
    "--network=host",
    "--net=host",
    "--pid=host",
    "--ipc=host",
    "--uts=host",
    "--userns=host",
    "--cap-add=ALL",
    "--cap-add=SYS_ADMIN",
    "--cap-add=NET_ADMIN",
    "--cap-add=SYS_PTRACE",
    "--cap-add=SYS_MODULE",
    "--security-opt=seccomp=unconfined",
    "--security-opt=apparmor=unconfined",
    "--security-opt=label=disable",
    "--security-opt=systempaths=unconfined",
    # Host filesystem / device access. A bind mount of / or a raw device is a
    # trivial escape; the base posture appends zero mounts (callers add them).
    "--mount",
    "--device",
)

# The only capabilities allowed back in: gosu needs SETUID/SETGID to drop from
# root to the unprivileged sandbox user. Nothing else.
ALLOWED_CAP_ADDS = {"--cap-add=SETUID", "--cap-add=SETGID"}

# Every combination of the optional knobs, as ``(runtime, caps, auto_remove,
# exec_id)`` tuples. The invariants below must hold across the whole
# cross-product, not just the defaults.
ALL_COMBOS = [
    (runtime, caps, auto_remove, exec_id)
    for runtime in ("runsc", "runc")
    for caps in (True, False)
    for auto_remove in (True, False)
    for exec_id in (None, "exec-abc123")
]


def _combo_id(combo):
    runtime, caps, auto_remove, exec_id = combo
    return f"{runtime}-caps{caps}-rm{auto_remove}-eid{bool(exec_id)}"


def _args(runtime="runsc", caps=True, auto_remove=True, exec_id=None):
    return base_isolation_args(
        "tako-test-container",
        runtime=runtime,
        enable_cap_restrictions=caps,
        execution_id=exec_id,
        auto_remove=auto_remove,
    )


class TestBaseIsolationPosture:
    """The always-on hardening flags that must be present on every container."""

    def test_command_is_docker_run(self):
        assert _args()[:2] == ["docker", "run"]

    def test_always_read_only_rootfs(self):
        """A writable rootfs would let untrusted code tamper with the runtime."""
        for c in ALL_COMBOS:
            args = _args(*c)
            assert "--read-only" in args, f"--read-only missing for {_combo_id(c)}"

    def test_always_init(self):
        """--init injects tini for correct signal handling and zombie reaping."""
        for c in ALL_COMBOS:
            assert "--init" in _args(*c), f"--init missing for {_combo_id(c)}"

    def test_always_named_and_labeled(self):
        """The ownership label is what lets orphan cleanup reap leaked containers."""
        for c in ALL_COMBOS:
            args = _args(*c)
            assert "--name=tako-test-container" in args
            assert f"--label={CONTAINER_LABEL}" in args, (
                f"ownership label missing for {_combo_id(c)}"
            )

    def test_cap_drop_all_when_restrictions_enabled(self):
        for runtime in ("runsc", "runc"):
            assert "--cap-drop=ALL" in _args(runtime=runtime, caps=True)

    def test_only_setuid_setgid_readded(self):
        """No capability other than the two gosu needs may ever be added back."""
        for c in ALL_COMBOS:
            args = _args(*c)
            cap_adds = {a for a in args if a.startswith("--cap-add=")}
            assert cap_adds <= ALLOWED_CAP_ADDS, (
                f"unexpected cap-add for {_combo_id(c)}: {cap_adds - ALLOWED_CAP_ADDS}"
            )
            # Guard the glued-form assumption: a future switch to space-separated
            # args (["--cap-add", "X"]) would silently disarm the check above and
            # the dangerous-flag denylist, so forbid the bare tokens outright.
            assert "--cap-add" not in args, f"bare --cap-add token for {_combo_id(c)}"
            assert "--cap-drop" not in args, f"bare --cap-drop token for {_combo_id(c)}"

    def test_runsc_runtime_emitted(self):
        assert "--runtime=runsc" in _args(runtime="runsc")

    def test_runc_runtime_is_implicit(self):
        """runc is docker's default and is never passed explicitly (some daemons
        reject ``--runtime=runc``); the only runtime ever named is runsc."""
        runtime_flags = [a for a in _args(runtime="runc") if a.startswith("--runtime=")]
        assert runtime_flags == []

    def test_runtime_flag_is_only_ever_runsc(self):
        for c in ALL_COMBOS:
            for a in _args(*c):
                if a.startswith("--runtime="):
                    assert a == "--runtime=runsc", (
                        f"unexpected runtime flag for {_combo_id(c)}: {a}"
                    )

    def test_base_args_grant_no_host_filesystem(self):
        """Mounts/devices are caller-appended; the base posture must grant no host
        paths. A bind mount of / or a raw device would be a trivial escape."""
        for c in ALL_COMBOS:
            args = _args(*c)
            assert "-v" not in args
            assert not any(a.startswith("--volume") for a in args), _combo_id(c)
            assert not any(a.startswith("--mount") for a in args), _combo_id(c)
            assert not any(a.startswith("--device") for a in args), _combo_id(c)


class TestNoDangerousFlags:
    """The negative space: the sandbox-escape flags that must never appear."""

    @pytest.mark.parametrize("combo", ALL_COMBOS, ids=_combo_id)
    def test_no_dangerous_flag_in_any_combo(self, combo):
        args = _args(*combo)
        for arg in args:
            for bad in DANGEROUS_FLAG_PREFIXES:
                assert not arg.startswith(bad), (
                    f"dangerous flag {arg!r} emitted for {_combo_id(combo)}"
                )


class TestAutoRemoveContract:
    """--rm governs whether the daemon reaps the container; the OOM-inspection
    path needs it OFF so it can ``docker inspect`` the exited container."""

    def test_auto_remove_true_emits_rm(self):
        assert "--rm" in _args(auto_remove=True)

    def test_auto_remove_false_omits_rm(self):
        # Callers that must read State.OOMKilled keep the container around and
        # remove it themselves once inspection is done.
        assert "--rm" not in _args(auto_remove=False)


class TestExecutionIdLabel:
    """The execution-id label traces an orphaned container back to its record."""

    def test_label_present_when_id_given(self):
        assert f"--label={EXECUTION_ID_LABEL}=exec-abc123" in _args(exec_id="exec-abc123")

    def test_label_absent_when_id_omitted(self):
        assert not any(a.startswith(f"--label={EXECUTION_ID_LABEL}=") for a in _args(exec_id=None))


class TestCapRestrictionsEscapeHatch:
    """``enable_cap_restrictions=False`` is a CI-only escape hatch for daemons
    that can't modify capability bounding sets. It only *omits* the cap drop/add
    set; it must never grant privileges or weaken any other hardening flag."""

    def test_disabling_caps_omits_all_cap_flags(self):
        args = _args(caps=False)
        assert not any(a.startswith("--cap-drop") for a in args)
        assert not any(a.startswith("--cap-add") for a in args)

    def test_disabling_caps_keeps_rest_of_posture(self):
        args = _args(caps=False)
        assert "--read-only" in args
        assert "--init" in args
        assert f"--label={CONTAINER_LABEL}" in args

    def test_disabling_caps_introduces_no_dangerous_flag(self):
        for arg in _args(caps=False):
            for bad in DANGEROUS_FLAG_PREFIXES:
                assert not arg.startswith(bad), f"dangerous flag {arg!r} with caps disabled"


class TestStrictModeFailsClosed:
    """Strict mode must never silently run untrusted code without gVisor.

    gVisor availability is controlled by monkeypatching the module-level
    ``check_gvisor_available`` so these stay deterministic and Docker-free.
    Replacing the whole function (not just the probe) bypasses its
    ``_gvisor_available`` cache, so no reset fixture is needed and tests can't
    pollute each other through it.
    """

    def _config(self, **kw):
        return TakoVMConfig(**kw)

    def test_strict_raises_when_gvisor_unavailable(self, monkeypatch):
        monkeypatch.setattr(worker, "check_gvisor_available", lambda: False)
        with pytest.raises(RuntimeUnavailableError):
            resolve_runtime(self._config(security_mode="strict"))

    def test_strict_uses_runsc_when_available(self, monkeypatch):
        monkeypatch.setattr(worker, "check_gvisor_available", lambda: True)
        assert resolve_runtime(self._config(security_mode="strict")) == "runsc"

    def test_strict_rejects_explicit_runc(self, monkeypatch):
        # Even if gVisor is available, an explicit runc request under strict is
        # an error: strict means "fail rather than weaken isolation".
        monkeypatch.setattr(worker, "check_gvisor_available", lambda: True)
        with pytest.raises(RuntimeUnavailableError):
            resolve_runtime(self._config(security_mode="strict", container_runtime="runc"))


class TestPermissiveModeFallback:
    """Permissive is the development default. It is allowed to fall back to runc,
    but that fallback must be explicit in the resolution logic, never the result
    of strict mode leaking through."""

    def _config(self, **kw):
        return TakoVMConfig(**kw)

    def test_permissive_uses_runsc_when_available(self, monkeypatch):
        monkeypatch.setattr(worker, "check_gvisor_available", lambda: True)
        assert resolve_runtime(self._config(security_mode="permissive")) == "runsc"

    def test_permissive_falls_back_to_runc_when_no_gvisor(self, monkeypatch):
        monkeypatch.setattr(worker, "check_gvisor_available", lambda: False)
        assert resolve_runtime(self._config(security_mode="permissive")) == "runc"

    def test_explicit_runc_permitted_in_permissive(self, monkeypatch):
        monkeypatch.setattr(worker, "check_gvisor_available", lambda: True)
        assert (
            resolve_runtime(self._config(security_mode="permissive", container_runtime="runc"))
            == "runc"
        )
