"""
Tests for Tako VM SDK client.

Tests the typed function execution client, the async job lifecycle, auth
passthrough, execution history, and the reliability layer (retries,
idempotency, correlation IDs, structured errors) with a mocked HTTP session.
"""

import re
from dataclasses import dataclass
from typing import Any, Optional, cast
from unittest.mock import MagicMock, patch

import pytest
import requests

from tako_vm.sdk.client import (
    CORRELATION_ID_HEADER,
    FALLBACK_READ_TIMEOUT,
    APIError,
    ClientError,
    ExecutionError,
    ExecutionResult,
    MalformedResponseError,
    ServerError,
    TakoVM,
    TakoVMError,
    TransportError,
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


def _response(
    json_value: Any = None,
    content: bytes = b"",
    status: int = 200,
    headers: Optional[dict] = None,
) -> MagicMock:
    """A canned requests.Response mock."""
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = json_value
    response.text = "" if json_value is None else str(json_value)
    response.content = content
    return response


def _mock_session(
    json_value: Any = None,
    content: bytes = b"",
    status: int = 200,
    headers: Optional[dict] = None,
) -> MagicMock:
    """A requests.Session mock whose .request() returns a canned response."""
    session = MagicMock()
    session.request.return_value = _response(json_value, content, status, headers)
    return session


def _calls_for(session: MagicMock, method: str, path_suffix: str = "") -> list:
    """Return session.request calls matching an HTTP method and URL suffix."""
    return [
        call
        for call in session.request.call_args_list
        if call.args[0] == method and call.args[1].endswith(path_suffix)
    ]


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

    def test_default_session_retries_gets_only(self):
        """The default session mounts a retry adapter for idempotent GETs only."""
        client = TakoVM()
        adapter = client._session.get_adapter("http://localhost:8000")
        retries = adapter.max_retries
        assert retries.total == 3
        assert retries.allowed_methods == frozenset({"GET"})
        assert set(retries.status_forcelist) == {502, 503, 504}

    def test_custom_session_is_used_verbatim(self):
        """A caller-supplied session is used as-is (no adapter swap)."""
        session = requests.Session()
        client = TakoVM(session=session)
        assert client._session is session


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
                "exit_code": 1,
            }
        )
        client = TakoVM(session=session)

        with pytest.raises(ExecutionError) as exc_info:
            client.send(add_numbers, InputData(x=1, y=2))

        assert "ZeroDivisionError" in str(exc_info.value)
        assert exc_info.value.stdout == "some output"
        assert exc_info.value.stderr == "error details"
        assert exc_info.value.exit_code == 1

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
        session = MagicMock()
        session.request.side_effect = requests.exceptions.ConnectionError("boom")
        client = TakoVM(session=session)
        result = client.send_raw(add_numbers, InputData(1, 2))
        assert result.success is False
        assert "Request failed" in (result.error or "")

    def test_exit_code_mapped_to_result(self):
        """send_raw() maps the server's exit_code into ExecutionResult."""
        session = _mock_session(
            {"success": True, "output": {"result": 3}, "execution_time": 0.5, "exit_code": 0}
        )
        client = TakoVM(session=session)
        result = client.send_raw(add_numbers, InputData(x=1, y=2), timeout=5)
        assert result.exit_code == 0

    def test_malformed_json_body_returns_failed_result(self):
        """A 2xx body that is not JSON becomes a failed result, never an exception."""
        response = _response(status=200, headers={CORRELATION_ID_HEADER: "cid-malformed"})
        response.json.side_effect = ValueError("no json")
        response.text = "<html>gateway</html>"
        session = MagicMock()
        session.request.return_value = response
        client = TakoVM(session=session)

        result = client.send_raw(add_numbers, InputData(1, 2), timeout=5)

        assert result.success is False
        assert "Malformed response" in (result.error or "")
        assert result.correlation_id == "cid-malformed"

    def test_send_raw_keeps_dict_on_deserialize_mismatch(self):
        """When the output doesn't fit the dataclass, send_raw keeps the raw dict."""
        session = _mock_session(
            {"success": True, "output": {"unexpected": 1}, "execution_time": 0.1}
        )
        client = TakoVM(session=session)
        result = client.send_raw(add_numbers, InputData(1, 2), timeout=5)
        assert result.success is True
        assert result.output == {"unexpected": 1}


class TestServerContractViolations:
    """A 2xx response with a schema-mismatched body must surface a structured,
    correlated MalformedResponseError — never a naked KeyError or a vague
    "Execution failed" — and must be logged loudly (verbose-on-failure)."""

    def test_submit_code_missing_job_id_raises_malformed(self, caplog):
        session = _mock_session({"status": "queued", "oops": 1})  # no job_id
        client = TakoVM(session=session)
        with caplog.at_level("ERROR"):
            with pytest.raises(MalformedResponseError) as exc:
                client.submit_code("print(1)", {})
        assert exc.value.correlation_id is not None
        assert any("missing 'job_id'" in r.getMessage() for r in caplog.records)

    def test_submit_code_non_dict_body_raises_malformed(self):
        session = _mock_session(["not", "a", "dict"])
        client = TakoVM(session=session)
        with pytest.raises(MalformedResponseError):
            client.submit_code("print(1)", {})

    def test_rerun_missing_job_id_raises_malformed(self):
        session = _mock_session({"nope": True})
        client = TakoVM(session=session)
        with pytest.raises(MalformedResponseError):
            client.rerun("job-x")

    def test_fork_missing_job_id_raises_malformed(self):
        session = _mock_session({"nope": True})
        client = TakoVM(session=session)
        with pytest.raises(MalformedResponseError):
            client.fork("job-x", "print(1)")

    def test_execute_missing_success_field_raises_malformed(self, caplog):
        # Valid JSON, but not the execution-result shape. Must not be coerced
        # into a vague "Execution failed".
        session = _mock_session({"stdout": "", "exit_code": 0})  # no 'success'
        client = TakoVM(session=session)
        with caplog.at_level("ERROR"):
            with pytest.raises(MalformedResponseError):
                client._execute("print(1)", {})
        assert any("missing 'success'" in r.getMessage() for r in caplog.records)

    def test_submit_code_all_retries_fail_raises_transport(self, caplog):
        # Every attempt fails -> the real transport error must surface, not an
        # UnboundLocalError from `data` never being assigned.
        session = MagicMock()
        session.request.side_effect = requests.exceptions.ConnectionError("refused")
        client = TakoVM(session=session)
        with caplog.at_level("ERROR"):
            with pytest.raises(TransportError):
                client.submit_code("print(1)", {})
        assert any("gave up after" in r.getMessage() for r in caplog.records)


class TestAuthPassthrough:
    """The SDK forwards caller-supplied headers and never interprets them."""

    def test_headers_forwarded_on_every_request(self):
        session = _mock_session({"status": "healthy"})
        client = TakoVM(session=session, headers={"X-API-Key": "secret"})
        client.health()
        assert session.request.call_args.kwargs["headers"]["X-API-Key"] == "secret"

    def test_no_headers_sends_only_correlation_id(self):
        session = _mock_session({"status": "healthy"})
        client = TakoVM(session=session)
        client.health()
        sent = session.request.call_args.kwargs["headers"]
        assert set(sent) == {CORRELATION_ID_HEADER}
        assert sent[CORRELATION_ID_HEADER]


class TestErrorTaxonomy:
    """Tests for the structured exception hierarchy raised by _request."""

    def test_connection_error_raises_transport_error(self):
        session = MagicMock()
        session.request.side_effect = requests.exceptions.ConnectionError("connection refused")
        client = TakoVM(session=session)

        with pytest.raises(TransportError) as exc_info:
            client.get_status("job-1")

        assert "connection refused" in str(exc_info.value)
        assert exc_info.value.correlation_id is not None

    def test_503_raises_server_error_with_detail(self):
        """A 503 with a JSON body raises ServerError carrying detail + correlation_id."""
        session = _mock_session(
            {"detail": "Queue is full", "correlation_id": "abc-123"}, status=503
        )
        client = TakoVM(session=session)

        with pytest.raises(ServerError) as exc_info:
            client.get_status("job-1")

        err = exc_info.value
        assert err.status_code == 503
        assert err.detail == "Queue is full"
        assert err.correlation_id == "abc-123"
        assert err.retryable is True

    def test_500_server_error_not_retryable(self):
        session = _mock_session({"detail": "Internal server error"}, status=500)
        client = TakoVM(session=session)

        with pytest.raises(ServerError) as exc_info:
            client.get_status("job-1")

        assert exc_info.value.status_code == 500
        assert exc_info.value.retryable is False

    def test_422_raises_client_error_with_detail(self):
        """A 422 validation error raises ClientError with the detail preserved."""
        session = _mock_session(
            {"detail": [{"loc": ["body", "code"], "msg": "Code cannot be empty"}]}, status=422
        )
        client = TakoVM(session=session)

        with pytest.raises(ClientError) as exc_info:
            client.submit_code("print(1)")

        assert exc_info.value.status_code == 422
        assert "Code cannot be empty" in (exc_info.value.detail or "")

    def test_404_raises_client_error(self):
        session = _mock_session({"detail": "Job not found"}, status=404)
        client = TakoVM(session=session)

        with pytest.raises(ClientError) as exc_info:
            client.get_result("missing-job", timeout=5)

        assert exc_info.value.status_code == 404
        assert session.request.call_count == 1

    def test_sync_execute_not_retried_on_transport_error(self):
        """Sync POST /execute is never retried (it is not idempotent)."""
        session = MagicMock()
        session.request.side_effect = requests.exceptions.ConnectionError("boom")
        client = TakoVM(session=session)

        # Explicit timeout avoids the job-type lookup GET
        result = client.send_raw(add_numbers, InputData(1, 2), timeout=5)

        assert result.success is False
        assert session.request.call_count == 1
        assert session.request.call_args.args[0] == "POST"

    def test_sync_execute_reports_http_error_as_failed_result(self):
        """send_raw() keeps its legacy contract: HTTP errors become failed results."""
        session = _mock_session({"detail": "Queue is full"}, status=503)
        client = TakoVM(session=session)

        result = client.send_raw(add_numbers, InputData(1, 2), timeout=5)

        assert result.success is False
        assert "503" in (result.error or "")
        assert result.correlation_id is not None

    def test_exception_hierarchy(self):
        """All SDK exceptions derive from TakoVMError."""
        assert issubclass(TransportError, TakoVMError)
        assert issubclass(APIError, TakoVMError)
        assert issubclass(ServerError, APIError)
        assert issubclass(ClientError, APIError)
        assert issubclass(ExecutionError, TakoVMError)
        assert issubclass(ValidationError, TakoVMError)


class TestCorrelation:
    """Tests for correlation ID propagation."""

    def test_correlation_header_sent_on_every_request(self):
        session = _mock_session({"status": "healthy"})
        client = TakoVM(session=session, correlation_id="fixed-cid-001")
        client.health()
        sent = session.request.call_args.kwargs["headers"]
        assert sent[CORRELATION_ID_HEADER] == "fixed-cid-001"

    def test_correlation_id_generated_when_absent(self):
        """A correlation id is auto-generated and exposed on the exception."""
        session = MagicMock()
        session.request.side_effect = requests.exceptions.ConnectionError("down")
        client = TakoVM(session=session)

        with pytest.raises(TransportError) as exc_info:
            client.get_status("job-1")

        sent_cid = session.request.call_args.kwargs["headers"][CORRELATION_ID_HEADER]
        assert sent_cid  # non-empty
        assert exc_info.value.correlation_id == sent_cid

    def test_correlation_id_read_from_response(self):
        """The server's response correlation id is surfaced on the result."""
        session = _mock_session(
            {"success": True, "output": {"result": 3}, "execution_time": 0.5},
            headers={CORRELATION_ID_HEADER: "server-cid-9"},
        )
        client = TakoVM(session=session)
        result = client.send_raw(add_numbers, InputData(x=1, y=2), timeout=5)

        assert result.correlation_id == "server-cid-9"

    def test_execution_error_carries_correlation_id(self):
        session = _mock_session(
            {"success": False, "output": None, "execution_time": 0.1, "error": "Failed"},
            headers={CORRELATION_ID_HEADER: "server-cid-7"},
        )
        client = TakoVM(session=session)

        with pytest.raises(ExecutionError) as exc_info:
            client.send(add_numbers, InputData(1, 2), timeout=5)

        assert exc_info.value.correlation_id == "server-cid-7"

    def test_caller_header_overrides_generated_correlation_id(self):
        session = _mock_session({"status": "healthy"})
        client = TakoVM(session=session, headers={CORRELATION_ID_HEADER: "caller-cid"})
        client.health()
        sent = session.request.call_args.kwargs["headers"]
        assert sent[CORRELATION_ID_HEADER] == "caller-cid"


class TestTimeoutResolution:
    """Tests for client-side HTTP read-timeout resolution."""

    def test_read_timeout_covers_exec_plus_startup(self):
        """The HTTP read timeout outlives the server-side exec+startup window."""
        session = _mock_session({"success": True, "output": {"result": 3}, "execution_time": 0.5})
        client = TakoVM(session=session)
        client.send(add_numbers, InputData(x=1, y=2), timeout=120, startup_timeout=180)

        connect_timeout, read_timeout = session.request.call_args.kwargs["timeout"]
        assert connect_timeout <= 10
        assert read_timeout >= 120 + 180  # never below exec + startup budget

    def test_omitted_timeout_resolved_from_job_type(self):
        """When timeout is omitted, the client resolves it from GET /job-types/{name}."""

        def respond(method, url, **kwargs):
            if method == "GET" and "/job-types/" in url:
                return _response({"name": "default", "timeout": 120})
            return _response({"success": True, "output": {"result": 3}, "execution_time": 0.5})

        session = MagicMock()
        session.request.side_effect = respond
        client = TakoVM(session=session)
        client.send(add_numbers, InputData(x=1, y=2))

        (post_call,) = _calls_for(session, "POST", "/execute")
        _, read_timeout = post_call.kwargs["timeout"]
        assert read_timeout >= 120  # job-type default, not client default_timeout + 10
        # Timeout is not sent in the payload (server resolves its own default)
        assert "timeout" not in post_call.kwargs["json"]
        assert len(_calls_for(session, "GET", "/job-types/default")) == 1

    def test_fallback_read_timeout_when_lookup_fails(self):
        """If the job-type lookup fails, a generous fallback read timeout is used."""

        def respond(method, url, **kwargs):
            if method == "GET" and "/job-types/" in url:
                return _response({"detail": "nope"}, status=500)
            return _response({"success": True, "output": {"result": 3}, "execution_time": 0.5})

        session = MagicMock()
        session.request.side_effect = respond
        client = TakoVM(session=session)
        client.send(add_numbers, InputData(x=1, y=2))

        (post_call,) = _calls_for(session, "POST", "/execute")
        _, read_timeout = post_call.kwargs["timeout"]
        assert read_timeout == FALLBACK_READ_TIMEOUT

    def test_job_type_timeout_lookup_is_cached(self):
        """The job-type timeout lookup happens once per client per job type."""

        def respond(method, url, **kwargs):
            if method == "GET" and "/job-types/" in url:
                return _response({"name": "default", "timeout": 120})
            return _response({"success": True, "output": {"result": 3}, "execution_time": 0.5})

        session = MagicMock()
        session.request.side_effect = respond
        client = TakoVM(session=session)
        client.send(add_numbers, InputData(x=1, y=2))
        client.send(add_numbers, InputData(x=3, y=4))

        assert len(_calls_for(session, "GET", "/job-types/default")) == 1
        assert len(_calls_for(session, "POST", "/execute")) == 2


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
        assert session.request.call_args.kwargs["params"] == {
            "wait": "true",
            "timeout": 30,
            "view": "full",
        }
        assert session.request.call_args.args[1].endswith("/jobs/j/result")

    def test_get_result_without_timeout_does_not_wait(self):
        session = _mock_session({"status": "queued"})
        client = TakoVM(session=session)
        client.get_result("j")
        assert session.request.call_args.kwargs["params"] == {}

    def test_get_result_clamps_timeout_to_server_wait_cap(self):
        session = _mock_session({"status": "completed"})
        client = TakoVM(session=session)
        client.get_result("j", timeout=900)
        assert session.request.call_args.kwargs["params"] == {"wait": "true", "timeout": 300}

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


class TestIdempotency:
    """Tests for retry-safe async submission via idempotency keys."""

    def test_submit_code_autogenerates_server_compatible_key(self):
        session = _mock_session({"job_id": "job-1"})
        client = TakoVM(session=session)
        client.submit_code("print(1)")

        key = session.request.call_args.kwargs["json"]["idempotency_key"]
        # Server constraint: ^[a-zA-Z0-9_-]+$, 8-255 chars
        assert re.fullmatch(r"[a-zA-Z0-9_-]{8,255}", key)

    @patch("tako_vm.sdk.client.time.sleep")
    def test_submit_retries_transport_error_with_same_key(self, mock_sleep):
        """submit_code() retries transient failures, reusing the same idempotency key."""
        session = MagicMock()
        session.request.side_effect = [
            requests.exceptions.ConnectionError("dropped"),
            _response({"job_id": "job-2"}),
        ]
        client = TakoVM(session=session)
        job_id = client.submit_code("print(1)")

        assert job_id == "job-2"
        post_calls = _calls_for(session, "POST", "/execute/async")
        assert len(post_calls) == 2
        keys = {call.kwargs["json"]["idempotency_key"] for call in post_calls}
        assert len(keys) == 1  # same key on retry

    @patch("tako_vm.sdk.client.time.sleep")
    def test_submit_retries_retryable_5xx(self, mock_sleep):
        session = MagicMock()
        session.request.side_effect = [
            _response({"detail": "temporarily unavailable"}, status=503),
            _response({"job_id": "job-3"}),
        ]
        client = TakoVM(session=session)
        assert client.submit_code("print(1)") == "job-3"
        assert session.request.call_count == 2

    def test_submit_does_not_retry_client_errors(self):
        session = _mock_session({"detail": "Code cannot be empty"}, status=422)
        client = TakoVM(session=session)

        with pytest.raises(ClientError):
            client.submit_code("print(1)")

        assert session.request.call_count == 1

    @patch("tako_vm.sdk.client.time.sleep")
    def test_submit_gives_up_after_max_attempts(self, mock_sleep):
        session = MagicMock()
        session.request.side_effect = requests.exceptions.ConnectionError("down")
        client = TakoVM(session=session)

        with pytest.raises(TransportError):
            client.submit_code("print(1)")

        assert session.request.call_count == 3

    @patch("tako_vm.sdk.client.time.sleep")
    def test_submit_typed_retry_safe(self, mock_sleep):
        """submit() (typed) inherits retry-safe submission from submit_code()."""
        session = MagicMock()
        session.request.side_effect = [
            _response({"detail": "bad gateway"}, status=502),
            _response({"job_id": "job-4"}),
        ]
        client = TakoVM(session=session)
        assert client.submit(add_numbers, InputData(1, 2)) == "job-4"


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


class TestExecutionResultFields:
    """Tests for the ExecutionResult dataclass."""

    def test_new_traceability_fields_default_none(self):
        result = ExecutionResult(
            success=True,
            output={"key": "value"},
            execution_time=1.5,
            stdout="output",
            stderr="",
            error=None,
            job_type="default",
        )
        assert result.exit_code is None
        assert result.correlation_id is None
        assert result.job_id is None


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

    def test_configure_forwards_full_client_args(self):
        """configure() must accept the same knobs as TakoVM, not just headers."""
        sess = requests.Session()
        configure(
            base_url="http://test:9000",
            timeout=45,
            headers={"X-API-Key": "k"},
            session=sess,
            connect_timeout=3.0,
            correlation_id="fixed-cid",
        )
        client = _get_client()
        assert client._session is sess
        assert client.connect_timeout == 3.0
        assert client.correlation_id == "fixed-cid"

    def test_send_forwards_new_kwargs(self):
        """Module-level send() must pass through startup_timeout/idempotency_key/correlation_id."""
        import tako_vm.sdk.client as client_mod

        mock_client = MagicMock()
        with patch.object(client_mod, "_get_client", return_value=mock_client):
            client_mod.send(
                add_numbers,
                InputData(1, 2),
                timeout=10,
                job_type="cpu",
                requirements=["numpy"],
                startup_timeout=99,
                idempotency_key="idem-1",
                correlation_id="cid-1",
            )
        mock_client.send.assert_called_once_with(
            add_numbers,
            InputData(1, 2),
            timeout=10,
            job_type="cpu",
            requirements=["numpy"],
            startup_timeout=99,
            idempotency_key="idem-1",
            correlation_id="cid-1",
        )

    @pytest.mark.parametrize(
        "fn_name, args, kwargs",
        [
            ("submit", (add_numbers, InputData(1, 2)), {"job_type": "cpu"}),
            ("submit_code", ("print(1)",), {"idempotency_key": "k"}),
            ("get_status", ("job-1",), {}),
            ("get_result", ("job-1",), {"timeout": 5, "view": "full"}),
            ("cancel", ("job-1",), {}),
            ("rerun", ("job-1",), {"timeout": 7}),
            ("fork", ("job-1", "print(2)"), {"job_type": "cpu"}),
            ("download_artifact", ("job-1", "out.bin"), {}),
            ("list_executions", (), {"status": "succeeded", "limit": 10}),
            ("get_execution", ("exec-1",), {"view": "full"}),
            ("pool_stats", (), {}),
            ("dlq_stats", (), {}),
            ("health", (), {}),
            ("list_job_types", (), {}),
            ("get_job_type", ("default",), {}),
            ("build_job_type", ("default",), {}),
        ],
    )
    def test_module_helpers_delegate_to_default_client(self, fn_name, args, kwargs):
        """Every module-level helper forwards to the matching default-client method.

        Wrappers forward positional args verbatim and pass keyword args through
        (filling unset ones with the method defaults), so we assert the
        positionals match exactly and every kwarg we supplied was forwarded.
        """
        import tako_vm.sdk.client as client_mod

        mock_client = MagicMock()
        with patch.object(client_mod, "_get_client", return_value=mock_client):
            result = getattr(client_mod, fn_name)(*args, **kwargs)
        method = getattr(mock_client, fn_name)
        method.assert_called_once()
        call = method.call_args
        # Positionals we supplied are forwarded as a leading prefix (some wrappers
        # additionally pass a defaulted positional, e.g. submit_code's input_data).
        assert call.args[: len(args)] == args
        for key, value in kwargs.items():
            assert call.kwargs[key] == value
        assert result is method.return_value
