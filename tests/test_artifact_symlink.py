"""
Artifact/result collection must reject symlinks.

Untrusted code in the sandbox can drop a symlink in /output pointing at a
host-readable file (the config with api_keys, /proc/self/environ, another run's
data). The host-side worker collects artifacts and reads result.json/.tako_phase
*outside* any sandbox, so following such a symlink would exfiltrate host files.
These are pure host-side file tests — no Docker required.
"""

from pathlib import Path

import pytest

from tako_vm.config import TakoVMConfig
from tako_vm.execution.worker import CodeExecutor, parse_phase_file


@pytest.fixture
def executor(tmp_path):
    config = TakoVMConfig(security_mode="permissive", data_dir=str(tmp_path / "data"))
    return CodeExecutor(config=config)


def _host_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "host_secret.txt"
    secret.write_text("API_KEY=supersecret")
    return secret


class TestArtifactSymlinkRejection:
    def test_symlink_artifact_is_skipped(self, executor, tmp_path):
        secret = _host_secret(tmp_path)
        out = tmp_path / "output"
        out.mkdir()
        (out / "real.txt").write_text("ok")
        (out / "evil.txt").symlink_to(secret)  # points at a host file

        artifacts = executor._collect_artifacts(out, "job-1")
        names = {a.name for a in artifacts}

        assert "real.txt" in names
        assert "evil.txt" not in names  # symlink rejected before copy
        # The secret content must never have been copied into permanent storage.
        copied = (tmp_path / "data" / "runs" / "job-1" / "artifacts").glob("*")
        assert all("supersecret" not in p.read_text() for p in copied if p.is_file())

    def test_real_files_still_collected(self, executor, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "a.txt").write_text("hello")
        artifacts = executor._collect_artifacts(out, "job-2")
        assert {a.name for a in artifacts} == {"a.txt"}


class TestPhaseFileSymlinkRejection:
    def test_symlinked_phase_file_returns_none(self, tmp_path):
        secret = _host_secret(tmp_path)
        out = tmp_path / "output"
        out.mkdir()
        (out / ".tako_phase").symlink_to(secret)
        assert parse_phase_file(out) is None

    def test_real_phase_file_is_parsed(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / ".tako_phase").write_text("phase=completed\ntotal_ms=10\n")
        assert parse_phase_file(out) is not None
