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
"""

import inspect
import textwrap
from dataclasses import MISSING, asdict, dataclass, fields, is_dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, cast, get_type_hints

import requests

# Type variables for generic typing
InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


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


class TakoVMError(Exception):
    """Base exception for tako_vm errors."""


class SDKExecutionError(TakoVMError):
    """Raised when code execution fails via the SDK."""

    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


# Backward compatibility alias
ExecutionError = SDKExecutionError


class ValidationError(TakoVMError):
    """Raised when input/output validation fails."""


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
                mTLS, proxies, pooling). A fresh session is created otherwise.
        """
        self.base_url = base_url.rstrip("/")
        self.default_timeout = timeout
        self._headers = dict(headers) if headers else {}
        self._session = session or requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        expect_json: bool = True,
        http_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Any:
        """Issue a request, forwarding the caller-supplied auth headers."""
        merged = {**self._headers, **kwargs.pop("headers", {})}
        response = self._session.request(
            method,
            f"{self.base_url}{path}",
            headers=merged or None,
            timeout=http_timeout if http_timeout is not None else self.default_timeout + 10,
            **kwargs,
        )
        response.raise_for_status()
        return response.json() if expect_json else response

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
        )

        if not result.success:
            raise ExecutionError(
                result.error or "Execution failed", stdout=result.stdout, stderr=result.stderr
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
        )

        if result.success and result.output:
            try:
                result.output = output_cls(**cast(Dict[str, Any], result.output))
            except (TypeError, ValueError, KeyError):
                # Keep as dict if deserialization fails (type mismatch, missing fields, etc.)
                pass

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
    ) -> ExecutionResult:
        """Execute code synchronously via POST /execute."""
        payload = self._execute_payload(
            code,
            input_data,
            timeout=timeout,
            job_type=job_type,
            requirements=requirements,
            startup_timeout=startup_timeout,
            idempotency_key=idempotency_key,
        )
        try:
            data = self._request(
                "POST",
                "/execute",
                json=payload,
                http_timeout=(timeout or self.default_timeout) + 10,
            )
        except requests.exceptions.RequestException as e:
            return ExecutionResult(
                success=False,
                output=None,
                execution_time=0,
                stdout="",
                stderr="",
                error=f"Request failed: {e}",
            )

        return ExecutionResult(
            success=data.get("success", False),
            output=data.get("output"),
            execution_time=data.get("execution_time", 0),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            error=data.get("error"),
            job_type=data.get("job_type"),
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
    ) -> str:
        """Submit raw code for asynchronous execution; returns the job id."""
        payload = self._execute_payload(
            code,
            input_data or {},
            timeout=timeout,
            job_type=job_type,
            requirements=requirements,
            startup_timeout=startup_timeout,
            idempotency_key=idempotency_key,
        )
        return self._request("POST", "/execute/async", json=payload)["job_id"]

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
        )

    def get_status(self, job_id: str) -> dict:
        """Get an async job's status (GET /jobs/{job_id})."""
        return self._request("GET", f"/jobs/{job_id}")

    def get_result(
        self, job_id: str, *, timeout: Optional[int] = None, view: Optional[str] = None
    ) -> dict:
        """
        Get an async job's result, waiting up to ``timeout`` seconds for it
        (GET /jobs/{job_id}/result). Pass ``view="full"`` for artifacts,
        resource usage, hashes, and lineage.
        """
        params: Dict[str, Any] = {}
        if timeout is not None:
            params["timeout"] = timeout
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
        return self._request("POST", f"/jobs/{job_id}/rerun", json=body)["job_id"]

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
        return self._request("POST", f"/jobs/{job_id}/fork", json=body)["job_id"]

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
) -> None:
    """
    Configure the default tako_vm client used by the module-level helpers.

    Args:
        base_url: URL of the code executor API.
        timeout: Default execution timeout in seconds.
        headers: Headers forwarded on every request (e.g. for authentication).
    """
    global _default_client
    _default_client = TakoVM(base_url=base_url, timeout=timeout, headers=headers)


def _get_client() -> TakoVM:
    """Get the default client, creating one if necessary."""
    global _default_client
    if _default_client is None:
        _default_client = TakoVM()
    return _default_client


def send(
    func: Callable[[InputT], OutputT],
    input_data: InputT,
    timeout: Optional[int] = None,
    job_type: Optional[str] = None,
    requirements: Optional[List[str]] = None,
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
        func, input_data, timeout=timeout, job_type=job_type, requirements=requirements
    )


def send_raw(
    func: Callable[[InputT], OutputT],
    input_data: InputT,
    timeout: Optional[int] = None,
    job_type: Optional[str] = None,
    requirements: Optional[List[str]] = None,
) -> ExecutionResult:
    """
    Execute a typed function and return the raw result (default client).

    Same as send() but returns ExecutionResult instead of raising on failure.
    """
    return _get_client().send_raw(
        func, input_data, timeout=timeout, job_type=job_type, requirements=requirements
    )


def list_job_types() -> list:
    """List available job types (default client)."""
    return _get_client().list_job_types()


def get_job_type(name: str) -> dict:
    """Get a specific job type by name (default client)."""
    return _get_client().get_job_type(name)
