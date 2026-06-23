"""
tako_vm - Typed function execution SDK for the secure code executor.

Provides a typed interface for executing functions in isolated containers:

    from dataclasses import dataclass
    import tako_vm

    @dataclass
    class InputStruct:
        args1: int
        args2: int

    @dataclass
    class OutputStruct:
        return1: int

    def my_func(input: InputStruct) -> OutputStruct:
        return OutputStruct(return1=input.args1 + input.args2)

    result = tako_vm.send(my_func, InputStruct(1, 2))
    print(result.return1)  # 3

Authentication is handled by the *caller*, not the SDK: pass whatever headers
your deployment requires and they are forwarded verbatim on every request. The
SDK never interprets credentials.

    client = TakoVM("https://tako.internal", headers={"X-API-Key": KEY})

A preconfigured ``requests.Session`` may be supplied for retries, mTLS, proxies,
connection pooling, etc.:

    client = TakoVM("https://tako.internal", session=my_session)

Reliability notes:

- When no session is supplied, the SDK builds a pooled ``requests.Session``
  that transparently retries idempotent GETs (status/result polling, job
  types, health) on 502/503/504. POSTs are never retried at the transport
  layer because the sync ``/execute`` endpoint is not idempotent (a blind
  retry could re-execute the code).
- For retry-safe submission use ``submit()``/``submit_code()``, which go
  through the async API with an auto-generated idempotency key so a retried
  POST returns the existing job instead of double-executing.
- Every request carries an ``X-Correlation-ID`` header; the id is exposed on
  results and exceptions for end-to-end tracing.
- HTTP failures raise a structured taxonomy: ``TransportError`` for
  connection/timeout failures, ``ServerError`` (with ``retryable``) for 5xx,
  and ``ClientError`` for 4xx, all carrying the server's ``detail`` and
  correlation id. The sync ``send()``/``send_raw()`` path keeps its legacy
  contract and reports these as a failed ``ExecutionResult`` instead.
"""

import inspect
import json
import logging
import textwrap
import time
import uuid
from dataclasses import MISSING, asdict, dataclass, fields, is_dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, cast, get_type_hints

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Server-side cap on /jobs/{id}/result?wait=true long-polling
# (MAX_WAIT_TIMEOUT in tako_vm/server/app.py).
_MAX_RESULT_WAIT_SECONDS = 300

# Type variables for generic typing
InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")

# Header used by the server's CorrelationIdMiddleware
CORRELATION_ID_HEADER = "X-Correlation-ID"

# Connect timeout: establishing a TCP connection should be fast.
DEFAULT_CONNECT_TIMEOUT = 10.0

# Server-side caps (see ExecuteRequest in tako_vm/server/app.py):
# execution timeout <= 300s, startup timeout <= 600s. When we cannot determine
# the effective execution timeout, the HTTP read timeout must cover the worst
# case so the client never kills a job that the server would still complete.
MAX_SERVER_EXEC_TIMEOUT = 300.0
MAX_SERVER_STARTUP_TIMEOUT = 600.0
HTTP_TIMEOUT_BUFFER = 30.0
FALLBACK_READ_TIMEOUT = MAX_SERVER_EXEC_TIMEOUT + MAX_SERVER_STARTUP_TIMEOUT + HTTP_TIMEOUT_BUFFER

# Async submission: retries are safe because every submission carries an
# idempotency key, so a retried POST returns the existing job.
SUBMIT_MAX_ATTEMPTS = 3
SUBMIT_BACKOFF_INITIAL = 0.5
SUBMIT_BACKOFF_CAP = 8.0


@dataclass
class ExecutionResult:
    """Result of a function execution."""

    success: bool
    output: Any
    execution_time: float
    stdout: str
    stderr: str
    error: Optional[str] = None
    job_type: Optional[str] = None
    exit_code: Optional[int] = None
    correlation_id: Optional[str] = None
    job_id: Optional[str] = None
    # Effective isolation runtime the job ran under: 'runsc' (gVisor) or 'runc'
    # (weaker fallback). Lets a caller confirm the gVisor boundary was in effect
    # before trusting the output. None if the server predates this field.
    runtime: Optional[str] = None
    # Set when the run succeeded but the output dict could not be coerced into
    # the expected dataclass (output stays a raw dict). Lets callers detect the
    # mismatch programmatically instead of scraping logs.
    deserialization_error: Optional[str] = None


class TakoVMError(Exception):
    """Base exception for tako_vm errors."""


class TransportError(TakoVMError):
    """Raised when the HTTP request itself fails (connection, DNS, timeout).

    The request may or may not have reached the server, so the SDK never
    retries non-idempotent POSTs after this error.
    """

    def __init__(self, message: str, correlation_id: Optional[str] = None):
        super().__init__(message)
        self.correlation_id = correlation_id


class APIError(TakoVMError):
    """Base for HTTP error responses from the server (4xx/5xx)."""

    def __init__(
        self,
        message: str,
        status_code: int,
        detail: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.correlation_id = correlation_id


class ServerError(APIError):
    """Raised on 5xx responses. ``retryable`` is True for 502/503/504."""

    def __init__(
        self,
        message: str,
        status_code: int,
        detail: Optional[str] = None,
        correlation_id: Optional[str] = None,
        retryable: bool = False,
    ):
        super().__init__(message, status_code, detail, correlation_id)
        self.retryable = retryable


class ClientError(APIError):
    """Raised on 4xx responses (invalid request, not found, conflict, ...)."""


class SDKExecutionError(TakoVMError):
    """Raised when code execution fails via the SDK."""

    def __init__(
        self,
        message: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: Optional[int] = None,
        correlation_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.correlation_id = correlation_id


# Backward compatibility alias
ExecutionError = SDKExecutionError


class MalformedResponseError(TakoVMError):
    """Raised when the server returns 2xx but the body violates the expected
    schema (missing/renamed fields). Surfaces a contract mismatch loudly
    instead of letting a naked KeyError escape with no correlation context."""

    def __init__(self, message: str, correlation_id: Optional[str] = None):
        super().__init__(message)
        self.correlation_id = correlation_id


class ValidationError(TakoVMError):
    """Raised when input/output validation fails."""


def _require_job_id(data: Any, correlation_id: Optional[str]) -> str:
    """Extract ``job_id`` from a server response, raising a structured,
    correlated error if the body is malformed instead of a naked KeyError."""
    if not isinstance(data, dict) or not data.get("job_id"):
        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        logger.error(
            "Server response missing 'job_id' (correlation_id=%s, keys=%s)",
            correlation_id,
            keys,
        )
        raise MalformedResponseError(
            f"Server response did not contain a job_id (got keys: {keys})",
            correlation_id=correlation_id,
        )
    return data["job_id"]


def _build_session(pool_size: int = 10) -> requests.Session:
    """Build a pooled session that retries idempotent GETs only.

    POSTs are deliberately excluded from transport-level retries: the sync
    ``/execute`` endpoint is not idempotent, so a blind retry could execute
    the submitted code twice. Retry-safe submission is provided by
    ``TakoVM.submit()``/``submit_code()`` via the async API's idempotency keys.
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class TakoVM:
    """
    Client for the secure code executor.

    Covers typed-function execution (``send``), the async job lifecycle
    (``submit``/``get_status``/``get_result``/``cancel``/``rerun``/``fork``),
    artifact download, execution history, and metadata.

    Example:
        client = TakoVM("http://localhost:8000")
        result = client.send(my_func, InputStruct(1, 2))
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        timeout: int = 30,
        headers: Optional[Dict[str, str]] = None,
        session: Optional[requests.Session] = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        correlation_id: Optional[str] = None,
    ):
        """
        Initialize the TakoVM client.

        Args:
            base_url: URL of the code executor API.
            timeout: Default execution timeout in seconds.
            headers: Headers forwarded on every request. Use this to
                authenticate (e.g. ``{"X-API-Key": ...}`` or
                ``{"Authorization": "Bearer ..."}``); the SDK does not
                interpret them.
            session: Optional preconfigured ``requests.Session`` (retries,
                mTLS, proxies, pooling). When omitted, a pooled session with
                a GET-only retry adapter is created.
            connect_timeout: HTTP connect timeout in seconds (kept short).
            correlation_id: Fixed correlation id to send on every request.
                If not set, a fresh id is generated per request.
        """
        self.base_url = base_url.rstrip("/")
        self.default_timeout = timeout
        self.connect_timeout = connect_timeout
        self.correlation_id = correlation_id
        self._headers = dict(headers) if headers else {}
        self._session = session or _build_session()
        # Cache of job type name -> effective execution timeout (or None).
        self._job_type_timeouts: Dict[str, Optional[int]] = {}

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #

    def _resolve_correlation_id(self, correlation_id: Optional[str]) -> str:
        return correlation_id or self.correlation_id or str(uuid.uuid4())

    @staticmethod
    def _parse_error_body(response: requests.Response) -> Tuple[str, Optional[str]]:
        """Extract (detail, correlation_id) from a FastAPI JSON error body."""
        detail: Optional[str] = None
        correlation_id: Optional[str] = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            raw_detail = body.get("detail")
            if isinstance(raw_detail, str):
                detail = raw_detail
            elif raw_detail is not None:
                # 422 validation errors are a list of dicts
                detail = json.dumps(raw_detail)
            raw_cid = body.get("correlation_id")
            if isinstance(raw_cid, str):
                correlation_id = raw_cid
        if detail is None:
            text = (response.text or "").strip()
            detail = text[:500] if text else f"HTTP {response.status_code}"
        return detail, correlation_id

    def _request(
        self,
        method: str,
        path: str,
        *,
        expect_json: bool = True,
        http_timeout: Optional[float] = None,
        correlation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Issue a request, forwarding the caller-supplied auth headers.

        Every request carries an ``X-Correlation-ID`` header (caller-supplied
        headers win on conflict). Failures are translated into the SDK error
        taxonomy.

        Raises:
            TransportError: connection/DNS/timeout failures
            ServerError: 5xx responses (``retryable`` set for 502/503/504)
            ClientError: 4xx responses
        """
        cid = self._resolve_correlation_id(correlation_id)
        merged = {CORRELATION_ID_HEADER: cid, **self._headers, **kwargs.pop("headers", {})}
        read_timeout = http_timeout if http_timeout is not None else self.default_timeout + 10
        try:
            response = self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=merged,
                timeout=(self.connect_timeout, read_timeout),
                **kwargs,
            )
        except requests.exceptions.RequestException as e:
            logger.warning("Transport error on %s %s (correlation_id=%s): %s", method, path, cid, e)
            raise TransportError(f"{method} {path} failed: {e}", correlation_id=cid) from e

        if response.status_code >= 400:
            detail, body_cid = self._parse_error_body(response)
            resp_cid = body_cid or response.headers.get(CORRELATION_ID_HEADER) or cid
            logger.warning(
                "%s %s returned %s (correlation_id=%s): %s",
                method,
                path,
                response.status_code,
                resp_cid,
                detail,
            )
            if response.status_code >= 500:
                raise ServerError(
                    f"Server error {response.status_code} on {method} {path}: {detail}",
                    status_code=response.status_code,
                    detail=detail,
                    correlation_id=resp_cid,
                    retryable=response.status_code in (502, 503, 504),
                )
            raise ClientError(
                f"Client error {response.status_code} on {method} {path}: {detail}",
                status_code=response.status_code,
                detail=detail,
                correlation_id=resp_cid,
            )
        return response.json() if expect_json else response

    def _job_type_timeout(self, job_type: Optional[str]) -> Optional[int]:
        """Best-effort lookup of a job type's effective execution timeout.

        Results (including lookup failures) are cached per client to keep
        this cheap; it is only consulted when the caller omits ``timeout``.
        """
        name = (job_type or "default").split("@")[0]
        if name in self._job_type_timeouts:
            return self._job_type_timeouts[name]
        resolved: Optional[int] = None
        try:
            info = self._request("GET", f"/job-types/{name}", http_timeout=10)
            value = info.get("timeout") if isinstance(info, dict) else None
            if isinstance(value, int):
                resolved = value
            # Only a definitive answer (success, whether or not a timeout was
            # defined) is cached. A transient lookup failure below is NOT
            # cached, so the client re-probes once the server recovers instead
            # of pinning the conservative fallback for its whole lifetime.
            self._job_type_timeouts[name] = resolved
        except TakoVMError as e:
            logger.warning(
                "Could not resolve timeout for job type %r (%s); "
                "falling back to the maximum read timeout of %.0fs for this call "
                "(will re-check on next use)",
                name,
                e,
                FALLBACK_READ_TIMEOUT,
            )
        return resolved

    def _resolve_read_timeout(
        self,
        timeout: Optional[int],
        startup_timeout: Optional[int],
        job_type: Optional[str],
    ) -> float:
        """Pick an HTTP read timeout that always outlives the server-side job.

        The server resolves an omitted ``timeout`` to the job type's default
        and additionally allows a startup phase (dependency install), so a
        naive ``default_timeout + 10`` read timeout would kill long jobs
        client-side while they succeed server-side.
        """
        exec_t: Optional[float] = float(timeout) if timeout is not None else None
        if exec_t is None:
            jt = self._job_type_timeout(job_type)
            if jt is not None:
                exec_t = float(jt)
        startup_t = (
            float(startup_timeout) if startup_timeout is not None else MAX_SERVER_STARTUP_TIMEOUT
        )
        if exec_t is None:
            return FALLBACK_READ_TIMEOUT
        return exec_t + startup_t + HTTP_TIMEOUT_BUFFER

    # ------------------------------------------------------------------ #
    # Typed function execution (synchronous)
    # ------------------------------------------------------------------ #

    def send(
        self,
        func: Callable[[InputT], OutputT],
        input_data: InputT,
        timeout: Optional[int] = None,
        job_type: Optional[str] = None,
        requirements: Optional[List[str]] = None,
        startup_timeout: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> OutputT:
        """
        Execute a typed function in an isolated container and return its output.

        Args:
            func: Function to execute. Must have type hints for input and output.
            input_data: Input dataclass instance.
            timeout: Execution timeout in seconds (uses job type default if not set).
            job_type: Job type name (uses "default" if not set).
            requirements: Python packages to install at runtime, e.g.
                ``["pandas", "numpy>=1.20"]`` (requires the server to allow it).
            startup_timeout: Timeout for the startup phase (container + deps).
            idempotency_key: Client key for idempotent submission.
            correlation_id: Correlation id for tracing (auto-generated if omitted).

        Returns:
            Output dataclass instance.

        Raises:
            ValidationError: If input/output types are invalid.
            ExecutionError: If execution fails.
        """
        input_cls, output_cls = self._resolve_io_types(func, input_data)
        code = self._generate_code(func, input_cls, output_cls)
        result = self._execute(
            code,
            asdict(cast(Any, input_data)),
            timeout=timeout,
            job_type=job_type,
            requirements=requirements,
            startup_timeout=startup_timeout,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

        if not result.success:
            raise ExecutionError(
                result.error or "Execution failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                correlation_id=result.correlation_id,
            )

        try:
            return cast(OutputT, output_cls(**cast(Dict[str, Any], result.output)))
        except (TypeError, ValueError) as e:
            raise ValidationError(
                f"Failed to deserialize output to {output_cls.__name__}: {e}"
            ) from e

    def send_raw(
        self,
        func: Callable[[InputT], OutputT],
        input_data: InputT,
        timeout: Optional[int] = None,
        job_type: Optional[str] = None,
        requirements: Optional[List[str]] = None,
        startup_timeout: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ExecutionResult:
        """
        Execute a typed function and return the raw result.

        Same as send() but returns ExecutionResult instead of raising on failure.
        Useful when you want to handle errors yourself or inspect stdout/stderr.
        """
        input_cls, output_cls = self._resolve_io_types(func, input_data)
        code = self._generate_code(func, input_cls, output_cls)
        result = self._execute(
            code,
            asdict(cast(Any, input_data)),
            timeout=timeout,
            job_type=job_type,
            requirements=requirements,
            startup_timeout=startup_timeout,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

        if result.success and result.output:
            try:
                result.output = output_cls(**cast(Dict[str, Any], result.output))
            except (TypeError, ValueError, KeyError) as e:
                # Keep as dict if deserialization fails (type mismatch, missing fields, etc.).
                result.deserialization_error = (
                    f"Could not deserialize output to {output_cls.__name__}: {e}"
                )
                logger.warning(
                    "Could not deserialize output to %s (correlation_id=%s); "
                    "returning the raw dict instead: %s",
                    output_cls.__name__,
                    result.correlation_id,
                    e,
                )

        return result

    def _resolve_io_types(self, func: Callable[..., Any], input_data: Any) -> Tuple[type, type]:
        """Validate a typed function and return its (input_cls, output_cls)."""
        if not is_dataclass(input_data):
            raise ValidationError(
                f"input_data must be a dataclass instance, got {type(input_data)}"
            )

        hints = get_type_hints(func)
        if "return" not in hints:
            raise ValidationError("Function must have a return type hint")

        output_type = hints["return"]
        if not inspect.isclass(output_type) or not is_dataclass(output_type):
            raise ValidationError(f"Return type must be a dataclass, got {output_type}")

        params = [k for k in hints.keys() if k != "return"]
        if not params:
            raise ValidationError("Function must have at least one parameter")

        input_type = hints[params[0]]
        if not inspect.isclass(input_type) or not is_dataclass(input_type):
            raise ValidationError(f"Input parameter must be a dataclass type, got {input_type}")

        return cast(type, input_type), cast(type, output_type)

    def _execute(
        self,
        code: str,
        input_data: dict,
        *,
        timeout: Optional[int] = None,
        job_type: Optional[str] = None,
        requirements: Optional[List[str]] = None,
        startup_timeout: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Execute code synchronously via POST /execute.

        This POST is intentionally never retried: ``/execute`` is not
        idempotent, so a retry after an ambiguous transport failure could run
        the code twice. Use submit()/submit_code() for retry-safe submission.
        Transport and HTTP errors are reported as a failed ExecutionResult
        (legacy contract) carrying the correlation id.
        """
        payload = self._execute_payload(
            code,
            input_data,
            timeout=timeout,
            job_type=job_type,
            requirements=requirements,
            startup_timeout=startup_timeout,
            idempotency_key=idempotency_key,
        )
        cid = self._resolve_correlation_id(correlation_id)
        read_timeout = self._resolve_read_timeout(timeout, startup_timeout, job_type)
        logger.debug(
            "Submitting sync execution (correlation_id=%s, job_type=%s, read_timeout=%.0fs)",
            cid,
            job_type or "default",
            read_timeout,
        )
        try:
            response = self._request(
                "POST",
                "/execute",
                json=payload,
                expect_json=False,
                http_timeout=read_timeout,
                correlation_id=cid,
            )
        except (TransportError, APIError) as e:
            return ExecutionResult(
                success=False,
                output=None,
                execution_time=0,
                stdout="",
                stderr="",
                error=f"Request failed: {e}",
                correlation_id=e.correlation_id or cid,
            )

        result_cid = response.headers.get(CORRELATION_ID_HEADER) or cid
        try:
            data = response.json()
        except ValueError as e:
            snippet = (response.text or "")[:500]
            logger.error(
                "Sync execution returned a non-JSON body "
                "(correlation_id=%s, status=%s): %s | body=%r",
                result_cid,
                response.status_code,
                e,
                snippet,
            )
            return ExecutionResult(
                success=False,
                output=None,
                execution_time=0,
                stdout="",
                stderr="",
                error=(
                    f"Malformed response from server (HTTP {response.status_code}): "
                    f"{e}; body={snippet!r}"
                ),
                correlation_id=result_cid,
            )
        if not isinstance(data, dict) or "success" not in data:
            # Valid JSON, but not the execution-result shape we expect. Surface
            # the contract mismatch instead of coercing it to a vague
            # "Execution failed" with no diagnostic.
            keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            logger.error(
                "Execution response missing 'success' field (correlation_id=%s, keys=%s)",
                result_cid,
                keys,
            )
            raise MalformedResponseError(
                f"Execution response was missing the 'success' field (got keys: {keys})",
                correlation_id=result_cid,
            )
        logger.debug(
            "Sync execution finished (correlation_id=%s, success=%s)",
            result_cid,
            data.get("success", False),
        )
        return ExecutionResult(
            success=data.get("success", False),
            output=data.get("output"),
            execution_time=data.get("execution_time", 0),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            error=data.get("error"),
            job_type=data.get("job_type"),
            exit_code=data.get("exit_code"),
            correlation_id=result_cid,
            job_id=data.get("execution_id") or data.get("job_id"),
            runtime=data.get("runtime"),
        )

    @staticmethod
    def _execute_payload(
        code: str,
        input_data: dict,
        *,
        timeout: Optional[int],
        job_type: Optional[str],
        requirements: Optional[List[str]],
        startup_timeout: Optional[int],
        idempotency_key: Optional[str],
    ) -> Dict[str, Any]:
        """Build a /execute(/async) request body, omitting unset optionals."""
        payload: Dict[str, Any] = {"code": code, "input_data": input_data}
        if timeout is not None:
            payload["timeout"] = timeout
        if startup_timeout is not None:
            payload["startup_timeout"] = startup_timeout
        if job_type is not None:
            payload["job_type"] = job_type
        if requirements:
            payload["requirements"] = list(requirements)
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return payload

    # ------------------------------------------------------------------ #
    # Code generation (sandbox wrapper)
    # ------------------------------------------------------------------ #

    def _generate_code(
        self, func: Callable[..., Any], input_type: type[Any], output_type: type[Any]
    ) -> str:
        """Generate wrapper code for execution in the sandbox."""

        # Get function source
        func_source = inspect.getsource(func)
        func_source = textwrap.dedent(func_source)

        # Get dataclass definitions
        input_source = self._get_dataclass_source(input_type)
        output_source = self._get_dataclass_source(output_type)

        # Build the wrapper code
        code = f"""
from dataclasses import dataclass, asdict
import json

# Input dataclass definition
{input_source}

# Output dataclass definition
{output_source}

# User function
{func_source}

# Execution wrapper
def _execute():
    # Read input
    with open("/input/data.json") as f:
        input_dict = json.load(f)

    # Deserialize to input dataclass
    input_obj = {input_type.__name__}(**input_dict)

    # Execute function
    result = {func.__name__}(input_obj)

    # Serialize output
    output_dict = asdict(result)

    # Write output
    with open("/output/result.json", "w") as f:
        json.dump(output_dict, f)

_execute()
"""
        return code.strip()

    def _get_dataclass_source(self, cls: type[Any]) -> str:
        """Get the source code for a dataclass."""
        try:
            source = inspect.getsource(cls)
            return textwrap.dedent(source)
        except (OSError, TypeError):
            # If we can't get source, generate it from fields
            return self._generate_dataclass_source(cls)

    def _generate_dataclass_source(self, cls: type[Any]) -> str:
        """Generate dataclass source code from its fields."""
        lines = ["@dataclass", f"class {cls.__name__}:"]

        for field in fields(cast(Any, cls)):
            type_name = self._get_type_name(field.type)
            if field.default_factory is MISSING:
                if field.default is not MISSING:
                    lines.append(f"    {field.name}: {type_name} = {repr(field.default)}")
                else:
                    lines.append(f"    {field.name}: {type_name}")
            else:
                lines.append(f"    {field.name}: {type_name}")

        return "\n".join(lines)

    def _get_type_name(self, t: object) -> str:
        """Get a string representation of a type."""
        if hasattr(t, "__name__"):
            return cast(Any, t).__name__
        return str(t)

    # ------------------------------------------------------------------ #
    # Async job lifecycle
    # ------------------------------------------------------------------ #

    def submit_code(
        self,
        code: str,
        input_data: Optional[dict] = None,
        *,
        timeout: Optional[int] = None,
        startup_timeout: Optional[int] = None,
        job_type: Optional[str] = None,
        requirements: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        """Submit raw code for asynchronous execution; returns the job id.

        An idempotency key is auto-generated when not supplied, which makes
        submission retry-safe: transient failures (transport errors,
        502/503/504) are retried with backoff using the *same* key, so the
        server returns the existing job instead of executing the code twice.
        """
        key = idempotency_key or f"sdk-{uuid.uuid4().hex}"
        cid = self._resolve_correlation_id(correlation_id)
        payload = self._execute_payload(
            code,
            input_data or {},
            timeout=timeout,
            job_type=job_type,
            requirements=requirements,
            startup_timeout=startup_timeout,
            idempotency_key=key,
        )
        logger.debug("Submitting async job (correlation_id=%s, idempotency_key=%s)", cid, key)
        data: Optional[Dict[str, Any]] = None
        for attempt in range(SUBMIT_MAX_ATTEMPTS):
            try:
                data = self._request("POST", "/execute/async", json=payload, correlation_id=cid)
                break
            except (TransportError, ServerError) as e:
                if isinstance(e, ServerError) and not e.retryable:
                    raise
                if attempt == SUBMIT_MAX_ATTEMPTS - 1:
                    logger.error(
                        "Async submit gave up after %d attempts "
                        "(correlation_id=%s, idempotency_key=%s): %s",
                        SUBMIT_MAX_ATTEMPTS,
                        cid,
                        key,
                        e,
                    )
                    raise
                delay = min(SUBMIT_BACKOFF_INITIAL * (2**attempt), SUBMIT_BACKOFF_CAP)
                logger.warning(
                    "Async submit attempt %d failed (correlation_id=%s), retrying in %.1fs: %s",
                    attempt + 1,
                    cid,
                    delay,
                    e,
                )
                time.sleep(delay)
        job_id = _require_job_id(data, cid)
        logger.debug("Async job %s queued (correlation_id=%s)", job_id, cid)
        return job_id

    def submit(
        self,
        func: Callable[[InputT], OutputT],
        input_data: InputT,
        *,
        timeout: Optional[int] = None,
        startup_timeout: Optional[int] = None,
        job_type: Optional[str] = None,
        requirements: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        """Submit a typed function for asynchronous execution; returns the job id."""
        input_cls, output_cls = self._resolve_io_types(func, input_data)
        code = self._generate_code(func, input_cls, output_cls)
        return self.submit_code(
            code,
            asdict(cast(Any, input_data)),
            timeout=timeout,
            startup_timeout=startup_timeout,
            job_type=job_type,
            requirements=requirements,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

    def get_status(self, job_id: str) -> dict:
        """Get an async job's status (GET /jobs/{job_id})."""
        return self._request("GET", f"/jobs/{job_id}")

    def get_result(
        self, job_id: str, *, timeout: Optional[int] = None, view: Optional[str] = None
    ) -> dict:
        """
        Get an async job's result, waiting up to ``timeout`` seconds for it
        (GET /jobs/{job_id}/result?wait=true). Without ``timeout``, returns
        the current record immediately (which may still be queued/running).
        Pass ``view="full"`` for artifacts, resource usage, hashes, and lineage.
        """
        params: Dict[str, Any] = {}
        if timeout:
            # The server only honors ``timeout`` when ``wait`` is set, and caps
            # the long-poll at MAX_WAIT_TIMEOUT (300s). Clamp rather than 422.
            wait_timeout = min(timeout, _MAX_RESULT_WAIT_SECONDS)
            if wait_timeout != timeout:
                logger.warning(
                    "get_result(timeout=%s) exceeds the server's %ss long-poll cap; "
                    "waiting %ss per request instead",
                    timeout,
                    _MAX_RESULT_WAIT_SECONDS,
                    wait_timeout,
                )
            params["wait"] = "true"
            params["timeout"] = wait_timeout
        if view:
            params["view"] = view
        return self._request(
            "GET",
            f"/jobs/{job_id}/result",
            params=params,
            http_timeout=(timeout or self.default_timeout) + 10,
        )

    def cancel(self, job_id: str) -> dict:
        """Cancel a queued or running job (POST /jobs/{job_id}/cancel)."""
        return self._request("POST", f"/jobs/{job_id}/cancel")

    def rerun(
        self, job_id: str, *, job_type: Optional[str] = None, timeout: Optional[int] = None
    ) -> str:
        """Re-run a previous job with the same code/input; returns the new job id."""
        body: Dict[str, Any] = {}
        if job_type is not None:
            body["job_type"] = job_type
        if timeout is not None:
            body["timeout"] = timeout
        data = self._request("POST", f"/jobs/{job_id}/rerun", json=body)
        return _require_job_id(data, None)

    def fork(
        self,
        job_id: str,
        code: str,
        *,
        job_type: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Re-run a previous job's input with new code; returns the new job id."""
        body: Dict[str, Any] = {"code": code}
        if job_type is not None:
            body["job_type"] = job_type
        if timeout is not None:
            body["timeout"] = timeout
        data = self._request("POST", f"/jobs/{job_id}/fork", json=body)
        return _require_job_id(data, None)

    def download_artifact(self, job_id: str, artifact_name: str) -> bytes:
        """Download a job artifact's bytes (GET /jobs/{job_id}/artifacts/{name})."""
        response = self._request(
            "GET", f"/jobs/{job_id}/artifacts/{artifact_name}", expect_json=False
        )
        return response.content

    # ------------------------------------------------------------------ #
    # Execution history & metadata
    # ------------------------------------------------------------------ #

    def list_executions(
        self,
        *,
        status: Optional[str] = None,
        job_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        view: Optional[str] = None,
    ) -> dict:
        """List execution records (GET /executions); returns a paginated response."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if job_type:
            params["job_type"] = job_type
        if view:
            params["view"] = view
        return self._request("GET", "/executions", params=params)

    def get_execution(self, execution_id: str, *, view: Optional[str] = None) -> dict:
        """Get a single execution record (GET /executions/{execution_id})."""
        params = {"view": view} if view else {}
        return self._request("GET", f"/executions/{execution_id}", params=params)

    def pool_stats(self) -> dict:
        """Worker pool statistics (GET /pool/stats)."""
        return self._request("GET", "/pool/stats", http_timeout=10)

    def dlq_stats(self) -> dict:
        """Dead-letter-queue statistics (GET /dlq/stats)."""
        return self._request("GET", "/dlq/stats", http_timeout=10)

    def health(self) -> dict:
        """Check API health status."""
        return self._request("GET", "/health", http_timeout=10)

    def list_job_types(self) -> list:
        """List available job types."""
        return self._request("GET", "/job-types", http_timeout=10)

    def get_job_type(self, name: str) -> dict:
        """Get a specific job type by name."""
        return self._request("GET", f"/job-types/{name}", http_timeout=10)

    def build_job_type(self, name: str) -> dict:
        """Build the image for a job type (POST /job-types/{name}/build)."""
        return self._request("POST", f"/job-types/{name}/build")


# Default client instance
_default_client: Optional[TakoVM] = None


def configure(
    base_url: str = "http://localhost:8000",
    timeout: int = 30,
    headers: Optional[Dict[str, str]] = None,
    session: Optional[requests.Session] = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    correlation_id: Optional[str] = None,
) -> None:
    """
    Configure the default tako_vm client used by the module-level helpers.

    Accepts the same arguments as :class:`TakoVM`. Call this once at startup
    and every module-level helper (``send``, ``submit``, ``get_result``, ...)
    delegates to the configured client.

    Args:
        base_url: URL of the code executor API.
        timeout: Default execution timeout in seconds.
        headers: Headers forwarded on every request (e.g. for authentication).
        session: Optional preconfigured ``requests.Session`` (retries, mTLS,
            proxies, pooling). When omitted, a pooled GET-retry session is built.
        connect_timeout: HTTP connect timeout in seconds.
        correlation_id: Fixed correlation id to send on every request.
    """
    global _default_client
    _default_client = TakoVM(
        base_url=base_url,
        timeout=timeout,
        headers=headers,
        session=session,
        connect_timeout=connect_timeout,
        correlation_id=correlation_id,
    )


def _get_client() -> TakoVM:
    """Get the default client, creating one if necessary."""
    global _default_client
    if _default_client is None:
        _default_client = TakoVM()
    return _default_client


# --------------------------------------------------------------------------- #
# Module-level convenience helpers
#
# Each delegates to the default client (configured via ``configure()``) and
# mirrors the signature of the corresponding ``TakoVM`` method, so the flat
# ``tako_vm.<fn>`` API stays at full parity with the class.
# --------------------------------------------------------------------------- #


def send(
    func: Callable[[InputT], OutputT],
    input_data: InputT,
    timeout: Optional[int] = None,
    job_type: Optional[str] = None,
    requirements: Optional[List[str]] = None,
    *,
    startup_timeout: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> OutputT:
    """
    Execute a typed function in an isolated container (default client).

    Example:
        @dataclass
        class Input:
            x: int
            y: int

        @dataclass
        class Output:
            result: int

        def add(input: Input) -> Output:
            return Output(result=input.x + input.y)

        result = tako_vm.send(add, Input(1, 2))
        print(result.result)  # 3
    """
    return _get_client().send(
        func,
        input_data,
        timeout=timeout,
        job_type=job_type,
        requirements=requirements,
        startup_timeout=startup_timeout,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


def send_raw(
    func: Callable[[InputT], OutputT],
    input_data: InputT,
    timeout: Optional[int] = None,
    job_type: Optional[str] = None,
    requirements: Optional[List[str]] = None,
    *,
    startup_timeout: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> ExecutionResult:
    """
    Execute a typed function and return the raw result (default client).

    Same as send() but returns ExecutionResult instead of raising on failure.
    """
    return _get_client().send_raw(
        func,
        input_data,
        timeout=timeout,
        job_type=job_type,
        requirements=requirements,
        startup_timeout=startup_timeout,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


# --- Async job lifecycle ---------------------------------------------------- #


def submit(
    func: Callable[[InputT], OutputT],
    input_data: InputT,
    *,
    timeout: Optional[int] = None,
    startup_timeout: Optional[int] = None,
    job_type: Optional[str] = None,
    requirements: Optional[List[str]] = None,
    idempotency_key: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> str:
    """Submit a typed function for asynchronous execution (default client); returns the job id."""
    return _get_client().submit(
        func,
        input_data,
        timeout=timeout,
        startup_timeout=startup_timeout,
        job_type=job_type,
        requirements=requirements,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


def submit_code(
    code: str,
    input_data: Optional[dict] = None,
    *,
    timeout: Optional[int] = None,
    startup_timeout: Optional[int] = None,
    job_type: Optional[str] = None,
    requirements: Optional[List[str]] = None,
    idempotency_key: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> str:
    """Submit raw code for asynchronous execution (default client); returns the job id."""
    return _get_client().submit_code(
        code,
        input_data,
        timeout=timeout,
        startup_timeout=startup_timeout,
        job_type=job_type,
        requirements=requirements,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


def get_status(job_id: str) -> dict:
    """Get an async job's status (default client)."""
    return _get_client().get_status(job_id)


def get_result(job_id: str, *, timeout: Optional[int] = None, view: Optional[str] = None) -> dict:
    """Get an async job's result, waiting up to ``timeout`` seconds (default client)."""
    return _get_client().get_result(job_id, timeout=timeout, view=view)


def cancel(job_id: str) -> dict:
    """Cancel a queued or running job (default client)."""
    return _get_client().cancel(job_id)


def rerun(job_id: str, *, job_type: Optional[str] = None, timeout: Optional[int] = None) -> str:
    """Re-run a previous job with the same code/input (default client); returns the new job id."""
    return _get_client().rerun(job_id, job_type=job_type, timeout=timeout)


def fork(
    job_id: str,
    code: str,
    *,
    job_type: Optional[str] = None,
    timeout: Optional[int] = None,
) -> str:
    """Re-run a previous job's input with new code (default client); returns the new job id."""
    return _get_client().fork(job_id, code, job_type=job_type, timeout=timeout)


def download_artifact(job_id: str, artifact_name: str) -> bytes:
    """Download a job artifact's bytes (default client)."""
    return _get_client().download_artifact(job_id, artifact_name)


# --- Execution history & metadata ------------------------------------------- #


def list_executions(
    *,
    status: Optional[str] = None,
    job_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    view: Optional[str] = None,
) -> dict:
    """List execution records (default client); returns a paginated response."""
    return _get_client().list_executions(
        status=status, job_type=job_type, limit=limit, offset=offset, view=view
    )


def get_execution(execution_id: str, *, view: Optional[str] = None) -> dict:
    """Get a single execution record (default client)."""
    return _get_client().get_execution(execution_id, view=view)


def pool_stats() -> dict:
    """Worker pool statistics (default client)."""
    return _get_client().pool_stats()


def dlq_stats() -> dict:
    """Dead-letter-queue statistics (default client)."""
    return _get_client().dlq_stats()


def health() -> dict:
    """Check API health status (default client)."""
    return _get_client().health()


def list_job_types() -> list:
    """List available job types (default client)."""
    return _get_client().list_job_types()


def get_job_type(name: str) -> dict:
    """Get a specific job type by name (default client)."""
    return _get_client().get_job_type(name)


def build_job_type(name: str) -> dict:
    """Build the image for a job type (default client)."""
    return _get_client().build_job_type(name)
