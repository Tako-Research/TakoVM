---
description: "Handle errors in Tako VM: error response format, timeouts, out-of-memory, and the built-in retry and resilience features."
---

# Error Handling

This guide covers handling various error scenarios in Tako VM, including the built-in resilience features.

## Error Response Format

When execution fails, the response includes error details:

```json
{
  "success": false,
  "output": null,
  "stdout": "",
  "stderr": "Traceback (most recent call last):\n...",
  "exit_code": 1,
  "error": "ZeroDivisionError: division by zero",
  "execution_time": 0.15
}
```

For async jobs, the `ExecutionRecord` includes structured error information:

```json
{
  "execution_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "failed",
  "error": {
    "type": "division_error",
    "message": "Division by zero"
  }
}
```

## Error Classification

Tako VM automatically classifies errors into specific types for easier handling:

### Signal-Based Errors (Exit Codes)

| Exit Code | Error Type | Description |
|-----------|------------|-------------|
| 137 | `oom` | Out of memory (SIGKILL) |
| 143 | `cancelled` | Process terminated (SIGTERM) |
| 139 | `segfault` | Segmentation fault (SIGSEGV) |
| 134 | `abort` | Process aborted (SIGABRT) |
| 136 | `arithmetic_error` | Floating point exception (SIGFPE) |
| 135 | `bus_error` | Bus error (SIGBUS) |
| 141 | `pipe_error` | Broken pipe (SIGPIPE) |

### Python Error Types

| Error Type | Python Exception | Description |
|------------|------------------|-------------|
| `syntax_error` | SyntaxError, IndentationError | Invalid Python syntax |
| `import_error` | ImportError, ModuleNotFoundError | Missing module |
| `type_error` | TypeError | Type mismatch |
| `value_error` | ValueError | Invalid value |
| `key_error` | KeyError | Missing dictionary key |
| `index_error` | IndexError | List index out of range |
| `attribute_error` | AttributeError | Missing attribute |
| `name_error` | NameError | Undefined variable |
| `file_not_found` | FileNotFoundError | File doesn't exist |
| `recursion_error` | RecursionError | Stack overflow |
| `division_error` | ZeroDivisionError | Division by zero |
| `encoding_error` | UnicodeError | Encoding/decoding failure |
| `json_error` | JSONDecodeError | Invalid JSON |

### System Errors

| Error Type | Description |
|------------|-------------|
| `permission` | Permission denied |
| `os_error` | OS/IO error |
| `network_error` | Connection failed |
| `network_timeout` | Network request timed out |
| `timeout` | Execution exceeded time limit |
| `killed` | Process killed by system |
| `service_unavailable` | Docker circuit breaker open |
| `internal_error` | Tako VM internal error |

## Built-in Resilience Features

### Circuit Breaker

Tako VM includes a circuit breaker that prevents cascading failures when Docker is unavailable:

```
GET /health
```

```json
{
  "status": "healthy",
  "docker_available": true,
  "circuit_breaker": {
    "state": "closed",
    "failure_count": 0,
    "success_count": 5,
    "last_failure": null
  }
}
```

**Circuit States:**

| State | Description |
|-------|-------------|
| `closed` | Normal operation, requests pass through |
| `open` | Docker failing, requests rejected immediately |
| `half_open` | Testing recovery, limited requests allowed |

When the circuit is open, jobs return immediately with:
```json
{
  "error": {
    "type": "service_unavailable",
    "message": "Service temporarily unavailable"
  }
}
```

### Automatic Retry

Tako VM automatically retries transient Docker failures with exponential backoff:

- Max attempts: 2
- Base delay: 1 second
- Exponential backoff with jitter

Transient errors that trigger retry:
- Circuit breaker open
- Docker daemon connection issues
- Temporary resource unavailability

### Dead Letter Queue (DLQ)

Jobs that fail with internal errors are automatically added to a dead letter queue for investigation:

```bash
# View DLQ statistics
curl http://localhost:8000/dlq/stats
```

```json
{
  "total": 3,
  "by_error_type": {
    "internal_error": 2,
    "service_unavailable": 1
  }
}
```

```bash
# List DLQ entries
curl http://localhost:8000/dlq

# Remove processed entry
curl -X DELETE http://localhost:8000/dlq/1
```

### Correlation IDs

All requests are assigned a correlation ID for distributed tracing:

```bash
# Pass your own correlation ID
curl -H "X-Correlation-ID: my-trace-123" http://localhost:8000/execute/async ...
```

The correlation ID appears in:
- Response headers (`X-Correlation-ID`)
- Log messages
- DLQ entries

## Common Errors

### Syntax Errors

```python
response = requests.post("http://localhost:8000/execute", json={
    "code": "def broken(",  # Invalid syntax
    "input_data": {}
})

result = response.json()
# success: false
# stderr: "SyntaxError: unexpected EOF while parsing"
```

### Runtime Errors

```python
code = """
import json
with open('/input/data.json') as f:
    data = json.load(f)
result = 100 / data['value']  # Division by zero if value=0
"""

response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {"value": 0}
})

result = response.json()
# success: false
# stderr contains: "ZeroDivisionError: division by zero"
```

### Timeout Errors

```python
response = requests.post("http://localhost:8000/execute", json={
    "code": "import time; time.sleep(60)",
    "input_data": {},
    "timeout": 5
})

result = response.json()
# success: false
# error: "Execution timeout exceeded (5s)"
```

### Import Errors

```python
code = """
import pandas  # Not available in 'default' environment
"""

response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {},
    "job_type": "default"
})

result = response.json()
# success: false
# stderr: "ModuleNotFoundError: No module named 'pandas'"
```

**Solution**: Use the correct environment:

```python
response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {},
    "job_type": "data-processing"  # Has pandas
})
```

## Handling Errors in Python

```python
def execute_safely(code, input_data, **kwargs):
    """Execute code with error handling."""
    try:
        response = requests.post(
            "http://localhost:8000/execute",
            json={"code": code, "input_data": input_data, **kwargs},
            timeout=kwargs.get("timeout", 30) + 10  # HTTP timeout
        )
        response.raise_for_status()

    except requests.exceptions.Timeout:
        return {"success": False, "error": "HTTP request timed out"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Cannot connect to Tako VM"}
    except requests.exceptions.HTTPError as e:
        return {"success": False, "error": f"HTTP error: {e}"}

    result = response.json()

    if not result["success"]:
        # Log or handle the error
        print(f"Execution failed: {result.get('error')}")
        print(f"Stderr: {result.get('stderr')}")

    return result
```

## Handling Errors by Type

```python
def handle_execution_result(result):
    """Handle execution result based on error type."""
    if result.get("success"):
        return result["output"]

    error = result.get("error", {})
    error_type = error.get("type") if isinstance(error, dict) else None

    # Permanent errors - don't retry
    if error_type in ["syntax_error", "import_error", "name_error"]:
        raise ValueError(f"Code error: {error}")

    # Resource errors - might need adjustment
    if error_type in ["oom", "timeout"]:
        raise ResourceError(f"Resource limit exceeded: {error}")

    # Transient errors - safe to retry
    if error_type in ["service_unavailable", "network_error"]:
        raise RetryableError(f"Temporary failure: {error}")

    # Unknown error
    raise RuntimeError(f"Execution failed: {error}")
```

## Memory Errors (OOM)

When code exceeds memory limits:

```json
{
  "success": false,
  "exit_code": 137,
  "error": {
    "type": "oom",
    "message": "Execution exceeded memory limit"
  }
}
```

Exit code 137 indicates the process was killed (OOM).

## Validating Input

Prevent errors by validating before execution:

```python
def validate_request(code, input_data, timeout=None):
    """Validate execution request."""
    errors = []

    # Check code
    if not code or not code.strip():
        errors.append("Code cannot be empty")

    if len(code) > 100_000:  # 100KB limit
        errors.append("Code exceeds size limit")

    # Check input
    try:
        json.dumps(input_data)
    except (TypeError, ValueError) as e:
        errors.append(f"Input not JSON serializable: {e}")

    # Check timeout
    if timeout is not None:
        if timeout < 1 or timeout > 300:
            errors.append("Timeout must be between 1 and 300 seconds")

    return errors

# Usage
errors = validate_request(code, input_data)
if errors:
    print(f"Validation failed: {errors}")
else:
    result = execute(code, input_data)
```

## Retrying Failed Jobs

```python
import time

def execute_with_retry(code, input_data, max_retries=3, **kwargs):
    """Execute with automatic retry on transient failures."""

    # Error types that are safe to retry
    RETRYABLE_ERRORS = {"service_unavailable", "network_error", "network_timeout"}
    # Error types that should not be retried
    PERMANENT_ERRORS = {"syntax_error", "import_error", "name_error", "type_error"}

    for attempt in range(max_retries):
        result = execute_safely(code, input_data, **kwargs)

        if result["success"]:
            return result

        # Check error type
        error = result.get("error", {})
        error_type = error.get("type") if isinstance(error, dict) else None

        # Don't retry permanent failures
        if error_type in PERMANENT_ERRORS:
            return result

        # Only retry transient failures
        if error_type not in RETRYABLE_ERRORS:
            return result

        # Retry with exponential backoff
        if attempt < max_retries - 1:
            wait = 2 ** attempt  # 1, 2, 4 seconds
            print(f"Retrying in {wait}s (attempt {attempt + 2}/{max_retries})...")
            time.sleep(wait)

    return result
```

## Monitoring Error Rates

Use the health and DLQ endpoints to monitor system health:

```python
def check_system_health():
    """Check Tako VM system health."""
    health = requests.get("http://localhost:8000/health").json()
    dlq = requests.get("http://localhost:8000/dlq/stats").json()

    issues = []

    if health["status"] != "healthy":
        issues.append(f"System degraded: {health['circuit_breaker']['state']}")

    if health["circuit_breaker"]["state"] == "open":
        issues.append("Circuit breaker open - Docker issues")

    if dlq["total"] > 10:
        issues.append(f"High DLQ count: {dlq['total']} failed jobs")

    return issues
```

## Debugging Tips

1. **Check stdout/stderr** - Often contains stack traces
2. **Use print statements** - Debug output is captured
3. **Test locally first** - Run code outside Tako VM
4. **Check environment** - Ensure packages are available
5. **Review limits** - Memory, CPU, timeout settings
6. **Check correlation ID** - Trace requests through logs
7. **Monitor circuit breaker** - Check Docker health
8. **Review DLQ** - Investigate recurring failures

---

## Next Steps

- [Async Jobs](async-jobs.md) - Long-running task patterns
- [Environments](environments.md) - Configure job types with dependencies
- [Deployment](../deployment/how-to-deploy.md) - Deploy to production
