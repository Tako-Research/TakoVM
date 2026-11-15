from dataclasses import replace

from tako_vm.job_types import JobType
from tako_vm.version import VersionManager


def _version_manager() -> VersionManager:
    return VersionManager(storage=None)  # type: ignore[arg-type]


def test_compute_digest_changes_when_session_or_gpu_settings_change():
    manager = _version_manager()
    base = JobType(name="model-job")

    base_digest = manager.compute_digest(base)
    session_digest = manager.compute_digest(replace(base, session_enabled=True))
    gpu_digest = manager.compute_digest(
        replace(base, gpu_enabled=True, gpu_vendor="nvidia", gpu_count=1)
    )

    assert base_digest != session_digest
    assert base_digest != gpu_digest
    assert session_digest != gpu_digest


def test_compute_digest_is_stable_for_reordered_gpu_device_ids():
    manager = _version_manager()

    a = JobType(
        name="gpu-job",
        gpu_enabled=True,
        gpu_vendor="nvidia",
        gpu_device_ids=["GPU-2", "GPU-1"],
    )
    b = JobType(
        name="gpu-job",
        gpu_enabled=True,
        gpu_vendor="nvidia",
        gpu_device_ids=["GPU-1", "GPU-2"],
    )

    assert manager.compute_digest(a) == manager.compute_digest(b)
