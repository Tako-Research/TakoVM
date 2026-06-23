"""
Tests for the Sandbox class (library mode).

These tests verify the direct Docker sandbox execution without a server.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from tako_vm.constants import UV_CACHE_TMP_DIR, UV_CACHE_VOLUME, UV_CACHE_VOLUME_DIR
from tako_vm.sandbox import DEFAULT_STARTUP_TIMEOUT, Sandbox, SandboxResult
from tako_vm.sandbox import run as sandbox_run


def _make_workspace_dirs(tmp_path):
    """Create code/input/output dirs for _build_docker_command unit tests."""
    code_dir = tmp_path / "code"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    code_dir.mkdir()
    input_dir.mkdir()
    output_dir.mkdir()
    return code_dir, input_dir, output_dir


class TestSandboxResult:
    """Tests for SandboxResult dataclass."""

    def test_sandbox_result_defaults(self):
        """SandboxResult has correct defaults."""
        result = SandboxResult()
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.success is True
        assert result.output is None
        assert result.error is None
        assert result.duration_ms is None

    def test_sandbox_result_with_values(self):
        """SandboxResult stores provided values."""
        result = SandboxResult(
            stdout="hello\n",
            stderr="warning\n",
            exit_code=1,
            success=False,
            output={"key": "value"},
            error="Something went wrong",
            duration_ms=150,
        )
        assert result.stdout == "hello\n"
        assert result.stderr == "warning\n"
        assert result.exit_code == 1
        assert result.success is False
        assert result.output == {"key": "value"}
        assert result.error == "Something went wrong"
        assert result.duration_ms == 150


class TestSandboxBasic:
    """Basic Sandbox execution tests."""

    def test_sandbox_simple_print(self):
        """Execute simple print statement."""
        with Sandbox() as sb:
            result = sb.run("print('hello world')")

        assert result.success is True
        assert result.exit_code == 0
        assert "hello world" in result.stdout

    def test_sandbox_arithmetic(self):
        """Execute arithmetic and print result."""
        with Sandbox() as sb:
            result = sb.run("print(1 + 2 + 3)")

        assert result.success is True
        assert "6" in result.stdout

    def test_sandbox_multiline_code(self):
        """Execute multiline Python code."""
        code = """
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)

print(factorial(5))
"""
        with Sandbox() as sb:
            result = sb.run(code)

        assert result.success is True
        assert "120" in result.stdout

    def test_sandbox_without_context_manager(self):
        """Sandbox works without context manager."""
        sb = Sandbox()
        result = sb.run("print('no context')")

        assert result.success is True
        assert "no context" in result.stdout


class TestSandboxInputOutput:
    """Tests for input/output data handling."""

    def test_sandbox_with_input_data(self):
        """Input data is accessible in container."""
        code = """
import json
with open('/input/data.json') as f:
    data = json.load(f)
print(f"x={data['x']}, y={data['y']}")
"""
        with Sandbox() as sb:
            result = sb.run(code, input_data={"x": 10, "y": 20})

        assert result.success is True
        assert "x=10" in result.stdout
        assert "y=20" in result.stdout

    def test_sandbox_output_json(self):
        """Output JSON is parsed and returned."""
        code = """
import json
result = {"sum": 30, "product": 200}
with open('/output/result.json', 'w') as f:
    json.dump(result, f)
print("Done")
"""
        with Sandbox() as sb:
            result = sb.run(code)

        assert result.success is True
        assert result.output == {"sum": 30, "product": 200}
        assert "Done" in result.stdout

    def test_sandbox_input_and_output(self):
        """Full input/output pipeline."""
        code = """
import json
with open('/input/data.json') as f:
    data = json.load(f)
result = {"sum": data['a'] + data['b']}
with open('/output/result.json', 'w') as f:
    json.dump(result, f)
"""
        with Sandbox() as sb:
            result = sb.run(code, input_data={"a": 15, "b": 25})

        assert result.success is True
        assert result.output == {"sum": 40}


class TestSandboxErrors:
    """Tests for error handling."""

    def test_sandbox_syntax_error(self):
        """Syntax errors are captured."""
        with Sandbox() as sb:
            result = sb.run("def broken(")

        assert result.success is False
        assert result.exit_code != 0
        # Stderr should contain syntax error info
        assert "SyntaxError" in result.stderr or "syntax" in result.stderr.lower()

    def test_sandbox_runtime_error(self):
        """Runtime errors are captured."""
        with Sandbox() as sb:
            result = sb.run("print(1/0)")

        assert result.success is False
        assert result.exit_code != 0
        assert "ZeroDivisionError" in result.stderr

    def test_sandbox_import_error(self):
        """Import errors for non-existent packages."""
        with Sandbox() as sb:
            result = sb.run("import nonexistent_package_12345")

        assert result.success is False
        assert "ModuleNotFoundError" in result.stderr or "ImportError" in result.stderr

    def test_sandbox_invalid_output_json(self):
        """Invalid JSON in output file is handled."""
        code = """
with open('/output/result.json', 'w') as f:
    f.write('not valid json {')
print("Done")
"""
        with Sandbox() as sb:
            result = sb.run(code)

        # Execution succeeds but output is None (couldn't parse)
        assert result.success is True
        assert result.output is None


class TestSandboxTimeout:
    """Tests for timeout handling."""

    def test_sandbox_respects_timeout(self):
        """Long-running code is killed after timeout."""
        code = """
import time
print("Starting...")
time.sleep(30)
print("Done")
"""
        with Sandbox(timeout=2) as sb:
            result = sb.run(code)

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

    def test_sandbox_run_timeout_override(self):
        """Per-run timeout overrides default."""
        code = """
import time
time.sleep(30)
print("Done")
"""
        with Sandbox(timeout=60) as sb:
            result = sb.run(code, timeout=2)

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

    def test_sandbox_fast_code_succeeds(self):
        """Code that finishes quickly succeeds."""
        with Sandbox(timeout=30) as sb:
            result = sb.run("print('fast')")

        assert result.success is True
        assert result.duration_ms is not None
        assert result.duration_ms < 30000  # Less than 30 seconds


class TestSandboxRequirements:
    """Tests for runtime package installation."""

    def test_sandbox_with_requirements(self):
        """Install and use packages at runtime."""
        code = """
import requests
print(f"requests version: {requests.__version__}")
"""
        with Sandbox(allow_runtime_requirements=True) as sb:
            result = sb.run(code, requirements=["requests"])

        assert result.success is True
        assert "requests version:" in result.stdout

    def test_sandbox_multiple_requirements(self):
        """Install multiple packages."""
        code = """
import requests
import httpx
print(f"requests: {requests.__version__}")
print(f"httpx: {httpx.__version__}")
"""
        with Sandbox(allow_runtime_requirements=True) as sb:
            result = sb.run(code, requirements=["requests", "httpx"])

        assert result.success is True
        assert "requests:" in result.stdout
        assert "httpx:" in result.stdout

    def test_sandbox_versioned_requirement(self):
        """Install specific package versions."""
        code = """
import requests
print(f"version: {requests.__version__}")
"""
        with Sandbox(allow_runtime_requirements=True) as sb:
            result = sb.run(code, requirements=["requests>=2.20.0"])

        assert result.success is True
        assert "version:" in result.stdout

    def test_sandbox_rejects_requirements_by_default(self):
        """Runtime dependency installation is opt-in."""
        with Sandbox() as sb:
            result = sb.run("print('hi')", requirements=["requests"])

        assert result.success is False
        assert result.exit_code == -1
        assert result.error is not None
        assert "Runtime dependency installation is disabled" in result.error

    def test_sandbox_dependency_proxy_url_validation(self):
        """Sandbox validates dependency proxy URL."""
        with Sandbox(dependency_proxy_url=" https://proxy.example:8443 ") as sb:
            assert sb.config.dependency_proxy_url == "https://proxy.example:8443"

        with pytest.raises(ValueError, match="dependency_proxy_url"):
            Sandbox(dependency_proxy_url="file:///tmp/proxy")

        with pytest.raises(ValueError, match="path, query, or fragment"):
            Sandbox(dependency_proxy_url="https://proxy.example:8443/proxy")

    def test_sandbox_dependency_proxy_is_scoped_to_runtime_requirements(self, tmp_path):
        """Sandbox passes proxy only when runtime requirements are present."""
        code_dir = tmp_path / "code"
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        code_dir.mkdir()
        input_dir.mkdir()
        output_dir.mkdir()

        sb = Sandbox(
            allow_runtime_requirements=True,
            dependency_proxy_url="https://proxy.example:8443",
        )
        cmd, _ = sb._build_docker_command(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
            requirements=["requests"],
        )

        assert "--env=TAKO_DEPENDENCY_PROXY_URL=https://proxy.example:8443" in cmd
        assert not any(arg.startswith("--env=HTTP_PROXY=") for arg in cmd)

        (input_dir / "_requirements.txt").unlink()
        cmd, _ = sb._build_docker_command(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
        )

        assert not any(arg.startswith("--env=TAKO_DEPENDENCY_PROXY_URL=") for arg in cmd)

    def test_sandbox_rejects_invalid_requirement(self, tmp_path):
        """Invalid pip requirements raise instead of being silently dropped."""
        code_dir, input_dir, output_dir = _make_workspace_dirs(tmp_path)

        sb = Sandbox(allow_runtime_requirements=True)
        with pytest.raises(ValueError, match="Invalid pip requirement") as excinfo:
            sb._build_docker_command(
                code_dir=code_dir,
                input_dir=input_dir,
                output_dir=output_dir,
                timeout=30,
                requirements=["requests", "evil`touch /tmp/pwned`"],
            )

        # The error names the offending requirement
        assert "evil`touch /tmp/pwned`" in str(excinfo.value)
        # Nothing was written before the validation failure
        assert not (input_dir / "_requirements.txt").exists()

    def test_sandbox_policy_checked_before_validation(self, tmp_path):
        """An all-invalid requirements list cannot bypass the policy check."""
        code_dir, input_dir, output_dir = _make_workspace_dirs(tmp_path)

        sb = Sandbox()  # allow_runtime_requirements=False
        with pytest.raises(ValueError, match="Runtime dependency installation is disabled"):
            sb._build_docker_command(
                code_dir=code_dir,
                input_dir=input_dir,
                output_dir=output_dir,
                timeout=30,
                requirements=["evil`touch /tmp/pwned`"],
            )

    @pytest.mark.parametrize(
        ("cache_enabled", "expect_cache_mount"),
        [(False, False), (True, True)],
    )
    def test_sandbox_dependency_cache_is_opt_in(self, tmp_path, cache_enabled, expect_cache_mount):
        """Sandbox mounts the shared uv cache only when explicitly enabled."""
        code_dir = tmp_path / "code"
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        code_dir.mkdir()
        input_dir.mkdir()
        output_dir.mkdir()

        sb = Sandbox(
            allow_runtime_requirements=True,
            enable_runtime_dependency_cache=cache_enabled,
        )
        cmd, _ = sb._build_docker_command(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
            requirements=["requests"],
        )

        cache_mount = f"--mount=type=volume,source={UV_CACHE_VOLUME},target={UV_CACHE_VOLUME_DIR}"
        expected_cache_dir = UV_CACHE_VOLUME_DIR if cache_enabled else UV_CACHE_TMP_DIR
        assert (cache_mount in cmd) is expect_cache_mount
        assert f"--env=UV_CACHE_DIR={expected_cache_dir}" in cmd


class TestSandboxTimeoutEnforcement:
    """Unit tests for in-container timeout enforcement (no container needed)."""

    def test_docker_command_includes_timeout_env_vars(self, tmp_path):
        """Both timeout env vars are passed so the container self-enforces limits."""
        code_dir, input_dir, output_dir = _make_workspace_dirs(tmp_path)

        sb = Sandbox(startup_timeout=90)
        cmd, _ = sb._build_docker_command(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=7,
        )

        assert "--env=TAKO_STARTUP_TIMEOUT=90" in cmd
        assert "--env=TAKO_EXECUTION_TIMEOUT=7" in cmd
        # Env vars must come before the image name to be docker run options
        assert cmd.index("--env=TAKO_EXECUTION_TIMEOUT=7") < cmd.index(sb.config.image)

    def test_docker_command_default_startup_timeout(self, tmp_path):
        """Default startup timeout matches the server default (120s)."""
        code_dir, input_dir, output_dir = _make_workspace_dirs(tmp_path)

        sb = Sandbox()
        assert sb.config.startup_timeout == DEFAULT_STARTUP_TIMEOUT
        cmd, _ = sb._build_docker_command(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
        )

        assert f"--env=TAKO_STARTUP_TIMEOUT={DEFAULT_STARTUP_TIMEOUT}" in cmd
        assert "--env=TAKO_EXECUTION_TIMEOUT=30" in cmd

    def test_subprocess_budget_includes_startup_timeout_with_requirements(self, monkeypatch):
        """Dependency install time is budgeted separately from code timeout."""
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        sb = Sandbox(allow_runtime_requirements=True, startup_timeout=100)
        sb._image_checked = True  # Skip docker image inspect
        monkeypatch.setattr(
            Sandbox,
            "_build_docker_command",
            lambda self, **kwargs: (["docker", "run", "fake"], "fake-container"),
        )
        monkeypatch.setattr("tako_vm.sandbox.subprocess.run", fake_run)
        # The post-run container removal makes its own subprocess.run call;
        # stub it out so it doesn't clobber the recorded docker-run timeout.
        monkeypatch.setattr("tako_vm.sandbox.remove_container", lambda name: True)

        sb.run("print('hi')", timeout=10, requirements=["requests"])
        assert recorded["timeout"] == 100 + 10 + 5

        sb.run("print('hi')", timeout=10)
        assert recorded["timeout"] == 10 + 5

    def test_timeout_preserves_partial_output(self, monkeypatch):
        """Partial stdout/stderr from TimeoutExpired is surfaced in the result."""

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd,
                kwargs.get("timeout"),
                output=b"partial stdout",
                stderr=b"partial stderr",
            )

        removed = {}
        sb = Sandbox()
        sb._image_checked = True  # Skip docker image inspect
        monkeypatch.setattr(
            Sandbox,
            "_build_docker_command",
            lambda self, **kwargs: (["docker", "run", "fake"], "fake-container"),
        )
        monkeypatch.setattr("tako_vm.sandbox.subprocess.run", fake_run)
        monkeypatch.setattr(
            "tako_vm.sandbox.remove_container", lambda name: removed.setdefault("name", name)
        )

        result = sb.run("print('hi')", timeout=2)

        assert result.success is False
        assert result.exit_code == -1
        assert result.stdout == "partial stdout"
        assert result.stderr == "partial stderr"
        # TimeoutExpired is the *host backstop* path, deliberately worded to
        # distinguish it from the in-container (exit 124) timeout.
        assert "host backstop" in result.error.lower()
        assert "in-container timeout did not fire" in result.error.lower()
        # The container is no longer started with --rm, so the timeout path
        # must kill+remove it (docker rm -f) itself.
        assert removed["name"] == "fake-container"


class TestSandboxOOMDetection:
    """Unit tests for exit-137 OOM verification and container removal.

    The sandbox no longer runs containers with ``--rm``: a 137 exit is
    checked against ``docker inspect .State.OOMKilled`` (the only
    authoritative OOM signal) and the container is removed in a finally on
    every exit path. These tests monkeypatch subprocess.run and the docker
    helpers, so no container is needed.
    """

    @staticmethod
    def _make_sandbox(monkeypatch, returncode, events, oom_killed):
        """Build a Sandbox whose docker run is faked to exit with returncode."""

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=returncode, stdout="", stderr=""
            )

        def fake_inspect(name):
            events.append(("inspect", name))
            return oom_killed

        def fake_remove(name):
            events.append(("remove", name))
            return True

        sb = Sandbox(memory_limit="256m")
        sb._image_checked = True  # Skip docker image inspect
        monkeypatch.setattr(
            Sandbox,
            "_build_docker_command",
            lambda self, **kwargs: (["docker", "run", "fake"], "fake-container"),
        )
        monkeypatch.setattr("tako_vm.sandbox.subprocess.run", fake_run)
        monkeypatch.setattr("tako_vm.sandbox.inspect_oom_killed", fake_inspect)
        monkeypatch.setattr("tako_vm.sandbox.remove_container", fake_remove)
        return sb

    def test_exit_137_oom_killed_reports_oom(self, monkeypatch):
        """137 + State.OOMKilled=true -> OOM error including the memory limit."""
        events = []
        sb = self._make_sandbox(monkeypatch, returncode=137, events=events, oom_killed=True)

        result = sb.run("x = 'a' * 10**9")

        assert result.success is False
        assert result.exit_code == 137
        assert "out of memory" in result.error
        assert "memory_limit=256m" in result.error
        # Inspect must happen before the finally removes the container
        assert events == [("inspect", "fake-container"), ("remove", "fake-container")]

    def test_exit_137_not_oom_reports_sigkill(self, monkeypatch):
        """137 + State.OOMKilled=false -> killed-but-not-OOM error."""
        events = []
        sb = self._make_sandbox(monkeypatch, returncode=137, events=events, oom_killed=False)

        result = sb.run("import sys; sys.exit(137)")

        assert result.success is False
        assert result.exit_code == 137
        assert "killed (SIGKILL)" in result.error
        assert "not by the memory limit" in result.error
        assert "out of memory" not in result.error
        assert ("remove", "fake-container") in events

    def test_exit_137_inspect_failure_falls_back_to_oom(self, monkeypatch):
        """137 + inspect failed (None) -> assume OOM so a flaky inspect never loses one."""
        events = []
        sb = self._make_sandbox(monkeypatch, returncode=137, events=events, oom_killed=None)

        result = sb.run("x = 'a' * 10**9")

        assert result.success is False
        assert "out of memory" in result.error

    def test_container_removed_on_success(self, monkeypatch):
        """Without --rm, the sandbox must remove the container after a clean exit."""
        events = []
        sb = self._make_sandbox(monkeypatch, returncode=0, events=events, oom_killed=False)

        result = sb.run("print('hi')")

        assert result.success is True
        assert result.error is None
        assert ("remove", "fake-container") in events
        # No 137, so no inspect round-trip
        assert ("inspect", "fake-container") not in events

    def test_container_removed_on_failure(self, monkeypatch):
        """The container is removed after a non-zero, non-137 exit too."""
        events = []
        sb = self._make_sandbox(monkeypatch, returncode=1, events=events, oom_killed=False)

        result = sb.run("raise RuntimeError('boom')")

        assert result.success is False
        assert result.exit_code == 1
        assert result.error is None
        assert ("remove", "fake-container") in events
        assert ("inspect", "fake-container") not in events

    def test_no_container_removal_when_validation_fails(self, monkeypatch):
        """ValueError before docker run means there is no container to remove."""
        events = []

        def fake_remove(name):
            events.append(("remove", name))
            return True

        sb = Sandbox(memory_limit="256m")  # runtime requirements disabled
        sb._image_checked = True  # Skip docker image inspect
        monkeypatch.setattr("tako_vm.sandbox.remove_container", fake_remove)
        monkeypatch.setattr("tako_vm.sandbox.subprocess.run", _fail_if_called)

        result = sb.run("print('hi')", requirements=["pandas"])

        assert result.success is False
        assert "disabled" in result.error
        assert events == []

    def test_docker_command_omits_rm(self, tmp_path):
        """The run command must not use --rm so the exited container can be inspected."""
        code_dir, input_dir, output_dir = _make_workspace_dirs(tmp_path)

        sb = Sandbox()
        cmd, _ = sb._build_docker_command(
            code_dir=code_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            timeout=30,
        )

        assert "--rm" not in cmd


def _fail_if_called(*args, **kwargs):
    raise AssertionError("docker run must not be attempted when validation fails")


@pytest.mark.requires_host_mounts
class TestSandboxPackageDirs:
    """Tests for local package mounting.

    These tests require mounting host paths into Docker containers,
    which doesn't work in VM environments (e.g., Lima on macOS)
    or CI environments where temp paths may not be accessible to Docker.
    """

    def test_sandbox_with_package_dirs(self):
        """Mount local directory as package."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a local package
            pkg_dir = Path(tmpdir) / "my_utils"
            pkg_dir.mkdir()
            (pkg_dir / "__init__.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")

            # Run code that imports the package
            code = """
from my_utils import greet
print(greet('World'))
"""
            sb = Sandbox(package_dirs=[str(pkg_dir.parent)])
            result = sb.run(code)

        assert result.success is True
        assert "Hello, World!" in result.stdout

    def test_sandbox_multiple_package_dirs(self):
        """Mount multiple local directories."""
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            # Create first package
            pkg1 = Path(tmpdir1) / "utils1"
            pkg1.mkdir()
            (pkg1 / "__init__.py").write_text("VALUE = 'from utils1'\n")

            # Create second package
            pkg2 = Path(tmpdir2) / "utils2"
            pkg2.mkdir()
            (pkg2 / "__init__.py").write_text("VALUE = 'from utils2'\n")

            code = """
from utils1 import VALUE as V1
from utils2 import VALUE as V2
print(f'{V1} and {V2}')
"""
            sb = Sandbox(package_dirs=[str(pkg1.parent), str(pkg2.parent)])
            result = sb.run(code)

        assert result.success is True
        assert "from utils1" in result.stdout
        assert "from utils2" in result.stdout


class TestSandboxConfiguration:
    """Tests for Sandbox configuration."""

    def test_sandbox_custom_timeout(self):
        """Custom timeout is applied."""
        sb = Sandbox(timeout=5)
        assert sb.config.timeout == 5

    def test_sandbox_custom_memory_limit(self):
        """Custom memory limit is applied."""
        sb = Sandbox(memory_limit="1g")
        assert sb.config.memory_limit == "1g"

    def test_sandbox_custom_cpu_limit(self):
        """Custom CPU limit is applied."""
        sb = Sandbox(cpu_limit=2.0)
        assert sb.config.cpu_limit == 2.0

    def test_sandbox_network_enabled(self):
        """Network can be enabled."""
        sb = Sandbox(network_enabled=True)
        assert sb.config.network_enabled is True

    def test_sandbox_auto_build_disabled(self):
        """Auto-build can be disabled."""
        sb = Sandbox(auto_build=False, image="nonexistent-image:test")
        assert sb.auto_build is False


class TestSandboxConvenienceFunction:
    """Tests for the standalone run() function."""

    def test_run_function_simple(self):
        """Convenience run() function works."""
        result = sandbox_run("print('convenience')")

        assert result.success is True
        assert "convenience" in result.stdout

    def test_run_function_with_input(self):
        """Convenience run() with input data."""
        code = """
import json
with open('/input/data.json') as f:
    data = json.load(f)
print(data['message'])
"""
        result = sandbox_run(code, input_data={"message": "hello from run()"})

        assert result.success is True
        assert "hello from run()" in result.stdout

    def test_run_function_with_timeout(self):
        """Convenience run() respects timeout."""
        result = sandbox_run("import time; time.sleep(30)", timeout=2)

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()


class TestSandboxDuration:
    """Tests for execution duration tracking."""

    def test_duration_is_tracked(self):
        """Duration is recorded in result."""
        with Sandbox() as sb:
            result = sb.run("print('test')")

        assert result.duration_ms is not None
        assert result.duration_ms > 0

    def test_duration_reflects_code_time(self):
        """Duration reflects actual execution time."""
        code = """
import time
time.sleep(1)
print('done')
"""
        with Sandbox(timeout=30) as sb:
            result = sb.run(code)

        assert result.success is True
        assert result.duration_ms is not None
        # Should take at least 1000ms (1 second sleep)
        assert result.duration_ms >= 1000


class TestSandboxSecurity:
    """Tests for security isolation (basic checks)."""

    def test_sandbox_no_network_by_default(self):
        """Network is disabled by default."""
        sb = Sandbox()
        assert sb.config.network_enabled is False

    def test_sandbox_read_only_filesystem(self):
        """Container has read-only root filesystem."""
        # Try to write to root - should fail
        code = """
try:
    with open('/test.txt', 'w') as f:
        f.write('test')
    print('WRITE_SUCCEEDED')
except Exception as e:
    print(f'WRITE_FAILED: {type(e).__name__}')
"""
        with Sandbox() as sb:
            result = sb.run(code)

        assert result.success is True
        assert "WRITE_FAILED" in result.stdout

    def test_sandbox_tmp_is_writable(self):
        """Tmp directory is writable."""
        code = """
with open('/tmp/test.txt', 'w') as f:
    f.write('test')
with open('/tmp/test.txt') as f:
    print(f.read())
"""
        with Sandbox() as sb:
            result = sb.run(code)

        assert result.success is True
        assert "test" in result.stdout

    def test_sandbox_output_is_writable(self):
        """Output directory is writable."""
        code = """
with open('/output/test.txt', 'w') as f:
    f.write('output test')
print('written')
"""
        with Sandbox() as sb:
            result = sb.run(code)

        assert result.success is True
        assert "written" in result.stdout
