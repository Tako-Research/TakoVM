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

For production deployments with job queuing, persistence, and audit trails, use the HTTP server and client.

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

---

## Functions

### `tako_vm.configure()`

Configure the default client.

```python
tako_vm.configure(
    base_url="http://localhost:8000",
    timeout=30
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | str | `"http://localhost:8000"` | Server URL |
| `timeout` | int | 30 | Default execution timeout |

---

### `tako_vm.send()`

Execute a typed function and return the result.

```python
result = tako_vm.send(func, input_data, timeout=None, job_type=None)
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `func` | Callable | Yes | Function with type hints |
| `input_data` | dataclass | Yes | Input dataclass instance |
| `timeout` | int | No | Timeout in seconds |
| `job_type` | str | No | Environment name |

**Returns**: Output dataclass instance

**Raises**:
- `ValidationError`: Invalid input/output types
- `ExecutionError`: Execution failed

!!! important "How `send()` works"
    The function you pass to `send()` does **not** run locally. Instead:

    1. The function's source code is extracted via `inspect.getsource()`
    2. The input dataclass is serialized to JSON and written to `/input/data.json`
    3. The code is sent to the Tako VM server and executed in an isolated Docker container
    4. The output is deserialized back into your output dataclass

    This means:

    - **Do not reference local variables** outside the function — they won't exist in the container
    - **Imports must be inside the function** or available in the container's Python environment
    - **The return type annotation** determines how `/output/result.json` is parsed back

---

### `tako_vm.send_raw()`

Execute a function and return raw result (doesn't raise on failure).

```python
result = tako_vm.send_raw(func, input_data, timeout=None, job_type=None)
```

**Returns**: `ExecutionResult` object

---

### `tako_vm.list_job_types()`

List available environments.

```python
job_types = tako_vm.list_job_types()
for jt in job_types:
    print(jt["name"], jt["requirements"])
```

---

### `tako_vm.get_job_type()`

Get a specific environment.

```python
jt = tako_vm.get_job_type("data-processing")
print(jt["memory_limit"])  # "1g"
```

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
