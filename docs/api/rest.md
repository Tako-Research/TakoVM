# REST API Reference

Complete reference for the Tako VM HTTP API.

## Quick Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/execute` | POST | **Sync** - Execute code, block until complete |
| `/execute/async` | POST | **Async** - Submit job, return `job_id` immediately |
| `/jobs/{id}` | GET | Get job status |
| `/jobs/{id}/result` | GET | Get result (add `?wait=true` to block) |
| `/jobs/{id}/artifacts/{name}` | GET | Download output file |
| `/jobs/{id}/cancel` | POST | Cancel running job |
| `/jobs/{id}/rerun` | POST | Re-execute with same code/inputs |
| `/jobs/{id}/fork` | POST | Re-execute with modified code |
| `/health` | GET | Health check with queue stats |

## Base URL

```text
http://localhost:8000
```

## Headers

### Request Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | `application/json` for all POST requests |
| `X-Correlation-ID` | Optional. Pass your own correlation ID for tracing |

### Response Headers

| Header | Description |
|--------|-------------|
| `X-Correlation-ID` | Correlation ID for request tracing |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |

---

## Execute Code (Sync)

Execute code and wait for the result.

```http
POST /execute
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | Yes | Python code to execute |
| `input_data` | object | No | Input data (available at `/input/data.json`, defaults to `{}`) |
| `timeout` | integer | No | Code execution timeout in seconds (default: 30). Does not include container startup or dependency installation time. |
| `startup_timeout` | integer | No | Container startup timeout in seconds (10-600, default: 60). Includes container creation and dependency installation. |
| `job_type` | string | No | Environment name (default: "default") |
| `requirements` | array | No | Python packages to install at runtime (e.g., `["pandas", "numpy>=1.20"]`) |

### Example Request

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import pandas as pd; print(pd.__version__)",
    "input_data": {},
    "requirements": ["pandas"]
  }'
```

### Example with Custom Timeouts

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import tensorflow as tf; print(tf.__version__)",
    "requirements": ["tensorflow"],
    "startup_timeout": 300,
    "timeout": 60
  }'
```

This example sets a 5-minute startup timeout (for installing TensorFlow) and a 1-minute code execution timeout.

### Example with Input/Output

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import json\nwith open(\"/input/data.json\") as f: d=json.load(f)\nwith open(\"/output/result.json\",\"w\") as f: json.dump({\"sum\":d[\"a\"]+d[\"b\"]},f)",
    "input_data": {"a": 10, "b": 20},
    "timeout": 30,
    "job_type": "default"
  }'
```

### Response

```json
{
  "success": true,
  "output": {"sum": 30},
  "stdout": "",
  "stderr": "",
  "exit_code": 0,
  "execution_time": 0.35,
  "error": null,
  "job_type": "default"
}
```

!!! note
    The sync `/execute` endpoint returns a simpler response without detailed timing. For detailed timing breakdowns, use the async endpoint and fetch results with `?view=full`.

---

## Execute Code (Async)

Submit code for execution and return immediately.

```http
POST /execute/async
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | Yes | Python code to execute |
| `input_data` | object | No | Input data (available at `/input/data.json`, defaults to `{}`) |
| `timeout` | integer | No | Code execution timeout in seconds (default: 30). Does not include container startup or dependency installation time. |
| `startup_timeout` | integer | No | Container startup timeout in seconds (10-600, default: 60). Includes container creation and dependency installation. |
| `job_type` | string | No | Environment name (default: "default") |
| `requirements` | array | No | Python packages to install at runtime (e.g., `["pandas", "numpy>=1.20"]`) |
| `idempotency_key` | string | No | Unique key for idempotent submission |

### Idempotency

When `idempotency_key` is provided:

- If the key was never used before, the job is submitted normally
- If the key was used with the **same** payload, returns the existing job
- If the key was used with a **different** payload, returns `409 Conflict`

This ensures safe retries without duplicate job execution.

### Response

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

### Response (Idempotent Duplicate)

If the same `idempotency_key` and payload are submitted again, the existing job is returned:

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "succeeded"
}
```

The status will reflect the current state of the existing job (e.g., `queued`, `running`, `succeeded`).

---

## Get Job Status

Get the current status of a job.

```http
GET /jobs/{job_id}
```

### Response

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running",
  "created_at": "2024-01-15T10:30:00Z",
  "started_at": "2024-01-15T10:30:01Z"
}
```

### Status Values

There are two status systems. When you submit an async job, the **queue** tracks it as `pending` → `running` → `completed`. The **execution record** uses more granular statuses:

| Status | Where | Description |
|--------|-------|-------------|
| `pending` | Queue | Waiting for a worker to pick up the job |
| `running` | Both | Currently executing |
| `queued` | Record | Job created and persisted, not yet started |
| `succeeded` | Record | Completed successfully |
| `failed` | Record | Failed with error |
| `timeout` | Record | Exceeded time limit |
| `oom` | Record | Out of memory |
| `cancelled` | Record | Cancelled by user |

When polling `/jobs/{id}`, you'll typically see: `queued` → `running` → `succeeded`/`failed`/`timeout`/`oom`.

---

## Get Job Result

Wait for job completion and return the result.

```http
GET /jobs/{job_id}/result
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `wait` | boolean | false | Wait for completion |
| `timeout` | integer | 30 | Max seconds to wait (if wait=true) |
| `view` | string | null | Response detail level (`full` for extended fields) |

### Response (Completed Job)

```json
{
  "execution_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "succeeded",
  "job_type": "default",
  "job_version": "latest",
  "created_at": "2024-01-15T10:30:00Z",
  "started_at": "2024-01-15T10:30:01Z",
  "ended_at": "2024-01-15T10:30:02Z",
  "duration_ms": 350,
  "exit_code": 0,
  "stdout": "",
  "stderr": "",
  "output": {"sum": 30},
  "error": null,
  "timing": {
    "startup_ms": 1200,
    "dep_install_ms": 0,
    "execution_ms": 350,
    "total_ms": 1550,
    "phase_at_exit": "execution"
  }
}
```

### Response (Full View)

When `?view=full` is specified, additional fields are included:

```json
{
  "execution_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "succeeded",
  "job_type": "default",
  "job_version": "latest",
  "created_at": "2024-01-15T10:30:00Z",
  "started_at": "2024-01-15T10:30:01Z",
  "ended_at": "2024-01-15T10:30:02Z",
  "duration_ms": 350,
  "exit_code": 0,
  "stdout": "",
  "stderr": "",
  "output": {"sum": 30},
  "error": null,
  "timing": {
    "startup_ms": 1200,
    "dep_install_ms": 0,
    "execution_ms": 350,
    "total_ms": 1550,
    "phase_at_exit": "execution"
  },
  "job_ref": "default@sha256:a1b2c3d4e5f6",
  "artifacts": [
    {"name": "result.json", "size_bytes": 128, "sha256": "abc123...", "content_type": "application/json"}
  ],
  "input_artifacts": [
    {"name": "_code.py", "size_bytes": 256, "sha256": "def456...", "content_type": "text/x-python"},
    {"name": "_input.json", "size_bytes": 64, "sha256": "ghi789...", "content_type": "application/json"}
  ],
  "resource_usage": {
    "max_rss_mb": 64.5,
    "cpu_time_ms": 250,
    "wall_time_ms": 350
  },
  "code_hash": "abc123...",
  "input_hash": "def456...",
  "parent_execution_id": null,
  "relationship": null,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "idempotency_key": "my-unique-key"
}
```

#### Extended Fields (view=full)

| Field | Type | Description |
|-------|------|-------------|
| `timing` | object | Detailed timing breakdown (see below) |
| `job_ref` | string | Pinned environment reference (job_type@digest) |
| `artifacts` | array | Output artifact metadata (name, size_bytes, sha256, content_type) |
| `input_artifacts` | array | Input artifact metadata (includes internal `_code.py`, `_input.json`) |
| `resource_usage` | object | Resource metrics: `max_rss_mb`, `cpu_time_ms`, `wall_time_ms` |
| `code_hash` | string | SHA-256 hash of the submitted code |
| `input_hash` | string | SHA-256 hash of the input data |
| `parent_execution_id` | string | ID of parent execution (if rerun/fork) |
| `relationship` | string | Relationship to parent (`rerun` or `fork`) |
| `stdout_truncated` | boolean | Whether stdout was truncated |
| `stderr_truncated` | boolean | Whether stderr was truncated |
| `idempotency_key` | string | The idempotency key used for submission |

#### Timing Object

| Field | Type | Description |
|-------|------|-------------|
| `startup_ms` | integer | Time spent in startup phase (container init + dep install) |
| `dep_install_ms` | integer | Time spent installing dependencies (subset of startup) |
| `execution_ms` | integer | Time spent executing user code |
| `total_ms` | integer | Total container runtime |
| `phase_at_exit` | string | Phase when container exited: `startup`, `execution`, `completed`, or `failed` |

### Response (Job In Progress)

If `wait=false` and the job is still running:

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "Job still in progress",
  "queue_position": 3
}
```

---

## Rerun Job

Rerun a previous execution with the same code and inputs.

```http
POST /jobs/{job_id}/rerun
```

### Response

```json
{
  "job_id": "661f9511-f30c-52e5-b827-557766551111",
  "status": "queued"
}
```

The new job will have:
- Same code as the original
- Same input data as the original
- Same job type and timeout settings
- Lineage tracking back to the original execution (visible in `?view=full` response)

---

## Fork Job

Fork a job with new code but the same inputs.

```http
POST /jobs/{job_id}/fork
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | Yes | New Python code to execute |

### Example Request

```bash
curl -X POST http://localhost:8000/jobs/550e8400-e29b-41d4-a716-446655440000/fork \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import json\nwith open(\"/input/data.json\") as f: d=json.load(f)\nwith open(\"/output/result.json\",\"w\") as f: json.dump({\"product\":d[\"a\"]*d[\"b\"]},f)"
  }'
```

### Response

```json
{
  "job_id": "772fa622-g41d-63f6-c938-668877662222",
  "status": "queued"
}
```

The new job will have:
- The new code provided in the request
- Same input data as the original
- Same job type and timeout settings
- Lineage tracking back to the original execution (visible in `?view=full` response)

---

## Cancel Job

Cancel a pending or running job.

```http
POST /jobs/{job_id}/cancel
```

### Response

```json
{
  "status": "cancelled",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## Get Artifact

Download an artifact from a completed job.

```http
GET /jobs/{job_id}/artifacts/{artifact_name}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `job_id` | string | The job/execution ID |
| `artifact_name` | string | Name of the artifact file |

### Response Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | MIME type of the artifact |
| `Content-Length` | Size in bytes |
| `ETag` | Content hash for caching |
| `Content-Disposition` | `attachment; filename="{artifact_name}"` |

### Conditional Requests

Supports `If-None-Match` header with ETag for conditional requests:

```bash
curl -H "If-None-Match: \"abc123...\"" \
  http://localhost:8000/jobs/550e8400.../artifacts/result.json
```

Returns `304 Not Modified` if the artifact hasn't changed.

### Example Request

```bash
curl -O http://localhost:8000/jobs/550e8400-e29b-41d4-a716-446655440000/artifacts/result.json
```

---

## Get Artifact Metadata

Get metadata about an artifact without downloading it.

```http
HEAD /jobs/{job_id}/artifacts/{artifact_name}
```

### Response Headers

Same as `GET /jobs/{job_id}/artifacts/{artifact_name}`:

| Header | Description |
|--------|-------------|
| `Content-Type` | MIME type of the artifact |
| `Content-Length` | Size in bytes |
| `ETag` | Content hash for caching |

### Example Request

```bash
curl -I http://localhost:8000/jobs/550e8400-e29b-41d4-a716-446655440000/artifacts/result.json
```

### Example Response

```http
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 128
ETag: "sha256:abc123def456..."
```

---

## List Job Types

Get available execution environments.

```http
GET /job-types
```

### Response

```json
[
  {
    "name": "default",
    "requirements": [],
    "python_version": "3.11",
    "memory_limit": "512m",
    "cpu_limit": 1.0,
    "timeout": 30,
    "image_exists": true
  },
  {
    "name": "data-processing",
    "requirements": ["pandas", "numpy"],
    "python_version": "3.11",
    "memory_limit": "1g",
    "cpu_limit": 2.0,
    "timeout": 60,
    "image_exists": true
  }
]
```

---

## Get Job Type

Get a specific environment's configuration.

```http
GET /job-types/{name}
```

### Response

```json
{
  "name": "data-processing",
  "requirements": ["pandas", "numpy"],
  "python_version": "3.11",
  "memory_limit": "1g",
  "cpu_limit": 2.0,
  "timeout": 60,
  "image_exists": true
}
```

---

## Build Job Type

Explicitly build a job type container image.

```http
POST /job-types/{name}/build
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `version_tag` | string | null | Semantic version tag (e.g., "v1.0.0") |

### Response

```json
{
  "status": "built",
  "job_type": "data-processing",
  "version": "data-processing@sha256:a1b2c3d4e5f6",
  "digest": "a1b2c3d4e5f6",
  "image_ref": "tako-vm-data-processing:latest"
}
```

---

## Health Check

Check server health status.

```http
GET /health
```

### Response

```json
{
  "status": "healthy",
  "docker_available": true,
  "gvisor_available": true,
  "circuit_breaker": {
    "state": "closed",
    "failure_count": 0,
    "success_count": 5,
    "last_failure": null
  },
  "version": "0.1.4",
  "production_mode": false,
  "queue_stats": {
    "pending": 0,
    "running": 2,
    "max_workers": 4,
    "max_queue_size": 100
  }
}
```

### Status Values

| Status | Description |
|--------|-------------|
| `healthy` | All systems operational |
| `degraded` | Docker unavailable or circuit breaker open |

### Circuit Breaker States

| State | Description |
|-------|-------------|
| `closed` | Normal operation |
| `open` | Docker failing, requests rejected |
| `half_open` | Testing recovery |

---

## Worker Pool Stats

Get worker pool statistics.

```http
GET /pool/stats
```

### Response

```json
{
  "pending": 5,
  "running": 4,
  "max_workers": 4,
  "max_queue_size": 100
}
```

---

## List Executions

List execution records with pagination.

```http
GET /executions
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | null | Filter by status |
| `job_type` | string | null | Filter by job type |
| `limit` | integer | 100 | Max records (1-1000) |
| `offset` | integer | 0 | Pagination offset |
| `view` | string | null | Response detail level (`full` for extended fields) |

### Response

Paginated response with metadata:

```json
{
  "items": [
    {
      "execution_id": "550e8400-e29b-41d4-a716-446655440000",
      "status": "succeeded",
      "job_type": "default",
      "...": "..."
    }
  ],
  "limit": 100,
  "offset": 0,
  "has_more": true,
  "count": 100
}
```

| Field | Description |
|-------|-------------|
| `items` | Array of execution records |
| `limit` | Maximum records returned |
| `offset` | Number of records skipped |
| `has_more` | Whether more records exist |
| `count` | Number of items in this response |

---

## Get Execution

Get a specific execution record.

```http
GET /executions/{execution_id}
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `view` | string | null | Response detail level (`full` for extended fields) |

### Response

Same format as job result. When `?view=full` is specified, includes all extended fields documented in the [Get Job Result](#response-full-view) section.

---

## Dead Letter Queue

### Get DLQ Statistics

Get statistics about failed jobs in the dead letter queue.

```http
GET /dlq/stats
```

### Response

```json
{
  "total": 3,
  "by_error_type": {
    "internal_error": 2,
    "service_unavailable": 1
  }
}
```

---

### List DLQ Entries

List entries in the dead letter queue.

```http
GET /dlq
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `error_type` | string | null | Filter by error type |
| `limit` | integer | 100 | Max entries (1-1000) |
| `offset` | integer | 0 | Pagination offset |

### Response

Paginated response with metadata:

```json
{
  "items": [
    {
      "id": 1,
      "job_id": "550e8400-e29b-41d4-a716-446655440000",
      "job_data": {
        "code": "...",
        "input_data": {},
        "job_type": "default"
      },
      "error_type": "internal_error",
      "error_message": "Docker daemon not responding",
      "retry_count": 0,
      "created_at": "2024-01-15T10:30:00Z",
      "client_ip": "192.168.1.100",
      "correlation_id": "abc-123-def"
    }
  ],
  "limit": 100,
  "offset": 0,
  "has_more": false,
  "count": 1
}
```

---

### Remove DLQ Entry

Remove a processed entry from the dead letter queue.

```http
DELETE /dlq/{entry_id}
```

### Response

```json
{
  "status": "removed",
  "entry_id": 1
}
```

---

## Execution Errors

When a job fails during execution, the `error` field in the response contains detailed information about the failure.

### Error Object Structure

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | The error type (see table below) |
| `message` | string | Human-readable error description |
| `phase` | string | The execution phase where the error occurred (`startup` or `execution`) |

### Error Types

#### Timeout Errors

| Type | Description |
|------|-------------|
| `timeout` | Generic execution exceeded time limit (legacy) |
| `startup_timeout` | Startup phase (dep install) exceeded time limit |
| `execution_timeout` | Code execution phase exceeded time limit |

#### Signal-Based Errors

| Type | Description |
|------|-------------|
| `oom` | Out of memory (SIGKILL/137) |
| `cancelled` | Cancelled by user (SIGTERM/143) |
| `segfault` | Segmentation fault (SIGSEGV/139) |
| `abort` | Process aborted (SIGABRT/134) |
| `arithmetic_error` | Floating point exception (SIGFPE/136) |
| `bus_error` | Bus error (SIGBUS/135) |
| `pipe_error` | Broken pipe (SIGPIPE/141) |
| `killed` | Process killed by system |

#### Python Errors

| Type | Description |
|------|-------------|
| `syntax_error` | SyntaxError, IndentationError |
| `import_error` | ImportError, ModuleNotFoundError |
| `type_error` | TypeError |
| `value_error` | ValueError |
| `key_error` | KeyError |
| `index_error` | IndexError |
| `attribute_error` | AttributeError |
| `name_error` | NameError (undefined variable) |
| `file_not_found` | FileNotFoundError |
| `file_error` | IsADirectoryError, NotADirectoryError |
| `os_error` | OSError, IOError |
| `recursion_error` | RecursionError |
| `assertion_error` | AssertionError |
| `division_error` | ZeroDivisionError |
| `overflow_error` | OverflowError |
| `encoding_error` | UnicodeError |
| `json_error` | JSONDecodeError |

#### Other Errors

| Type | Description |
|------|-------------|
| `permission` | Permission denied |
| `dependency_error` | Package installation failure (uv/pip) |
| `network_error` | ConnectionError |
| `network_timeout` | Network request timed out |
| `docker_error` | Docker image/command not found |
| `service_unavailable` | Circuit breaker open |
| `config_error` | Configuration error |
| `internal_error` | Internal Tako VM error |
| `runtime_error` | Generic runtime error |
| `unknown` | Unknown error type |

### Example: Startup Timeout Error

When dependency installation takes too long (from async job result with `?view=full`):

```json
{
  "execution_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "timeout",
  "stdout": "",
  "stderr": "Installing dependencies...",
  "exit_code": null,
  "error": {
    "type": "startup_timeout",
    "message": "Container startup timed out after 60 seconds during dependency installation",
    "phase": "startup"
  },
  "timing": {
    "startup_ms": 60000,
    "dep_install_ms": 58800,
    "execution_ms": 0,
    "total_ms": 60000,
    "phase_at_exit": "startup"
  }
}
```

### Example: Execution Timeout Error

When user code runs too long:

```json
{
  "execution_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "timeout",
  "stdout": "Processing started...\n",
  "stderr": "",
  "exit_code": null,
  "error": {
    "type": "execution_timeout",
    "message": "Code execution timed out after 30 seconds",
    "phase": "execution"
  },
  "timing": {
    "startup_ms": 1500,
    "dep_install_ms": 0,
    "execution_ms": 30000,
    "total_ms": 31500,
    "phase_at_exit": "execution"
  }
}
```

---

## HTTP Error Responses

### 400 Bad Request

Invalid request format.

```json
{
  "detail": "Invalid JSON in request body"
}
```

### 404 Not Found

Resource not found.

```json
{
  "detail": "Job not found"
}
```

### 409 Conflict

Idempotency key reused with different payload.

```json
{
  "detail": "Idempotency key already used with different payload",
  "existing_job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 408 Request Timeout

Timeout waiting for job completion.

```json
{
  "detail": "Timeout waiting for job"
}
```

### 503 Service Unavailable

Queue full or service degraded.

```json
{
  "detail": "Job queue is full, try again later"
}
```

### 500 Internal Server Error

Server error. Includes correlation ID for tracing.

```json
{
  "detail": "Internal server error",
  "correlation_id": "abc-123-def-456"
}
```

!!! tip
    Use the `correlation_id` to search server logs for debugging.

---

## Next Steps

- [Python SDK](sdk.md) - Use the SDK for type-safe execution
- [Async Jobs](../guide/async-jobs.md) - Long-running task patterns
- [Error Handling](../guide/error-handling.md) - Handle failures gracefully
- [Deployment](../deployment/how-to-deploy.md) - Deploy to production
