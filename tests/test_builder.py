from pathlib import Path

import pytest

from tako_vm.execution.builder import BuildError, ContainerBuilder
from tako_vm.execution.docker import DEFAULT_EXECUTOR_IMAGE
from tako_vm.job_types import JobType, JobTypeRegistry


def _empty_registry(tmp_path: Path, *names: str) -> JobTypeRegistry:
    registry = JobTypeRegistry(config_path=tmp_path / "job_types.json")
    for name in names:
        registry.register(JobType(name=name), persist=False)
    return registry


class TestBuildAllSkipExisting:
    def test_skips_present_images_when_enabled(self, tmp_path, monkeypatch):
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
    def test_default_base_is_executor_image(self):
        dockerfile = ContainerBuilder().generate_dockerfile(JobType(name="jt"))

        assert f"FROM {DEFAULT_EXECUTOR_IMAGE}" in dockerfile
        assert "FROM python:" not in dockerfile

    def test_custom_base_image_is_honored(self):
        dockerfile = ContainerBuilder().generate_dockerfile(
            JobType(name="jt", base_image="my-executor:latest")
        )

        assert "FROM my-executor:latest" in dockerfile

    def test_no_user_cmd_or_entrypoint_overrides(self):
        dockerfile = ContainerBuilder().generate_dockerfile(JobType(name="jt"))

        for line in dockerfile.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("USER "), line
            assert not stripped.startswith("CMD "), line
            assert not stripped.startswith("ENTRYPOINT "), line


class TestImagesStripSetuidBinaries:
    STRIP_STEP = "find / -xdev -perm /6000 -type f -exec chmod a-s {} +"

    def test_executor_dockerfile_strips_setuid_bits(self):
        text = (
            Path(__file__).resolve().parent.parent / "docker" / "Dockerfile.executor"
        ).read_text()
        assert f"RUN {self.STRIP_STEP}" in text

    def test_strip_runs_after_packages_are_installed(self):
        text = (
            Path(__file__).resolve().parent.parent / "docker" / "Dockerfile.executor"
        ).read_text()
        assert text.index("apt-get install") < text.index(self.STRIP_STEP)

    def test_generated_job_type_dockerfile_strips_setuid_bits(self):
        dockerfile = ContainerBuilder().generate_dockerfile(
            JobType(name="jt", requirements=["requests"])
        )
        assert f"RUN {self.STRIP_STEP}" in dockerfile
        assert dockerfile.index("uv pip install") < dockerfile.index(self.STRIP_STEP)


class TestEntrypointInstallsUnprivileged:
    def _entrypoint_text(self):
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh").read_text()

    def _install_cmd_block(self, text):
        start = text.index("UV_INSTALL_CMD=(")
        end = text.index(")", start)
        return text[start:end]

    def test_install_command_runs_under_gosu(self):
        block = self._install_cmd_block(self._entrypoint_text())
        assert "gosu sandbox" in block
        assert "uv pip install" in block
        assert block.index("gosu sandbox") < block.index("uv pip install")

    def test_install_targets_are_writable_by_sandbox_user(self):
        text = self._entrypoint_text()
        assert 'gosu sandbox mkdir -p "$TARGET_DIR"' in text
        assert 'gosu sandbox mkdir -p "$UV_CACHE_DIR"' in text

    def test_entrypoint_never_hard_fails_on_chown(self):
        for line_no, line in enumerate(self._entrypoint_text().splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("chown "):
                continue
            assert "|| true" in stripped or "2>/dev/null" in stripped, (
                f"entrypoint.sh:{line_no} runs an unguarded chown under `set -e`; "
                f"CAP_CHOWN is not held under --cap-drop=ALL, so this aborts the "
                f"container before user code runs: {stripped!r}"
            )

    def test_install_sets_home_for_sandbox_user(self):
        text = self._entrypoint_text()
        assert "HOME=$SANDBOX_HOME" in text
        assert 'env "${INSTALL_ENV[@]}"' in self._install_cmd_block(text)

    def test_requirements_are_baked_at_build_time(self):
        dockerfile = ContainerBuilder().generate_dockerfile(
            JobType(name="jt", requirements=["pandas", "numpy>=1.26"])
        )

        assert 'RUN uv pip install --system --no-cache "pandas" "numpy>=1.26"' in dockerfile

    def test_sandbox_user_creation_is_guarded(self):
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
