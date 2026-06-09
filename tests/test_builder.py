"""
Tests for ContainerBuilder.build_all image build orchestration.

Covers the opt-in ``skip_existing`` behavior: ``build_all`` skips job types
whose image is already present instead of rebuilding it, while the default
still builds every registered job type.
"""

from pathlib import Path

from tako_vm.execution.builder import ContainerBuilder
from tako_vm.job_types import JobType, JobTypeRegistry


def _empty_registry(tmp_path: Path, *names: str) -> JobTypeRegistry:
    # A non-existent config path yields an empty registry (the package
    # job_types.json is not loaded); persist=False keeps the test off disk.
    registry = JobTypeRegistry(config_path=tmp_path / "job_types.json")
    for name in names:
        registry.register(JobType(name=name), persist=False)
    return registry


class TestBuildAllSkipExisting:
    def test_skips_present_images_when_enabled(self, tmp_path, monkeypatch):
        """skip_existing=True does not rebuild images that already exist."""
        builder = ContainerBuilder()
        built: list[str] = []
        monkeypatch.setattr(builder, "image_exists", lambda job_type: True)
        monkeypatch.setattr(
            builder, "build", lambda job_type, **kwargs: built.append(job_type.name) or True
        )

        results = builder.build_all(_empty_registry(tmp_path, "alpha", "beta"), skip_existing=True)

        assert built == []
        assert results == {"alpha": True, "beta": True}

    def test_builds_only_missing_images_when_enabled(self, tmp_path, monkeypatch):
        """skip_existing=True still builds images that are absent."""
        builder = ContainerBuilder()
        built: list[str] = []
        monkeypatch.setattr(builder, "image_exists", lambda job_type: job_type.name == "present")
        monkeypatch.setattr(
            builder, "build", lambda job_type, **kwargs: built.append(job_type.name) or True
        )

        results = builder.build_all(
            _empty_registry(tmp_path, "present", "absent"), skip_existing=True
        )

        assert built == ["absent"]
        assert results == {"present": True, "absent": True}

    def test_builds_every_job_type_by_default(self, tmp_path, monkeypatch):
        """Default (skip_existing=False) builds every job type, ignoring existence."""
        builder = ContainerBuilder()
        built: list[str] = []
        monkeypatch.setattr(builder, "image_exists", lambda job_type: True)
        monkeypatch.setattr(
            builder, "build", lambda job_type, **kwargs: built.append(job_type.name) or True
        )

        results = builder.build_all(_empty_registry(tmp_path, "alpha", "beta"))

        assert built == ["alpha", "beta"]
        assert results == {"alpha": True, "beta": True}
