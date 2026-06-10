"""
Tests for ContainerBuilder.

Covers the opt-in ``skip_existing`` behavior of ``build_all`` (skip job types
whose image is already present) and the generated Dockerfile's executor
entrypoint contract: built images must derive from the executor base image
(inheriting ``ENTRYPOINT /entrypoint.sh``) and must not override USER/CMD —
otherwise the worker refuses to run them (see CodeExecutor._resolve_image).
"""

from pathlib import Path

import pytest

from tako_vm.execution.builder import BuildError, ContainerBuilder
from tako_vm.execution.docker import DEFAULT_EXECUTOR_IMAGE
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


class TestGenerateDockerfileEntrypointContract:
    """Built images must keep the executor entrypoint contract."""

    def test_default_base_is_executor_image(self):
        """Without a base_image the build derives from the executor base, so
        ENTRYPOINT /entrypoint.sh (timeouts, phase file, gosu drop, running
        /code/main.py) is inherited."""
        dockerfile = ContainerBuilder().generate_dockerfile(JobType(name="jt"))

        assert f"FROM {DEFAULT_EXECUTOR_IMAGE}" in dockerfile
        assert "FROM python:" not in dockerfile

    def test_custom_base_image_is_honored(self):
        dockerfile = ContainerBuilder().generate_dockerfile(
            JobType(name="jt", base_image="my-executor:latest")
        )

        assert "FROM my-executor:latest" in dockerfile

    def test_no_user_cmd_or_entrypoint_overrides(self):
        """USER sandbox would prevent the entrypoint from installing extra
        requirements and gosu-dropping; a CMD would mask the contract."""
        dockerfile = ContainerBuilder().generate_dockerfile(JobType(name="jt"))

        for line in dockerfile.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("USER "), line
            assert not stripped.startswith("CMD "), line
            assert not stripped.startswith("ENTRYPOINT "), line

    def test_requirements_are_baked_at_build_time(self):
        dockerfile = ContainerBuilder().generate_dockerfile(
            JobType(name="jt", requirements=["pandas", "numpy>=1.26"])
        )

        assert 'RUN uv pip install --system --no-cache "pandas" "numpy>=1.26"' in dockerfile

    def test_sandbox_user_creation_is_guarded(self):
        """useradd must be a no-op on the executor base (user already exists)
        or the build would fail with 'user sandbox exists'."""
        dockerfile = ContainerBuilder().generate_dockerfile(JobType(name="jt"))

        assert "id -u sandbox >/dev/null 2>&1 || useradd -m -u 1000 sandbox" in dockerfile

    def test_invalid_python_version_raises(self):
        with pytest.raises(BuildError, match="Invalid python_version"):
            ContainerBuilder().generate_dockerfile(
                JobType(name="jt", python_version="3.11; rm -rf /")
            )

    def test_invalid_base_image_raises(self):
        with pytest.raises(BuildError, match="Invalid base_image"):
            ContainerBuilder().generate_dockerfile(
                JobType(name="jt", base_image="bad image\nFROM evil")
            )
