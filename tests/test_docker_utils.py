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
    def test_none_becomes_empty_string(self):
        assert decode_subprocess_stream(None) == ""

    def test_str_passes_through(self):
        assert decode_subprocess_stream("hello") == "hello"

    def test_bytes_are_utf8_decoded(self):
        assert decode_subprocess_stream(b"hello") == "hello"

    def test_invalid_utf8_is_replaced_not_raised(self):
        assert decode_subprocess_stream(b"\xff\xfe") == "��"


@pytest.fixture(autouse=True)
def _clean_image_caches():
    reset_image_caches()
    yield
    reset_image_caches()


class TestIsNativeLinux:
    def test_is_native_linux_on_linux(self):
        with patch("platform.system", return_value="Linux"):
            assert is_native_linux() is True

    def test_is_native_linux_on_macos(self):
        with patch("platform.system", return_value="Darwin"):
            assert is_native_linux() is False

    def test_is_native_linux_on_windows(self):
        with patch("platform.system", return_value="Windows"):
            assert is_native_linux() is False


class TestGenerateContainerName:
    def test_generate_container_name_with_job_id(self):
        name = generate_container_name("tako", job_id="abc123")
        assert name == "tako-abc123"

    def test_generate_container_name_without_job_id(self):
        name = generate_container_name("tako-sandbox")

        assert name.startswith("tako-sandbox-")
        suffix = name.replace("tako-sandbox-", "")
        assert len(suffix) == 12
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_generate_container_name_unique(self):
        names = [generate_container_name("tako") for _ in range(10)]
        assert len(set(names)) == 10

    def test_generate_container_name_custom_prefix(self):
        name = generate_container_name("my-app", job_id="job1")
        assert name == "my-app-job1"

    def test_attempt_zero_keeps_plain_name(self):
        assert generate_container_name("tako", job_id="abc123", attempt=0) == "tako-abc123"

    def test_retry_attempts_get_unique_suffix(self):
        assert generate_container_name("tako", job_id="abc123", attempt=1) == "tako-abc123-r1"
        assert generate_container_name("tako", job_id="abc123", attempt=2) == "tako-abc123-r2"

    def test_attempt_names_are_distinct_per_attempt(self):
        names = {generate_container_name("tako", job_id="j", attempt=a) for a in range(4)}
        assert len(names) == 4


class TestKillContainer:
    @patch("subprocess.run")
    def test_kill_container_calls_docker(self, mock_run):
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
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert kill_container("nonexistent-container") is False

    @patch("subprocess.run")
    def test_kill_container_ignores_docker_errors(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)

        assert kill_container("already-stopped-container") is False

    @patch("subprocess.run")
    def test_kill_container_handles_exception(self, mock_run):
        mock_run.side_effect = Exception("Unexpected error")

        assert kill_container("error-container") is False


class TestRemoveContainer:
    @patch("subprocess.run")
    def test_remove_container_calls_docker_rm_force(self, mock_run):
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
        mock_run.return_value = MagicMock(returncode=1)

        assert remove_container("already-gone") is False

    @patch("subprocess.run")
    def test_remove_container_ignores_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert remove_container("tako-test-123") is False

    @patch("subprocess.run")
    def test_remove_container_handles_exception(self, mock_run):
        mock_run.side_effect = Exception("Unexpected error")

        assert remove_container("error-container") is False


class TestInspectOomKilled:
    @patch("subprocess.run")
    def test_inspect_calls_docker_with_oomkilled_format(self, mock_run):
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
        mock_run.return_value = MagicMock(returncode=0, stdout="false\n")

        assert inspect_oom_killed("tako-test-123") is False

    @patch("subprocess.run")
    def test_returns_none_when_container_missing(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error: No such object: tako-test-123"
        )

        assert inspect_oom_killed("tako-test-123") is None

    @patch("subprocess.run")
    def test_returns_none_on_unparseable_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="<no value>\n")

        assert inspect_oom_killed("tako-test-123") is None

    @patch("subprocess.run")
    def test_returns_none_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert inspect_oom_killed("tako-test-123") is None

    @patch("subprocess.run")
    def test_returns_none_on_unexpected_exception(self, mock_run):
        mock_run.side_effect = Exception("boom")

        assert inspect_oom_killed("tako-test-123") is None


class TestImageExists:
    @patch("subprocess.run")
    def test_calls_docker_image_inspect_with_timeout(self, mock_run):
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
        mock_run.return_value = MagicMock(returncode=0)

        assert image_exists("tako-vm-foo:latest") is True
        assert image_exists("tako-vm-foo:latest") is True

        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_negative_result_is_not_cached(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)

        assert image_exists("tako-vm-missing:latest") is False
        assert image_exists("tako-vm-missing:latest") is False

        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_cache_is_per_image(self, mock_run):
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
        mock_run.return_value = self._inspect_result("null\n")

        assert image_has_executor_entrypoint("python:3.11-slim") is False

    @patch("subprocess.run")
    def test_failed_inspect_returns_none(self, mock_run):
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
        mock_run.return_value = self._inspect_result("null\n")

        assert image_has_executor_entrypoint("python:3.11-slim") is False
        assert image_has_executor_entrypoint("python:3.11-slim") is False

        assert mock_run.call_count == 2


class TestContainerNameValidation:
    def test_container_name_format_with_job_id(self):
        name = generate_container_name("tako", job_id="test-job-123")

        assert name[0].isalnum()
        assert all(c.isalnum() or c in "_.-" for c in name)

    def test_container_name_format_uuid(self):
        name = generate_container_name("tako")

        assert name[0].isalnum()
        assert all(c.isalnum() or c in "_.-" for c in name)

    def test_container_name_length(self):
        name = generate_container_name("tako-sandbox", job_id="a" * 64)

        assert len(name) > 0


class TestBaseIsolationArgs:
    def test_always_on_flags_present(self):
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
        args = base_isolation_args("c1", runtime="runc")
        assert f"--label={CONTAINER_LABEL}" in args

    def test_execution_id_label_applied_when_given(self):
        args = base_isolation_args("c1", runtime="runc", execution_id="job-42")
        assert f"--label={EXECUTION_ID_LABEL}=job-42" in args

    def test_execution_id_label_omitted_when_absent(self):
        args = base_isolation_args("c1", runtime="runc")
        assert not any(a.startswith(f"--label={EXECUTION_ID_LABEL}") for a in args)

    def test_caps_dropped_by_default(self):
        args = base_isolation_args("c1", runtime="runc")
        assert "--cap-drop=ALL" in args
        assert "--cap-add=SETUID" in args
        assert "--cap-add=SETGID" in args

    def test_caps_can_be_disabled(self):
        args = base_isolation_args("c1", runtime="runc", enable_cap_restrictions=False)
        assert not any(a.startswith("--cap-") for a in args)

    def test_runsc_adds_runtime_flag(self):
        assert "--runtime=runsc" in base_isolation_args("c1", runtime="runsc")

    def test_runc_is_implicit(self):
        assert not any(a.startswith("--runtime") for a in base_isolation_args("c1", runtime="runc"))

    def test_auto_remove_default_includes_rm(self):
        assert "--rm" in base_isolation_args("c1", runtime="runc")

    def test_auto_remove_false_omits_rm(self):
        args = base_isolation_args("c1", runtime="runc", auto_remove=False)
        assert "--rm" not in args
        assert args[:6] == [
            "docker",
            "run",
            "--name=c1",
            f"--label={CONTAINER_LABEL}",
            "--init",
            "--read-only",
        ]
