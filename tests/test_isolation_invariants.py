import pytest
from pydantic import ValidationError

from tako_vm.config import TakoVMConfig
from tako_vm.constants import UV_CACHE_VOLUME, uv_cache_volume
from tako_vm.execution import worker
from tako_vm.execution.docker import (
    CONTAINER_LABEL,
    EXECUTION_ID_LABEL,
    base_isolation_args,
    build_session_run_command,
    validate_session_workspace_dir,
)
from tako_vm.execution.worker import RuntimeUnavailableError, resolve_runtime

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
    "--mount",
    "--device",
)

ALLOWED_CAP_ADDS = {"--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=KILL"}

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
    def test_command_is_docker_run(self):
        assert _args()[:2] == ["docker", "run"]

    def test_always_read_only_rootfs(self):
        for c in ALL_COMBOS:
            args = _args(*c)
            assert "--read-only" in args, f"--read-only missing for {_combo_id(c)}"

    def test_always_init(self):
        for c in ALL_COMBOS:
            assert "--init" in _args(*c), f"--init missing for {_combo_id(c)}"

    def test_always_named_and_labeled(self):
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
        for c in ALL_COMBOS:
            args = _args(*c)
            cap_adds = {a for a in args if a.startswith("--cap-add=")}
            assert cap_adds <= ALLOWED_CAP_ADDS, (
                f"unexpected cap-add for {_combo_id(c)}: {cap_adds - ALLOWED_CAP_ADDS}"
            )
            assert "--cap-add" not in args, f"bare --cap-add token for {_combo_id(c)}"
            assert "--cap-drop" not in args, f"bare --cap-drop token for {_combo_id(c)}"

    def test_runsc_runtime_emitted(self):
        assert "--runtime=runsc" in _args(runtime="runsc")

    def test_runc_runtime_is_implicit(self):
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
        for c in ALL_COMBOS:
            args = _args(*c)
            assert "-v" not in args
            assert not any(a.startswith("--volume") for a in args), _combo_id(c)
            assert not any(a.startswith("--mount") for a in args), _combo_id(c)
            assert not any(a.startswith("--device") for a in args), _combo_id(c)


class TestNoDangerousFlags:
    @pytest.mark.parametrize("combo", ALL_COMBOS, ids=_combo_id)
    def test_no_dangerous_flag_in_any_combo(self, combo):
        args = _args(*combo)
        for arg in args:
            for bad in DANGEROUS_FLAG_PREFIXES:
                assert not arg.startswith(bad), (
                    f"dangerous flag {arg!r} emitted for {_combo_id(combo)}"
                )


class TestAutoRemoveContract:
    def test_auto_remove_true_emits_rm(self):
        assert "--rm" in _args(auto_remove=True)

    def test_auto_remove_false_omits_rm(self):
        assert "--rm" not in _args(auto_remove=False)


class TestExecutionIdLabel:
    def test_label_present_when_id_given(self):
        assert f"--label={EXECUTION_ID_LABEL}=exec-abc123" in _args(exec_id="exec-abc123")

    def test_label_absent_when_id_omitted(self):
        assert not any(a.startswith(f"--label={EXECUTION_ID_LABEL}=") for a in _args(exec_id=None))


class TestCapRestrictionsEscapeHatch:
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
        monkeypatch.setattr(worker, "check_gvisor_available", lambda: True)
        with pytest.raises(RuntimeUnavailableError):
            resolve_runtime(self._config(security_mode="strict", container_runtime="runc"))


class TestPermissiveModeFallback:
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


def _security_flags(argv):
    noise = ("--name=", "--label=tako-vm.execution-id", "--mount=type=bind", "--env=TAKO_")
    return {a for a in argv if not a.startswith(noise)}


def _worker_argv(cfg, tmp_path, requirements=None):
    import subprocess as _sp

    from tako_vm.execution.worker import DEFAULT_JOB_TYPE, CodeExecutor

    captured = {}

    def fake_run(cmd, *a, **kw):
        if "cmd" not in captured and list(cmd[:2]) == ["docker", "run"]:
            captured["cmd"] = list(cmd)

        class R:
            returncode, stdout, stderr = 0, "", ""

        return R()

    dirs = {}
    for name in ("code", "input", "output"):
        d = tmp_path / f"w-{name}"
        d.mkdir()
        dirs[name] = d

    executor = CodeExecutor(config=cfg)
    real_run = worker.subprocess.run
    worker.subprocess.run = fake_run
    try:
        executor._run_container(
            code_dir=dirs["code"],
            input_dir=dirs["input"],
            output_dir=dirs["output"],
            timeout=30,
            startup_timeout=120,
            job_type=DEFAULT_JOB_TYPE,
            extra_requirements=requirements,
            job_id="paritytest",
            meta_dir=None,
        )
    finally:
        worker.subprocess.run = real_run
    assert _sp
    return captured.get("cmd", [])


def _sandbox_argv(tmp_path, requirements=None):
    from tako_vm.sandbox import Sandbox

    dirs = {}
    for name in ("code", "input", "output"):
        d = tmp_path / f"s-{name}"
        d.mkdir()
        dirs[name] = d

    sb = Sandbox(allow_runtime_requirements=bool(requirements))
    sb._image_checked = True
    cmd, _ = sb._build_docker_command(
        code_dir=dirs["code"],
        input_dir=dirs["input"],
        output_dir=dirs["output"],
        timeout=30,
        requirements=requirements,
    )
    return cmd


class TestExecutionPathParity:
    @pytest.fixture(autouse=True)
    def _pin_config(self, monkeypatch, tmp_path):
        from tako_vm import config as config_mod

        monkeypatch.setattr(worker, "check_gvisor_available", lambda: False)
        cfg = TakoVMConfig(
            security_mode="permissive",
            container_runtime="runc",
            allow_runtime_requirements=True,
            data_dir=str(tmp_path / "data"),
        )
        cfg.resolve_paths()
        monkeypatch.setattr(config_mod, "_config", cfg)
        self.cfg = cfg

    @pytest.mark.parametrize("requirements", [None, ["idna"]], ids=["no-reqs", "with-reqs"])
    def test_paths_assemble_identical_security_flags(self, tmp_path, requirements):
        worker_flags = _security_flags(_worker_argv(self.cfg, tmp_path, requirements))
        sandbox_flags = _security_flags(_sandbox_argv(tmp_path, requirements))
        assert worker_flags, "worker argv was not captured"
        assert worker_flags == sandbox_flags, (
            "execution paths drifted.\n"
            f"  only in CodeExecutor: {sorted(worker_flags - sandbox_flags)}\n"
            f"  only in Sandbox:      {sorted(sandbox_flags - worker_flags)}"
        )

    @pytest.mark.parametrize("build", [_worker_argv, None], ids=["worker", "sandbox"])
    def test_seccomp_profile_applied_on_both_paths(self, tmp_path, build):
        argv = build(self.cfg, tmp_path) if build else _sandbox_argv(tmp_path)
        assert any(a.startswith("--security-opt=seccomp=") for a in argv), (
            "custom seccomp profile missing; enable_seccomp is on by default and "
            "the docs describe it as an always-on control"
        )

    @pytest.mark.parametrize("build", [_worker_argv, None], ids=["worker", "sandbox"])
    def test_tmp_is_noexec_without_runtime_requirements(self, tmp_path, build):
        argv = build(self.cfg, tmp_path) if build else _sandbox_argv(tmp_path)
        tmpfs = [a for a in argv if a.startswith("--tmpfs=/tmp")]
        assert tmpfs, "no /tmp tmpfs flag emitted"
        assert "noexec" in tmpfs[0], f"/tmp must be noexec when no deps are installed: {tmpfs[0]}"


class TestSeccompProfileStartsContainers:
    REQUIRED_PRCTL_OPTIONS = {1, 8, 15, 24, 47}

    def _profile(self):
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "tako_vm" / "seccomp_profile.json"
        return json.loads(path.read_text())

    def test_profile_is_default_deny(self):
        assert self._profile()["defaultAction"] == "SCMP_ACT_ERRNO"

    def test_required_prctl_options_allowed(self):
        allowed = set()
        for rule in self._profile()["syscalls"]:
            if rule.get("names") != ["prctl"] or rule.get("action") != "SCMP_ACT_ALLOW":
                continue
            args = rule.get("args") or []
            if len(args) == 1 and args[0].get("op") == "SCMP_CMP_EQ":
                allowed.add(args[0]["value"])
            elif not args:
                allowed.update(self.REQUIRED_PRCTL_OPTIONS)
        missing = self.REQUIRED_PRCTL_OPTIONS - allowed
        assert not missing, (
            f"seccomp profile blocks prctl option(s) {sorted(missing)} that the OCI "
            "runtime needs during container init; containers will fail to start"
        )

    def test_no_multi_value_prctl_rule(self):
        for rule in self._profile()["syscalls"]:
            if rule.get("names") != ["prctl"]:
                continue
            eq_on_zero = [
                a
                for a in (rule.get("args") or [])
                if a.get("index") == 0 and a.get("op") == "SCMP_CMP_EQ"
            ]
            assert len(eq_on_zero) <= 1, (
                "prctl rule lists multiple values in one rule; args are AND-ed, "
                f"so this rule matches nothing: {rule}"
            )


class TestSecureDefaults:
    def test_security_mode_defaults_to_strict(self):
        assert TakoVMConfig().security_mode == "strict"

    def test_container_runtime_defaults_to_gvisor(self):
        assert TakoVMConfig().container_runtime == "runsc"

    def test_server_binds_loopback_by_default(self):
        assert TakoVMConfig().is_loopback_host()

    def test_seccomp_and_caps_on_by_default(self):
        cfg = TakoVMConfig()
        assert cfg.enable_seccomp
        assert cfg.enable_cap_restrictions

    def test_runtime_deps_and_shared_cache_off_by_default(self):
        cfg = TakoVMConfig()
        assert not cfg.allow_runtime_requirements
        assert not cfg.enable_runtime_dependency_cache

    def test_unauthenticated_network_bind_is_refused(self):
        with pytest.raises(ValidationError):
            TakoVMConfig(server_host="0.0.0.0", api_auth_enabled=False)

    def test_unauthenticated_loopback_bind_is_allowed(self):
        assert TakoVMConfig(server_host="127.0.0.1", api_auth_enabled=False)

    def test_authenticated_network_bind_is_allowed(self):
        assert TakoVMConfig(server_host="0.0.0.0", api_auth_enabled=True, api_keys=["k" * 32])

    def test_explicit_opt_out_permits_unauthenticated_bind(self):
        cfg = TakoVMConfig(
            server_host="0.0.0.0",
            api_auth_enabled=False,
            allow_unauthenticated_network_access=True,
        )
        assert any("authentication is disabled" in w for w in cfg.security_warnings())


class TestDependencyCacheScoping:
    def test_distinct_scopes_get_distinct_volumes(self):
        assert uv_cache_volume("alpha") != uv_cache_volume("beta")

    def test_same_scope_is_stable(self):
        assert uv_cache_volume("alpha") == uv_cache_volume("alpha")

    def test_scope_is_sanitized_for_the_docker_cli(self):
        volume = uv_cache_volume("../../etc/passwd; rm -rf /")
        assert "/" not in volume and ";" not in volume and " " not in volume

    def test_empty_scope_does_not_produce_a_bare_shared_volume(self):
        assert uv_cache_volume("") != UV_CACHE_VOLUME


SESSIONS_ROOT = "/srv/sessions"


class TestSessionWorkspaceContainment:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "relative/path",
            "/srv/sessions/../../etc",
            "/",
            "/etc",
            "/root",
            "/var/run",
            "/var/lib/docker",
            "/etc/ssh",
            "/tmp/ws\x00/etc",
            "/tmp/ws\nmalicious",
        ],
    )
    def test_unsafe_workspace_dirs_are_refused(self, bad):
        with pytest.raises(ValueError):
            validate_session_workspace_dir(bad, SESSIONS_ROOT)

    def test_ordinary_workspace_dir_is_accepted(self):
        assert validate_session_workspace_dir(
            "/srv/sessions/sess-abc/workspace", SESSIONS_ROOT
        ) == ("/srv/sessions/sess-abc/workspace")

    def test_bare_dot_components_are_normalized_not_rejected(self):
        assert validate_session_workspace_dir("/srv/sessions/./x", SESSIONS_ROOT) == (
            "/srv/sessions/x"
        )

    def test_symlinked_workspace_cannot_redirect_the_mount(self, tmp_path):
        link = tmp_path / "workspace"
        link.symlink_to("/etc")
        with pytest.raises(ValueError):
            validate_session_workspace_dir(str(link), str(tmp_path))

    def test_builder_refuses_to_emit_an_unsafe_mount(self):
        with pytest.raises(ValueError):
            build_session_run_command(
                "tako-session-x",
                runtime="runsc",
                image="code-executor:latest",
                workspace_dir="/srv/sessions/../../../etc",
                sessions_root=SESSIONS_ROOT,
                session_id="sess-x",
            )

    @pytest.mark.parametrize(
        "outside",
        [
            "/opt/app/secrets",
            "/srv/other-tenant/data",
            "/var/lib/postgresql/data",
            "/srv/sessions-evil/x",
            "/srv/sessions",
        ],
    )
    def test_workspace_outside_sessions_root_is_refused(self, outside):
        with pytest.raises(ValueError):
            validate_session_workspace_dir(outside, SESSIONS_ROOT)

    def test_symlink_inside_root_cannot_point_outside_it(self, tmp_path):
        root = tmp_path / "sessions"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (root / "sess-a").symlink_to(elsewhere)
        with pytest.raises(ValueError):
            validate_session_workspace_dir(str(root / "sess-a"), str(root))

    def test_symlinked_parent_of_uncreated_workspace_is_caught(self, tmp_path):
        root = tmp_path / "sessions"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (root / "sess-a").symlink_to(elsewhere)
        with pytest.raises(ValueError):
            validate_session_workspace_dir(str(root / "sess-a" / "workspace"), str(root))

    def test_real_directory_inside_root_is_accepted(self, tmp_path):
        root = tmp_path / "sessions"
        workspace = root / "sess-a" / "workspace"
        workspace.mkdir(parents=True)
        assert validate_session_workspace_dir(str(workspace), str(root)) == str(workspace)

    @pytest.mark.parametrize("bad_root", ["", "relative/root", "/", "/etc", "/srv/x/../.."])
    def test_unsafe_sessions_root_is_refused(self, bad_root):
        with pytest.raises(ValueError):
            validate_session_workspace_dir("/srv/sessions/sess-a", bad_root)

    @pytest.mark.parametrize(
        ("nested", "misconfigured_root"),
        [
            ("/etc/ssh/keys", "/etc/ssh"),
            ("/var/lib/docker/volumes/x", "/var/lib/docker/volumes"),
            ("/var/run/docker.sock/x", "/var/run/docker.sock"),
            ("/run/secrets/x/y", "/run/secrets/x"),
            ("/proc/1/root/x", "/proc/1/root"),
        ],
    )
    def test_system_trees_are_refused_at_any_depth(self, nested, misconfigured_root):
        with pytest.raises(ValueError):
            validate_session_workspace_dir(nested, misconfigured_root)

    def test_default_data_dir_locations_remain_usable(self):
        for root in ("/root/.tako_vm/sessions", "/home/tako/.tako_vm/sessions"):
            assert validate_session_workspace_dir(f"{root}/sess-a", root) == f"{root}/sess-a"

    def test_config_exposes_sessions_dir_under_data_dir(self, tmp_path):
        cfg = TakoVMConfig(data_dir=str(tmp_path))
        assert cfg.sessions_dir == cfg.data_dir / "sessions"

    def test_builder_mounts_only_the_validated_workspace(self):
        cmd = build_session_run_command(
            "tako-session-x",
            runtime="runsc",
            image="code-executor:latest",
            workspace_dir="/srv/sessions/sess-x/workspace",
            sessions_root=SESSIONS_ROOT,
            session_id="sess-x",
        )
        assert cmd[cmd.index("-v") + 1] == "/srv/sessions/sess-x/workspace:/workspace"

    def test_builder_refuses_workspace_outside_sessions_root(self):
        with pytest.raises(ValueError):
            build_session_run_command(
                "tako-session-x",
                runtime="runsc",
                image="code-executor:latest",
                workspace_dir="/opt/app/secrets",
                sessions_root=SESSIONS_ROOT,
                session_id="sess-x",
            )
