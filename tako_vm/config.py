import logging
import os
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from tako_vm.constants import DEFAULT_IMAGE

logger = logging.getLogger(__name__)

CONFIG_SEARCH_PATHS = [
    Path("tako_vm.yaml"),
    Path("config/tako_vm.yaml"),
    Path.home() / ".tako_vm" / "config.yaml",
    Path("/etc/tako_vm/config.yaml"),
]

_SAFE_PROXY_URL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/=,+-[]{}@%"
)

_LOOPBACK_HOSTS = frozenset({"localhost", "::1", "[::1]"})


def host_is_loopback(host: str) -> bool:
    value = host.strip().lower()
    return value in _LOOPBACK_HOSTS or value.startswith("127.")


def _sanitize_validation_error(e: ValidationError) -> str:
    lines: List[str] = []
    for err in e.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", ())) or "config"
        lines.append(f"field {loc}: {err.get('msg', 'invalid value')}")
    return "; ".join(lines) or "validation failed"


def _parse_size_to_mb(v: str, *, allow_k_and_bytes: bool) -> int:
    if v.endswith("g"):
        return int(v[:-1]) * 1024
    if v.endswith("m"):
        return int(v[:-1])
    if allow_k_and_bytes:
        if v.endswith("k"):
            return int(v[:-1]) // 1024
        return int(v) // (1024 * 1024)
    raise ValueError("memory_limit must end with 'm' or 'g'")


def get_default_data_dir() -> Path:
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "tako_vm"
    return Path.home() / ".tako_vm"


class ContainerLimits(BaseModel):
    model_config = {"extra": "forbid"}

    nofile_soft: int = Field(default=256, ge=64, le=65536)
    nofile_hard: int = Field(default=256, ge=64, le=65536)

    nproc_soft: int = Field(default=50, ge=10, le=1000)
    nproc_hard: int = Field(default=50, ge=10, le=1000)

    fsize: int = Field(default=104857600, ge=1048576, le=1073741824)

    tmpfs_size: str = Field(default="100m")

    pids_limit: int = Field(default=100, ge=10, le=1000)

    @field_validator("tmpfs_size")
    @classmethod
    def validate_tmpfs_size(cls, v: str) -> str:
        v = v.lower().strip()
        if not v:
            raise ValueError("tmpfs_size cannot be empty")

        size_mb = _parse_size_to_mb(v, allow_k_and_bytes=True)

        if size_mb < 10:
            raise ValueError("tmpfs_size must be at least 10m")
        if size_mb > 2048:
            raise ValueError("tmpfs_size must be at most 2g")

        return v

    @model_validator(mode="after")
    def validate_limits(self) -> "ContainerLimits":
        if self.nofile_hard < self.nofile_soft:
            raise ValueError("nofile_hard must be >= nofile_soft")
        if self.nproc_hard < self.nproc_soft:
            raise ValueError("nproc_hard must be >= nproc_soft")
        return self


class JobTypeGPUConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = Field(default=False)
    vendor: Optional[str] = Field(default=None, description="GPU vendor: 'nvidia' or 'amd'")
    count: Optional[int] = Field(default=None, ge=1, le=16)
    device_ids: List[str] = Field(default_factory=list, max_length=16)

    @field_validator("vendor")
    @classmethod
    def validate_vendor(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        normalized = v.lower().strip()
        if normalized not in {"nvidia", "amd"}:
            raise ValueError("gpu.vendor must be one of: amd, nvidia")
        return normalized

    @field_validator("device_ids")
    @classmethod
    def validate_device_ids(cls, values: List[str]) -> List[str]:
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

    startup_timeout: int = Field(default=120, ge=10, le=600)

    network_enabled: bool = Field(default=False, description="Allow network access (security risk)")
    session_enabled: bool = Field(
        default=False,
        description="Allow this job type to be used for long-running sessions",
    )
    gpu: JobTypeGPUConfig = Field(default_factory=JobTypeGPUConfig)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("name must contain only alphanumeric, dash, or underscore")
        return v

    @field_validator("memory_limit")
    @classmethod
    def validate_memory_limit(cls, v: str) -> str:
        v = v.lower().strip()
        if not v:
            raise ValueError("memory_limit cannot be empty")

        size_mb = _parse_size_to_mb(v, allow_k_and_bytes=False)

        if size_mb < 64:
            raise ValueError("memory_limit must be at least 64m")
        if size_mb > 32768:
            raise ValueError("memory_limit must be at most 32g")

        return v


class TakoVMConfig(BaseModel):
    production_mode: bool = Field(
        default=False, description="Strict mode requiring pre-built images"
    )

    log_level: str = Field(
        default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )

    server_host: str = Field(
        default="127.0.0.1",
        description=(
            "Server host to bind to. Defaults to loopback: the API executes arbitrary code and "
            "ships with authentication disabled, so binding all interfaces by default would "
            "expose an unauthenticated code-execution endpoint to the network. Set 0.0.0.0 "
            "only together with api_auth_enabled (see allow_unauthenticated_network_access)"
        ),
    )
    allow_unauthenticated_network_access: bool = Field(
        default=False,
        description=(
            "Escape hatch: permit binding a non-loopback interface while api_auth_enabled is "
            "false. Refused by default, because that combination is an unauthenticated remote "
            "code-execution endpoint. Only set this when something else (a private network, a "
            "service mesh, an authenticating reverse proxy) provides the access control"
        ),
    )
    server_port: int = Field(default=8000, ge=1, le=65535, description="Server port to bind to")

    api_max_payload_bytes: int = Field(
        default=2097152, ge=1024, le=104857600, description="Maximum HTTP request payload size"
    )
    api_auth_enabled: bool = Field(
        default=False,
        description=(
            "Require API key authentication. The default (false) is development-friendly; "
            "production deployments should enable this"
        ),
    )
    api_keys: List[str] = Field(
        default_factory=list,
        description="Accepted API keys when api_auth_enabled is true",
    )
    api_auth_header: str = Field(
        default="X-API-Key",
        description="Header used for API key authentication",
    )
    api_rate_limit_enabled: bool = Field(default=True, description="Enable API rate limiting")
    api_rate_limit_requests: int = Field(
        default=120, ge=1, le=100000, description="Requests allowed per rate limit window"
    )
    api_rate_limit_window_seconds: int = Field(
        default=60, ge=1, le=3600, description="Rate limit window duration in seconds"
    )

    max_retry_attempts: int = Field(
        default=2, ge=1, le=10, description="Maximum retry attempts for transient failures"
    )
    retry_base_delay: float = Field(
        default=1.0, ge=0.1, le=60.0, description="Base delay between retries in seconds"
    )

    queue_wait_timeout: float = Field(
        default=1.0, ge=0.1, le=30.0, description="Queue wait timeout in seconds"
    )

    data_dir_str: str = Field(default_factory=lambda: str(get_default_data_dir()), alias="data_dir")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid_levels:
            raise ValueError(f"log_level must be one of: {', '.join(sorted(valid_levels))}")
        return v.upper()

    @field_validator("api_auth_header")
    @classmethod
    def validate_api_auth_header(cls, v: str) -> str:
        normalized = v.strip()
        if not normalized:
            raise ValueError("api_auth_header cannot be empty")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if any(char not in allowed for char in normalized):
            raise ValueError("api_auth_header must be a valid HTTP header name")
        return normalized

    @field_validator("api_keys")
    @classmethod
    def validate_api_keys(cls, values: List[str]) -> List[str]:
        normalized: List[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.strip()
            if not key:
                raise ValueError("api_keys cannot contain empty values")
            if any(char in key for char in ("\n", "\r", "\x00")):
                raise ValueError("api_keys cannot contain control characters")
            if len(key) < 16:
                raise ValueError("api_keys entries must be at least 16 characters")
            if key in seen:
                raise ValueError("api_keys cannot contain duplicate values")
            seen.add(key)
            normalized.append(key)
        return normalized

    def is_loopback_host(self) -> bool:
        return host_is_loopback(self.server_host)

    def ensure_bind_host_allowed(self, host: str) -> None:
        if self.api_auth_enabled or self.allow_unauthenticated_network_access:
            return
        if host_is_loopback(host):
            return
        raise ValueError(
            f"refusing to bind non-loopback host '{host}' with "
            "api_auth_enabled=false: this exposes an unauthenticated code-execution "
            "endpoint to the network. Fix it in one of three ways: set "
            "api_auth_enabled=true and populate api_keys; bind loopback "
            "(server_host: 127.0.0.1 in the config, or --host 127.0.0.1); or set "
            "allow_unauthenticated_network_access=true if access control is enforced "
            "outside Tako VM"
        )

    @model_validator(mode="after")
    def validate_api_auth_config(self) -> "TakoVMConfig":
        if self.api_auth_enabled and not self.api_keys:
            raise ValueError("api_keys must contain at least one key when api_auth_enabled=true")

        self.ensure_bind_host_allowed(self.server_host)
        return self

    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/tako_vm",
        description="PostgreSQL connection URL",
    )
    seccomp_profile_path_str: Optional[str] = Field(default=None, alias="seccomp_profile_path")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
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

    max_workers: int = Field(default=4, ge=1, le=64)
    max_queue_size: int = Field(default=100, ge=1, le=10000)

    max_stdout_bytes: int = Field(default=65536, ge=1024, le=104857600)
    max_stderr_bytes: int = Field(default=65536, ge=1024, le=104857600)
    max_artifact_bytes: int = Field(default=10485760, ge=1024, le=1073741824)
    max_total_artifacts_bytes: int = Field(default=52428800, ge=1024, le=10737418240)

    max_input_bytes: int = Field(default=1048576, ge=1024, le=104857600)
    max_code_bytes: int = Field(default=102400, ge=1024, le=10485760)

    default_timeout: int = Field(default=30, ge=1, le=3600)

    default_startup_timeout: int = Field(default=120, ge=10, le=600)

    max_timeout: int = Field(default=300, ge=1, le=86400)

    max_startup_timeout: int = Field(default=600, ge=30, le=1800)

    allow_runtime_requirements: bool = Field(
        default=False,
        description="Allow jobs to install Python packages at runtime",
    )
    dependency_proxy_url: Optional[str] = Field(
        default=None,
        description="Optional HTTP(S)/SOCKS proxy URL used only during runtime dependency installs",
    )
    enable_runtime_dependency_cache: bool = Field(
        default=False,
        description="Mount a shared uv cache volume for runtime dependency installs",
    )

    sessions_enabled: bool = Field(
        default=False,
        description="Enable long-running sessions (persistence scaffolding; off by default)",
    )
    session_idle_timeout_seconds: int = Field(
        default=1800,
        ge=30,
        le=86400,
        description="Default idle window before a session is reaped, in seconds",
    )
    session_max_ttl_seconds: int = Field(
        default=86400,
        ge=60,
        le=604800,
        description="Default absolute maximum session lifetime, in seconds",
    )
    session_max_concurrent: int = Field(
        default=50,
        ge=1,
        description="Maximum number of concurrent live sessions",
    )

    execution_record_ttl_days: int = Field(default=30, ge=1, le=3650)
    dlq_ttl_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description=(
            "Days to retain dead-letter-queue entries. Defaults to 30 to match "
            "execution record retention: DLQ entries are forensic records of "
            "internal failures and should not expire sooner than ordinary records."
        ),
    )

    docker_image: str = Field(default=DEFAULT_IMAGE)
    enable_seccomp: bool = Field(default=True)
    enable_cap_restrictions: bool = Field(
        default=True,
        description="Enable capability restrictions (--cap-drop=ALL --cap-add=...)",
    )
    enable_userns: bool = Field(default=False)

    container_runtime: str = Field(
        default="runsc",
        description="Container runtime: 'runsc' (gVisor) for strong isolation, 'runc' for standard Docker",
    )

    security_mode: str = Field(
        default="strict",
        description=(
            "Security mode: 'strict' (default) FAILS if gVisor is unavailable rather than "
            "running untrusted code behind a weaker boundary; 'permissive' allows a silent "
            "fallback to runc. gVisor is the isolation boundary this project promises, so the "
            "default fails closed. Set 'permissive' only when you accept runc-level isolation "
            "(e.g. local development or CI on a host without gVisor)"
        ),
    )

    @field_validator("container_runtime")
    @classmethod
    def validate_container_runtime(cls, v: str) -> str:
        valid_runtimes = {"runsc", "runc"}
        if v not in valid_runtimes:
            raise ValueError(
                f"container_runtime must be one of: {', '.join(sorted(valid_runtimes))}"
            )
        return v

    @field_validator("security_mode")
    @classmethod
    def validate_security_mode(cls, v: str) -> str:
        valid_modes = {"strict", "permissive"}
        if v not in valid_modes:
            raise ValueError(f"security_mode must be one of: {', '.join(sorted(valid_modes))}")
        return v

    container_limits: ContainerLimits = Field(default_factory=ContainerLimits)

    job_types: List[JobTypeConfig] = Field(default_factory=list)

    @field_validator("dependency_proxy_url")
    @classmethod
    def validate_dependency_proxy_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        normalized = v.strip()
        if not normalized:
            return None
        if any(char in normalized for char in ("\n", "\r", "\x00")):
            raise ValueError("dependency_proxy_url cannot contain control characters")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https", "socks5"}:
            raise ValueError("dependency_proxy_url must use http://, https://, or socks5://")
        if not parsed.hostname:
            raise ValueError("dependency_proxy_url must include a hostname")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("dependency_proxy_url cannot include a path, query, or fragment")
        if any(char not in _SAFE_PROXY_URL_CHARS for char in normalized):
            raise ValueError("dependency_proxy_url contains unsupported characters")
        return normalized

    _resolved_data_dir: Optional[Path] = None
    _resolved_seccomp_profile_path: Optional[Path] = None

    model_config = {"extra": "forbid", "populate_by_name": True}

    @model_validator(mode="after")
    def validate_timeouts(self) -> "TakoVMConfig":
        if self.default_timeout > self.max_timeout:
            raise ValueError("default_timeout must be <= max_timeout")
        oversized_job_types = [
            job_type.name for job_type in self.job_types if job_type.timeout > self.max_timeout
        ]
        if oversized_job_types:
            names = ", ".join(oversized_job_types)
            raise ValueError(f"job type timeout must be <= max_timeout for: {names}")
        if self.default_startup_timeout > self.max_startup_timeout:
            raise ValueError("default_startup_timeout must be <= max_startup_timeout")
        if self.session_idle_timeout_seconds > self.session_max_ttl_seconds:
            raise ValueError("session_idle_timeout_seconds must be <= session_max_ttl_seconds")
        return self

    def resolve_paths(self) -> "TakoVMConfig":
        self._resolve_path_strings()
        self._ensure_data_dir()
        return self

    def _resolve_path_strings(self) -> None:
        self._resolved_data_dir = Path(self.data_dir_str)

        if self.seccomp_profile_path_str:
            self._resolved_seccomp_profile_path = Path(self.seccomp_profile_path_str)
        else:
            self._resolved_seccomp_profile_path = Path(
                str(_resource_files("tako_vm").joinpath("seccomp_profile.json"))
            )

    def _ensure_data_dir(self) -> None:
        if self._resolved_data_dir is None:
            self._resolve_path_strings()
        self._resolved_data_dir.mkdir(parents=True, exist_ok=True)  # type: ignore

    @property
    def data_dir(self) -> Path:
        if self._resolved_data_dir is None:
            self._resolve_path_strings()
        self._ensure_data_dir()
        return self._resolved_data_dir  # type: ignore

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def seccomp_profile_path(self) -> Path:
        if self._resolved_seccomp_profile_path is None:
            self.resolve_paths()
        return self._resolved_seccomp_profile_path  # type: ignore

    @property
    def data_dir_path(self) -> Path:
        return self.data_dir

    @property
    def seccomp_profile_path_resolved(self) -> Path:
        return self.seccomp_profile_path

    def security_warnings(self) -> List[str]:
        warnings: List[str] = []

        if not self.api_auth_enabled and not self.is_loopback_host():
            warnings.append(
                f"API authentication is disabled (api_auth_enabled=false) while the server "
                f"binds to non-loopback host '{self.server_host}', permitted only because "
                "allow_unauthenticated_network_access=true: anyone who can reach the port can "
                "execute code. Ensure access control is enforced in front of Tako VM."
            )

        if self.security_mode == "permissive":
            warnings.append(
                "security_mode is 'permissive': execution may silently fall back from gVisor "
                "(runsc) to runc if gVisor is unavailable, weakening sandbox isolation. Use "
                "security_mode='strict' in production."
            )

        return warnings

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            return default


def find_config_file() -> Optional[Path]:
    env_config = os.environ.get("TAKO_VM_CONFIG")
    if env_config:
        path = Path(env_config)
        if path.exists():
            return path

    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path

    return None


class ConfigurationError(Exception):
    pass


def load_config(config_path: Optional[Path] = None) -> TakoVMConfig:
    config_dict: Dict[str, Any] = {}

    def parse_env_int(var_name: str) -> int:
        value = os.environ[var_name]
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid configuration: {var_name} must be an integer (got {value!r})"
            ) from exc

    if config_path is None:
        config_path = find_config_file()

    if config_path and config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            if loaded:
                config_dict = loaded

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
    if "TAKO_VM_API_AUTH_ENABLED" in os.environ:
        config_dict["api_auth_enabled"] = os.environ["TAKO_VM_API_AUTH_ENABLED"].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_API_KEYS" in os.environ:
        config_dict["api_keys"] = [
            key.strip() for key in os.environ["TAKO_VM_API_KEYS"].split(",") if key.strip()
        ]
    if "TAKO_VM_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS" in os.environ:
        config_dict["allow_unauthenticated_network_access"] = os.environ[
            "TAKO_VM_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS"
        ].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_API_AUTH_HEADER" in os.environ:
        config_dict["api_auth_header"] = os.environ["TAKO_VM_API_AUTH_HEADER"]
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
    if "TAKO_VM_ALLOW_RUNTIME_REQUIREMENTS" in os.environ:
        config_dict["allow_runtime_requirements"] = os.environ[
            "TAKO_VM_ALLOW_RUNTIME_REQUIREMENTS"
        ].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_DEPENDENCY_PROXY_URL" in os.environ:
        config_dict["dependency_proxy_url"] = os.environ["TAKO_VM_DEPENDENCY_PROXY_URL"]
    if "TAKO_VM_ENABLE_RUNTIME_DEPENDENCY_CACHE" in os.environ:
        config_dict["enable_runtime_dependency_cache"] = os.environ[
            "TAKO_VM_ENABLE_RUNTIME_DEPENDENCY_CACHE"
        ].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_SESSIONS_ENABLED" in os.environ:
        config_dict["sessions_enabled"] = os.environ["TAKO_VM_SESSIONS_ENABLED"].lower() in (
            "true",
            "1",
            "yes",
        )
    if "TAKO_VM_SESSION_IDLE_TIMEOUT_SECONDS" in os.environ:
        config_dict["session_idle_timeout_seconds"] = parse_env_int(
            "TAKO_VM_SESSION_IDLE_TIMEOUT_SECONDS"
        )
    if "TAKO_VM_SESSION_MAX_TTL_SECONDS" in os.environ:
        config_dict["session_max_ttl_seconds"] = parse_env_int("TAKO_VM_SESSION_MAX_TTL_SECONDS")
    if "TAKO_VM_SESSION_MAX_CONCURRENT" in os.environ:
        config_dict["session_max_concurrent"] = parse_env_int("TAKO_VM_SESSION_MAX_CONCURRENT")
    try:
        config = TakoVMConfig(**config_dict)
        config.resolve_paths()
    except ValidationError as e:
        raise ConfigurationError(f"Invalid configuration: {_sanitize_validation_error(e)}") from e
    except Exception as e:
        logger.error("Configuration load failed with %s", type(e).__name__, exc_info=True)
        raise ConfigurationError(
            f"Invalid configuration: {type(e).__name__} (see logs for details)"
        ) from e

    for warning in config.security_warnings():
        logger.warning(warning)

    return config


_config: Optional[TakoVMConfig] = None
_config_path: Optional[Path] = None


def get_config() -> TakoVMConfig:
    global _config
    if _config is None:
        _config = load_config(_config_path)
    return _config


def set_config(config: TakoVMConfig) -> None:
    global _config
    _config = config


def set_config_path(path: Optional[Path]) -> None:
    global _config_path, _config
    _config_path = path
    _config = None


def get_config_path() -> Optional[Path]:
    if _config_path:
        return _config_path
    return find_config_file()


def reset_config() -> None:
    global _config, _config_path
    _config = None
    _config_path = None


def validate_config_file(path: Path) -> List[str]:
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
    except ValidationError as e:
        errors.append(_sanitize_validation_error(e))
    except Exception as e:
        errors.append(str(e))
    return errors
