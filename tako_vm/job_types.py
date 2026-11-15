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
    from tako_vm.config import get_default_data_dir

    data_dir = os.environ.get("TAKO_VM_DATA_DIR")
    base = Path(data_dir) if data_dir else get_default_data_dir()
    return base / "job_types.json"


def _legacy_registry_path() -> Optional[Path]:
    try:
        return Path(str(_resource_files("tako_vm").joinpath("job_types.json")))
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        return None


@dataclass
class JobType:
    name: str

    requirements: list[str] = field(default_factory=list)

    python_version: str = "3.11"

    base_image: Optional[str] = None

    shared_code: list[str] = field(default_factory=list)

    environment: dict[str, str] = field(default_factory=dict)

    memory_limit: str = "512m"

    cpu_limit: float = 1.0

    timeout: int = 30

    startup_timeout: int = 120

    network_enabled: bool = False

    session_enabled: bool = False

    gpu_enabled: bool = False

    gpu_vendor: Optional[str] = None

    gpu_count: Optional[int] = None

    gpu_device_ids: list[str] = field(default_factory=list)

    @property
    def image_name(self) -> str:
        return f"tako-vm-{self.name}:latest"

    def to_dict(self) -> dict:
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
        return cls.from_config(candidate.to_config())

    @classmethod
    def from_config(cls, config: JobTypeConfig) -> JobType:
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
    def __init__(self, config_path: Optional[Path] = None):
        self._legacy_path: Optional[Path] = None
        if config_path is None:
            config_path = _default_registry_path()
            self._legacy_path = _legacy_registry_path()
        self.config_path = config_path
        self._job_types: dict[str, JobType] = {}
        self._load()

    def _load(self):
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
        data = {"job_types": [jt.to_dict() for jt in self._job_types.values()]}
        directory = self.config_path.parent
        directory.mkdir(parents=True, exist_ok=True)

        lock_path = self.config_path.with_name(self.config_path.name + ".lock")
        with open(lock_path, "a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
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
        self._job_types[job_type.name] = job_type
        if persist:
            self._save()

    def register_many(self, job_types: list[JobType], persist: bool = True) -> None:
        if not job_types:
            return

        for job_type in job_types:
            self._job_types[job_type.name] = job_type

        if persist:
            self._save()

    def get(self, name: str) -> Optional[JobType]:
        return self._job_types.get(name)

    def list(self) -> list[JobType]:
        return list(self._job_types.values())

    def remove(self, name: str) -> bool:
        if name in self._job_types:
            del self._job_types[name]
            self._save()
            return True
        return False


DEFAULT_JOB_TYPES = [
    JobType(
        name="default",
        requirements=[],
        memory_limit="512m",
        cpu_limit=1.0,
        timeout=30,
        startup_timeout=60,
    ),
    JobType(
        name="data-processing",
        requirements=["pandas", "numpy"],
        memory_limit="1g",
        cpu_limit=2.0,
        timeout=60,
        startup_timeout=180,
    ),
    JobType(
        name="ml-inference",
        requirements=["numpy", "scikit-learn"],
        memory_limit="2g",
        cpu_limit=2.0,
        timeout=120,
        startup_timeout=180,
    ),
]


def init_default_job_types(registry: JobTypeRegistry) -> None:
    for jt in DEFAULT_JOB_TYPES:
        if registry.get(jt.name) is None:
            registry.register(jt)


def merge_config_job_types(
    registry: JobTypeRegistry, config_job_types: list["JobTypeConfig"]
) -> int:
    if not config_job_types:
        return 0

    runtime_job_types = [
        JobType.from_config(job_type_config) for job_type_config in config_job_types
    ]
    registry.register_many(runtime_job_types, persist=False)
    return len(runtime_job_types)
