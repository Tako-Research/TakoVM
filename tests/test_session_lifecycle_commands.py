"""Executable specification of the session-container command builders.

Phase 1 introduces long-lived *session* containers: durable, per-agent
workspaces that survive across many ``docker exec`` executions. ``docker.py``
grows two pure command builders for them, and these tests pin their output so
the session path can never silently weaken the isolation posture that
``base_isolation_args`` (the single source of truth) guarantees, nor leak a
dangerous flag, nor run a session exec as root.

Mirrors ``tests/test_isolation_invariants.py``: deliberately Docker-free, these
assert on the *assembled command* across a cross-product of the optional knobs,
not on a running container, so they run everywhere, fast, and gate every PR.

The builders under test are pure (no subprocess), so nothing here touches the
daemon. The reuse of ``base_isolation_args`` is the security crux: the session
run command MUST carry every isolation flag a job carries.
"""

import pytest

from tako_vm.execution.docker import (
    CONTAINER_LABEL,
    SESSION_ID_LABEL,
    base_isolation_args,
    build_session_exec_command,
    build_session_run_command,
)

# Flags that would silently break out of, or substantially weaken, the sandbox.
# Neither builder may ever emit any of these, under any argument combo. Mirrors
# the denylist in test_isolation_invariants.py and extends it for the session
# specifics (a writable host root, an explicit --rm that would reap the session,
# privilege escalation).
DANGEROUS_FLAG_PREFIXES = (
    "--privileged",
    "--network=host",
    "--net=host",
    "--network=bridge",  # sessions have NO egress in Phase 1
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
    # Host filesystem / device access beyond the single workspace bind. A bind
    # mount of / or a raw device is a trivial escape.
    "--mount",
    "--device",
)

# A host bind of "/" (or any token that mounts the host root) would be a trivial
# escape. The only -v value allowed is the workspace mount.
HOST_ROOT_MOUNTS = ("/:/", "/:", "//", "/:/workspace")

# The only capabilities allowed back in: gosu needs SETUID/SETGID to drop from
# root to the unprivileged sandbox user, and the root-side `timeout` supervisor
# needs KILL to signal that dropped (uid 1000) process when its budget expires.
# gosu clears all capabilities on the uid switch, so none reach user code.
# Nothing else. Kept in lockstep with test_isolation_invariants.ALLOWED_CAP_ADDS.
ALLOWED_CAP_ADDS = {"--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=KILL"}

WORKSPACE_DIR = "/srv/sessions/sess-abc123/workspace"
IMAGE = "code-executor:latest"
SESSION_ID = "sess-abc123"

# Every combination of the optional run-command knobs, as
# ``(runtime, caps, memory, cpu)`` tuples. The invariants below must hold across
# the whole cross-product, not just the defaults.
RUN_COMBOS = [
    (runtime, caps, memory, cpu)
    for runtime in ("runsc", "runc")
    for caps in (True, False)
    for memory in (None, "512m")
    for cpu in (None, "1.5")
]


def _run_combo_id(combo):
    runtime, caps, memory, cpu = combo
    return f"{runtime}-caps{caps}-mem{bool(memory)}-cpu{bool(cpu)}"


def _run(runtime="runsc", caps=True, memory=None, cpu=None, **kw):
    return build_session_run_command(
        "tako-session-test",
        runtime=runtime,
        image=IMAGE,
        workspace_dir=WORKSPACE_DIR,
        session_id=SESSION_ID,
        memory_limit=memory,
        cpu_limit=cpu,
        enable_cap_restrictions=caps,
        **kw,
    )


# ---------------------------------------------------------------------------
# build_session_run_command: isolation reuse
# ---------------------------------------------------------------------------


class TestRunReusesIsolationPosture:
    """The session run command must carry everything base_isolation_args
    guarantees: the gVisor posture for a session is identical to a job's."""

    def test_command_is_docker_run(self):
        assert _run()[:2] == ["docker", "run"]

    def test_always_read_only_rootfs(self):
        for c in RUN_COMBOS:
            assert "--read-only" in _run(*c), f"--read-only missing for {_run_combo_id(c)}"

    def test_always_init(self):
        for c in RUN_COMBOS:
            assert "--init" in _run(*c), f"--init missing for {_run_combo_id(c)}"

    def test_always_named_and_labeled(self):
        for c in RUN_COMBOS:
            args = _run(*c)
            assert "--name=tako-session-test" in args, _run_combo_id(c)
            assert f"--label={CONTAINER_LABEL}" in args, (
                f"ownership label missing for {_run_combo_id(c)}"
            )

    def test_cap_drop_all_when_restrictions_enabled(self):
        for runtime in ("runsc", "runc"):
            assert "--cap-drop=ALL" in _run(runtime=runtime, caps=True)

    def test_only_setuid_setgid_readded(self):
        for c in RUN_COMBOS:
            args = _run(*c)
            cap_adds = {a for a in args if a.startswith("--cap-add=")}
            assert cap_adds <= ALLOWED_CAP_ADDS, (
                f"unexpected cap-add for {_run_combo_id(c)}: {cap_adds - ALLOWED_CAP_ADDS}"
            )
            # Forbid the bare space-separated forms so a future switch can't
            # silently disarm the glued-form denylist.
            assert "--cap-add" not in args, f"bare --cap-add token for {_run_combo_id(c)}"
            assert "--cap-drop" not in args, f"bare --cap-drop token for {_run_combo_id(c)}"

    def test_runsc_runtime_emitted(self):
        assert "--runtime=runsc" in _run(runtime="runsc")

    def test_runc_runtime_is_implicit(self):
        """runc is docker's default and is never named (some daemons reject
        ``--runtime=runc``); the only runtime ever emitted is runsc."""
        runtime_flags = [a for a in _run(runtime="runc") if a.startswith("--runtime=")]
        assert runtime_flags == []

    def test_runtime_flag_is_only_ever_runsc(self):
        for c in RUN_COMBOS:
            for a in _run(*c):
                if a.startswith("--runtime="):
                    assert a == "--runtime=runsc", (
                        f"unexpected runtime flag for {_run_combo_id(c)}: {a}"
                    )

    def test_run_carries_every_base_isolation_flag(self):
        """Belt and suspenders: every flag base_isolation_args(auto_remove=False)
        emits must survive into the session run command unchanged. If a future
        refactor stops starting from base_isolation_args, this fails loudly."""
        for c in RUN_COMBOS:
            runtime, caps, _memory, _cpu = c
            base = base_isolation_args(
                "tako-session-test",
                runtime=runtime,
                enable_cap_restrictions=caps,
                auto_remove=False,
            )
            args = _run(*c)
            for flag in base:
                assert flag in args, f"base flag {flag!r} dropped for {_run_combo_id(c)}"


# ---------------------------------------------------------------------------
# build_session_run_command: session specifics
# ---------------------------------------------------------------------------


class TestRunSessionSpecifics:
    """The flags that make a session container different from a per-job one:
    detached, no egress, long-lived (never --rm), labeled, writable workspace."""

    def test_always_detached(self):
        for c in RUN_COMBOS:
            assert "-d" in _run(*c), f"-d missing for {_run_combo_id(c)}"

    def test_always_network_none(self):
        """Phase 1 sessions have NO egress."""
        for c in RUN_COMBOS:
            assert "--network=none" in _run(*c), f"--network=none missing for {_run_combo_id(c)}"
            assert "--network=bridge" not in _run(*c), _run_combo_id(c)

    def test_never_rm(self):
        """A session is long-lived: --rm would let the daemon reap it the
        instant the keepalive process exits. auto_remove must be False."""
        for c in RUN_COMBOS:
            assert "--rm" not in _run(*c), f"--rm leaked into session run for {_run_combo_id(c)}"

    def test_session_id_label_present(self):
        for c in RUN_COMBOS:
            assert f"--label={SESSION_ID_LABEL}={SESSION_ID}" in _run(*c), _run_combo_id(c)

    def test_workspace_mount_present_and_read_write(self):
        """The workspace is the only writable cross-exec surface: it must be
        mounted -v at /workspace, pointing at workspace_dir, and NOT :ro."""
        for c in RUN_COMBOS:
            args = _run(*c)
            assert "-v" in args, f"-v missing for {_run_combo_id(c)}"
            idx = args.index("-v")
            mount = args[idx + 1]
            assert mount == f"{WORKSPACE_DIR}:/workspace", (
                f"unexpected workspace mount for {_run_combo_id(c)}: {mount!r}"
            )
            assert not mount.endswith(":ro"), f"workspace mounted read-only for {_run_combo_id(c)}"
            assert mount.endswith(":/workspace"), _run_combo_id(c)

    def test_only_one_volume_mount(self):
        """Exactly one -v (the workspace). No surprise extra host binds."""
        for c in RUN_COMBOS:
            args = _run(*c)
            assert args.count("-v") == 1, f"unexpected number of -v for {_run_combo_id(c)}"
            assert not any(a.startswith("--volume") for a in args), _run_combo_id(c)
            assert not any(a.startswith("--mount") for a in args), _run_combo_id(c)

    def test_memory_limit_present_when_given_absent_when_none(self):
        with_mem = _run(memory="512m")
        assert "--memory=512m" in with_mem
        without = _run(memory=None)
        assert not any(a.startswith("--memory") for a in without)

    def test_cpu_limit_present_when_given_absent_when_none(self):
        with_cpu = _run(cpu="1.5")
        assert "--cpus=1.5" in with_cpu
        without = _run(cpu=None)
        assert not any(a.startswith("--cpus") for a in without)

    def test_image_present(self):
        for c in RUN_COMBOS:
            assert IMAGE in _run(*c), f"image missing for {_run_combo_id(c)}"

    def test_keepalive_appended_last_by_default(self):
        for c in RUN_COMBOS:
            args = _run(*c)
            assert args[-2:] == ["sleep", "infinity"], (
                f"default keepalive not appended last for {_run_combo_id(c)}: {args[-3:]}"
            )
            # And it lands AFTER the image (image then keepalive).
            assert args.index(IMAGE) < len(args) - 2, _run_combo_id(c)

    def test_custom_keepalive_appended_after_image(self):
        args = _run(keepalive_cmd=["tail", "-f", "/dev/null"])
        assert args[-3:] == ["tail", "-f", "/dev/null"]
        assert args.index(IMAGE) == len(args) - 4

    def test_keepalive_is_last_so_flags_precede_image(self):
        """The image and keepalive are the trailing positional args; no docker
        flag may appear after the image (docker would treat it as a container
        arg, not a run flag)."""
        for c in RUN_COMBOS:
            args = _run(*c)
            img_idx = args.index(IMAGE)
            trailing = args[img_idx + 1 :]
            # Everything after the image is the keepalive command, none of which
            # should look like a docker run flag we care about.
            assert all(not t.startswith("--network") for t in trailing), _run_combo_id(c)
            assert all(not t.startswith("--memory") for t in trailing), _run_combo_id(c)


# ---------------------------------------------------------------------------
# build_session_run_command: no dangerous flags
# ---------------------------------------------------------------------------


class TestRunNoDangerousFlags:
    @pytest.mark.parametrize("combo", RUN_COMBOS, ids=_run_combo_id)
    def test_no_dangerous_flag_in_any_combo(self, combo):
        args = _run(*combo)
        for arg in args:
            for bad in DANGEROUS_FLAG_PREFIXES:
                assert not arg.startswith(bad), (
                    f"dangerous flag {arg!r} emitted for {_run_combo_id(combo)}"
                )

    @pytest.mark.parametrize("combo", RUN_COMBOS, ids=_run_combo_id)
    def test_no_host_root_bind(self, combo):
        """The single -v must never mount the host root."""
        args = _run(*combo)
        for arg in args:
            for bad in HOST_ROOT_MOUNTS:
                assert arg != bad, f"host-root bind {arg!r} for {_run_combo_id(combo)}"

    @pytest.mark.parametrize("combo", RUN_COMBOS, ids=_run_combo_id)
    def test_never_runs_as_root(self, combo):
        args = _run(*combo)
        assert "-u" not in args or args[args.index("-u") + 1] not in ("0", "0:0", "root")
        assert "--user=0" not in args
        assert "--user=root" not in args


# ---------------------------------------------------------------------------
# build_session_exec_command
# ---------------------------------------------------------------------------

EXEC_TIMEOUTS = (None, 30, 30.0, 0.5)


def _exec(command=("python", "-c", "print(1)"), timeout=None, **kw):
    return build_session_exec_command(
        "tako-session-test",
        command=list(command),
        timeout_seconds=timeout,
        **kw,
    )


class TestExecCommand:
    def test_command_is_docker_exec(self):
        assert _exec()[:2] == ["docker", "exec"]

    def test_targets_named_container(self):
        assert "tako-session-test" in _exec()

    def test_runs_in_workspace_by_default(self):
        args = _exec()
        assert "-w" in args
        assert args[args.index("-w") + 1] == "/workspace"

    def test_respects_custom_workdir(self):
        args = _exec(workdir="/workspace/sub")
        assert args[args.index("-w") + 1] == "/workspace/sub"

    def test_drops_to_unprivileged_user(self):
        """Exec must run as the unprivileged sandbox user (uid 1000), never
        root. Mirrors the run path's --user=1000:1000."""
        args = _exec()
        assert "-u" in args, "exec must drop privileges with -u"
        assert args[args.index("-u") + 1] == "1000:1000"

    @pytest.mark.parametrize("timeout", EXEC_TIMEOUTS)
    def test_never_runs_as_root(self, timeout):
        args = _exec(timeout=timeout)
        # -u value is never root.
        assert args[args.index("-u") + 1] not in ("0", "0:0", "root")
        assert "--user=0" not in args
        assert "--user=0:0" not in args
        assert "--user=root" not in args
        # No -u 0 pair anywhere.
        for i, tok in enumerate(args[:-1]):
            if tok == "-u":
                assert args[i + 1] not in ("0", "0:0", "root"), "exec drops to root"

    @pytest.mark.parametrize("timeout", EXEC_TIMEOUTS)
    def test_no_dangerous_flag(self, timeout):
        args = _exec(timeout=timeout)
        for arg in args:
            for bad in DANGEROUS_FLAG_PREFIXES:
                assert not arg.startswith(bad), (
                    f"dangerous flag {arg!r} in exec (timeout={timeout})"
                )

    def test_no_timeout_wrapper_when_none(self):
        args = _exec(timeout=None)
        assert "timeout" not in args
        # The user command is appended directly after the container name.
        assert args[-3:] == ["python", "-c", "print(1)"]

    def test_timeout_wraps_command_when_given(self):
        args = _exec(timeout=30)
        assert "timeout" in args, "GNU timeout wrapper missing"
        t_idx = args.index("timeout")
        # The duration carries an 's' suffix and the user command follows.
        assert f"{30}s" in args, "timeout duration missing"
        assert args[-3:] == ["python", "-c", "print(1)"], "user command not after timeout wrapper"
        # The wrapper sits before the user command.
        assert t_idx < args.index("python")

    def test_timeout_targets_the_user_command_not_the_container(self):
        """The timeout wrapper must come AFTER the container name (it wraps the
        in-container command), never before docker's own args."""
        args = _exec(timeout=30)
        assert args.index("timeout") > args.index("tako-session-test")

    def test_float_timeout_formatted(self):
        args = _exec(timeout=0.5)
        assert "0.5s" in args
