"""
Tests for Tako VM configuration module.

Tests config loading, validation, and environment variable overrides.
"""

import logging
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

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
def reset_config_fixture(monkeypatch):
    """Reset config and scrub config env overrides before and after each test.

    Other test modules legitimately mutate process env (e.g. the CLI's managed
    postgres helper sets TAKO_VM_DATABASE_URL); if those leak in, load_config's
    env override layer silently replaces values under test.
    """
    for var in ("TAKO_VM_CONFIG", "TAKO_VM_DATABASE_URL", "TAKO_VM_SECURITY_MODE"):
        monkeypatch.delenv(var, raising=False)
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
        # Secure by default: fail closed rather than silently fall back to runc.
        assert config.security_mode == "strict"
        # Loopback by default: the API executes arbitrary code and ships with
        # auth disabled, so it must not be network-reachable out of the box.
        assert config.server_host == "127.0.0.1"
        assert config.allow_unauthenticated_network_access is False
        assert config.api_max_payload_bytes == 2097152
        assert config.api_rate_limit_enabled is True
        assert config.api_rate_limit_requests == 120
        assert config.api_rate_limit_window_seconds == 60
        assert config.api_auth_enabled is False
        assert config.api_keys == []
        assert config.api_auth_header == "X-API-Key"
        assert config.allow_runtime_requirements is False
        assert config.dependency_proxy_url is None
        assert config.enable_runtime_dependency_cache is False

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

        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(
                max_timeout=60,
                job_types=[{"name": "bypass", "timeout": 600}],
            )
        assert "job type timeout must be <= max_timeout for: bypass" in str(exc_info.value)

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

    def test_tako_vm_config_dependency_proxy_url_validation(self):
        """Dependency proxy URL accepts proxy schemes and rejects unsafe values."""
        config = TakoVMConfig(dependency_proxy_url=" https://proxy.example:8443 ")
        assert config.dependency_proxy_url == "https://proxy.example:8443"

        with pytest.raises(ValueError, match="dependency_proxy_url"):
            TakoVMConfig(dependency_proxy_url="file:///tmp/proxy")

        with pytest.raises(ValueError, match="control characters"):
            TakoVMConfig(dependency_proxy_url="https://proxy.example\nbad")

        with pytest.raises(ValueError, match="path, query, or fragment"):
            TakoVMConfig(dependency_proxy_url="https://proxy.example:8443/proxy")

    def test_tako_vm_config_api_auth_validation(self):
        """API auth requires usable keys when enabled."""
        config = TakoVMConfig(api_auth_enabled=True, api_keys=[" aaaaaaaaaaaaaaaa "])
        assert config.api_keys == ["aaaaaaaaaaaaaaaa"]

        with pytest.raises(ValueError, match="api_keys"):
            TakoVMConfig(api_auth_enabled=True)

        with pytest.raises(ValueError, match="at least 16"):
            TakoVMConfig(api_auth_enabled=True, api_keys=["short"])

        with pytest.raises(ValueError, match="api_auth_header"):
            TakoVMConfig(api_auth_header="Bad Header")

    def test_tako_vm_config_get_method(self):
        """TakoVMConfig.get() provides dict-like access."""
        config = TakoVMConfig(max_workers=8)
        assert config.get("max_workers") == 8
        assert config.get("nonexistent", "default") == "default"

    def test_tako_vm_config_forbids_extra(self):
        """TakoVMConfig rejects unknown fields."""
        with pytest.raises(ValueError):
            TakoVMConfig.model_validate({"unknown_field": "value"})


class TestBindHostRule:
    """The fail-closed bind rule is one rule, applied wherever a host appears."""

    def test_validator_delegates_to_ensure_bind_host_allowed(self):
        """The model validator refuses the unauthenticated non-loopback bind."""
        with pytest.raises(ValidationError) as exc_info:
            TakoVMConfig(server_host="0.0.0.0", api_auth_enabled=False)

        assert "refusing to bind non-loopback host" in str(exc_info.value)

    def test_ensure_bind_host_allowed_refuses_late_resolved_host(self):
        """A host resolved after validation (a --host flag) hits the same rule."""
        config = TakoVMConfig(server_host="127.0.0.1", api_auth_enabled=False)

        with pytest.raises(ValueError) as exc_info:
            config.ensure_bind_host_allowed("0.0.0.0")

        message = str(exc_info.value)
        assert "refusing to bind non-loopback host '0.0.0.0'" in message
        assert "api_auth_enabled=true" in message
        assert "127.0.0.1" in message
        assert "allow_unauthenticated_network_access=true" in message

    def test_ensure_bind_host_allowed_accepts_loopback_forms(self):
        """Every loopback spelling is permitted with auth off."""
        config = TakoVMConfig(server_host="127.0.0.1", api_auth_enabled=False)

        for host in ("127.0.0.1", "127.0.0.5", "localhost", "::1", "[::1]", " LOCALHOST "):
            config.ensure_bind_host_allowed(host)

    def test_ensure_bind_host_allowed_permitted_when_auth_enabled(self):
        config = TakoVMConfig(api_auth_enabled=True, api_keys=["a" * 16])
        config.ensure_bind_host_allowed("0.0.0.0")

    def test_ensure_bind_host_allowed_permitted_by_opt_out(self):
        config = TakoVMConfig(allow_unauthenticated_network_access=True)
        config.ensure_bind_host_allowed("0.0.0.0")


class TestSecurityWarnings:
    """Tests for TakoVMConfig.security_warnings()."""

    def test_security_warnings_default_config(self):
        """The defaults are secure, so they produce no warnings at all.

        Previously the defaults were permissive + 0.0.0.0 and emitted two
        warnings. Warnings were the wrong control for those: the defaults now
        fail closed instead (strict mode, loopback bind), so a clean default
        deployment has nothing to warn about.
        """
        assert TakoVMConfig().security_warnings() == []

    def test_security_warnings_when_unauthenticated_bind_is_opted_into(self):
        """The escape hatch still has to be loud."""
        config = TakoVMConfig(
            server_host="0.0.0.0",
            api_auth_enabled=False,
            allow_unauthenticated_network_access=True,
        )
        warnings = config.security_warnings()
        assert any("api_auth_enabled" in w for w in warnings)

    def test_security_warnings_empty_for_locked_down_config(self):
        """Auth enabled + strict mode produces no warnings."""
        config = TakoVMConfig(
            api_auth_enabled=True,
            api_keys=["aaaaaaaaaaaaaaaa"],
            security_mode="strict",
        )
        assert config.security_warnings() == []

    def test_security_warnings_empty_for_loopback_strict_config(self):
        """Loopback host suppresses the auth warning even without auth."""
        config = TakoVMConfig(server_host="127.0.0.1", security_mode="strict")
        assert config.security_warnings() == []

        config = TakoVMConfig(server_host="localhost", security_mode="strict")
        assert config.security_warnings() == []

    def test_security_warnings_permissive_only(self):
        """Auth-enabled config in permissive mode warns about gVisor fallback only."""
        config = TakoVMConfig(
            api_auth_enabled=True,
            api_keys=["aaaaaaaaaaaaaaaa"],
            security_mode="permissive",
        )
        warnings = config.security_warnings()

        assert len(warnings) == 1
        assert "permissive" in warnings[0]

    def test_load_config_logs_security_warnings(self, caplog, monkeypatch):
        """load_config emits security warnings via logging.

        Driven from an explicitly weakened config: the defaults are secure now,
        so they emit nothing, and asserting on a default load would test that
        the logging path is dead rather than that it works.
        """
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")
        with caplog.at_level(logging.WARNING, logger="tako_vm.config"):
            load_config()

        messages = [record.getMessage() for record in caplog.records]
        assert any("permissive" in message for message in messages)


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
        monkeypatch.setenv("TAKO_VM_API_AUTH_ENABLED", "true")
        monkeypatch.setenv("TAKO_VM_API_KEYS", "aaaaaaaaaaaaaaaa,bbbbbbbbbbbbbbbb")
        monkeypatch.setenv("TAKO_VM_API_AUTH_HEADER", "X-Tako-Key")
        monkeypatch.setenv("TAKO_VM_ALLOW_RUNTIME_REQUIREMENTS", "true")
        monkeypatch.setenv("TAKO_VM_DEPENDENCY_PROXY_URL", "https://proxy.example:8443")
        monkeypatch.setenv("TAKO_VM_ENABLE_RUNTIME_DEPENDENCY_CACHE", "true")

        config = load_config()

        assert config.api_max_payload_bytes == 4096
        assert config.api_rate_limit_enabled is False
        assert config.api_rate_limit_requests == 42
        assert config.api_rate_limit_window_seconds == 15
        assert config.api_auth_enabled is True
        assert config.api_keys == ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]
        assert config.api_auth_header == "X-Tako-Key"
        assert config.allow_runtime_requirements is True
        assert config.dependency_proxy_url == "https://proxy.example:8443"
        assert config.enable_runtime_dependency_cache is True

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

    def test_load_config_redacts_database_password_from_errors(self):
        """ConfigurationError must not echo secrets embedded in invalid values."""
        password = "sup3r-s3cret-hunter2"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(f'database_url: "mysql://tako:{password}@db.internal:3306/tako"\n')
            f.flush()
            config_path = Path(f.name)

        try:
            with pytest.raises(ConfigurationError) as exc_info:
                load_config(config_path)
        finally:
            config_path.unlink()

        message = str(exc_info.value)
        assert password not in message
        assert "database_url" in message

    def test_load_config_redacts_api_keys_from_errors(self):
        """Too-short API keys are not echoed back in the error message."""
        secret_key = "hunter2secret"  # < 16 chars, fails validation
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(f'api_keys: ["{secret_key}"]\n')
            f.flush()
            config_path = Path(f.name)

        try:
            with pytest.raises(ConfigurationError) as exc_info:
                load_config(config_path)
        finally:
            config_path.unlink()

        message = str(exc_info.value)
        assert secret_key not in message
        assert "api_keys" in message

    def test_validate_config_file_redacts_secrets(self):
        """validate_config_file errors must not echo secret input values."""
        password = "sup3r-s3cret-hunter2"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(f'database_url: "mysql://tako:{password}@db.internal:3306/tako"\n')
            f.flush()
            config_path = Path(f.name)

        try:
            errors = validate_config_file(config_path)
        finally:
            config_path.unlink()

        assert len(errors) > 0
        assert all(password not in error for error in errors)
        assert any("database_url" in error for error in errors)

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


class TestDLQTTLConfig:
    """Tests for dlq_ttl_days retention config."""

    def test_dlq_ttl_days_default_matches_record_retention(self):
        """DLQ TTL defaults to 30 days, same as execution record retention."""
        config = TakoVMConfig()
        assert config.dlq_ttl_days == 30
        assert config.dlq_ttl_days == config.execution_record_ttl_days

    def test_dlq_ttl_days_bounds(self):
        """dlq_ttl_days accepts 1-365 and rejects values outside that range."""
        assert TakoVMConfig(dlq_ttl_days=1).dlq_ttl_days == 1
        assert TakoVMConfig(dlq_ttl_days=365).dlq_ttl_days == 365

        with pytest.raises(ValueError):
            TakoVMConfig(dlq_ttl_days=0)
        with pytest.raises(ValueError):
            TakoVMConfig(dlq_ttl_days=366)
