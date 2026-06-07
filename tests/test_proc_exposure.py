"""
Test /proc filesystem exposure vulnerability.

These tests demonstrate the security vulnerability described in:
https://equixly.com/blog/2025/11/04/path-traversal-ai-containers/

WARNING: These are intentionally vulnerable scenarios to document the risk.
"""

import pytest

from tako_vm.config import TakoVMConfig
from tako_vm.execution.worker import CodeExecutor


@pytest.fixture
def executor():
    """Create a CodeExecutor for testing."""
    return CodeExecutor(
        config=TakoVMConfig(
            allow_runtime_requirements=True,
            security_mode="permissive",
            enable_cap_restrictions=False,
            enable_seccomp=False,
        )
    )


class TestProcExposure:
    """Test cases demonstrating /proc filesystem exposure."""

    def test_environment_variable_leakage(self, executor):
        """
        Demonstrate that user code can read /proc/self/environ.

        VULNERABILITY: User code can access all environment variables including:
        - dependency specifications if they are passed through environment variables
        - Custom job_type.environment variables
        """
        code = """
import json

# Read environment variables from /proc (bypassing os.environ for demonstration)
with open('/proc/self/environ', 'rb') as f:
    env_data = f.read()

# Parse null-byte separated key=value pairs
env_vars = {}
for entry in env_data.split(b'\\x00'):
    if b'=' in entry:
        key, value = entry.split(b'=', 1)
        try:
            env_vars[key.decode('utf-8')] = value.decode('utf-8')
        except UnicodeDecodeError:
            pass  # Skip binary data

# Extract Tako-specific variables
tako_vars = {k: v for k, v in env_vars.items() if k.startswith('TAKO_')}

# Exfiltrate via output
with open('/output/result.json', 'w') as f:
    json.dump({
        "total_vars": len(env_vars),
        "tako_vars": tako_vars,
        "has_path": "PATH" in env_vars,
        "has_home": "HOME" in env_vars
    }, f)
"""

        job = {
            "id": "test-proc-environ",
            "code": code,
            "input_data": {},
            "requirements": ["requests"],
        }

        result = executor.execute_job(job)

        # /proc environment access remains possible, but dependency specs should not leak.
        assert result["success"], (
            f"Execution failed: stderr={result.get('stderr')}, error={result.get('error')}"
        )
        output = result["output"]

        # Verify environment variable leakage
        assert output["total_vars"] > 0
        assert output["has_path"] is True
        assert output["has_home"] is True

        # Dependency requirements are passed via /input/_requirements.txt, not env.
        assert "TAKO_REQUIREMENTS" not in output["tako_vars"]

        print("\n/proc environment exposure observed:")
        print(f"   - Total environment variables exposed: {output['total_vars']}")
        print(f"   - Tako variables exposed: {list(output['tako_vars'].keys())}")
        print("   - Requirements leaked: no")

    def test_binary_extraction_via_proc_exe(self, executor):
        """
        Demonstrate that user code can extract the Python binary via /proc/self/exe.

        VULNERABILITY: Attackers can reverse engineer the Python runtime to find:
        - Python version and patchlevel
        - Compiled C extensions
        - Potential vulnerabilities
        """
        code = """
import os
import json

# Get info about the running binary
exe_path = os.readlink('/proc/self/exe')

# Check if we can read it (we can)
try:
    with open('/proc/self/exe', 'rb') as f:
        # Read just the first 4 bytes (ELF magic)
        magic = f.read(4)
        is_elf = magic == b'\\x7fELF'

    # In a real attack, the full binary would be copied:
    # shutil.copy('/proc/self/exe', '/output/python_binary')

    with open('/output/result.json', 'w') as f:
        json.dump({
            "exe_path": exe_path,
            "is_elf": is_elf,
            "readable": True,
            "attack_possible": True
        }, f)
except Exception as e:
    with open('/output/result.json', 'w') as f:
        json.dump({"error": str(e)}, f)
"""

        job = {
            "id": "test-proc-exe",
            "code": code,
            "input_data": {},
        }

        result = executor.execute_job(job)

        # This test PASSES, demonstrating the vulnerability
        assert result["success"], (
            f"Execution failed: stderr={result.get('stderr')}, error={result.get('error')}"
        )
        output = result["output"]

        assert output["readable"] is True
        assert output["is_elf"] is True
        assert output["attack_possible"] is True

        print("\n⚠️  VULNERABILITY CONFIRMED:")
        print(f"   - Python binary path: {output['exe_path']}")
        print(f"   - Binary is readable: {output['readable']}")
        print("   - Full binary extraction is possible")

    def test_file_descriptor_enumeration(self, executor):
        """
        Demonstrate that user code can enumerate open file descriptors.

        VULNERABILITY: Reveals:
        - Open configuration files
        - Database connections
        - Unix sockets
        - Log files
        """
        code = """
import os
import json

# Enumerate file descriptors
fds = {}
for fd_num in range(256):
    fd_path = f'/proc/self/fd/{fd_num}'
    try:
        target = os.readlink(fd_path)
        fds[str(fd_num)] = target
    except (FileNotFoundError, OSError):
        pass

with open('/output/result.json', 'w') as f:
    json.dump({
        "fd_count": len(fds),
        "file_descriptors": fds
    }, f)
"""

        job = {
            "id": "test-proc-fd",
            "code": code,
            "input_data": {},
        }

        result = executor.execute_job(job)

        # This test PASSES, demonstrating the vulnerability
        assert result["success"], (
            f"Execution failed: stderr={result.get('stderr')}, error={result.get('error')}"
        )
        output = result["output"]

        assert output["fd_count"] > 0

        print("\n⚠️  VULNERABILITY CONFIRMED:")
        print(f"   - Open file descriptors exposed: {output['fd_count']}")
        print(f"   - File descriptors: {output['file_descriptors']}")

    def test_process_enumeration(self, executor):
        """
        Demonstrate that user code can enumerate processes via /proc/[PID]/cmdline.

        VULNERABILITY: Reveals running processes and their command arguments.
        """
        code = """
import os
import json

# Try to enumerate processes
processes = {}
for pid in range(1, 100):
    cmdline_path = f'/proc/{pid}/cmdline'
    try:
        with open(cmdline_path, 'rb') as f:
            cmdline = f.read().replace(b'\\x00', b' ').decode('utf-8', errors='ignore')
            if cmdline:
                processes[str(pid)] = cmdline.strip()
    except (FileNotFoundError, PermissionError):
        pass

with open('/output/result.json', 'w') as f:
    json.dump({
        "process_count": len(processes),
        "processes": processes
    }, f)
"""

        job = {
            "id": "test-proc-pid",
            "code": code,
            "input_data": {},
        }

        result = executor.execute_job(job)

        # This test PASSES (at least own process visible)
        assert result["success"], (
            f"Execution failed: stderr={result.get('stderr')}, error={result.get('error')}"
        )
        output = result["output"]

        # In a container, at minimum the running process itself is visible
        assert output["process_count"] >= 1

        print("\n⚠️  VULNERABILITY CONFIRMED:")
        print(f"   - Processes enumerated: {output['process_count']}")
        print(f"   - Process details: {output['processes']}")

    def test_custom_env_variable_leakage(self, executor):
        """
        Demonstrate leakage of custom environment variables from job_type.environment.

        This is particularly dangerous if job types include API keys or secrets.
        """
        code = """
import os
import json

# Read custom environment variable
custom_secret = os.environ.get('API_KEY', 'not_found')
custom_config = os.environ.get('DATABASE_URL', 'not_found')

with open('/output/result.json', 'w') as f:
    json.dump({
        "api_key": custom_secret,
        "database_url": custom_config,
        "all_env": dict(os.environ)
    }, f)
"""

        # Simulate a job type with custom environment variables
        from tako_vm.job_types import JobType

        job_type = JobType(
            name="test-with-secrets",
            requirements=[],
            memory_limit="256m",
            cpu_limit=0.5,
            timeout=10,
            environment={
                "API_KEY": "sk-secret-api-key-12345",
                "DATABASE_URL": "postgresql://user:password@db.internal:5432/app",
            },
        )

        executor.registry.register(job_type)

        job = {
            "id": "test-env-secrets",
            "code": code,
            "input_data": {},
            "job_type": "test-with-secrets",
        }

        result = executor.execute_job(job)

        # This test PASSES, demonstrating secret leakage
        assert result["success"], (
            f"Execution failed: stderr={result.get('stderr')}, error={result.get('error')}"
        )
        output = result["output"]

        # CRITICAL: Secrets are exposed
        assert output["api_key"] == "sk-secret-api-key-12345"
        assert "password" in output["database_url"]

        print("\n🔴 CRITICAL VULNERABILITY CONFIRMED:")
        print(f"   - API key leaked: {output['api_key']}")
        print(f"   - Database URL leaked: {output['database_url']}")
        print("   - Custom environment variables are NOT SAFE for secrets!")


@pytest.mark.skip(reason="Demonstrates vulnerability - do not run in CI")
class TestMitigations:
    """Test potential mitigations (not yet implemented)."""

    def test_proc_environ_should_be_blocked(self, executor):
        """
        FUTURE: Test that /proc/self/environ access should be blocked.

        This test should FAIL until proper mitigations are implemented.
        """
        code = """
try:
    with open('/proc/self/environ', 'rb') as f:
        f.read()
    result = "accessible"
except PermissionError:
    result = "blocked"
except FileNotFoundError:
    result = "not_found"

with open('/output/result.json', 'w') as f:
    import json
    json.dump({"status": result}, f)
"""

        job = {
            "id": "test-mitigation",
            "code": code,
            "input_data": {},
        }

        result = executor.execute_job(job)
        assert result["success"], (
            f"Execution failed: stderr={result.get('stderr')}, error={result.get('error')}"
        )

        # This assertion WILL FAIL until mitigations are implemented
        # Currently /proc/self/environ is "accessible"
        # After mitigations, it should be "blocked" or "not_found"
        assert result["output"]["status"] in ("blocked", "not_found"), (
            "/proc/self/environ should not be accessible"
        )


if __name__ == "__main__":
    # Run tests with verbose output
    pytest.main([__file__, "-v", "-s"])
