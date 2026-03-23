"""
Tests for Tako VM configuration module.

Tests config loading, validation, and environment variable overrides.
"""

import tempfile
from pathlib import Path

import pytest

from tako_vm.config import (
    ConfigurationError,
    ContainerLimits,
    JobTypeConfig,
    JobTypeGPUConfig,
    TakoVMConfig,
    find_config_file,
    get_config,
    load_config,
    reset_config,
    set_config_path,
    validate_config_file,
)


@pytest.fixture(autouse=True)
def reset_config_fixture():
    """Reset config before and after each test."""
    reset_config()
    yield
    reset_config()


class TestContainerLimits:
    """Tests for ContainerLimits validation."""

    def test_container_limits_defaults(self):
        """ContainerLimits has sensible defaults."""
        limits = ContainerLimits()
        assert limits.nofile_soft == 256
        assert limits.nofile_hard == 256
        assert limits.nproc_soft == 50
        assert limits.pids_limit == 100
        assert limits.tmpfs_size == "100m"

    def test_container_limits_custom_values(self):
        """ContainerLimits accepts custom values within bounds."""
        limits = ContainerLimits(
            nofile_soft=512,
            nofile_hard=1024,
            nproc_soft=100,
            nproc_hard=200,
            fsize=209715200,  # 200MB
            tmpfs_size="256m",
            pids_limit=200,
        )
        assert limits.nofile_soft == 512
        assert limits.nofile_hard == 1024

    def test_container_limits_tmpfs_size_formats(self):
        """ContainerLimits accepts various tmpfs size formats."""
        # Megabytes
        limits = ContainerLimits(tmpfs_size="256m")
        assert limits.tmpfs_size == "256m"

        # Gigabytes
        limits = ContainerLimits(tmpfs_size="1g")
        assert limits.tmpfs_size == "1g"

    def test_container_limits_tmpfs_bounds(self):
        """ContainerLimits validates tmpfs size bounds."""
        # Too small
        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(tmpfs_size="5m")
        assert "at least 10m" in str(exc_info.value)

        # Too large
        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(tmpfs_size="3g")
        assert "at most 2g" in str(exc_info.value)

    def test_container_limits_hard_ge_soft(self):
        """ContainerLimits requires hard limits >= soft limits."""
        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(nofile_soft=1024, nofile_hard=512)
        assert "nofile_hard must be >= nofile_soft" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(nproc_soft=100, nproc_hard=50)
        assert "nproc_hard must be >= nproc_soft" in str(exc_info.value)

    def test_container_limits_forbids_extra(self):
        """ContainerLimits rejects unknown fields."""
        with pytest.raises(ValueError):
            ContainerLimits.model_validate({"unknown_field": "value"})


class TestJobTypeConfig:
    """Tests for JobTypeConfig validation."""

    def test_job_type_config_minimal(self):
        """JobTypeConfig works with just name."""
        config = JobTypeConfig(name="test-job")
        assert config.name == "test-job"
        assert config.requirements == []
        assert config.timeout == 30
        assert config.network_enabled is False

    def test_job_type_config_full(self):
        """JobTypeConfig accepts all fields."""
        config = JobTypeConfig(
            name="data-processing",
            requirements=["pandas", "numpy"],
            python_version="3.11",
            base_image="custom-base:latest",
            shared_code=["utils.py"],
            environment={"API_KEY": "secret"},
            memory_limit="1g",
            cpu_limit=2.0,
            timeout=60,
            startup_timeout=180,
            network_enabled=True,
        )
        assert config.requirements == ["pandas", "numpy"]
        assert config.memory_limit == "1g"
        assert config.network_enabled is True

    def test_job_type_config_name_validation(self):
        """JobTypeConfig validates name format."""
        # Valid names
        JobTypeConfig(name="valid-name")
        JobTypeConfig(name="valid_name")
        JobTypeConfig(name="valid123")

        # Invalid names
        with pytest.raises(ValueError):
            JobTypeConfig(name="invalid name")  # spaces

        with pytest.raises(ValueError):
            JobTypeConfig(name="invalid.name")  # dots

    def test_job_type_config_memory_limit_formats(self):
        """JobTypeConfig validates memory limit format."""
        # Valid formats
        JobTypeConfig(name="test", memory_limit="512m")
        JobTypeConfig(name="test", memory_limit="1g")
        JobTypeConfig(name="test", memory_limit="2G")  # uppercase OK

        # Invalid format
        with pytest.raises(ValueError):
            JobTypeConfig(name="test", memory_limit="512")  # no unit

        with pytest.raises(ValueError):
            JobTypeConfig(name="test", memory_limit="512k")  # no KB support

    def test_job_type_config_memory_limit_bounds(self):
        """JobTypeConfig validates memory limit bounds."""
        # Too small
        with pytest.raises(ValueError) as exc_info:
            JobTypeConfig(name="test", memory_limit="32m")
        assert "at least 64m" in str(exc_info.value)

        # Too large
        with pytest.raises(ValueError) as exc_info:
            JobTypeConfig(name="test", memory_limit="64g")
        assert "at most 32g" in str(exc_info.value)


class TestJobTypeGPUConfig:
    """Tests for JobTypeGPUConfig validation."""

    def test_job_type_gpu_config_defaults(self):
        """GPU config defaults to disabled with no selection."""
        config = JobTypeGPUConfig()
        assert config.enabled is False
        assert config.vendor is None
        assert config.count is None
        assert config.device_ids == []

    def test_job_type_gpu_config_nvidia_count(self):
        """NVIDIA GPU config accepts count selection."""
        config = JobTypeGPUConfig(enabled=True, vendor="NVIDIA", count=2)
        assert config.enabled is True
        assert config.vendor == "nvidia"
        assert config.count == 2

        multi_gpu = JobTypeGPUConfig(
            enabled=True,
            vendor="NVIDIA",
            device_ids=[" GPU-1 ", "GPU-2"],
        )
        assert multi_gpu.device_ids == ["GPU-1", "GPU-2"]

    def test_job_type_gpu_config_rejects_missing_vendor(self):
        """Enabled GPU config requires vendor."""
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(enabled=True)
        assert "gpu.vendor is required" in str(exc_info.value)

    def test_job_type_gpu_config_rejects_count_with_device_ids(self):
        """GPU config forbids combining count and device_ids."""
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(
                enabled=True,
                vendor="nvidia",
                count=1,
                device_ids=["GPU-123"],
            )
        assert "mutually exclusive" in str(exc_info.value)

    def test_job_type_gpu_config_rejects_amd_count(self):
        """AMD GPU config does not support count selection."""
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(enabled=True, vendor="amd", count=1)
        assert "only supported" in str(exc_info.value)

    def test_job_type_gpu_config_rejects_fields_when_disabled(self):
        """GPU details are rejected unless enabled=true."""
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(enabled=False, vendor="nvidia")
        assert "gpu.enabled must be true" in str(exc_info.value)

    def test_job_type_gpu_config_device_ids_validation(self):
        """Device IDs are stripped and validated."""
        config = JobTypeGPUConfig(enabled=True, vendor="nvidia", device_ids=[" GPU-1 ", "GPU-2"])
        assert config.device_ids == ["GPU-1", "GPU-2"]

        with pytest.raises(ValueError):
            JobTypeGPUConfig(enabled=True, vendor="nvidia", device_ids=["bad,id"])

        with pytest.raises(ValueError):
            JobTypeGPUConfig(enabled=True, vendor="nvidia", device_ids=["GPU-1", "GPU-1"])

        with pytest.raises(ValueError):
            JobTypeGPUConfig(enabled=True, vendor="nvidia", device_ids=[" GPU-1 ", "GPU-1"])

        with pytest.raises(ValueError):
            JobTypeGPUConfig(enabled=True, vendor="nvidia", device_ids=["GPU-1", "gpu-1"])


class TestTakoVMConfig:
    """Tests for TakoVMConfig validation."""

    def test_tako_vm_config_defaults(self):
        """TakoVMConfig has sensible defaults."""
        config = TakoVMConfig()
        assert config.production_mode is False
        assert config.max_workers == 4
        assert config.default_timeout == 30
        assert config.container_runtime == "runsc"
        assert config.security_mode == "permissive"
        assert config.api_max_payload_bytes == 2097152
        assert config.api_rate_limit_enabled is True
        assert config.api_rate_limit_requests == 120
        assert config.api_rate_limit_window_seconds == 60

    def test_tako_vm_config_path_resolution(self):
        """TakoVMConfig resolves data_dir while keeping database URL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = TakoVMConfig(data_dir=tmpdir)
            config.resolve_paths()

            assert config.data_dir == Path(tmpdir)
            assert config.database_url.startswith("postgresql://")

    def test_tako_vm_config_timeout_validation(self):
        """TakoVMConfig validates timeout relationships."""
        # default_timeout > max_timeout should fail
        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(default_timeout=100, max_timeout=50)
        assert "default_timeout must be <= max_timeout" in str(exc_info.value)

    def test_tako_vm_config_normalizes_psycopg_url_scheme(self):
        """database_url normalizes postgresql+psycopg scheme for psycopg pool."""
        config = TakoVMConfig(database_url="postgresql+psycopg://user:pass@localhost:5432/testdb")
        assert config.database_url == "postgresql://user:pass@localhost:5432/testdb"

    def test_tako_vm_config_accepts_unix_socket_dsn(self):
        """database_url accepts libpq unix socket DSNs."""
        config = TakoVMConfig(database_url="postgresql:///testdb?host=/var/run/postgresql")
        assert config.database_url == "postgresql:///testdb?host=/var/run/postgresql"

    def test_tako_vm_config_container_runtime_validation(self):
        """TakoVMConfig validates container runtime."""
        # Valid runtimes
        TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        TakoVMConfig(container_runtime="runc", security_mode="permissive")

        # Invalid runtime
        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(container_runtime="invalid")
        assert "container_runtime must be one of" in str(exc_info.value)

    def test_tako_vm_config_security_mode_validation(self):
        """TakoVMConfig validates security mode."""
        # Valid modes
        TakoVMConfig(security_mode="strict")
        TakoVMConfig(security_mode="permissive")

        # Invalid mode
        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(security_mode="invalid")
        assert "security_mode must be one of" in str(exc_info.value)

    def test_tako_vm_config_log_level_validation(self):
        """TakoVMConfig validates and normalizes log level."""
        config = TakoVMConfig(log_level="debug")
        assert config.log_level == "DEBUG"

        config = TakoVMConfig(log_level="WARNING")
        assert config.log_level == "WARNING"

        with pytest.raises(ValueError):
            TakoVMConfig(log_level="TRACE")  # Invalid level

    def test_tako_vm_config_with_job_types(self):
        """TakoVMConfig supports embedded job types."""
        config = TakoVMConfig(
            job_types=[
                JobTypeConfig(name="job-a", requirements=["pandas"]),
                JobTypeConfig(name="job-b", timeout=60),
            ]
        )
        assert len(config.job_types) == 2
        assert config.job_types[0].name == "job-a"

    def test_tako_vm_config_get_method(self):
        """TakoVMConfig.get() provides dict-like access."""
        config = TakoVMConfig(max_workers=8)
        assert config.get("max_workers") == 8
        assert config.get("nonexistent", "default") == "default"

    def test_tako_vm_config_forbids_extra(self):
        """TakoVMConfig rejects unknown fields."""
        with pytest.raises(ValueError):
            TakoVMConfig.model_validate({"unknown_field": "value"})


class TestConfigLoading:
    """Tests for config file loading."""

    def test_load_config_defaults(self):
        """load_config returns defaults when no file exists."""
        config = load_config()
        assert isinstance(config, TakoVMConfig)
        assert config.max_workers == 4

    def test_load_config_from_file(self):
        """load_config reads YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: 8
default_timeout: 60
security_mode: permissive
"""
            )
            f.flush()
            config_path = Path(f.name)

        try:
            config = load_config(config_path)
            assert config.max_workers == 8
            assert config.default_timeout == 60
            assert config.security_mode == "permissive"
        finally:
            config_path.unlink()

    def test_load_config_env_override(self, monkeypatch):
        """Environment variables override config file values."""
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "PERMISSIVE")

        config = load_config()
        assert config.security_mode == "permissive"

    def test_load_config_env_container_runtime(self, monkeypatch):
        """TAKO_VM_CONTAINER_RUNTIME env var is normalized."""
        monkeypatch.setenv("TAKO_VM_CONTAINER_RUNTIME", "RUNC")
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")

        config = load_config()
        assert config.container_runtime == "runc"

    def test_load_config_env_api_protection_overrides(self, monkeypatch):
        """API protection environment variables override config values."""
        monkeypatch.setenv("TAKO_VM_API_MAX_PAYLOAD_BYTES", "4096")
        monkeypatch.setenv("TAKO_VM_API_RATE_LIMIT_ENABLED", "false")
        monkeypatch.setenv("TAKO_VM_API_RATE_LIMIT_REQUESTS", "42")
        monkeypatch.setenv("TAKO_VM_API_RATE_LIMIT_WINDOW_SECONDS", "15")

        config = load_config()

        assert config.api_max_payload_bytes == 4096
        assert config.api_rate_limit_enabled is False
        assert config.api_rate_limit_requests == 42
        assert config.api_rate_limit_window_seconds == 15

    @pytest.mark.parametrize(
        "var_name",
        [
            "TAKO_VM_API_MAX_PAYLOAD_BYTES",
            "TAKO_VM_API_RATE_LIMIT_REQUESTS",
            "TAKO_VM_API_RATE_LIMIT_WINDOW_SECONDS",
        ],
    )
    def test_load_config_env_invalid_api_protection_int_raises(self, monkeypatch, var_name):
        """Invalid API protection integer env vars raise ConfigurationError."""
        monkeypatch.setenv(var_name, "not-a-number")

        with pytest.raises(ConfigurationError) as exc_info:
            load_config()

        assert var_name in str(exc_info.value)

    def test_load_config_invalid_raises(self):
        """load_config raises ConfigurationError for invalid config."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: -1  # Invalid: must be >= 1
"""
            )
            f.flush()
            config_path = Path(f.name)

        try:
            with pytest.raises(ConfigurationError):
                load_config(config_path)
        finally:
            config_path.unlink()

    def test_validate_config_file_valid(self):
        """validate_config_file returns empty list for valid file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: 4
default_timeout: 30
"""
            )
            f.flush()
            config_path = Path(f.name)

        try:
            errors = validate_config_file(config_path)
            assert errors == []
        finally:
            config_path.unlink()

    def test_validate_config_file_invalid(self):
        """validate_config_file returns errors for invalid file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: "not a number"
"""
            )
            f.flush()
            config_path = Path(f.name)

        try:
            errors = validate_config_file(config_path)
            assert len(errors) > 0
        finally:
            config_path.unlink()

    def test_validate_config_file_not_found(self):
        """validate_config_file handles missing file."""
        errors = validate_config_file(Path("/nonexistent/config.yaml"))
        assert len(errors) == 1
        assert "not found" in errors[0].lower()


class TestConfigGlobals:
    """Tests for global config management."""

    def test_get_config_singleton(self):
        """get_config returns same instance."""
        config1 = get_config()
        config2 = get_config()
        assert config1 is config2

    def test_reset_config(self):
        """reset_config clears cached config."""
        config1 = get_config()
        reset_config()
        config2 = get_config()
        # New instance after reset
        assert config1 is not config2

    def test_set_config_path(self):
        """set_config_path changes config source."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: 16
"""
            )
            f.flush()
            config_path = Path(f.name)

        try:
            set_config_path(config_path)
            config = get_config()
            assert config.max_workers == 16
        finally:
            config_path.unlink()
            reset_config()

    def test_find_config_file_env_override(self, monkeypatch):
        """TAKO_VM_CONFIG env var overrides search paths."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("max_workers: 4")
            f.flush()
            config_path = f.name

        try:
            monkeypatch.setenv("TAKO_VM_CONFIG", config_path)
            found = find_config_file()
            assert found == Path(config_path)
        finally:
            Path(config_path).unlink()
