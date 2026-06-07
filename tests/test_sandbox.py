"""
Tests for the Sandbox class (library mode).

These tests verify the direct Docker sandbox execution without a server.
"""

import tempfile
from pathlib import Path

import pytest

from tako_vm.constants import UV_CACHE_VOLUME
from tako_vm.sandbox import Sandbox, SandboxResult
from tako_vm.sandbox import run as sandbox_run


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

        cache_mount = f"--mount=type=volume,source={UV_CACHE_VOLUME},target=/root/.cache/uv"
        expected_cache_dir = "/root/.cache/uv" if cache_enabled else "/tmp/uv-cache"
        assert (cache_mount in cmd) is expect_cache_mount
        assert f"--env=UV_CACHE_DIR={expected_cache_dir}" in cmd


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
