"""
Job Type Registry - Define and manage pre-built container configurations.

Job types allow you to pre-configure containers with specific dependencies,
making execution faster and more predictable.

Example:
    from job_types import JobTypeRegistry, JobType

    registry = JobTypeRegistry()
    registry.register(JobType(
        name="data-processing",
        requirements=["pandas", "numpy"],
    ))
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import TYPE_CHECKING, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from tako_vm.config import JobTypeConfig

logger = logging.getLogger(__name__)


def _default_registry_path() -> Path:
    """
    Default location for the writable job type registry file.

    Lives under the Tako VM data directory (TAKO_VM_DATA_DIR or
    ~/.tako_vm), never inside the installed package: site-packages may be
    read-only and is shared by every consumer of the same environment.
    """
    from tako_vm.config import get_default_data_dir

    data_dir = os.environ.get("TAKO_VM_DATA_DIR")
    base = Path(data_dir) if data_dir else get_default_data_dir()
    return base / "job_types.json"


def _legacy_registry_path() -> Optional[Path]:
    """Legacy registry location inside the installed package (read-only)."""
    try:
        return Path(str(_resource_files("tako_vm").joinpath("job_types.json")))
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        return None


@dataclass
class JobType:
    """Configuration for a job type container."""

    name: str
    """Unique identifier for this job type."""

    requirements: list[str] = field(default_factory=list)
    """Python packages to install (via uv pip install at runtime)."""

    python_version: str = "3.11"
    """Python version to use."""

    base_image: Optional[str] = None
    """Custom base image. If None, uses python:{version}-slim."""

    shared_code: list[str] = field(default_factory=list)
    """Paths to Python files/modules to include in container."""

    environment: dict[str, str] = field(default_factory=dict)
    """Environment variables to set in container."""

    memory_limit: str = "512m"
    """Memory limit for container."""

    cpu_limit: float = 1.0
    """CPU limit for container."""

    timeout: int = 30
    """Default timeout for code execution in seconds."""

    startup_timeout: int = 120
    """Default timeout for startup phase (container init + deps) in seconds."""

    network_enabled: bool = False
    """Allow network access (default: no network for security)."""

    session_enabled: bool = False
    """Allow this job type to be used for long-running sessions."""

    gpu_enabled: bool = False
    """Enable GPU access for this job type."""

    gpu_vendor: Optional[str] = None
    """GPU vendor ('nvidia' or 'amd')."""

    gpu_count: Optional[int] = None
    """Number of GPUs (NVIDIA only)."""

    gpu_device_ids: list[str] = field(default_factory=list)
    """Specific GPU device IDs/UUIDs to expose."""

    @property
    def image_name(self) -> str:
        """Docker image name for this job type."""
        return f"tako-vm-{self.name}:latest"

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "requirements": self.requirements,
            "python_version": self.python_version,
            "base_image": self.base_image,
            "shared_code": self.shared_code,
            "environment": self.environment,
            "memory_limit": self.memory_limit,
            "cpu_limit": self.cpu_limit,
            "timeout": self.timeout,
            "startup_timeout": self.startup_timeout,
            "network_enabled": self.network_enabled,
            "session_enabled": self.session_enabled,
            "gpu": {
                "enabled": self.gpu_enabled,
                "vendor": self.gpu_vendor,
                "count": self.gpu_count,
                "device_ids": self.gpu_device_ids,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> JobType:
        """
        Create from dictionary, validating against JobTypeConfig bounds.

        Raw registry JSON is untrusted (hand-edited or tampered files feed
        directly into container launches), so entries are validated with the
        same pydantic constraints as the YAML config path. Raises
        pydantic.ValidationError on out-of-bounds or malformed values.
        """
        gpu = data.get("gpu") or {}

        candidate = cls(
            name=data["name"],
            requirements=list(data.get("requirements", [])),
            python_version=data.get("python_version", "3.11"),
            base_image=data.get("base_image"),
            shared_code=list(data.get("shared_code", [])),
            environment=dict(data.get("environment", {})),
            memory_limit=data.get("memory_limit", "512m"),
            cpu_limit=float(data.get("cpu_limit", 1.0)),
            timeout=int(data.get("timeout", 30)),
            startup_timeout=int(data.get("startup_timeout", 120)),
            network_enabled=bool(data.get("network_enabled", False)),
            session_enabled=bool(data.get("session_enabled", False)),
            gpu_enabled=bool(data.get("gpu_enabled", gpu.get("enabled", False))),
            gpu_vendor=data.get("gpu_vendor", gpu.get("vendor")),
            gpu_count=data.get("gpu_count", gpu.get("count")),
            gpu_device_ids=list(data.get("gpu_device_ids", gpu.get("device_ids", []))),
        )
        # Round-trip through the pydantic model to enforce bounds (memory_limit
        # format, cpu_limit/timeout ranges, GPU cross-field rules) and pick up
        # normalized values.
        return cls.from_config(candidate.to_config())

    @classmethod
    def from_config(cls, config: JobTypeConfig) -> JobType:
        """
        Create JobType from JobTypeConfig (for config loading).

        Args:
            config: Pydantic JobTypeConfig from YAML/config file

        Returns:
            JobType dataclass instance
        """
        return cls(
            name=config.name,
            requirements=list(config.requirements),
            python_version=config.python_version,
            base_image=config.base_image,
            shared_code=list(config.shared_code),
            environment=dict(config.environment),
            memory_limit=config.memory_limit,
            cpu_limit=config.cpu_limit,
            timeout=config.timeout,
            startup_timeout=config.startup_timeout,
            network_enabled=config.network_enabled,
            session_enabled=config.session_enabled,
            gpu_enabled=config.gpu.enabled,
            gpu_vendor=config.gpu.vendor,
            gpu_count=config.gpu.count,
            gpu_device_ids=list(config.gpu.device_ids),
        )

    def to_config(self) -> JobTypeConfig:
        """
        Convert to JobTypeConfig (for serialization).

        Returns:
            Pydantic JobTypeConfig instance
        """
        from tako_vm.config import JobTypeConfig, JobTypeGPUConfig

        return JobTypeConfig(
            name=self.name,
            requirements=list(self.requirements),
            python_version=self.python_version,
            base_image=self.base_image,
            shared_code=list(self.shared_code),
            environment=dict(self.environment),
            memory_limit=self.memory_limit,
            cpu_limit=self.cpu_limit,
            timeout=self.timeout,
            startup_timeout=self.startup_timeout,
            network_enabled=self.network_enabled,
            session_enabled=self.session_enabled,
            gpu=JobTypeGPUConfig(
                enabled=self.gpu_enabled,
                vendor=self.gpu_vendor,
                count=self.gpu_count,
                device_ids=list(self.gpu_device_ids),
            ),
        )


class JobTypeRegistry:
    """
    Registry for job type configurations.

    Stores job types in a JSON file for persistence.
    """

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialize the registry.

        Args:
            config_path: Path to config file. Defaults to job_types.json in the
                Tako VM data directory (TAKO_VM_DATA_DIR or ~/.tako_vm).
        """
        self._legacy_path: Optional[Path] = None
        if config_path is None:
            config_path = _default_registry_path()
            # Older releases stored the registry inside site-packages. Read
            # from there once if the data-dir file does not exist yet, but
            # never write back to the package directory.
            self._legacy_path = _legacy_registry_path()
        self.config_path = config_path
        self._job_types: dict[str, JobType] = {}
        self._load()

    def _load(self):
        """Load job types from config file."""
        path = self.config_path
        if not path.exists() and self._legacy_path is not None and self._legacy_path.exists():
            path = self._legacy_path

        if not path.exists():
            return

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        for item in data.get("job_types", []):
            try:
                jt = JobType.from_dict(item)
            except Exception as exc:
                name = item.get("name", "<unknown>") if isinstance(item, dict) else "<unknown>"
                logger.error(
                    "Skipping invalid job type %r in %s: %s",
                    name,
                    path,
                    exc,
                )
                continue
            self._job_types[jt.name] = jt

    def _save(self):
        """Save job types to config file atomically."""
        data = {"job_types": [jt.to_dict() for jt in self._job_types.values()]}
        directory = self.config_path.parent
        directory.mkdir(parents=True, exist_ok=True)

        lock_path = self.config_path.with_name(self.config_path.name + ".lock")
        with open(lock_path, "a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                # Write to a temp file in the same directory, then atomically
                # replace, so readers never observe a torn/partial file.
                fd, tmp_name = tempfile.mkstemp(
                    dir=directory, prefix=self.config_path.name + ".", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp_name, self.config_path)
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def register(self, job_type: JobType, persist: bool = True) -> None:
        """
        Register a new job type.

        Args:
            job_type: Job type configuration
            persist: Whether to save registry changes to disk
        """
        self._job_types[job_type.name] = job_type
        if persist:
            self._save()

    def register_many(self, job_types: list[JobType], persist: bool = True) -> None:
        """Register multiple job types at once."""
        if not job_types:
            return

        for job_type in job_types:
            self._job_types[job_type.name] = job_type

        if persist:
            self._save()

    def get(self, name: str) -> Optional[JobType]:
        """
        Get a job type by name.

        Args:
            name: Job type name

        Returns:
            JobType or None if not found
        """
        return self._job_types.get(name)

    def list(self) -> list[JobType]:
        """List all registered job types."""
        return list(self._job_types.values())

    def remove(self, name: str) -> bool:
        """
        Remove a job type.

        Args:
            name: Job type name

        Returns:
            True if removed, False if not found
        """
        if name in self._job_types:
            del self._job_types[name]
            self._save()
            return True
        return False


# Default job types
DEFAULT_JOB_TYPES = [
    JobType(
        name="default",
        requirements=[],
        memory_limit="512m",
        cpu_limit=1.0,
        timeout=30,
        startup_timeout=60,  # No deps, minimal startup
    ),
    JobType(
        name="data-processing",
        requirements=["pandas", "numpy"],
        memory_limit="1g",
        cpu_limit=2.0,
        timeout=60,
        startup_timeout=180,  # pandas/numpy take time to install
    ),
    JobType(
        name="ml-inference",
        requirements=["numpy", "scikit-learn"],
        memory_limit="2g",
        cpu_limit=2.0,
        timeout=120,
        startup_timeout=180,  # scikit-learn takes time to install
    ),
]


def init_default_job_types(registry: JobTypeRegistry) -> None:
    """Initialize registry with default job types."""
    for jt in DEFAULT_JOB_TYPES:
        if registry.get(jt.name) is None:
            registry.register(jt)


def merge_config_job_types(
    registry: JobTypeRegistry, config_job_types: list["JobTypeConfig"]
) -> int:
    """
    Merge config-defined job types into a runtime registry.

    Job types from config override any existing registry entry with the same name.
    Changes are applied in memory only (no persistence to job_types.json).

    Args:
        registry: Runtime job type registry
        config_job_types: Job types loaded from TakoVM config

    Returns:
        Number of config job types merged
    """
    if not config_job_types:
        return 0

    runtime_job_types = [
        JobType.from_config(job_type_config) for job_type_config in config_job_types
    ]
    registry.register_many(runtime_job_types, persist=False)
    return len(runtime_job_types)
