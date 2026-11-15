import pytest

import tako_vm.execution.worker as worker_module
from tako_vm.config import TakoVMConfig
from tako_vm.execution import (
    CodeExecutor,
    RuntimeUnavailableError,
    reset_gvisor_check,
)


@pytest.fixture(autouse=True)
def reset_gvisor_cache():
    reset_gvisor_check()
    yield
    reset_gvisor_check()


class TestRuntimeResolutionWithGvisorAvailable:
    @pytest.fixture(autouse=True)
    def mock_gvisor_available(self, monkeypatch):
        monkeypatch.setattr(worker_module, "_gvisor_available", True)

    def test_runsc_strict_uses_gvisor(self):
        config = TakoVMConfig(container_runtime="runsc", security_mode="strict")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runsc"

    def test_runsc_permissive_uses_gvisor(self):
        config = TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runsc"

    def test_runc_strict_raises_error(self):
        config = TakoVMConfig(container_runtime="runc", security_mode="strict")
        with pytest.raises(RuntimeUnavailableError) as exc_info:
            CodeExecutor(config=config)
        assert "Cannot use 'runc' runtime in strict security mode" in str(exc_info.value)

    def test_runc_permissive_allows_runc(self):
        config = TakoVMConfig(container_runtime="runc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runc"


class TestRuntimeResolutionWithoutGvisor:
    @pytest.fixture(autouse=True)
    def mock_gvisor_unavailable(self, monkeypatch):
        monkeypatch.setattr(worker_module, "_gvisor_available", False)

    def test_runsc_strict_raises_error(self):
        config = TakoVMConfig(container_runtime="runsc", security_mode="strict")
        with pytest.raises(RuntimeUnavailableError) as exc_info:
            CodeExecutor(config=config)
        assert "gVisor (runsc) runtime is not available" in str(exc_info.value)
        assert "strict mode" in str(exc_info.value)

    def test_runsc_permissive_falls_back_to_runc(self):
        config = TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runc"

    def test_runc_strict_raises_error(self):
        config = TakoVMConfig(container_runtime="runc", security_mode="strict")
        with pytest.raises(RuntimeUnavailableError) as exc_info:
            CodeExecutor(config=config)
        assert "Cannot use 'runc' runtime in strict security mode" in str(exc_info.value)

    def test_runc_permissive_uses_runc(self):
        config = TakoVMConfig(container_runtime="runc", security_mode="permissive")
        executor = CodeExecutor(config=config)
        assert executor._runtime == "runc"


class TestGvisorAvailabilityCheck:
    def test_check_caches_result(self, monkeypatch):
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

        result1 = check_gvisor_available()
        assert result1 is True
        assert call_count == 1

        result2 = check_gvisor_available()
        assert result2 is True
        assert call_count == 1

    def test_reset_clears_cache(self, monkeypatch):
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
        assert call_count == 2


class TestConfigValidation:
    def test_invalid_container_runtime_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(container_runtime="invalid")
        assert "container_runtime must be one of" in str(exc_info.value)

    def test_invalid_security_mode_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(security_mode="invalid")
        assert "security_mode must be one of" in str(exc_info.value)

    def test_valid_runtimes_accepted(self):
        for runtime in ["runsc", "runc"]:
            config = TakoVMConfig(container_runtime=runtime, security_mode="permissive")
            assert config.container_runtime == runtime

    def test_valid_security_modes_accepted(self):
        for mode in ["strict", "permissive"]:
            config = TakoVMConfig(security_mode=mode, container_runtime="runc")
            assert config.security_mode == mode


class TestEnvVarNormalization:
    def test_security_mode_uppercase_normalized(self, monkeypatch):
        from tako_vm.config import load_config, reset_config

        reset_config()
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "PERMISSIVE")
        config = load_config()
        assert config.security_mode == "permissive"

    def test_security_mode_mixed_case_normalized(self, monkeypatch):
        from tako_vm.config import load_config, reset_config

        reset_config()
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "Permissive")
        config = load_config()
        assert config.security_mode == "permissive"

    def test_container_runtime_uppercase_normalized(self, monkeypatch):
        from tako_vm.config import load_config, reset_config

        reset_config()
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")
        monkeypatch.setenv("TAKO_VM_CONTAINER_RUNTIME", "RUNC")
        config = load_config()
        assert config.container_runtime == "runc"


class TestPlatformDetection:
    def test_is_native_linux_on_linux(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        from tako_vm.execution.worker import is_native_linux

        assert is_native_linux() is True

    def test_is_native_linux_on_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        from tako_vm.execution.worker import is_native_linux

        assert is_native_linux() is False

    def test_is_native_linux_on_windows(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        from tako_vm.execution.worker import is_native_linux

        assert is_native_linux() is False
