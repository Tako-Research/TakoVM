"""
Tests for Tako VM SDK client.

Tests the typed function execution client with mocked HTTP responses.
"""

from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from tako_vm.sdk.client import (
    ExecutionError,
    ExecutionResult,
    TakoVM,
    TakoVMError,
    ValidationError,
    _get_client,
    configure,
    send,
)


@dataclass
class InputData:
    """Test input dataclass."""

    x: int
    y: int


@dataclass
class OutputData:
    """Test output dataclass."""

    result: int


def add_numbers(input: InputData) -> OutputData:
    """Test function that adds two numbers."""
    return OutputData(result=input.x + input.y)


class TestTakoVMClient:
    """Tests for TakoVM client class."""

    def test_client_init(self):
        """Client initializes with default values."""
        client = TakoVM()
        assert client.base_url == "http://localhost:8000"
        assert client.default_timeout == 30

    def test_client_custom_url(self):
        """Client accepts custom URL."""
        client = TakoVM(base_url="http://custom:9000/")
        assert client.base_url == "http://custom:9000"  # Trailing slash removed

    def test_client_custom_timeout(self):
        """Client accepts custom timeout."""
        client = TakoVM(timeout=60)
        assert client.default_timeout == 60


class TestCodeGeneration:
    """Tests for code generation."""

    def test_generate_code(self):
        """Client generates valid wrapper code."""
        client = TakoVM()
        code = client._generate_code(add_numbers, InputData, OutputData)

        assert "from dataclasses import dataclass" in code
        assert "class InputData:" in code
        assert "class OutputData:" in code
        assert "def add_numbers" in code
        assert "/input/data.json" in code
        assert "/output/result.json" in code

    def test_generate_dataclass_source(self):
        """Client can generate dataclass source from fields."""
        client = TakoVM()
        source = client._generate_dataclass_source(InputData)

        assert "@dataclass" in source
        assert "class InputData:" in source
        assert "x: int" in source
        assert "y: int" in source


class TestValidation:
    """Tests for input/output validation."""

    def test_send_requires_dataclass_input(self):
        """send() requires dataclass input."""
        client = TakoVM()

        with pytest.raises(ValidationError) as exc_info:
            client.send(add_numbers, cast(Any, {"x": 1, "y": 2}))  # dict, not dataclass

        assert "must be a dataclass instance" in str(exc_info.value)

    def test_send_requires_return_type_hint(self):
        """send() requires function to have return type hint."""
        client = TakoVM()

        def no_return_hint(input: InputData):
            return OutputData(result=input.x + input.y)

        with pytest.raises(ValidationError) as exc_info:
            client.send(no_return_hint, InputData(1, 2))

        assert "return type hint" in str(exc_info.value)

    def test_send_requires_dataclass_return_type(self):
        """send() requires return type to be a dataclass."""
        client = TakoVM()

        def dict_return(input: InputData) -> dict:
            return {"result": input.x + input.y}

        with pytest.raises(ValidationError) as exc_info:
            client.send(dict_return, InputData(1, 2))

        assert "must be a dataclass" in str(exc_info.value)

    def test_send_requires_parameter(self):
        """send() requires function to have at least one parameter."""
        client = TakoVM()

        def no_params() -> OutputData:
            return OutputData(result=0)

        with pytest.raises(ValidationError) as exc_info:
            client.send(cast(Any, no_params), InputData(1, 2))

        assert "at least one parameter" in str(exc_info.value)


class TestExecution:
    """Tests for execution with mocked HTTP."""

    @patch("tako_vm.sdk.client.requests.post")
    def test_send_success(self, mock_post):
        """send() returns deserialized output on success."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "output": {"result": 15},
            "execution_time": 0.5,
            "stdout": "",
            "stderr": "",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        client = TakoVM()
        result = client.send(add_numbers, InputData(x=5, y=10))

        assert isinstance(result, OutputData)
        assert result.result == 15

    @patch("tako_vm.sdk.client.requests.post")
    def test_send_execution_failure(self, mock_post):
        """send() raises ExecutionError on failure."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": False,
            "output": None,
            "execution_time": 0.1,
            "stdout": "some output",
            "stderr": "error details",
            "error": "Execution failed: ZeroDivisionError",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        client = TakoVM()

        with pytest.raises(ExecutionError) as exc_info:
            client.send(add_numbers, InputData(x=1, y=2))

        assert "ZeroDivisionError" in str(exc_info.value)
        assert exc_info.value.stdout == "some output"
        assert exc_info.value.stderr == "error details"

    @patch("tako_vm.sdk.client.requests.post")
    def test_send_raw_returns_result(self, mock_post):
        """send_raw() returns ExecutionResult instead of raising."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": False,
            "output": None,
            "execution_time": 0.1,
            "stdout": "output",
            "stderr": "error",
            "error": "Failed",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        client = TakoVM()
        result = client.send_raw(add_numbers, InputData(x=1, y=2))

        assert isinstance(result, ExecutionResult)
        assert result.success is False
        assert result.error == "Failed"

    @patch("tako_vm.sdk.client.requests.post")
    def test_send_with_job_type(self, mock_post):
        """send() passes job_type to API."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "output": {"result": 3},
            "execution_time": 0.5,
            "stdout": "",
            "stderr": "",
            "job_type": "custom-job",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        client = TakoVM()
        client.send(add_numbers, InputData(x=1, y=2), job_type="custom-job")

        # Verify job_type was included in request
        call_args = mock_post.call_args
        assert call_args[1]["json"]["job_type"] == "custom-job"

    @patch("tako_vm.sdk.client.requests.post")
    def test_send_with_timeout(self, mock_post):
        """send() passes timeout to API."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "output": {"result": 3},
            "execution_time": 0.5,
            "stdout": "",
            "stderr": "",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        client = TakoVM()
        client.send(add_numbers, InputData(x=1, y=2), timeout=60)

        call_args = mock_post.call_args
        assert call_args[1]["json"]["timeout"] == 60


class TestHealthCheck:
    """Tests for health check endpoint."""

    @patch("tako_vm.sdk.client.requests.get")
    def test_health_success(self, mock_get):
        """health() returns server status."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "healthy",
            "docker_available": True,
            "version": "1.0.0",
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        client = TakoVM()
        result = client.health()

        assert result["status"] == "healthy"
        assert result["docker_available"] is True


class TestJobTypes:
    """Tests for job type endpoints."""

    @patch("tako_vm.sdk.client.requests.get")
    def test_list_job_types(self, mock_get):
        """list_job_types() returns job type list."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"name": "default", "requirements": []},
            {"name": "data-processing", "requirements": ["pandas"]},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        client = TakoVM()
        result = client.list_job_types()

        assert len(result) == 2
        assert result[0]["name"] == "default"

    @patch("tako_vm.sdk.client.requests.get")
    def test_get_job_type(self, mock_get):
        """get_job_type() returns specific job type."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "name": "data-processing",
            "requirements": ["pandas", "numpy"],
            "timeout": 60,
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        client = TakoVM()
        result = client.get_job_type("data-processing")

        assert result["name"] == "data-processing"
        assert "pandas" in result["requirements"]


class TestModuleLevelFunctions:
    """Tests for module-level convenience functions."""

    def test_configure(self):
        """configure() sets default client."""
        configure(base_url="http://test:9000", timeout=45)
        client = _get_client()

        assert client.base_url == "http://test:9000"
        assert client.default_timeout == 45

    def test_get_client_creates_default(self):
        """_get_client() creates default client if not configured."""
        # Reset by setting to a known URL first
        configure(base_url="http://localhost:8000")
        client = _get_client()
        assert client is not None

    @patch("tako_vm.sdk.client.requests.post")
    def test_send_module_function(self, mock_post):
        """Module-level send() uses default client."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "output": {"result": 5},
            "execution_time": 0.1,
            "stdout": "",
            "stderr": "",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        configure(base_url="http://localhost:8000")
        result = send(add_numbers, InputData(x=2, y=3))

        assert result.result == 5


class TestExecutionResult:
    """Tests for ExecutionResult dataclass."""

    def test_execution_result_fields(self):
        """ExecutionResult has expected fields."""
        result = ExecutionResult(
            success=True,
            output={"key": "value"},
            execution_time=1.5,
            stdout="output",
            stderr="",
            error=None,
            job_type="default",
        )
        assert result.success is True
        assert result.output == {"key": "value"}
        assert result.execution_time == 1.5
        assert result.job_type == "default"


class TestExceptionHierarchy:
    """Tests for exception classes."""

    def test_tako_vm_error_base(self):
        """TakoVMError is base exception."""
        assert issubclass(ExecutionError, TakoVMError)
        assert issubclass(ValidationError, TakoVMError)

    def test_execution_error_captures_output(self):
        """ExecutionError captures stdout/stderr."""
        error = ExecutionError("Failed", stdout="out", stderr="err")
        assert str(error) == "Failed"
        assert error.stdout == "out"
        assert error.stderr == "err"
