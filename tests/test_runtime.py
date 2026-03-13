"""
Tests for container runtime resolution logic.

These tests verify that the gVisor/runc runtime selection works correctly
based on security mode and runtime availability.
"""

import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution import (
    CodeExecutor,
    RuntimeUnavailableError,
    reset_gvisor_check,
)
from tako_vm.job_types import JobType


@pytest.fixture(autouse=True)
def reset_gvisor_cache():
    """Reset gVisor availability cache before each test."""
    reset_gvisor_check()
    yield
    reset_gvisor_check()


class TestRuntimeResolutionWithGvisorAvailable:
    """Tests when gVisor IS available."""

    @pytest.fixture(autouse=True)
    def mock_gvisor_available(self, monkeypatch):
        """Mock gVisor as available."""
        monkeypatch.setattr(worker_module, "_gvisor_available", True)

    def test_runsc_strict_uses_gvisor(self):
        """Default config (runsc + strict) uses gVisor when available."""
        config = TakoVMConfig(container_runtime="runsc", security_mode="strict")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runsc"

    def test_runsc_permissive_uses_gvisor(self):
        """Permissive mode still uses gVisor when available."""
        config = TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runsc"

    def test_runc_strict_raises_error(self):
        """Explicitly requesting runc in strict mode raises error."""
        config = TakoVMConfig(container_runtime="runc", security_mode="strict")
        with pytest.raises(RuntimeUnavailableError) as exc_info:
            CodeExecutor(config=config)
        assert "Cannot use 'runc' runtime in strict security mode" in str(exc_info.value)

    def test_runc_permissive_allows_runc(self):
        """Explicitly requesting runc in permissive mode is allowed."""
        config = TakoVMConfig(container_runtime="runc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runc"


class TestRuntimeResolutionWithoutGvisor:
    """Tests when gVisor is NOT available."""

    @pytest.fixture(autouse=True)
    def mock_gvisor_unavailable(self, monkeypatch):
        """Mock gVisor as unavailable."""
        monkeypatch.setattr(worker_module, "_gvisor_available", False)

    def test_runsc_strict_raises_error(self):
        """Strict mode with runsc requested but unavailable raises error."""
        config = TakoVMConfig(container_runtime="runsc", security_mode="strict")
        with pytest.raises(RuntimeUnavailableError) as exc_info:
            CodeExecutor(config=config)
        assert "gVisor (runsc) runtime is not available" in str(exc_info.value)
        assert "strict mode" in str(exc_info.value)

    def test_runsc_permissive_falls_back_to_runc(self):
        """Permissive mode falls back to runc when gVisor unavailable."""
        config = TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runc"

    def test_runc_strict_raises_error(self):
        """Runc in strict mode still raises (regardless of gVisor availability)."""
        config = TakoVMConfig(container_runtime="runc", security_mode="strict")
        with pytest.raises(RuntimeUnavailableError) as exc_info:
            CodeExecutor(config=config)
        assert "Cannot use 'runc' runtime in strict security mode" in str(exc_info.value)

    def test_runc_permissive_uses_runc(self):
        """Runc in permissive mode uses runc."""
        config = TakoVMConfig(container_runtime="runc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runc"


class TestGvisorAvailabilityCheck:
    """Tests for the check_gvisor_available function."""

    def test_check_caches_result(self, monkeypatch):
        """Result is cached after first check."""
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1

            class MockResult:
                returncode = 0
                stdout = "runc runsc"

            return MockResult()

        monkeypatch.setattr("subprocess.run", mock_run)

        # First call should invoke subprocess
        from tako_vm.execution.worker import check_gvisor_available

        result1 = check_gvisor_available()
        assert result1 is True
        assert call_count == 1

        # Second call should use cache
        result2 = check_gvisor_available()
        assert result2 is True
        assert call_count == 1  # Still 1, not 2

    def test_reset_clears_cache(self, monkeypatch):
        """reset_gvisor_check clears the cached result."""
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1

            class MockResult:
                returncode = 0
                stdout = "runc runsc"

            return MockResult()

        monkeypatch.setattr("subprocess.run", mock_run)

        from tako_vm.execution.worker import check_gvisor_available

        check_gvisor_available()
        assert call_count == 1

        reset_gvisor_check()
        check_gvisor_available()
        assert call_count == 2  # Called again after reset


class TestConfigValidation:
    """Tests for config validation of runtime settings."""

    def test_invalid_container_runtime_rejected(self):
        """Invalid container_runtime value is rejected."""
        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(container_runtime="invalid")
        assert "container_runtime must be one of" in str(exc_info.value)

    def test_invalid_security_mode_rejected(self):
        """Invalid security_mode value is rejected."""
        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(security_mode="invalid")
        assert "security_mode must be one of" in str(exc_info.value)

    def test_valid_runtimes_accepted(self):
        """Valid container_runtime values are accepted."""
        for runtime in ["runsc", "runc"]:
            config = TakoVMConfig(container_runtime=runtime, security_mode="permissive")
            assert config.container_runtime == runtime

    def test_valid_security_modes_accepted(self):
        """Valid security_mode values are accepted."""
        for mode in ["strict", "permissive"]:
            config = TakoVMConfig(security_mode=mode, container_runtime="runc")
            assert config.security_mode == mode


class TestEnvVarNormalization:
    """Tests for environment variable case normalization."""

    def test_security_mode_uppercase_normalized(self, monkeypatch):
        """Uppercase TAKO_VM_SECURITY_MODE is normalized to lowercase."""
        from tako_vm.config import load_config, reset_config

        reset_config()
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "PERMISSIVE")
        config = load_config()
        assert config.security_mode == "permissive"

    def test_security_mode_mixed_case_normalized(self, monkeypatch):
        """Mixed case TAKO_VM_SECURITY_MODE is normalized to lowercase."""
        from tako_vm.config import load_config, reset_config

        reset_config()
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "Permissive")
        config = load_config()
        assert config.security_mode == "permissive"

    def test_container_runtime_uppercase_normalized(self, monkeypatch):
        """Uppercase TAKO_VM_CONTAINER_RUNTIME is normalized to lowercase."""
        from tako_vm.config import load_config, reset_config

        reset_config()
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")
        monkeypatch.setenv("TAKO_VM_CONTAINER_RUNTIME", "RUNC")
        config = load_config()
        assert config.container_runtime == "runc"


class TestPlatformDetection:
    """Tests for platform detection (native Linux vs Docker Desktop)."""

    def test_is_native_linux_on_linux(self, monkeypatch):
        """is_native_linux() returns True on Linux."""
        monkeypatch.setattr("platform.system", lambda: "Linux")
        from tako_vm.execution.worker import is_native_linux

        assert is_native_linux() is True

    def test_is_native_linux_on_macos(self, monkeypatch):
        """is_native_linux() returns False on macOS (Docker Desktop)."""
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        from tako_vm.execution.worker import is_native_linux

        assert is_native_linux() is False

    def test_is_native_linux_on_windows(self, monkeypatch):
        """is_native_linux() returns False on Windows (Docker Desktop)."""
        monkeypatch.setattr("platform.system", lambda: "Windows")
        from tako_vm.execution.worker import is_native_linux

        assert is_native_linux() is False


class TestGpuRuntimePolicy:
    """Tests for GPU-specific runtime resolution and Docker flags."""

    @pytest.fixture(autouse=True)
    def mock_gvisor_available(self, monkeypatch):
        """Mock gVisor as available for baseline executor initialization."""
        monkeypatch.setattr(worker_module, "_gvisor_available", True)

    def test_gpu_workload_rejected_in_strict_mode(self):
        """GPU workloads are blocked when strict mode requires gVisor."""
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="strict")
        )
        gpu_job = JobType(name="gpu-job", gpu_enabled=True, gpu_vendor="nvidia")

        with pytest.raises(RuntimeUnavailableError) as exc_info:
            executor.resolve_runtime_for_job_type(gpu_job)

        assert "GPU workloads require gVisor to be disabled" in str(exc_info.value)

    def test_gpu_workload_forces_runc_in_permissive_mode(self):
        """GPU workloads always use runc in permissive mode."""
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        )
        gpu_job = JobType(name="gpu-job", gpu_enabled=True, gpu_vendor="nvidia")

        assert executor.resolve_runtime_for_job_type(gpu_job) == "runc"

    def test_build_gpu_flags_for_nvidia_variants(self):
        """NVIDIA flag generation supports all/count/device selection."""
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        )

        assert executor.build_gpu_flags(
            JobType(name="all", gpu_enabled=True, gpu_vendor="nvidia")
        ) == ["--gpus=all"]
        assert executor.build_gpu_flags(
            JobType(name="count", gpu_enabled=True, gpu_vendor="nvidia", gpu_count=2)
        ) == ["--gpus=2"]
        assert executor.build_gpu_flags(
            JobType(
                name="devices",
                gpu_enabled=True,
                gpu_vendor="nvidia",
                gpu_device_ids=["GPU-1", "GPU-2"],
            )
        ) == ["--gpus=device=GPU-1,GPU-2"]

    def test_build_gpu_flags_for_amd(self):
        """AMD jobs mount required device nodes."""
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        )
        flags = executor.build_gpu_flags(JobType(name="amd", gpu_enabled=True, gpu_vendor="amd"))
        assert flags == ["--device=/dev/kfd", "--device=/dev/dri"]

    def test_build_gpu_env_vars_for_device_selection(self):
        """Device selection env vars are vendor-specific."""
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        )

        nvidia_env = executor.build_gpu_env_vars(
            JobType(
                name="nvidia",
                gpu_enabled=True,
                gpu_vendor="nvidia",
                gpu_device_ids=["0", "2"],
            )
        )
        assert nvidia_env == {"CUDA_VISIBLE_DEVICES": "0,2"}

        amd_env = executor.build_gpu_env_vars(
            JobType(
                name="amd",
                gpu_enabled=True,
                gpu_vendor="amd",
                gpu_device_ids=["card0"],
            )
        )
        assert amd_env == {
            "ROCR_VISIBLE_DEVICES": "card0",
            "HIP_VISIBLE_DEVICES": "card0",
        }

    def test_build_gpu_flags_rejects_unknown_vendor(self):
        """Unsupported GPU vendor raises a runtime error."""
        executor = CodeExecutor(
            config=TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        )

        with pytest.raises(RuntimeUnavailableError) as exc_info:
            executor.build_gpu_flags(JobType(name="bad", gpu_enabled=True, gpu_vendor="intel"))

        assert "Unsupported GPU vendor" in str(exc_info.value)
