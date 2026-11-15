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
    for var in ("TAKO_VM_CONFIG", "TAKO_VM_DATABASE_URL", "TAKO_VM_SECURITY_MODE"):
        monkeypatch.delenv(var, raising=False)
    reset_config()
    yield
    reset_config()


class TestContainerLimits:
    def test_container_limits_defaults(self):
        limits = ContainerLimits()
        assert limits.nofile_soft == 256
        assert limits.nofile_hard == 256
        assert limits.nproc_soft == 50
        assert limits.pids_limit == 100
        assert limits.tmpfs_size == "100m"

    def test_container_limits_custom_values(self):
        limits = ContainerLimits(
            nofile_soft=512,
            nofile_hard=1024,
            nproc_soft=100,
            nproc_hard=200,
            fsize=209715200,
            tmpfs_size="256m",
            pids_limit=200,
        )
        assert limits.nofile_soft == 512
        assert limits.nofile_hard == 1024

    def test_container_limits_tmpfs_size_formats(self):
        limits = ContainerLimits(tmpfs_size="256m")
        assert limits.tmpfs_size == "256m"

        limits = ContainerLimits(tmpfs_size="1g")
        assert limits.tmpfs_size == "1g"

    def test_container_limits_tmpfs_bounds(self):
        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(tmpfs_size="5m")
        assert "at least 10m" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(tmpfs_size="3g")
        assert "at most 2g" in str(exc_info.value)

    def test_container_limits_hard_ge_soft(self):
        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(nofile_soft=1024, nofile_hard=512)
        assert "nofile_hard must be >= nofile_soft" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            ContainerLimits(nproc_soft=100, nproc_hard=50)
        assert "nproc_hard must be >= nproc_soft" in str(exc_info.value)

    def test_container_limits_forbids_extra(self):
        with pytest.raises(ValueError):
            ContainerLimits.model_validate({"unknown_field": "value"})


class TestJobTypeConfig:
    def test_job_type_config_minimal(self):
        config = JobTypeConfig(name="test-job")
        assert config.name == "test-job"
        assert config.requirements == []
        assert config.timeout == 30
        assert config.network_enabled is False

    def test_job_type_config_full(self):
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
        JobTypeConfig(name="valid-name")
        JobTypeConfig(name="valid_name")
        JobTypeConfig(name="valid123")

        with pytest.raises(ValueError):
            JobTypeConfig(name="invalid name")

        with pytest.raises(ValueError):
            JobTypeConfig(name="invalid.name")

    def test_job_type_config_memory_limit_formats(self):
        JobTypeConfig(name="test", memory_limit="512m")
        JobTypeConfig(name="test", memory_limit="1g")
        JobTypeConfig(name="test", memory_limit="2G")

        with pytest.raises(ValueError):
            JobTypeConfig(name="test", memory_limit="512")

        with pytest.raises(ValueError):
            JobTypeConfig(name="test", memory_limit="512k")

    def test_job_type_config_memory_limit_bounds(self):
        with pytest.raises(ValueError) as exc_info:
            JobTypeConfig(name="test", memory_limit="32m")
        assert "at least 64m" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            JobTypeConfig(name="test", memory_limit="64g")
        assert "at most 32g" in str(exc_info.value)


class TestJobTypeGPUConfig:
    def test_job_type_gpu_config_defaults(self):
        config = JobTypeGPUConfig()
        assert config.enabled is False
        assert config.vendor is None
        assert config.count is None
        assert config.device_ids == []

    def test_job_type_gpu_config_nvidia_count(self):
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
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(enabled=True)
        assert "gpu.vendor is required" in str(exc_info.value)

    def test_job_type_gpu_config_rejects_count_with_device_ids(self):
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(
                enabled=True,
                vendor="nvidia",
                count=1,
                device_ids=["GPU-123"],
            )
        assert "mutually exclusive" in str(exc_info.value)

    def test_job_type_gpu_config_rejects_amd_count(self):
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(enabled=True, vendor="amd", count=1)
        assert "only supported" in str(exc_info.value)

    def test_job_type_gpu_config_rejects_fields_when_disabled(self):
        with pytest.raises(ValueError) as exc_info:
            JobTypeGPUConfig(enabled=False, vendor="nvidia")
        assert "gpu.enabled must be true" in str(exc_info.value)

    def test_job_type_gpu_config_device_ids_validation(self):
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
    def test_tako_vm_config_defaults(self):
        config = TakoVMConfig()
        assert config.production_mode is False
        assert config.max_workers == 4
        assert config.default_timeout == 30
        assert config.container_runtime == "runsc"
        assert config.security_mode == "strict"
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
        with tempfile.TemporaryDirectory() as tmpdir:
            config = TakoVMConfig(data_dir=tmpdir)
            config.resolve_paths()

            assert config.data_dir == Path(tmpdir)
            assert config.database_url.startswith("postgresql://")

    def test_tako_vm_config_timeout_validation(self):
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
        config = TakoVMConfig(database_url="postgresql+psycopg://user:pass@localhost:5432/testdb")
        assert config.database_url == "postgresql://user:pass@localhost:5432/testdb"

    def test_tako_vm_config_accepts_unix_socket_dsn(self):
        config = TakoVMConfig(database_url="postgresql:///testdb?host=/var/run/postgresql")
        assert config.database_url == "postgresql:///testdb?host=/var/run/postgresql"

    def test_tako_vm_config_container_runtime_validation(self):
        TakoVMConfig(container_runtime="runsc", security_mode="permissive")
        TakoVMConfig(container_runtime="runc", security_mode="permissive")

        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(container_runtime="invalid")
        assert "container_runtime must be one of" in str(exc_info.value)

    def test_tako_vm_config_security_mode_validation(self):
        TakoVMConfig(security_mode="strict")
        TakoVMConfig(security_mode="permissive")

        with pytest.raises(ValueError) as exc_info:
            TakoVMConfig(security_mode="invalid")
        assert "security_mode must be one of" in str(exc_info.value)

    def test_tako_vm_config_log_level_validation(self):
        config = TakoVMConfig(log_level="debug")
        assert config.log_level == "DEBUG"

        config = TakoVMConfig(log_level="WARNING")
        assert config.log_level == "WARNING"

        with pytest.raises(ValueError):
            TakoVMConfig(log_level="TRACE")

    def test_tako_vm_config_with_job_types(self):
        config = TakoVMConfig(
            job_types=[
                JobTypeConfig(name="job-a", requirements=["pandas"]),
                JobTypeConfig(name="job-b", timeout=60),
            ]
        )
        assert len(config.job_types) == 2
        assert config.job_types[0].name == "job-a"

    def test_tako_vm_config_dependency_proxy_url_validation(self):
        config = TakoVMConfig(dependency_proxy_url=" https://proxy.example:8443 ")
        assert config.dependency_proxy_url == "https://proxy.example:8443"

        with pytest.raises(ValueError, match="dependency_proxy_url"):
            TakoVMConfig(dependency_proxy_url="file:///tmp/proxy")

        with pytest.raises(ValueError, match="control characters"):
            TakoVMConfig(dependency_proxy_url="https://proxy.example\nbad")

        with pytest.raises(ValueError, match="path, query, or fragment"):
            TakoVMConfig(dependency_proxy_url="https://proxy.example:8443/proxy")

    def test_tako_vm_config_api_auth_validation(self):
        config = TakoVMConfig(api_auth_enabled=True, api_keys=[" aaaaaaaaaaaaaaaa "])
        assert config.api_keys == ["aaaaaaaaaaaaaaaa"]

        with pytest.raises(ValueError, match="api_keys"):
            TakoVMConfig(api_auth_enabled=True)

        with pytest.raises(ValueError, match="at least 16"):
            TakoVMConfig(api_auth_enabled=True, api_keys=["short"])

        with pytest.raises(ValueError, match="api_auth_header"):
            TakoVMConfig(api_auth_header="Bad Header")

    def test_tako_vm_config_get_method(self):
        config = TakoVMConfig(max_workers=8)
        assert config.get("max_workers") == 8
        assert config.get("nonexistent", "default") == "default"

    def test_tako_vm_config_forbids_extra(self):
        with pytest.raises(ValueError):
            TakoVMConfig.model_validate({"unknown_field": "value"})


class TestBindHostRule:
    def test_validator_delegates_to_ensure_bind_host_allowed(self):
        with pytest.raises(ValidationError) as exc_info:
            TakoVMConfig(server_host="0.0.0.0", api_auth_enabled=False)

        assert "refusing to bind non-loopback host" in str(exc_info.value)

    def test_ensure_bind_host_allowed_refuses_late_resolved_host(self):
        config = TakoVMConfig(server_host="127.0.0.1", api_auth_enabled=False)

        with pytest.raises(ValueError) as exc_info:
            config.ensure_bind_host_allowed("0.0.0.0")

        message = str(exc_info.value)
        assert "refusing to bind non-loopback host '0.0.0.0'" in message
        assert "api_auth_enabled=true" in message
        assert "127.0.0.1" in message
        assert "allow_unauthenticated_network_access=true" in message

    def test_ensure_bind_host_allowed_accepts_loopback_forms(self):
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
    def test_security_warnings_default_config(self):
        assert TakoVMConfig().security_warnings() == []

    def test_security_warnings_when_unauthenticated_bind_is_opted_into(self):
        config = TakoVMConfig(
            server_host="0.0.0.0",
            api_auth_enabled=False,
            allow_unauthenticated_network_access=True,
        )
        warnings = config.security_warnings()
        assert any("api_auth_enabled" in w for w in warnings)

    def test_security_warnings_empty_for_locked_down_config(self):
        config = TakoVMConfig(
            api_auth_enabled=True,
            api_keys=["aaaaaaaaaaaaaaaa"],
            security_mode="strict",
        )
        assert config.security_warnings() == []

    def test_security_warnings_empty_for_loopback_strict_config(self):
        config = TakoVMConfig(server_host="127.0.0.1", security_mode="strict")
        assert config.security_warnings() == []

        config = TakoVMConfig(server_host="localhost", security_mode="strict")
        assert config.security_warnings() == []

    def test_security_warnings_permissive_only(self):
        config = TakoVMConfig(
            api_auth_enabled=True,
            api_keys=["aaaaaaaaaaaaaaaa"],
            security_mode="permissive",
        )
        warnings = config.security_warnings()

        assert len(warnings) == 1
        assert "permissive" in warnings[0]

    def test_load_config_logs_security_warnings(self, caplog, monkeypatch):
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")
        with caplog.at_level(logging.WARNING, logger="tako_vm.config"):
            load_config()

        messages = [record.getMessage() for record in caplog.records]
        assert any("permissive" in message for message in messages)


class TestConfigLoading:
    def test_load_config_defaults(self):
        config = load_config()
        assert isinstance(config, TakoVMConfig)
        assert config.max_workers == 4

    def test_load_config_from_file(self):
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
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "PERMISSIVE")

        config = load_config()
        assert config.security_mode == "permissive"

    def test_load_config_env_container_runtime(self, monkeypatch):
        monkeypatch.setenv("TAKO_VM_CONTAINER_RUNTIME", "RUNC")
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")

        config = load_config()
        assert config.container_runtime == "runc"

    def test_load_config_env_api_protection_overrides(self, monkeypatch):
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
        monkeypatch.setenv(var_name, "not-a-number")

        with pytest.raises(ConfigurationError) as exc_info:
            load_config()

        assert var_name in str(exc_info.value)

    def test_load_config_invalid_raises(self):
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
        secret_key = "hunter2secret"
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
        errors = validate_config_file(Path("/nonexistent/config.yaml"))
        assert len(errors) == 1
        assert "not found" in errors[0].lower()


class TestConfigGlobals:
    def test_get_config_singleton(self):
        config1 = get_config()
        config2 = get_config()
        assert config1 is config2

    def test_reset_config(self):
        config1 = get_config()
        reset_config()
        config2 = get_config()
        assert config1 is not config2

    def test_set_config_path(self):
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
    def test_dlq_ttl_days_default_matches_record_retention(self):
        config = TakoVMConfig()
        assert config.dlq_ttl_days == 30
        assert config.dlq_ttl_days == config.execution_record_ttl_days

    def test_dlq_ttl_days_bounds(self):
        assert TakoVMConfig(dlq_ttl_days=1).dlq_ttl_days == 1
        assert TakoVMConfig(dlq_ttl_days=365).dlq_ttl_days == 365

        with pytest.raises(ValueError):
            TakoVMConfig(dlq_ttl_days=0)
        with pytest.raises(ValueError):
            TakoVMConfig(dlq_ttl_days=366)
