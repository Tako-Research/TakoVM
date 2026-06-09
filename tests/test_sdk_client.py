"""
Tests for Tako VM SDK client.

Tests the typed function execution client, the async job lifecycle, auth
passthrough, and execution history with a mocked HTTP session.
"""

from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from tako_vm.sdk.client import (
    ExecutionError,
    ExecutionResult,
    TakoVM,
    ValidationError,
    _get_client,
    configure,
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


def _mock_session(json_value: Any = None, content: bytes = b"") -> MagicMock:
    """A requests.Session mock whose .request() returns a canned response."""
    response = MagicMock()
    response.json.return_value = json_value
    response.content = content
    response.raise_for_status = MagicMock()
    session = MagicMock()
    session.request.return_value = response
    return session


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
    """Tests for synchronous execution (mocked HTTP session)."""

    def test_send_success(self):
        session = _mock_session({"success": True, "output": {"result": 15}, "execution_time": 0.5})
        client = TakoVM(session=session)
        result = client.send(add_numbers, InputData(x=5, y=10))

        assert isinstance(result, OutputData)
        assert result.result == 15
        method, url = session.request.call_args.args
        assert method == "POST"
        assert url.endswith("/execute")

    def test_send_execution_failure(self):
        session = _mock_session(
            {
                "success": False,
                "output": None,
                "execution_time": 0.1,
                "stdout": "some output",
                "stderr": "error details",
                "error": "Execution failed: ZeroDivisionError",
            }
        )
        client = TakoVM(session=session)

        with pytest.raises(ExecutionError) as exc_info:
            client.send(add_numbers, InputData(x=1, y=2))

        assert "ZeroDivisionError" in str(exc_info.value)
        assert exc_info.value.stdout == "some output"
        assert exc_info.value.stderr == "error details"

    def test_send_raw_returns_result(self):
        session = _mock_session(
            {"success": False, "output": None, "execution_time": 0.1, "error": "Failed"}
        )
        client = TakoVM(session=session)
        result = client.send_raw(add_numbers, InputData(x=1, y=2))

        assert isinstance(result, ExecutionResult)
        assert result.success is False
        assert result.error == "Failed"

    def test_send_passes_all_execute_params(self):
        session = _mock_session({"success": True, "output": {"result": 3}, "execution_time": 0.1})
        client = TakoVM(session=session)
        client.send(
            add_numbers,
            InputData(1, 2),
            job_type="custom",
            timeout=60,
            requirements=["pandas", "numpy>=1.20"],
            startup_timeout=120,
            idempotency_key="abc12345",
        )

        payload = session.request.call_args.kwargs["json"]
        assert payload["job_type"] == "custom"
        assert payload["timeout"] == 60
        assert payload["requirements"] == ["pandas", "numpy>=1.20"]
        assert payload["startup_timeout"] == 120
        assert payload["idempotency_key"] == "abc12345"

    def test_request_failure_returns_failed_result(self):
        import requests

        session = MagicMock()
        session.request.side_effect = requests.exceptions.ConnectionError("boom")
        client = TakoVM(session=session)
        result = client.send_raw(add_numbers, InputData(1, 2))
        assert result.success is False
        assert "Request failed" in (result.error or "")


class TestAuthPassthrough:
    """The SDK forwards caller-supplied headers and never interprets them."""

    def test_headers_forwarded_on_every_request(self):
        session = _mock_session({"status": "healthy"})
        client = TakoVM(session=session, headers={"X-API-Key": "secret"})
        client.health()
        assert session.request.call_args.kwargs["headers"]["X-API-Key"] == "secret"

    def test_no_headers_sends_none(self):
        session = _mock_session({"status": "healthy"})
        client = TakoVM(session=session)
        client.health()
        assert session.request.call_args.kwargs["headers"] is None


class TestAsyncLifecycle:
    """Tests for the async job lifecycle."""

    def test_submit_code_returns_job_id(self):
        session = _mock_session({"job_id": "job-123"})
        client = TakoVM(session=session)
        job_id = client.submit_code(
            "print(1)", {"x": 1}, requirements=["numpy"], idempotency_key="k1"
        )
        assert job_id == "job-123"
        method, url = session.request.call_args.args
        assert method == "POST"
        assert url.endswith("/execute/async")
        payload = session.request.call_args.kwargs["json"]
        assert payload["requirements"] == ["numpy"]
        assert payload["idempotency_key"] == "k1"

    def test_submit_typed(self):
        session = _mock_session({"job_id": "job-xyz"})
        client = TakoVM(session=session)
        assert client.submit(add_numbers, InputData(1, 2), job_type="t") == "job-xyz"
        assert session.request.call_args.kwargs["json"]["job_type"] == "t"

    def test_get_status(self):
        session = _mock_session({"job_id": "j", "status": "running"})
        client = TakoVM(session=session)
        assert client.get_status("j")["status"] == "running"
        assert session.request.call_args.args == ("GET", "http://localhost:8000/jobs/j")

    def test_get_result_passes_timeout_and_view(self):
        session = _mock_session({"status": "completed"})
        client = TakoVM(session=session)
        client.get_result("j", timeout=30, view="full")
        assert session.request.call_args.kwargs["params"] == {"timeout": 30, "view": "full"}
        assert session.request.call_args.args[1].endswith("/jobs/j/result")

    def test_cancel(self):
        session = _mock_session({"status": "cancelled", "job_id": "j"})
        client = TakoVM(session=session)
        assert client.cancel("j")["status"] == "cancelled"
        assert session.request.call_args.args == ("POST", "http://localhost:8000/jobs/j/cancel")

    def test_rerun_returns_new_job_id(self):
        session = _mock_session({"job_id": "new-job"})
        client = TakoVM(session=session)
        assert client.rerun("j", timeout=10) == "new-job"
        assert session.request.call_args.kwargs["json"] == {"timeout": 10}

    def test_fork_returns_new_job_id(self):
        session = _mock_session({"job_id": "forked"})
        client = TakoVM(session=session)
        assert client.fork("j", "print(2)") == "forked"
        assert session.request.call_args.args[1].endswith("/jobs/j/fork")
        assert session.request.call_args.kwargs["json"]["code"] == "print(2)"

    def test_download_artifact_returns_bytes(self):
        session = _mock_session(content=b"BINARY")
        client = TakoVM(session=session)
        data = client.download_artifact("j", "out.png")
        assert data == b"BINARY"
        assert session.request.call_args.args[1].endswith("/jobs/j/artifacts/out.png")


class TestExecutionHistory:
    """Tests for execution listing and metadata."""

    def test_list_executions(self):
        session = _mock_session(
            {
                "items": [{"execution_id": "a"}],
                "limit": 50,
                "offset": 0,
                "has_more": False,
                "count": 1,
            }
        )
        client = TakoVM(session=session)
        page = client.list_executions(status="succeeded", limit=10)
        assert page["count"] == 1
        params = session.request.call_args.kwargs["params"]
        assert params["status"] == "succeeded"
        assert params["limit"] == 10
        assert session.request.call_args.args[1].endswith("/executions")

    def test_get_execution(self):
        session = _mock_session({"execution_id": "a"})
        client = TakoVM(session=session)
        assert client.get_execution("a", view="full")["execution_id"] == "a"
        assert session.request.call_args.kwargs["params"] == {"view": "full"}

    def test_list_job_types(self):
        session = _mock_session([{"name": "default"}, {"name": "data-processing"}])
        client = TakoVM(session=session)
        result = client.list_job_types()
        assert len(result) == 2
        assert result[0]["name"] == "default"

    def test_get_job_type(self):
        session = _mock_session({"name": "data-processing", "requirements": ["pandas"]})
        client = TakoVM(session=session)
        assert client.get_job_type("data-processing")["name"] == "data-processing"

    def test_health(self):
        session = _mock_session({"status": "healthy", "docker_available": True})
        client = TakoVM(session=session)
        assert client.health()["status"] == "healthy"


class TestModuleLevelFunctions:
    """Tests for module-level convenience functions."""

    def test_configure_with_headers(self):
        configure(base_url="http://test:9000", timeout=45, headers={"X-API-Key": "k"})
        client = _get_client()
        assert client.base_url == "http://test:9000"
        assert client.default_timeout == 45
        assert client._headers == {"X-API-Key": "k"}

    def test_get_client_creates_default(self):
        configure(base_url="http://localhost:8000")
        assert _get_client() is not None
