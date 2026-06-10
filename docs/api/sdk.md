---
description: "Tako VM Python SDK reference — the TakoVM HTTP client (server mode) and the Sandbox class (library mode) for running code in isolated containers."
---

# Python SDK Reference

The Tako VM Python SDK provides two ways to execute code:

1. **TakoVM Client (Server Mode)** - HTTP client for the Tako VM server
2. **Sandbox (Library Mode)** - Direct execution without a server

## Installation

```bash
pip install "tako-vm[server]"
tako-vm setup                   # pull the executor Docker image
tako-vm server                  # start server (auto-starts PostgreSQL via Docker)
```

---

## TakoVM Client (Server Mode)

For deployments with job queuing, persistence, and audit trails, use the HTTP server and the `TakoVM` client.

### Quick Start

```python
from dataclasses import dataclass
import tako_vm

tako_vm.configure("http://localhost:8000")

@dataclass
class Input:
    x: int
    y: int

@dataclass
class Output:
    result: int

def add(input: Input) -> Output:
    return Output(result=input.x + input.y)

result = tako_vm.send(add, Input(10, 20))
print(result.result)  # 30
```

The module-level helpers use a default client configured via `tako_vm.configure()`, and mirror the full `TakoVM` surface — typed execution (`send`, `send_raw`), the async job lifecycle (`submit`, `submit_code`, `get_status`, `get_result`, `cancel`, `rerun`, `fork`, `download_artifact`), history (`list_executions`, `get_execution`), and metadata (`list_job_types`, `get_job_type`, `build_job_type`, `pool_stats`, `dlq_stats`, `health`). `configure()` accepts the same arguments as `TakoVM` (`headers`, `session`, `connect_timeout`, `correlation_id`). Instantiate `TakoVM` directly when you need multiple independently-configured clients in one process.

```python
import tako_vm

tako_vm.configure("http://localhost:8000", headers={"X-API-Key": KEY})

job_id = tako_vm.submit(add, Input(10, 20))
result = tako_vm.get_result(job_id, timeout=30)
```

### The `TakoVM` client

```python
from tako_vm import TakoVM

client = TakoVM(
    base_url="http://localhost:8000",
    timeout=30,
    headers={"X-API-Key": "..."},  # forwarded on every request
    session=None,                   # optional preconfigured requests.Session
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | str | `"http://localhost:8000"` | Server URL |
| `timeout` | int | 30 | Default execution timeout (seconds) |
| `headers` | dict | `None` | Headers forwarded on every request |
| `session` | requests.Session | `None` | Preconfigured session (retries, mTLS, proxies, pooling) |

### Authentication

The SDK does **not** implement authentication — it forwards whatever headers you give it, verbatim, on every request. Supply the credential your deployment requires (the server's API key, a bearer token from your gateway, etc.), or use a `session` for transport-level auth (mTLS).

```python
# API key (matches the server's api_auth_header, default "X-API-Key")
client = TakoVM("https://tako.internal", headers={"X-API-Key": API_KEY})

# Bearer token
client = TakoVM("https://tako.internal", headers={"Authorization": f"Bearer {token}"})

# mTLS / custom transport
import requests
sess = requests.Session(); sess.cert = ("client.crt", "client.key")
client = TakoVM("https://tako.internal", session=sess)
```

`tako_vm.configure(..., headers=..., session=...)` does the same for the module-level helpers.

### Reliability

The client ships with a reliability layer; all of it is automatic:

- **Transport retries (GETs only).** The default session retries idempotent GETs (status/result polling, job types, health) on 502/503/504 with backoff. POSTs are never retried at the transport layer — the sync `POST /execute` is not idempotent, so a blind retry could run code twice. Passing your own `session=` replaces this adapter.
- **Retry-safe async submission.** `submit()`/`submit_code()` auto-generate a server-compatible `idempotency_key` when you don't pass one and retry transient failures (connection errors, 502/503/504) with the *same* key, so the server returns the existing job instead of re-executing.
- **Correlation IDs.** Every request carries an `X-Correlation-ID` header (auto-generated, or fixed via `TakoVM(correlation_id=...)`). The id is exposed as `.correlation_id` on `ExecutionResult` and on SDK exceptions for end-to-end tracing.
- **Structured errors.** HTTP failures raise `TransportError` (connection/timeout), `ServerError` (5xx, with `.retryable` set for 502/503/504), or `ClientError` (4xx) — all subclasses of `TakoVMError` carrying `.status_code`, `.detail` (parsed from the server's error body), and `.correlation_id`. The sync `send()`/`send_raw()` path keeps its legacy contract and reports these as a failed `ExecutionResult` instead of raising them.
- **Client-side timeouts that outlive the server.** When `timeout` is omitted, the HTTP read timeout is resolved from the job type's default (`GET /job-types/{name}`, cached per client) plus the startup budget, with a generous fallback — so long-running jobs are never killed client-side while they would still complete server-side. If the job-type lookup fails, the client logs a warning and falls back to the maximum read timeout.
- **Verbose on failure.** Every failure path logs at `WARNING`/`ERROR` on the `tako_vm.sdk.client` logger with the correlation id, status, and server detail. A non-JSON 2xx body and an output that doesn't fit the return dataclass are both logged and surfaced (a failed `ExecutionResult` and the raw dict, respectively) rather than swallowed silently. Enable it with `logging.getLogger("tako_vm.sdk.client").setLevel(logging.DEBUG)`.

---

## Synchronous execution

### `send()` / `send_raw()`

Execute a typed function and wait for the result. `send()` returns the output dataclass (raising `ExecutionError` on failure); `send_raw()` returns an `ExecutionResult` and never raises on execution failure.

```python
result = client.send(
    func, input_data,
    timeout=None,           # execution timeout (s)
    job_type=None,          # job type name (default: "default")
    requirements=None,      # runtime packages, e.g. ["pandas", "numpy>=1.20"]
    startup_timeout=None,   # startup phase timeout (container + deps)
    idempotency_key=None,   # client key for idempotent submission
)
```

**Raises**: `ValidationError` (invalid input/output types), `ExecutionError` (`send` only).

!!! important "How `send()` works"
    The function you pass does **not** run locally. Its source is extracted via `inspect.getsource()`, the input dataclass is written to `/input/data.json`, the code runs in an isolated container, and `/output/result.json` is parsed back into your output dataclass. Therefore: **don't reference local variables** outside the function, **put imports inside** the function (or in the job type's image), and the **return annotation** drives output parsing.

---

## Async jobs

Submit work without blocking, then poll or wait for the result.

```python
job_id = client.submit(func, input_data, requirements=["pandas"])   # typed
job_id = client.submit_code("print(1)", {"x": 1})                   # raw code

client.get_status(job_id)                       # {"status": "running", ...}
record = client.get_result(job_id, timeout=60)  # waits up to 60s; view="full" for artifacts
client.cancel(job_id)                           # cancel a queued/running job

new_id = client.rerun(job_id)                   # re-run same code+input
new_id = client.fork(job_id, "print(2)")        # re-run the input with new code

data = client.download_artifact(job_id, "plot.png")   # -> bytes
```

| Method | Endpoint | Returns |
|--------|----------|---------|
| `submit(func, input, ...)` / `submit_code(code, input_data, ...)` | `POST /execute/async` | `job_id` (str) |
| `get_status(job_id)` | `GET /jobs/{id}` | status dict |
| `get_result(job_id, timeout=, view=)` | `GET /jobs/{id}/result` | execution record |
| `cancel(job_id)` | `POST /jobs/{id}/cancel` | cancel dict |
| `rerun(job_id, job_type=, timeout=)` | `POST /jobs/{id}/rerun` | new `job_id` |
| `fork(job_id, code, job_type=, timeout=)` | `POST /jobs/{id}/fork` | new `job_id` |
| `download_artifact(job_id, name)` | `GET /jobs/{id}/artifacts/{name}` | `bytes` |

---

## Execution history & metadata

```python
page = client.list_executions(status="succeeded", job_type=None, limit=50, offset=0, view=None)
record = client.get_execution(execution_id, view="full")

client.list_job_types()              # available job types
client.get_job_type("data-processing")
client.build_job_type("data-processing")   # build its image

client.health()                      # server health
client.pool_stats()                  # worker pool stats
client.dlq_stats()                   # dead-letter-queue stats
```

`list_executions()` returns a paginated response (`{"items": [...], "limit", "offset", "has_more", "count"}`).

!!! note "Sessions"
    A long-lived **session** API is in development and not yet part of this client. Until it lands, drive any session endpoints through `client._request(...)` or raw HTTP.

---

## Sandbox (Library Mode)

The `Sandbox` class provides direct code execution without running a server. Useful for development, testing, and simple scripts.

### Quick Start

```python
from tako_vm import Sandbox

with Sandbox() as sb:
    result = sb.run("print(1 + 1)")
    print(result.stdout)  # "2"
```

### With Dependencies

```python
from tako_vm import Sandbox

with Sandbox() as sb:
    result = sb.run("""
import pandas as pd
print(pd.__version__)
""", requirements=["pandas"])
    print(result.stdout)
```

### With Local Packages

```python
from tako_vm import Sandbox

# Mount local packages into the sandbox
sb = Sandbox(package_dirs=["./my_utils"])
result = sb.run("from my_utils import helper; helper.process()")
```

### Sandbox Class

```python
from tako_vm import Sandbox

sandbox = Sandbox(
    image="code-executor:latest",  # Docker image
    timeout=30,                     # Default timeout
    memory_limit="512m",            # Memory limit
    cpu_limit=1.0,                  # CPU limit
    network_enabled=False,          # Allow network access
    package_dirs=[],                # Local packages to mount
    auto_build=True,                # Auto-build image if missing
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `image` | str | `"code-executor:latest"` | Docker image to use |
| `timeout` | int | 30 | Default timeout in seconds |
| `memory_limit` | str | `"512m"` | Container memory limit |
| `cpu_limit` | float | 1.0 | CPU limit |
| `network_enabled` | bool | False | Allow network access |
| `package_dirs` | list | `[]` | Local directories to mount as Python packages |
| `auto_build` | bool | True | Build image automatically if missing |

### sandbox.run()

Execute code in the sandbox.

```python
result = sandbox.run(
    code,                    # Python code to execute
    input_data=None,         # Dict available at /input/data.json
    timeout=None,            # Override default timeout
    requirements=None,       # Packages to install (e.g., ["pandas"])
)
```

**Returns**: `SandboxResult`

### SandboxResult

```python
@dataclass
class SandboxResult:
    stdout: str              # Standard output
    stderr: str              # Standard error
    exit_code: int           # Exit code (0 = success)
    success: bool            # Whether execution succeeded
    output: Optional[dict]   # Parsed /output/result.json
    error: Optional[str]     # Error message if failed
    duration_ms: Optional[int]  # Execution time in ms
```
