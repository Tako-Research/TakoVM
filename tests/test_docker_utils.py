"""
Tests for Docker utility functions.

Tests container naming, cleanup, and platform detection.
"""

import subprocess
from unittest.mock import MagicMock, patch

from tako_vm.execution.docker import (
    generate_container_name,
    is_native_linux,
    kill_container,
)


class TestIsNativeLinux:
    """Tests for is_native_linux() function."""

    def test_is_native_linux_on_linux(self):
        """is_native_linux() returns True on Linux."""
        with patch("platform.system", return_value="Linux"):
            assert is_native_linux() is True

    def test_is_native_linux_on_macos(self):
        """is_native_linux() returns False on macOS."""
        with patch("platform.system", return_value="Darwin"):
            assert is_native_linux() is False

    def test_is_native_linux_on_windows(self):
        """is_native_linux() returns False on Windows."""
        with patch("platform.system", return_value="Windows"):
            assert is_native_linux() is False


class TestGenerateContainerName:
    """Tests for generate_container_name() function."""

    def test_generate_container_name_with_job_id(self):
        """Container name includes job_id when provided."""
        name = generate_container_name("tako", job_id="abc123")
        assert name == "tako-abc123"

    def test_generate_container_name_without_job_id(self):
        """Container name uses UUID when job_id not provided."""
        name = generate_container_name("tako-sandbox")

        assert name.startswith("tako-sandbox-")
        # UUID hex part should be 12 characters
        suffix = name.replace("tako-sandbox-", "")
        assert len(suffix) == 12
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_generate_container_name_unique(self):
        """Each call generates unique name."""
        names = [generate_container_name("tako") for _ in range(10)]
        assert len(set(names)) == 10  # All unique

    def test_generate_container_name_custom_prefix(self):
        """Supports custom prefix."""
        name = generate_container_name("my-app", job_id="job1")
        assert name == "my-app-job1"


class TestKillContainer:
    """Tests for kill_container() function."""

    @patch("subprocess.run")
    def test_kill_container_calls_docker(self, mock_run):
        """kill_container calls docker kill command."""
        mock_run.return_value = MagicMock(returncode=0)

        assert kill_container("tako-test-123") is True

        mock_run.assert_called_once_with(
            ["docker", "kill", "tako-test-123"],
            capture_output=True,
            timeout=10,
            check=False,
        )

    @patch("subprocess.run")
    def test_kill_container_ignores_errors(self, mock_run):
        """kill_container silently ignores errors."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert kill_container("nonexistent-container") is False

    @patch("subprocess.run")
    def test_kill_container_ignores_docker_errors(self, mock_run):
        """kill_container ignores docker command failures."""
        mock_run.return_value = MagicMock(returncode=1)  # Container not found

        assert kill_container("already-stopped-container") is False

    @patch("subprocess.run")
    def test_kill_container_handles_exception(self, mock_run):
        """kill_container handles unexpected exceptions."""
        mock_run.side_effect = Exception("Unexpected error")

        assert kill_container("error-container") is False


class TestContainerNameValidation:
    """Tests for container name format validation."""

    def test_container_name_format_with_job_id(self):
        """Container names with job_id follow expected format."""
        name = generate_container_name("tako", job_id="test-job-123")

        # Should be valid Docker container name
        # Docker allows: [a-zA-Z0-9][a-zA-Z0-9_.-]*
        assert name[0].isalnum()
        assert all(c.isalnum() or c in "_.-" for c in name)

    def test_container_name_format_uuid(self):
        """Container names with UUID follow expected format."""
        name = generate_container_name("tako")

        # Should be valid Docker container name
        assert name[0].isalnum()
        assert all(c.isalnum() or c in "_.-" for c in name)

    def test_container_name_length(self):
        """Container names are reasonable length."""
        name = generate_container_name("tako-sandbox", job_id="a" * 64)

        # Docker has a max container name length, but we don't enforce it
        # Just verify it's not empty
        assert len(name) > 0
