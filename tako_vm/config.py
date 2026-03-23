"""
Configuration management for Tako VM.

Uses Pydantic for validation with strict bounds checking.
Loads configuration from YAML file with optional env var overrides.
"""

import os
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Default config file locations (searched in order)
CONFIG_SEARCH_PATHS = [
    Path("tako_vm.yaml"),  # Current directory
    Path("config/tako_vm.yaml"),  # Config subdirectory
    Path.home() / ".tako_vm" / "config.yaml",  # User home
    Path("/etc/tako_vm/config.yaml"),  # System-wide
]


def get_default_data_dir() -> Path:
    """Get default data directory."""
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "tako_vm"
    return Path.home() / ".tako_vm"


# =============================================================================
# Pydantic Configuration Models with Validation
# =============================================================================


class ContainerLimits(BaseModel):
    """Container resource limits with validation bounds."""

    model_config = {"extra": "forbid"}

    # File descriptor limits
    nofile_soft: int = Field(default=256, ge=64, le=65536)
    nofile_hard: int = Field(default=256, ge=64, le=65536)

    # Process limits
    nproc_soft: int = Field(default=50, ge=10, le=1000)
    nproc_hard: int = Field(default=50, ge=10, le=1000)

    # Max file size in bytes (default 100MB)
    fsize: int = Field(default=104857600, ge=1048576, le=1073741824)

    # tmpfs size for /tmp (e.g., "100m", "256m", "1g")
    tmpfs_size: str = Field(default="100m")

    # PIDs limit
    pids_limit: int = Field(default=100, ge=10, le=1000)

    @field_validator("tmpfs_size")
    @classmethod
    def validate_tmpfs_size(cls, v: str) -> str:
        """Validate tmpfs size format and bounds."""
        v = v.lower().strip()
        if not v:
            raise ValueError("tmpfs_size cannot be empty")

        # Parse size
        if v.endswith("g"):
            size_mb = int(v[:-1]) * 1024
        elif v.endswith("m"):
            size_mb = int(v[:-1])
        elif v.endswith("k"):
            size_mb = int(v[:-1]) // 1024
        else:
            # Assume bytes
            size_mb = int(v) // (1024 * 1024)

        # Validate bounds (10MB to 2GB)
        if size_mb < 10:
            raise ValueError("tmpfs_size must be at least 10m")
        if size_mb > 2048:
            raise ValueError("tmpfs_size must be at most 2g")

        return v

    @model_validator(mode="after")
    def validate_limits(self) -> "ContainerLimits":
        """Ensure hard limits >= soft limits."""
        if self.nofile_hard < self.nofile_soft:
            raise ValueError("nofile_hard must be >= nofile_soft")
        if self.nproc_hard < self.nproc_soft:
            raise ValueError("nproc_hard must be >= nproc_soft")
        return self


class JobTypeGPUConfig(BaseModel):
    """GPU configuration for a job type."""

    model_config = {"extra": "forbid"}

    enabled: bool = Field(default=False)
    vendor: Optional[str] = Field(default=None, description="GPU vendor: 'nvidia' or 'amd'")
    count: Optional[int] = Field(default=None, ge=1, le=16)
    device_ids: List[str] = Field(default_factory=list, max_length=16)

    @field_validator("vendor")
    @classmethod
    def validate_vendor(cls, v: Optional[str]) -> Optional[str]:
        """Validate GPU vendor."""
        if v is None:
            return None
        normalized = v.lower().strip()
        if normalized not in {"nvidia", "amd"}:
            raise ValueError("gpu.vendor must be one of: amd, nvidia")
        return normalized

    @field_validator("device_ids")
    @classmethod
    def validate_device_ids(cls, values: List[str]) -> List[str]:
        """Validate GPU device ID list."""
        normalized: List[str] = []
        seen: set[str] = set()
        for value in values:
            device_id = value.strip()
            if not device_id:
                raise ValueError("gpu.device_ids cannot contain empty values")
            if "," in device_id:
                raise ValueError("gpu.device_ids entries cannot contain commas")
            if device_id.lower() in seen:
                raise ValueError("gpu.device_ids cannot contain duplicate values")
            seen.add(device_id.lower())
            normalized.append(device_id)
        return normalized

    @model_validator(mode="after")
    def validate_gpu_config(self) -> "JobTypeGPUConfig":
        """Validate cross-field GPU settings."""
        if not self.enabled:
            if self.vendor is not None or self.count is not None or self.device_ids:
                raise ValueError(
                    "gpu.enabled must be true when setting gpu.vendor, gpu.count, or gpu.device_ids"
                )
            return self

        if self.vendor is None:
            raise ValueError("gpu.vendor is required when gpu.enabled is true")

        if self.count is not None and self.device_ids:
            raise ValueError("gpu.count and gpu.device_ids are mutually exclusive")

        if self.vendor == "amd" and self.count is not None:
            raise ValueError("gpu.count is only supported for gpu.vendor='nvidia'")

        return self


class JobTypeConfig(BaseModel):
    """Job type configuration for embedding in main config."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=64)
    requirements: List[str] = Field(default_factory=list)
    python_version: str = Field(default="3.11")
    base_image: Optional[str] = None
    shared_code: List[str] = Field(default_factory=list)
    environment: Dict[str, str] = Field(default_factory=dict)
    memory_limit: str = Field(default="512m")
    cpu_limit: float = Field(default=1.0, ge=0.1, le=16.0)
    timeout: int = Field(default=30, ge=1, le=3600)
    """Timeout for code execution phase in seconds."""

    startup_timeout: int = Field(default=120, ge=10, le=600)
    """Timeout for startup phase (container init + dep install) in seconds."""

    network_enabled: bool = Field(default=False, description="Allow network access (security risk)")
    session_enabled: bool = Field(
        default=False,
        description="Allow this job type to be used for long-running sessions",
    )
    gpu: JobTypeGPUConfig = Field(default_factory=JobTypeGPUConfig)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate job type name format."""
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("name must contain only alphanumeric, dash, or underscore")
        return v

    @field_validator("memory_limit")
    @classmethod
    def validate_memory_limit(cls, v: str) -> str:
        """Validate memory limit format."""
        v = v.lower().strip()
        if not v:
            raise ValueError("memory_limit cannot be empty")

        # Parse and validate
        if v.endswith("g"):
            size_mb = int(v[:-1]) * 1024
        elif v.endswith("m"):
            size_mb = int(v[:-1])
        else:
            raise ValueError("memory_limit must end with 'm' or 'g'")

        if size_mb < 64:
            raise ValueError("memory_limit must be at least 64m")
        if size_mb > 32768:  # 32GB max
            raise ValueError("memory_limit must be at most 32g")

        return v


class TakoVMConfig(BaseModel):
    """Tako VM configuration with full validation."""

    # Mode
    production_mode: bool = Field(
        default=False, description="Strict mode requiring pre-built images"
    )

    # Logging
    log_level: str = Field(
        default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )

    # Server
    server_host: str = Field(default="0.0.0.0", description="Server host to bind to")
    server_port: int = Field(default=8000, ge=1, le=65535, description="Server port to bind to")

    # API-layer request protection
    api_max_payload_bytes: int = Field(
        default=2097152, ge=1024, le=104857600, description="Maximum HTTP request payload size"
    )
    api_rate_limit_enabled: bool = Field(default=True, description="Enable API rate limiting")
    api_rate_limit_requests: int = Field(
        default=120, ge=1, le=100000, description="Requests allowed per rate limit window"
    )
    api_rate_limit_window_seconds: int = Field(
        default=60, ge=1, le=3600, description="Rate limit window duration in seconds"
    )

    # Retry configuration
    max_retry_attempts: int = Field(
        default=2, ge=1, le=10, description="Maximum retry attempts for transient failures"
    )
    retry_base_delay: float = Field(
        default=1.0, ge=0.1, le=60.0, description="Base delay between retries in seconds"
    )

    # Queue wait timeout
    queue_wait_timeout: float = Field(
        default=1.0, ge=0.1, le=30.0, description="Queue wait timeout in seconds"
    )

    # Paths (stored as strings internally, exposed as Path via properties)
    data_dir_str: str = Field(default_factory=lambda: str(get_default_data_dir()), alias="data_dir")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is a valid Python logging level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid_levels:
            raise ValueError(f"log_level must be one of: {', '.join(sorted(valid_levels))}")
        return v.upper()

    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/tako_vm",
        description="PostgreSQL connection URL",
    )
    seccomp_profile_path_str: Optional[str] = Field(default=None, alias="seccomp_profile_path")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Validate and normalize PostgreSQL database URL format."""
        parsed = urlsplit(v)
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("database_url must use postgresql:// or postgresql+psycopg://")

        if parsed.scheme == "postgresql+psycopg":
            v = urlunsplit(
                ("postgresql", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
            )
            parsed = urlsplit(v)

        query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if not parsed.hostname and not query_params.get("host"):
            raise ValueError("database_url must include a hostname or host query parameter")
        if not parsed.path or parsed.path == "/":
            raise ValueError("database_url must include a database name")
        return v

    # Queue & Workers
    max_workers: int = Field(default=4, ge=1, le=64)
    max_queue_size: int = Field(default=100, ge=1, le=10000)

    # Output limits (with bounds)
    max_stdout_bytes: int = Field(default=65536, ge=1024, le=104857600)  # 1KB to 100MB
    max_stderr_bytes: int = Field(default=65536, ge=1024, le=104857600)  # 1KB to 100MB
    max_artifact_bytes: int = Field(default=10485760, ge=1024, le=1073741824)  # 1KB to 1GB
    max_total_artifacts_bytes: int = Field(default=52428800, ge=1024, le=10737418240)  # 1KB to 10GB

    # Input limits
    max_input_bytes: int = Field(default=1048576, ge=1024, le=104857600)  # 1KB to 100MB
    max_code_bytes: int = Field(default=102400, ge=1024, le=10485760)  # 1KB to 10MB

    # Execution limits
    default_timeout: int = Field(default=30, ge=1, le=3600)
    """Default timeout for code execution phase in seconds."""

    default_startup_timeout: int = Field(default=120, ge=10, le=600)
    """Default timeout for startup phase (container init + dep install) in seconds."""

    max_timeout: int = Field(default=300, ge=1, le=86400)
    """Maximum allowed timeout for code execution phase."""

    max_startup_timeout: int = Field(default=600, ge=30, le=1800)
    """Maximum allowed timeout for startup phase (up to 30 minutes for large deps)."""

    # Retention
    execution_record_ttl_days: int = Field(default=30, ge=1, le=3650)

    # Docker
    docker_image: str = Field(default="code-executor:latest")
    enable_seccomp: bool = Field(default=True)
    enable_cap_restrictions: bool = Field(
        default=True,
        description="Enable capability restrictions (--cap-drop=ALL --cap-add=...)",
    )
    # Note: Disabled by default because the entrypoint uses gosu to drop privileges
    # If you're using an image without gosu, set this to True
    enable_userns: bool = Field(default=False)

    # Container runtime (gVisor by default for strong isolation)
    container_runtime: str = Field(
        default="runsc",
        description="Container runtime: 'runsc' (gVisor) for strong isolation, 'runc' for standard Docker",
    )

    # Security mode
    security_mode: str = Field(
        default="permissive",
        description="Security mode: 'strict' fails if gVisor unavailable, 'permissive' allows fallback to runc",
    )

    @field_validator("container_runtime")
    @classmethod
    def validate_container_runtime(cls, v: str) -> str:
        """Validate container runtime."""
        valid_runtimes = {"runsc", "runc"}
        if v not in valid_runtimes:
            raise ValueError(
                f"container_runtime must be one of: {', '.join(sorted(valid_runtimes))}"
            )
        return v

    @field_validator("security_mode")
    @classmethod
    def validate_security_mode(cls, v: str) -> str:
        """Validate security mode."""
        valid_modes = {"strict", "permissive"}
        if v not in valid_modes:
            raise ValueError(f"security_mode must be one of: {', '.join(sorted(valid_modes))}")
        return v

    # Container limits (new!)
    container_limits: ContainerLimits = Field(default_factory=ContainerLimits)

    # Job types (new - can be defined in main config)
    job_types: List[JobTypeConfig] = Field(default_factory=list)

    # Internal: resolved paths (set after validation)
    _resolved_data_dir: Optional[Path] = None
    _resolved_seccomp_profile_path: Optional[Path] = None

    model_config = {"extra": "forbid", "populate_by_name": True}  # Reject unknown keys, allow alias

    @model_validator(mode="after")
    def validate_timeouts(self) -> "TakoVMConfig":
        """Ensure default timeouts <= max timeouts."""
        if self.default_timeout > self.max_timeout:
            raise ValueError("default_timeout must be <= max_timeout")
        if self.default_startup_timeout > self.max_startup_timeout:
            raise ValueError("default_startup_timeout must be <= max_startup_timeout")
        return self

    def resolve_paths(self) -> "TakoVMConfig":
        """Resolve all paths and create directories."""
        # Data directory
        self._resolved_data_dir = Path(self.data_dir_str)
        self._resolved_data_dir.mkdir(parents=True, exist_ok=True)

        # Seccomp profile
        if self.seccomp_profile_path_str:
            self._resolved_seccomp_profile_path = Path(self.seccomp_profile_path_str)
        else:
            self._resolved_seccomp_profile_path = Path(
                str(_resource_files("tako_vm").joinpath("seccomp_profile.json"))
            )

        return self

    # Backward-compatible properties that return Path objects
    @property
    def data_dir(self) -> Path:
        """Get data directory as Path (backward compatible)."""
        if self._resolved_data_dir is None:
            self.resolve_paths()
        return self._resolved_data_dir  # type: ignore

    @property
    def seccomp_profile_path(self) -> Path:
        """Get seccomp profile path (backward compatible)."""
        if self._resolved_seccomp_profile_path is None:
            self.resolve_paths()
        return self._resolved_seccomp_profile_path  # type: ignore

    # Aliases for new code (explicit _path suffix)
    @property
    def data_dir_path(self) -> Path:
        return self.data_dir

    @property
    def seccomp_profile_path_resolved(self) -> Path:
        return self.seccomp_profile_path

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value by key (for backward compatibility)."""
        try:
            return getattr(self, key)
        except AttributeError:
            return default


# =============================================================================
# Configuration Loading
# =============================================================================


def find_config_file() -> Optional[Path]:
    """Find config file from search paths."""
    # Check env var override first
    env_config = os.environ.get("TAKO_VM_CONFIG")
    if env_config:
        path = Path(env_config)
        if path.exists():
            return path

    # Search default paths
    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path

    return None


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""


def load_config(config_path: Optional[Path] = None) -> TakoVMConfig:
    """
    Load configuration from YAML file with validation.

    Search order:
    1. Explicit path argument
    2. TAKO_VM_CONFIG environment variable
    3. ./tako_vm.yaml
    4. ./config/tako_vm.yaml
    5. ~/.tako_vm/config.yaml
    6. /etc/tako_vm/config.yaml
    7. Built-in defaults

    Raises:
        ConfigurationError: If configuration validation fails
    """
    config_dict: Dict[str, Any] = {}

    def parse_env_int(var_name: str) -> int:
        """Parse integer environment variables with clear config errors."""
        value = os.environ[var_name]
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid configuration: {var_name} must be an integer (got {value!r})"
            ) from exc

    # Find config file
    if config_path is None:
        config_path = find_config_file()

    # Load from file if found
    if config_path and config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            if loaded:
                config_dict = loaded

    # Apply env var overrides
    if "TAKO_VM_DATA_DIR" in os.environ:
        config_dict["data_dir"] = os.environ["TAKO_VM_DATA_DIR"]
    if "TAKO_VM_DATABASE_URL" in os.environ:
        config_dict["database_url"] = os.environ["TAKO_VM_DATABASE_URL"]
    if "TAKO_VM_SECURITY_MODE" in os.environ:
        config_dict["security_mode"] = os.environ["TAKO_VM_SECURITY_MODE"].lower()
    if "TAKO_VM_CONTAINER_RUNTIME" in os.environ:
        config_dict["container_runtime"] = os.environ["TAKO_VM_CONTAINER_RUNTIME"].lower()
    if "TAKO_VM_ENABLE_SECCOMP" in os.environ:
        config_dict["enable_seccomp"] = os.environ["TAKO_VM_ENABLE_SECCOMP"].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_ENABLE_CAP_RESTRICTIONS" in os.environ:
        config_dict["enable_cap_restrictions"] = os.environ[
            "TAKO_VM_ENABLE_CAP_RESTRICTIONS"
        ].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_API_MAX_PAYLOAD_BYTES" in os.environ:
        config_dict["api_max_payload_bytes"] = parse_env_int("TAKO_VM_API_MAX_PAYLOAD_BYTES")
    if "TAKO_VM_API_RATE_LIMIT_ENABLED" in os.environ:
        config_dict["api_rate_limit_enabled"] = os.environ[
            "TAKO_VM_API_RATE_LIMIT_ENABLED"
        ].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_API_RATE_LIMIT_REQUESTS" in os.environ:
        config_dict["api_rate_limit_requests"] = parse_env_int("TAKO_VM_API_RATE_LIMIT_REQUESTS")
    if "TAKO_VM_API_RATE_LIMIT_WINDOW_SECONDS" in os.environ:
        config_dict["api_rate_limit_window_seconds"] = parse_env_int(
            "TAKO_VM_API_RATE_LIMIT_WINDOW_SECONDS"
        )
    # Validate and create config
    try:
        config = TakoVMConfig(**config_dict)
        config.resolve_paths()
        return config
    except Exception as e:
        raise ConfigurationError(f"Invalid configuration: {e}") from e


# Global config instance (lazy loaded)
_config: Optional[TakoVMConfig] = None
_config_path: Optional[Path] = None


def get_config() -> TakoVMConfig:
    """Get global configuration instance."""
    global _config
    if _config is None:
        _config = load_config(_config_path)
    return _config


def set_config(config: TakoVMConfig) -> None:
    """Set global configuration instance."""
    global _config
    _config = config


def set_config_path(path: Optional[Path]) -> None:
    """Set the config file path for lazy loading."""
    global _config_path, _config
    _config_path = path
    _config = None  # Reset so next get_config() reloads


def get_config_path() -> Optional[Path]:
    """Get the currently configured config file path."""
    if _config_path:
        return _config_path
    return find_config_file()


def reset_config() -> None:
    """Reset global config (useful for testing)."""
    global _config, _config_path
    _config = None
    _config_path = None


def validate_config_file(path: Path) -> List[str]:
    """
    Validate a config file without loading it globally.

    Returns:
        List of validation errors (empty if valid)
    """
    errors = []
    try:
        with open(path, encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)
        if config_dict:
            TakoVMConfig(**config_dict)
    except FileNotFoundError:
        errors.append(f"Config file not found: {path}")
    except yaml.YAMLError as e:
        errors.append(f"Invalid YAML: {e}")
    except Exception as e:
        errors.append(str(e))
    return errors
