"""
Tests for Docker utility functions.

Tests container naming, cleanup, and platform detection.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from tako_vm.execution.docker import (
    CONTAINER_LABEL,
    EXECUTION_ID_LABEL,
    base_isolation_args,
    decode_subprocess_stream,
    generate_container_name,
    image_exists,
    image_has_executor_entrypoint,
    inspect_oom_killed,
    is_native_linux,
    kill_container,
    remove_container,
    reset_image_caches,
)


class TestDecodeSubprocessStream:
    """Single source for coercing partial subprocess output (str | bytes |
    None), shared by the worker and the library Sandbox so the timeout-output
    decoding can't drift between them (previously duplicated as _coerce_output
    and _decode_stream)."""

    def test_none_becomes_empty_string(self):
        assert decode_subprocess_stream(None) == ""

    def test_str_passes_through(self):
        assert decode_subprocess_stream("hello") == "hello"

    def test_bytes_are_utf8_decoded(self):
        assert decode_subprocess_stream(b"hello") == "hello"

    def test_invalid_utf8_is_replaced_not_raised(self):
        # TimeoutExpired byte buffers can be cut mid-codepoint; decoding must
        # never raise (errors="replace").
        assert decode_subprocess_stream(b"\xff\xfe") == "��"


@pytest.fixture(autouse=True)
def _clean_image_caches():
    """image_exists/image_has_executor_entrypoint cache positives in-process;
    isolate every test from cache state left by another."""
    reset_image_caches()
    yield
    reset_image_caches()


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

    def test_attempt_zero_keeps_plain_name(self):
        """Attempt 0 keeps the deterministic name so cancel/watchdog kill paths match."""
        assert generate_container_name("tako", job_id="abc123", attempt=0) == "tako-abc123"

    def test_retry_attempts_get_unique_suffix(self):
        """Attempts > 0 get a -r{attempt} suffix so retries never collide with attempt 0."""
        assert generate_container_name("tako", job_id="abc123", attempt=1) == "tako-abc123-r1"
        assert generate_container_name("tako", job_id="abc123", attempt=2) == "tako-abc123-r2"

    def test_attempt_names_are_distinct_per_attempt(self):
        """Every attempt index yields a distinct container name."""
        names = {generate_container_name("tako", job_id="j", attempt=a) for a in range(4)}
        assert len(names) == 4


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


class TestRemoveContainer:
    """Tests for remove_container() function."""

    @patch("subprocess.run")
    def test_remove_container_calls_docker_rm_force(self, mock_run):
        """remove_container force-removes by name via docker rm -f."""
        mock_run.return_value = MagicMock(returncode=0)

        assert remove_container("tako-test-123") is True

        mock_run.assert_called_once_with(
            ["docker", "rm", "-f", "tako-test-123"],
            capture_output=True,
            timeout=10,
            check=False,
        )

    @patch("subprocess.run")
    def test_remove_container_returns_false_when_missing(self, mock_run):
        """A nonexistent container (nonzero exit) is reported as not removed."""
        mock_run.return_value = MagicMock(returncode=1)

        assert remove_container("already-gone") is False

    @patch("subprocess.run")
    def test_remove_container_ignores_timeout(self, mock_run):
        """remove_container silently ignores subprocess timeouts."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert remove_container("tako-test-123") is False

    @patch("subprocess.run")
    def test_remove_container_handles_exception(self, mock_run):
        """remove_container handles unexpected exceptions."""
        mock_run.side_effect = Exception("Unexpected error")

        assert remove_container("error-container") is False


class TestInspectOomKilled:
    """Tests for inspect_oom_killed(): the authoritative OOM signal."""

    @patch("subprocess.run")
    def test_inspect_calls_docker_with_oomkilled_format(self, mock_run):
        """inspect_oom_killed asks docker inspect for State.OOMKilled with a short timeout."""
        mock_run.return_value = MagicMock(returncode=0, stdout="true\n")

        assert inspect_oom_killed("tako-test-123") is True

        mock_run.assert_called_once_with(
            ["docker", "inspect", "--format", "{{.State.OOMKilled}}", "tako-test-123"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    @patch("subprocess.run")
    def test_returns_false_when_not_oom_killed(self, mock_run):
        """A SIGKILL that was not the OOM killer reports OOMKilled=false."""
        mock_run.return_value = MagicMock(returncode=0, stdout="false\n")

        assert inspect_oom_killed("tako-test-123") is False

    @patch("subprocess.run")
    def test_returns_none_when_container_missing(self, mock_run):
        """A failed inspect (container already gone) returns None, not False."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error: No such object: tako-test-123"
        )

        assert inspect_oom_killed("tako-test-123") is None

    @patch("subprocess.run")
    def test_returns_none_on_unparseable_output(self, mock_run):
        """Unexpected inspect output is treated as unknown (None)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="<no value>\n")

        assert inspect_oom_killed("tako-test-123") is None

    @patch("subprocess.run")
    def test_returns_none_on_timeout(self, mock_run):
        """A hung docker inspect returns None instead of raising."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert inspect_oom_killed("tako-test-123") is None

    @patch("subprocess.run")
    def test_returns_none_on_unexpected_exception(self, mock_run):
        """Any unexpected failure is swallowed and reported as unknown."""
        mock_run.side_effect = Exception("boom")

        assert inspect_oom_killed("tako-test-123") is None


class TestImageExists:
    """Tests for image_exists(): cached local-image presence check."""

    @patch("subprocess.run")
    def test_calls_docker_image_inspect_with_timeout(self, mock_run):
        """image_exists asks docker image inspect with a short timeout."""
        mock_run.return_value = MagicMock(returncode=0)

        assert image_exists("tako-vm-foo:latest") is True

        mock_run.assert_called_once_with(
            ["docker", "image", "inspect", "tako-vm-foo:latest"],
            capture_output=True,
            timeout=10,
            check=False,
        )

    @patch("subprocess.run")
    def test_missing_image_returns_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)

        assert image_exists("tako-vm-missing:latest") is False

    @patch("subprocess.run")
    def test_positive_result_is_cached(self, mock_run):
        """A second lookup for a present image must not hit the daemon again."""
        mock_run.return_value = MagicMock(returncode=0)

        assert image_exists("tako-vm-foo:latest") is True
        assert image_exists("tako-vm-foo:latest") is True

        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_negative_result_is_not_cached(self, mock_run):
        """A missing image may be built/pulled at any moment: re-check it."""
        mock_run.return_value = MagicMock(returncode=1)

        assert image_exists("tako-vm-missing:latest") is False
        assert image_exists("tako-vm-missing:latest") is False

        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_cache_is_per_image(self, mock_run):
        """Caching one image's presence must not answer for another image."""
        mock_run.return_value = MagicMock(returncode=0)

        assert image_exists("tako-vm-a:latest") is True
        assert image_exists("tako-vm-b:latest") is True

        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_timeout_returns_false(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert image_exists("tako-vm-foo:latest") is False

    @patch("subprocess.run")
    def test_exception_returns_false(self, mock_run):
        mock_run.side_effect = Exception("boom")

        assert image_exists("tako-vm-foo:latest") is False

    @patch("subprocess.run")
    def test_reset_image_caches_clears_positive_entries(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        assert image_exists("tako-vm-foo:latest") is True
        reset_image_caches()
        assert image_exists("tako-vm-foo:latest") is True

        assert mock_run.call_count == 2


class TestImageHasExecutorEntrypoint:
    """Tests for image_has_executor_entrypoint(): the executor contract check."""

    @staticmethod
    def _inspect_result(stdout):
        return MagicMock(returncode=0, stdout=stdout, stderr="")

    @patch("subprocess.run")
    def test_calls_docker_inspect_with_entrypoint_format(self, mock_run):
        mock_run.return_value = self._inspect_result('["/entrypoint.sh"]\n')

        assert image_has_executor_entrypoint("code-executor:latest") is True

        mock_run.assert_called_once_with(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .Config.Entrypoint}}",
                "code-executor:latest",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    @patch("subprocess.run")
    def test_other_entrypoint_returns_false(self, mock_run):
        mock_run.return_value = self._inspect_result('["python","-u","/code/main.py"]\n')

        assert image_has_executor_entrypoint("custom:latest") is False

    @patch("subprocess.run")
    def test_no_entrypoint_returns_false(self, mock_run):
        """A raw image like python:3.11-slim has a null entrypoint."""
        mock_run.return_value = self._inspect_result("null\n")

        assert image_has_executor_entrypoint("python:3.11-slim") is False

    @patch("subprocess.run")
    def test_failed_inspect_returns_none(self, mock_run):
        """Image not present / daemon unreachable is unknown, not False."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No such image")

        assert image_has_executor_entrypoint("missing:latest") is None

    @patch("subprocess.run")
    def test_unparseable_output_returns_none(self, mock_run):
        mock_run.return_value = self._inspect_result("not json")

        assert image_has_executor_entrypoint("weird:latest") is None

    @patch("subprocess.run")
    def test_exception_returns_none(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert image_has_executor_entrypoint("code-executor:latest") is None

    @patch("subprocess.run")
    def test_positive_result_is_cached(self, mock_run):
        mock_run.return_value = self._inspect_result('["/entrypoint.sh"]\n')

        assert image_has_executor_entrypoint("code-executor:latest") is True
        assert image_has_executor_entrypoint("code-executor:latest") is True

        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_negative_result_is_not_cached(self, mock_run):
        """An image can be rebuilt with the contract at any moment: re-check."""
        mock_run.return_value = self._inspect_result("null\n")

        assert image_has_executor_entrypoint("python:3.11-slim") is False
        assert image_has_executor_entrypoint("python:3.11-slim") is False

        assert mock_run.call_count == 2


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


class TestBaseIsolationArgs:
    """Tests for the shared isolation-base argument builder."""

    def test_always_on_flags_present(self):
        """The fixed isolation flags are always emitted, in order."""
        args = base_isolation_args("c1", runtime="runc")
        assert args[:7] == [
            "docker",
            "run",
            "--rm",
            "--name=c1",
            f"--label={CONTAINER_LABEL}",
            "--init",
            "--read-only",
        ]

    def test_executor_label_always_applied(self):
        """Every container gets the executor label so cleanup can find orphans."""
        args = base_isolation_args("c1", runtime="runc")
        assert f"--label={CONTAINER_LABEL}" in args

    def test_execution_id_label_applied_when_given(self):
        """The execution-id label maps a container back to its execution record."""
        args = base_isolation_args("c1", runtime="runc", execution_id="job-42")
        assert f"--label={EXECUTION_ID_LABEL}=job-42" in args

    def test_execution_id_label_omitted_when_absent(self):
        """No execution-id label when no execution id is provided."""
        args = base_isolation_args("c1", runtime="runc")
        assert not any(a.startswith(f"--label={EXECUTION_ID_LABEL}") for a in args)

    def test_caps_dropped_by_default(self):
        """Capabilities are dropped and only SETUID/SETGID re-added by default."""
        args = base_isolation_args("c1", runtime="runc")
        assert "--cap-drop=ALL" in args
        assert "--cap-add=SETUID" in args
        assert "--cap-add=SETGID" in args

    def test_caps_can_be_disabled(self):
        """Disabling cap restrictions omits the cap-drop/add flags."""
        args = base_isolation_args("c1", runtime="runc", enable_cap_restrictions=False)
        assert not any(a.startswith("--cap-") for a in args)

    def test_runsc_adds_runtime_flag(self):
        """gVisor is passed explicitly via --runtime=runsc."""
        assert "--runtime=runsc" in base_isolation_args("c1", runtime="runsc")

    def test_runc_is_implicit(self):
        """runc is docker's default and is not passed explicitly."""
        assert not any(a.startswith("--runtime") for a in base_isolation_args("c1", runtime="runc"))

    def test_auto_remove_default_includes_rm(self):
        """By default the container is auto-removed on exit (--rm)."""
        assert "--rm" in base_isolation_args("c1", runtime="runc")

    def test_auto_remove_false_omits_rm(self):
        """auto_remove=False omits --rm so the exited container can be inspected."""
        args = base_isolation_args("c1", runtime="runc", auto_remove=False)
        assert "--rm" not in args
        # The rest of the isolation posture is unchanged
        assert args[:6] == [
            "docker",
            "run",
            "--name=c1",
            f"--label={CONTAINER_LABEL}",
            "--init",
            "--read-only",
        ]
