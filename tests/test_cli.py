import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCLIVersion:
    def test_version_command(self):
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
    def test_help_without_command(self):
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "usage" in result.stderr.lower() or "usage" in result.stdout.lower()

    def test_help_flag(self):
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
    def test_validate_valid_config(self):
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
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "validate", "/nonexistent/config.yaml"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1


class TestCLIConfig:
    def test_config_show(self):
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
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "config", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "TAKO_VM_SECURITY_MODE": "permissive"},
        )
        assert result.returncode == 0
        import json

        data = json.loads(result.stdout)
        assert "max_workers" in data
        assert "database_url" in data
        assert "***@" in data["database_url"]
        assert "postgres:postgres@" not in data["database_url"]


class TestCLIStatus:
    def test_status_server_not_running(self):
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "status", "--url", "http://localhost:59999"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        combined = (result.stdout + result.stderr).lower()
        assert "cannot connect" in combined or "error" in combined


class TestCLIConfigPath:
    def test_config_path_nonexistent(self):
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "--config", "/nonexistent.yaml", "version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr.lower()

    def test_config_path_valid(self):
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
    def test_main_import(self):
        from tako_vm.cli import main

        assert callable(main)

    def test_main_check_status_function(self):
        from tako_vm.cli import check_status

        args = MagicMock()
        args.url = "http://localhost:59999"

        with pytest.raises(SystemExit) as exc_info:
            check_status(args)
        assert exc_info.value.code == 1

    def test_main_show_config_function(self):
        from tako_vm.cli import show_config
        from tako_vm.config import reset_config

        reset_config()

        args = MagicMock()
        args.json = False
        args.show_defaults = False

        show_config(args)

        reset_config()

    def test_main_show_config_json(self, capsys):
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
    def test_check_status_success(self, capsys):
        import requests

        from tako_vm.cli import check_status

        mock_response = MagicMock()
        mock_response.status_code = 200
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
        import requests

        from tako_vm.cli import check_status

        mock_response = MagicMock()
        mock_response.status_code = 200
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
        import requests

        from tako_vm.cli import check_status

        with patch.object(requests, "get", side_effect=Exception("Unexpected error")):
            args = MagicMock()
            args.url = "http://localhost:8000"

            with pytest.raises(SystemExit) as exc_info:
                check_status(args)
            assert exc_info.value.code == 1


class TestValidateConfigFunction:
    def test_validate_config_no_file_found(self, capsys):
        from tako_vm import config as config_module
        from tako_vm.cli import validate_config

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
        from tako_vm.cli import validate_config

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("max_workers: 4\nsecurity_mode: permissive\n")
            f.flush()
            config_path = Path(f.name)

        try:
            args = MagicMock()
            args.config_file = None
            args.config = config_path

            validate_config(args)
        finally:
            config_path.unlink()

    def test_validate_config_with_job_types(self, capsys):
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
    def test_show_config_with_job_types(self, capsys):
        from tako_vm import config as config_module
        from tako_vm.cli import show_config
        from tako_vm.config import JobTypeConfig, TakoVMConfig, reset_config

        reset_config()

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
    def test_run_server_development_mode(self, capsys):
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            args = MagicMock()
            args.host = "127.0.0.1"
            args.port = 8000
            args.reload = False
            args.workers = None

            run_server(args)

        captured = capsys.readouterr()
        assert "DEVELOPMENT mode" in captured.out
        mock_run.assert_called_once()

        reset_config()

    def test_run_server_production_mode(self, capsys):
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()

        mock_config = TakoVMConfig(
            production_mode=True,
            security_mode="permissive",
        )

        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch.object(uvicorn, "run"):
                args = MagicMock()
                args.host = "127.0.0.1"
                args.port = 8000
                args.reload = False
                args.workers = None

                run_server(args)

        captured = capsys.readouterr()
        assert "PRODUCTION mode" in captured.out

        reset_config()

    def test_run_server_custom_host_port(self):
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
            args.config = None

            run_server(args)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["host"] == "127.0.0.1"
        assert call_kwargs["port"] == 9000
        assert call_kwargs["reload"] is True

        reset_config()

    def test_run_server_refuses_non_loopback_host_flag_without_auth(self, capsys):
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()

        mock_config = TakoVMConfig(
            server_host="127.0.0.1",
            server_port=9999,
            security_mode="permissive",
        )

        args = argparse.Namespace(
            host="0.0.0.0",
            port=8000,
            reload=False,
            workers=None,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch.object(uvicorn, "run", mock_run):
                with pytest.raises(SystemExit) as exc_info:
                    run_server(args)

        assert exc_info.value.code == 1
        mock_run.assert_not_called()

        captured = capsys.readouterr()
        assert "refusing to bind non-loopback host '0.0.0.0'" in captured.err
        assert "api_auth_enabled=true" in captured.err
        assert "--host 127.0.0.1" in captured.err
        assert "allow_unauthenticated_network_access=true" in captured.err

        reset_config()

    def test_run_server_allows_non_loopback_host_flag_when_auth_enabled(self):
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()

        mock_config = TakoVMConfig(
            server_host="127.0.0.1",
            server_port=9999,
            security_mode="permissive",
            api_auth_enabled=True,
            api_keys=["k" * 32],
        )

        args = argparse.Namespace(
            host="0.0.0.0",
            port=8000,
            reload=False,
            workers=None,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch.object(uvicorn, "run", mock_run):
                run_server(args)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["host"] == "0.0.0.0"
        assert call_kwargs["port"] == 8000

        reset_config()

    def test_run_server_allows_non_loopback_host_flag_with_explicit_opt_out(self):
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()

        mock_config = TakoVMConfig(
            server_host="127.0.0.1",
            server_port=9999,
            security_mode="permissive",
            allow_unauthenticated_network_access=True,
        )

        args = argparse.Namespace(
            host="0.0.0.0",
            port=8000,
            reload=False,
            workers=None,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch.object(uvicorn, "run", mock_run):
                run_server(args)

        assert mock_run.call_args[1]["host"] == "0.0.0.0"

        reset_config()

    def test_run_server_host_port_fall_back_to_config(self):
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()

        mock_config = TakoVMConfig(
            server_host="127.0.0.1",
            server_port=9999,
            security_mode="permissive",
        )

        args = argparse.Namespace(
            host=None,
            port=None,
            reload=False,
            workers=None,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(config_module, "get_config", return_value=mock_config):
            with patch.object(uvicorn, "run", mock_run):
                run_server(args)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["host"] == "127.0.0.1"
        assert call_kwargs["port"] == 9999

        reset_config()

    def test_run_server_workers_passed_through(self, capsys):
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()

        args = argparse.Namespace(
            host=None,
            port=None,
            reload=False,
            workers=4,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            run_server(args)

        app_arg = mock_run.call_args[0][0]
        call_kwargs = mock_run.call_args[1]
        assert app_arg == "tako_vm.server.app:app"
        assert call_kwargs["workers"] == 4

        captured = capsys.readouterr()
        assert "WARNING: --workers 4 runs 4 independent worker pools" in captured.err
        assert "wait=true result streaming only works on the submitting worker" in captured.err
        assert "Prefer a single worker behind a load balancer" in captured.err

        reset_config()

    def test_run_server_default_single_worker_uses_app_object(self, capsys):
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config
        from tako_vm.server.app import app

        reset_config()

        args = argparse.Namespace(
            host=None,
            port=None,
            reload=False,
            workers=None,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            run_server(args)

        app_arg = mock_run.call_args[0][0]
        call_kwargs = mock_run.call_args[1]
        assert app_arg is app
        assert call_kwargs["workers"] == 1

        captured = capsys.readouterr()
        assert "independent worker pools" not in captured.err

        reset_config()

    def test_run_server_reload_uses_import_string(self):
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()

        args = argparse.Namespace(
            host=None,
            port=None,
            reload=True,
            workers=None,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            run_server(args)

        app_arg = mock_run.call_args[0][0]
        call_kwargs = mock_run.call_args[1]
        assert app_arg == "tako_vm.server.app:app"
        assert call_kwargs["reload"] is True

        reset_config()

    def test_run_server_reload_and_workers_mutually_exclusive(self, capsys):
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()

        args = argparse.Namespace(
            host=None,
            port=None,
            reload=True,
            workers=2,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            with pytest.raises(SystemExit) as exc_info:
                run_server(args)

        assert exc_info.value.code == 1
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "--reload cannot be combined with --workers" in captured.err

        reset_config()

    def test_run_server_invalid_workers_rejected(self, capsys):
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()

        args = argparse.Namespace(
            host=None,
            port=None,
            reload=False,
            workers=0,
            config=None,
            auto_start_postgres=False,
        )

        mock_run = MagicMock()
        with patch.object(uvicorn, "run", mock_run):
            with pytest.raises(SystemExit) as exc_info:
                run_server(args)

        assert exc_info.value.code == 1
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "--workers must be >= 1" in captured.err

        reset_config()

    def test_run_server_workers_exports_explicit_config_path(self, tmp_path, monkeypatch):
        import uvicorn

        from tako_vm.cli import run_server
        from tako_vm.config import reset_config

        reset_config()
        monkeypatch.delenv("TAKO_VM_CONFIG", raising=False)

        config_file = tmp_path / "tako_vm.yaml"
        config_file.write_text("max_workers: 4\nsecurity_mode: permissive\n")

        args = argparse.Namespace(
            host=None,
            port=None,
            reload=False,
            workers=2,
            config=config_file,
            auto_start_postgres=False,
        )

        with patch.object(uvicorn, "run", MagicMock()):
            run_server(args)

        try:
            assert os.environ.get("TAKO_VM_CONFIG") == str(config_file)
        finally:
            os.environ.pop("TAKO_VM_CONFIG", None)
            reset_config()

    def test_run_server_configuration_error(self):
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
    def test_server_help(self):
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
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "status", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--url" in result.stdout

    def test_validate_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "validate", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_config_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "tako_vm.cli", "config", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--json" in result.stdout

    def test_dev_help(self):
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
    @pytest.fixture(autouse=True)
    def _isolate_database_url_env(self):
        original = os.environ.get("TAKO_VM_DATABASE_URL")
        yield
        if original is None:
            os.environ.pop("TAKO_VM_DATABASE_URL", None)
        else:
            os.environ["TAKO_VM_DATABASE_URL"] = original

    def test_dev_up_without_server(self, capsys):
        from tako_vm.cli import MANAGED_POSTGRES_URL, dev_up

        args = argparse.Namespace(with_server=False, host="0.0.0.0", port=8000, reload=False)

        with patch("tako_vm.cli._ensure_managed_postgres") as ensure_pg:
            dev_up(args)

        captured = capsys.readouterr()
        ensure_pg.assert_called_once()
        assert "Local PostgreSQL is ready" in captured.out
        assert MANAGED_POSTGRES_URL in captured.out

    def test_run_server_auto_starts_local_postgres(self):
        import uvicorn

        from tako_vm import config as config_module
        from tako_vm.cli import DEFAULT_DATABASE_URL, MANAGED_POSTGRES_URL, run_server
        from tako_vm.config import TakoVMConfig, reset_config

        reset_config()
        mock_config = TakoVMConfig(database_url=DEFAULT_DATABASE_URL, security_mode="permissive")

        args = argparse.Namespace(
            host="127.0.0.1",
            port=8000,
            reload=False,
            workers=None,
            auto_start_postgres=True,
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
        from tako_vm.cli import dev_status

        args = argparse.Namespace()
        with patch("tako_vm.cli._managed_postgres_state", return_value="running"):
            with patch("tako_vm.cli._can_connect_database", return_value=True):
                dev_status(args)

        captured = capsys.readouterr()
        assert "Status: running (reachable)" in captured.out

    def test_dev_down_stops_running_container(self, capsys):
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
        from tako_vm.cli import dev_down

        args = argparse.Namespace()
        with patch("tako_vm.cli._managed_postgres_state", return_value="missing"):
            dev_down(args)

        captured = capsys.readouterr()
        assert "not created" in captured.out

    def test_dev_status_reports_docker_unavailable_when_daemon_down(self, capsys):
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
