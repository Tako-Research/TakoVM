"""
Tests for Tako VM CLI (command-line interface).

Tests command parsing and execution using subprocess/mock approaches.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCLIVersion:
    """Tests for version command."""

    def test_version_command(self):
        """tako-vm version prints version."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "tako-vm" in result.stdout.lower()
        from tako_vm import __version__

        assert __version__ in result.stdout


class TestCLIHelp:
    """Tests for help output."""

    def test_help_without_command(self):
        """tako-vm without command shows help."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli"],
            capture_output=True,
            text=True,
        )
        # Should exit with error and show help
        assert result.returncode == 1
        assert "usage" in result.stderr.lower() or "usage" in result.stdout.lower()

    def test_help_flag(self):
        """tako-vm --help shows help."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "server" in result.stdout
        assert "status" in result.stdout
        assert "validate" in result.stdout
        assert "config" in result.stdout


class TestCLIValidate:
    """Tests for validate command."""

    def test_validate_valid_config(self):
        """validate command accepts valid config."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: 4
default_timeout: 30
security_mode: permissive
"""
            )
            f.flush()
            config_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, "-m", "tako_vm.cli", "validate", config_path],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert "valid" in result.stdout.lower()
        finally:
            Path(config_path).unlink()

    def test_validate_invalid_config(self):
        """validate command rejects invalid config."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: -1
"""
            )
            f.flush()
            config_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, "-m", "tako_vm.cli", "validate", config_path],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 1
            assert "failed" in result.stdout.lower() or "error" in result.stderr.lower()
        finally:
            Path(config_path).unlink()

    def test_validate_nonexistent_file(self):
        """validate command handles missing file."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "validate", "/nonexistent/config.yaml"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1


class TestCLIConfig:
    """Tests for config command."""

    def test_config_show(self):
        """config command shows configuration."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "config"],
            capture_output=True,
            text=True,
            env={**os.environ, "TAKO_VM_SECURITY_MODE": "permissive"},
        )
        assert result.returncode == 0
        assert "Tako VM Configuration" in result.stdout
        assert "max_workers" in result.stdout

    def test_config_json_output(self):
        """config --json outputs JSON."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "config", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "TAKO_VM_SECURITY_MODE": "permissive"},
        )
        assert result.returncode == 0
        # Should be valid JSON
        import json

        data = json.loads(result.stdout)
        assert "max_workers" in data
        assert "database_url" in data
        assert "***@" in data["database_url"]
        assert "postgres:postgres@" not in data["database_url"]


class TestCLIStatus:
    """Tests for status command."""

    def test_status_server_not_running(self):
        """status command handles server not running."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "status", "--url", "http://localhost:59999"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "cannot connect" in result.stdout.lower() or "error" in result.stdout.lower()


class TestCLIConfigPath:
    """Tests for --config global option."""

    def test_config_path_nonexistent(self):
        """--config with nonexistent file shows error."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "--config", "/nonexistent.yaml", "version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr.lower()

    def test_config_path_valid(self):
        """--config with valid file is accepted."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("max_workers: 4\n")
            f.flush()
            config_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, "-m", "tako_vm.cli", "--config", config_path, "version"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
        finally:
            Path(config_path).unlink()


class TestCLIMain:
    """Tests for main() function directly."""

    def test_main_import(self):
        """Can import main from cli module."""
        from tako_vm.cli import main

        assert callable(main)

    def test_main_check_status_function(self):
        """check_status function handles connection errors."""
        from tako_vm.cli import check_status

        # Create mock args
        args = MagicMock()
        args.url = "http://localhost:59999"

        with pytest.raises(SystemExit) as exc_info:
            check_status(args)
        assert exc_info.value.code == 1

    def test_main_show_config_function(self):
        """show_config function works."""
        from tako_vm.cli import show_config
        from tako_vm.config import reset_config

        reset_config()

        args = MagicMock()
        args.json = False
        args.show_defaults = False

        # Should not raise
        show_config(args)

        reset_config()

    def test_main_show_config_json(self, capsys):
        """show_config with --json outputs JSON."""
        from tako_vm.cli import show_config
        from tako_vm.config import reset_config

        reset_config()

        args = MagicMock()
        args.json = True
        args.show_defaults = False

        show_config(args)

        captured = capsys.readouterr()
        import json

        data = json.loads(captured.out)
        assert "max_workers" in data

        reset_config()


class TestCheckStatusFunction:
    """Tests for check_status() function directly."""

    def test_check_status_success(self, capsys):
        """check_status displays server info on success."""
        import requests

        from tako_vm.cli import check_status

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "healthy",
            "docker_available": True,
            "version": "1.0.0",
        }

        with patch.object(requests, "get", return_value=mock_response):
            args = MagicMock()
            args.url = "http://localhost:8000"

            check_status(args)

        captured = capsys.readouterr()
        assert "healthy" in captured.out
        assert "available" in captured.out
        assert "1.0.0" in captured.out

    def test_check_status_docker_unavailable(self, capsys):
        """check_status shows docker unavailable status."""
        import requests

        from tako_vm.cli import check_status

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "degraded",
            "docker_available": False,
            "version": "1.0.0",
        }

        with patch.object(requests, "get", return_value=mock_response):
            args = MagicMock()
            args.url = "http://localhost:8000"

            check_status(args)

        captured = capsys.readouterr()
        assert "unavailable" in captured.out

    def test_check_status_generic_exception(self):
        """check_status handles generic exceptions."""
        import requests

        from tako_vm.cli import check_status

        with patch.object(requests, "get", side_effect=Exception("Unexpected error")):
            args = MagicMock()
            args.url = "http://localhost:8000"

            with pytest.raises(SystemExit) as exc_info:
                check_status(args)
            assert exc_info.value.code == 1


class TestValidateConfigFunction:
    """Tests for validate_config() function directly."""

    def test_validate_config_no_file_found(self, capsys):
        """validate_config exits when no config file found."""
        from tako_vm import config as config_module
        from tako_vm.cli import validate_config

        # Mock find_config_file to return None (it's imported from config module)
        with patch.object(config_module, "find_config_file", return_value=None):
            args = MagicMock()
            args.config_file = None
            args.config = None

            with pytest.raises(SystemExit) as exc_info:
                validate_config(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "No configuration file found" in captured.out

    def test_validate_config_uses_global_config(self):
        """validate_config uses --config when config_file not specified."""
        from tako_vm.cli import validate_config

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("max_workers: 4\nsecurity_mode: permissive\n")
            f.flush()
            config_path = Path(f.name)

        try:
            args = MagicMock()
            args.config_file = None
            args.config = config_path

            # Should not raise (valid config)
            validate_config(args)
        finally:
            config_path.unlink()

    def test_validate_config_with_job_types(self, capsys):
        """validate_config shows job types in summary."""
        from tako_vm.cli import validate_config

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
max_workers: 4
security_mode: permissive
job_types:
  - name: data-processing
    requirements:
      - pandas
    timeout: 60
  - name: ml-training
    requirements:
      - numpy
    timeout: 120
"""
            )
            f.flush()
            config_path = Path(f.name)

        try:
            args = MagicMock()
            args.config_file = config_path
            args.config = None

            validate_config(args)

            captured = capsys.readouterr()
            assert "data-processing" in captured.out
            assert "ml-training" in captured.out
            assert "Job types defined: 2" in captured.out
        finally:
            config_path.unlink()


class TestShowConfigFunction:
    """Tests for show_config() function directly."""

    def test_show_config_with_job_types(self, capsys):
        """show_config displays job types section."""
        from tako_vm import config as config_module
        from tako_vm.cli import show_config
        from tako_vm.config import JobTypeConfig, TakoVMConfig, reset_config

        reset_config()

        # Create config with job types
        mock_config = TakoVMConfig(
            security_mode="permissive",
            job_types=[
                JobTypeConfig(
                    name="test-job",
                    requirements=["pandas", "numpy"],
                    memory_limit="512m",
                    cpu_limit=1.0,
                    timeout=60,
                ),
            ],
        )

        # Patch at the config module since show_config imports from there
        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch.object(config_module, "get_config_path", return_value=None):
                args = MagicMock()
                args.json = False
                args.show_defaults = False

                show_config(args)

        captured = capsys.readouterr()
        assert "[Job Types]" in captured.out
        assert "test-job" in captured.out
        assert "pandas, numpy" in captured.out

        reset_config()

    def test_show_config_configuration_error(self):
        """show_config handles ConfigurationError."""
        from tako_vm import config as config_module
        from tako_vm.cli import show_config
        from tako_vm.config import ConfigurationError, reset_config

        reset_config()

        with patch.object(
            config_module, "get_config", side_effect=ConfigurationError("Invalid config")
        ):
            args = MagicMock()
            args.json = False

            with pytest.raises(SystemExit) as exc_info:
                show_config(args)
            assert exc_info.value.code == 1

        reset_config()

    def test_show_config_no_config_file(self, capsys):
        """show_config shows '(using defaults)' when no config file."""
        from tako_vm import config as config_module
        from tako_vm.cli import show_config
        from tako_vm.config import reset_config

        reset_config()

        with patch.object(config_module, "get_config_path", return_value=None):
            args = MagicMock()
            args.json = False
            args.show_defaults = False

            show_config(args)

        captured = capsys.readouterr()
        assert "(using defaults)" in captured.out

        reset_config()

    def test_show_config_with_config_file(self, capsys):
        """show_config shows config file path when available."""
        from tako_vm import config as config_module
        from tako_vm.cli import show_config
        from tako_vm.config import reset_config

        reset_config()

        with patch.object(config_module, "get_config_path", return_value=Path("/etc/tako_vm.yaml")):
            args = MagicMock()
            args.json = False
            args.show_defaults = False

            show_config(args)

        captured = capsys.readouterr()
        assert "/etc/tako_vm.yaml" in captured.out

        reset_config()


class TestRunServerFunction:
    """Tests for run_server() function."""

    def test_run_server_development_mode(self, capsys):
        """run_server starts in development mode by default."""
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            args = MagicMock()
            args.host = "0.0.0.0"
            args.port = 8000
            args.reload = False
            args.workers = None

            run_server(args)

        captured = capsys.readouterr()
        assert "DEVELOPMENT mode" in captured.out
        mock_run.assert_called_once()

        reset_config()

    def test_run_server_production_mode(self, capsys):
        """run_server indicates production mode."""
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()

        mock_config = TakoVMConfig(
            production_mode=True,
            security_mode="permissive",
        )

        # Patch at config module since run_server imports from there
        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch.object(uvicorn, "run"):
                args = MagicMock()
                args.host = "0.0.0.0"
                args.port = 8000
                args.reload = False
                args.workers = None

                run_server(args)

        captured = capsys.readouterr()
        assert "PRODUCTION mode" in captured.out

        reset_config()

    def test_run_server_custom_host_port(self):
        """run_server uses custom host and port from args."""
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            args = MagicMock()
            args.host = "127.0.0.1"
            args.port = 9000
            args.reload = True
            args.workers = None

            run_server(args)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["host"] == "127.0.0.1"
        assert call_kwargs["port"] == 9000
        assert call_kwargs["reload"] is True

        reset_config()

    def test_run_server_configuration_error(self):
        """run_server exits on configuration error."""
        from tako_vm import config as config_module
        from tako_vm.cli import run_server
        from tako_vm.config import ConfigurationError

        with patch.object(
            config_module, "get_config", side_effect=ConfigurationError("Bad config")
        ):
            args = MagicMock()
            args.host = "0.0.0.0"
            args.port = 8000
            args.reload = False

            with pytest.raises(SystemExit) as exc_info:
                run_server(args)
            assert exc_info.value.code == 1


class TestCLISubprocessExtended:
    """Extended subprocess tests for CLI edge cases."""

    def test_server_help(self):
        """server --help shows server options."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "server", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--host" in result.stdout
        assert "--port" in result.stdout
        assert "--reload" in result.stdout

    def test_status_help(self):
        """status --help shows status options."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "status", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--url" in result.stdout

    def test_validate_help(self):
        """validate --help shows validate options."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "validate", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_config_help(self):
        """config --help shows config options."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "config", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--json" in result.stdout

    def test_dev_help(self):
        """dev --help shows development subcommands."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "dev", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "up" in result.stdout
        assert "status" in result.stdout
        assert "down" in result.stdout

    def test_config_shows_all_sections(self):
        """config command shows all configuration sections."""
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "config"],
            capture_output=True,
            text=True,
            env={**os.environ, "TAKO_VM_SECURITY_MODE": "permissive"},
        )
        assert result.returncode == 0
        assert "[Mode]" in result.stdout
        assert "[Paths]" in result.stdout
        assert "[Queue & Workers]" in result.stdout
        assert "[Limits]" in result.stdout
        assert "[Container Limits]" in result.stdout
        assert "[Docker]" in result.stdout

    def test_validate_yaml_syntax_error(self):
        """validate command detects YAML syntax errors."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("invalid: yaml: syntax: [\n")
            f.flush()
            config_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, "-m", "tako_vm.cli", "validate", config_path],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 1
        finally:
            Path(config_path).unlink()

    def test_short_config_flag(self):
        """-c short flag works for --config."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("max_workers: 4\n")
            f.flush()
            config_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, "-m", "tako_vm.cli", "-c", config_path, "version"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
        finally:
            Path(config_path).unlink()


class TestCLIDevHelpers:
    """Tests for dev helper functionality."""

    def test_dev_up_without_server(self, capsys):
        """dev_up starts local postgres without launching server."""
        from tako_vm.cli import MANAGED_POSTGRES_URL, dev_up

        args = argparse.Namespace(with_server=False, host="0.0.0.0", port=8000, reload=False)

        with patch("tako_vm.cli._ensure_managed_postgres") as ensure_pg:
            dev_up(args)

        captured = capsys.readouterr()
        ensure_pg.assert_called_once()
        assert "Local PostgreSQL is ready" in captured.out
        assert MANAGED_POSTGRES_URL in captured.out

    def test_run_server_auto_starts_local_postgres(self):
        """run_server auto-starts managed postgres on default URL failure."""
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import DEFAULT_DATABASE_URL, MANAGED_POSTGRES_URL, run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()
        mock_config = TakoVMConfig(database_url=DEFAULT_DATABASE_URL, security_mode="permissive")

        args = argparse.Namespace(
            host="0.0.0.0", port=8000, reload=False, workers=None, auto_start_postgres=True
        )

        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch("tako_vm.cli._can_connect_database", return_value=False):
                with patch("tako_vm.cli._ensure_managed_postgres") as ensure_pg:
                    with patch.object(uvicorn, "run"):
                        run_server(args)

        ensure_pg.assert_called_once()
        assert mock_config.database_url == MANAGED_POSTGRES_URL

        reset_config()

    def test_dev_status_running(self, capsys):
        """dev_status reports running and reachable state."""
        from tako_vm.cli import dev_status

        args = argparse.Namespace()
        with patch("tako_vm.cli._managed_postgres_state", return_value="running"):
            with patch("tako_vm.cli._can_connect_database", return_value=True):
                dev_status(args)

        captured = capsys.readouterr()
        assert "Status: running (reachable)" in captured.out

    def test_dev_down_stops_running_container(self, capsys):
        """dev_down stops container when it is running."""
        from tako_vm.cli import MANAGED_POSTGRES_CONTAINER, dev_down

        args = argparse.Namespace()
        with patch("tako_vm.cli._managed_postgres_state", return_value="running"):
            with patch("tako_vm.cli.subprocess.run") as run_mock:
                dev_down(args)

        captured = capsys.readouterr()
        run_mock.assert_called_once_with(
            ["docker", "stop", MANAGED_POSTGRES_CONTAINER],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "Local PostgreSQL stopped" in captured.out

    def test_dev_down_missing_container(self, capsys):
        """dev_down handles missing container gracefully."""
        from tako_vm.cli import dev_down

        args = argparse.Namespace()
        with patch("tako_vm.cli._managed_postgres_state", return_value="missing"):
            dev_down(args)

        captured = capsys.readouterr()
        assert "not created" in captured.out

    def test_dev_status_reports_docker_unavailable_when_daemon_down(self, capsys):
        """dev_status surfaces daemon outage as docker unavailable."""
        from tako_vm.cli import dev_status

        args = argparse.Namespace()
        with patch(
            "tako_vm.cli.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, ["docker", "info"], stderr="Cannot connect to the Docker daemon"
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                dev_status(args)

        captured = capsys.readouterr()
        assert exc_info.value.code == 1
        assert "Status: docker unavailable" in captured.out
