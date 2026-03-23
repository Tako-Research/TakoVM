# Async Jobs

For long-running tasks, use async execution to avoid blocking.

## Sync vs Async

| Mode | Endpoint | Behavior |
|------|----------|----------|
| Sync | `POST /execute` | Blocks until complete |
| Async | `POST /execute/async` | Returns job ID immediately |

## Async Workflow

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  Submit  │────▶│  Get ID  │────▶│   Poll   │────▶│  Result  │
│   Job    │     │          │     │  Status  │     │          │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
```

## Submit Async Job

```python
import requests

response = requests.post("http://localhost:8000/execute/async", json={
    "code": """
import json
import time

with open("/input/data.json") as f:
    data = json.load(f)

# Simulate long processing
for i in range(10):
    print(f"Processing step {i+1}/10...")
    time.sleep(1)

with open("/output/result.json", "w") as f:
    json.dump({"processed": len(data["items"])}, f)
""",
    "input_data": {"items": list(range(100))}
})

job = response.json()
print(f"Job ID: {job['job_id']}")
print(f"Status: {job['status']}")
# Job ID: abc123-def456
# Status: pending
```

## Check Job Status

```python
job_id = "abc123-def456"

response = requests.get(f"http://localhost:8000/jobs/{job_id}")
status = response.json()

print(f"Status: {status['status']}")
# Status: running
```

### Status Values

The initial response from `/execute/async` returns `pending` (queue status). When you poll `/jobs/{id}`, you'll see **execution record** statuses:

| Status | Description |
|--------|-------------|
| `queued` | Job persisted, waiting for a worker |
| `running` | Currently executing |
| `succeeded` | Completed successfully |
| `failed` | Failed with error |
| `timeout` | Exceeded time limit |
| `oom` | Out of memory |
| `cancelled` | Cancelled by user |

!!! tip "Polling tip"
    When writing polling logic, check for terminal states: `succeeded`, `failed`, `timeout`, `oom`, `cancelled`.

## Wait for Result

Block until job completes:

```python
response = requests.get(
    f"http://localhost:8000/jobs/{job_id}/result",
    params={"timeout": 60}  # Wait up to 60 seconds
)

result = response.json()
print(f"Output: {result['output']}")
```

## Poll Pattern

```python
import time

def wait_for_job(job_id, poll_interval=1, max_wait=300):
    """Poll until job completes."""
    start = time.time()

    while time.time() - start < max_wait:
        response = requests.get(f"http://localhost:8000/jobs/{job_id}")
        status = response.json()

        if status["status"] in ["succeeded", "failed", "timeout", "oom", "cancelled"]:
            return status

        print(f"Status: {status['status']}...")
        time.sleep(poll_interval)

    raise TimeoutError(f"Job {job_id} did not complete in {max_wait}s")

# Usage
job_id = submit_job(...)
result = wait_for_job(job_id)
print(result)
```

## Cancel Job

Cancel a pending or running job:

```python
response = requests.post(f"http://localhost:8000/jobs/{job_id}/cancel")

result = response.json()
print(f"Status: {result['status']}")
```

!!! warning
    Running jobs may not stop immediately. The cancel request signals the worker, but execution may continue until a checkpoint.

## Concurrent Jobs

Submit multiple jobs in parallel:

```python
import concurrent.futures

def submit_job(data):
    response = requests.post("http://localhost:8000/execute/async", json={
        "code": "...",
        "input_data": data
    })
    return response.json()["job_id"]

# Submit 10 jobs concurrently
datasets = [{"id": i} for i in range(10)]

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    job_ids = list(executor.map(submit_job, datasets))

print(f"Submitted {len(job_ids)} jobs")

# Wait for all to complete
results = []
for job_id in job_ids:
    result = wait_for_job(job_id)
    results.append(result)
```

## Idempotency Support

Prevent duplicate job executions by providing an `idempotency_key`:

```python
response = requests.post("http://localhost:8000/execute/async", json={
    "code": "print('Processing payment...')",
    "input_data": {"order_id": "ORD-12345", "amount": 99.99},
    "idempotency_key": "payment-ORD-12345"
})

job = response.json()
print(f"Job ID: {job['job_id']}")
```

### Idempotency Behavior

| Scenario | Result |
|----------|--------|
| Same key + same payload | Returns existing job (no duplicate execution) |
| Same key + different payload | Returns `409 Conflict` error |
| No key provided | Always creates new job |

```python
# First submission - creates new job
response1 = requests.post("http://localhost:8000/execute/async", json={
    "code": "print('hello')",
    "idempotency_key": "my-unique-key"
})
job1 = response1.json()

# Second submission with same key + same payload - returns same job
response2 = requests.post("http://localhost:8000/execute/async", json={
    "code": "print('hello')",
    "idempotency_key": "my-unique-key"
})
job2 = response2.json()

assert job1["job_id"] == job2["job_id"]  # Same job returned

# Different payload with same key - conflict!
response3 = requests.post("http://localhost:8000/execute/async", json={
    "code": "print('different code')",
    "idempotency_key": "my-unique-key"
})
# response3.status_code == 409
```

!!! tip
    Use deterministic keys based on your business logic (e.g., `f"process-order-{order_id}"`) to ensure reliable deduplication.

## Rerun and Fork (Time Machine Debugging)

Re-execute jobs or create variations for debugging and experimentation.

### Rerun a Job

Re-execute a job with the exact same code and inputs:

```python
# Original job failed or you want to retry
original_job_id = "abc123-def456"

response = requests.post(f"http://localhost:8000/jobs/{original_job_id}/rerun")
new_job = response.json()

print(f"New Job ID: {new_job['job_id']}")
print(f"Parent: {new_job['parent_execution_id']}")
print(f"Relationship: {new_job['relationship']}")
# New Job ID: xyz789-uvw012
# Parent: abc123-def456
# Relationship: rerun
```

### Fork a Job

Execute new code with the same inputs from a previous job:

```python
original_job_id = "abc123-def456"

response = requests.post(
    f"http://localhost:8000/jobs/{original_job_id}/fork",
    json={
        "code": """
# Modified code - same inputs as original job
import json
with open("/input/data.json") as f:
    data = json.load(f)

# Try a different algorithm
result = {"count": len(data["items"]), "method": "v2"}

with open("/output/result.json", "w") as f:
    json.dump(result, f)
"""
    }
)

forked_job = response.json()
print(f"Forked Job ID: {forked_job['job_id']}")
print(f"Relationship: {forked_job['relationship']}")
# Forked Job ID: fork123-abc456
# Relationship: fork
```

### Lineage Tracking

Both rerun and fork operations maintain execution lineage:

| Field | Description |
|-------|-------------|
| `parent_execution_id` | ID of the original job |
| `relationship` | Type of relationship: `rerun` or `fork` |

```python
# Trace execution history
def get_lineage(job_id):
    """Get the execution chain for a job."""
    response = requests.get(f"http://localhost:8000/jobs/{job_id}?view=full")
    job = response.json()

    lineage = [job]
    while job.get("parent_execution_id"):
        response = requests.get(
            f"http://localhost:8000/jobs/{job['parent_execution_id']}?view=full"
        )
        job = response.json()
        lineage.append(job)

    return lineage[::-1]  # Oldest first

# Example: trace back to original execution
chain = get_lineage("fork123-abc456")
for job in chain:
    print(f"{job['job_id']} ({job.get('relationship', 'original')})")
```

## Full Execution Details

Use the `?view=full` query parameter to get extended job information:

```python
# Standard response
response = requests.get(f"http://localhost:8000/jobs/{job_id}")

# Full response with extended details
response = requests.get(f"http://localhost:8000/jobs/{job_id}?view=full")
full_details = response.json()
```

### Full View Fields

The full view includes additional fields:

| Field | Description |
|-------|-------------|
| `artifacts` | List of output artifacts with names and sizes |
| `resource_usage` | CPU time, memory peak, I/O stats |
| `content_hashes` | SHA-256 hashes of code and inputs |
| `parent_execution_id` | ID of parent job (if rerun/fork) |
| `relationship` | Relationship to parent: `rerun` or `fork` |

```python
response = requests.get(f"http://localhost:8000/jobs/{job_id}?view=full")
job = response.json()

# Artifacts information
for artifact in job.get("artifacts", []):
    print(f"  {artifact['name']}: {artifact['size']} bytes")

# Resource usage
usage = job.get("resource_usage", {})
print(f"CPU time: {usage.get('cpu_time_seconds', 0):.2f}s")
print(f"Memory peak: {usage.get('memory_peak_mb', 0):.1f} MB")

# Content hashes for verification
hashes = job.get("content_hashes", {})
print(f"Code hash: {hashes.get('code_hash', 'N/A')}")
print(f"Input hash: {hashes.get('input_hash', 'N/A')}")

# Lineage info
if job.get("parent_execution_id"):
    print(f"Parent: {job['parent_execution_id']}")
    print(f"Relationship: {job['relationship']}")
```

## Artifact Downloads

Download output files generated by job execution.

### Download Artifact

```python
job_id = "abc123-def456"
artifact_name = "result.json"

response = requests.get(
    f"http://localhost:8000/jobs/{job_id}/artifacts/{artifact_name}"
)

if response.status_code == 200:
    # ETag header for caching
    etag = response.headers.get("ETag")
    print(f"ETag: {etag}")

    # Save to file
    with open(artifact_name, "wb") as f:
        f.write(response.content)
```

### Check Artifact Metadata

Use HEAD request to get metadata without downloading:

```python
response = requests.head(
    f"http://localhost:8000/jobs/{job_id}/artifacts/{artifact_name}"
)

if response.status_code == 200:
    print(f"Content-Length: {response.headers.get('Content-Length')}")
    print(f"Content-Type: {response.headers.get('Content-Type')}")
    print(f"ETag: {response.headers.get('ETag')}")
```

### Conditional Downloads with ETag

Use ETag for efficient caching:

```python
# First download
response = requests.get(
    f"http://localhost:8000/jobs/{job_id}/artifacts/{artifact_name}"
)
etag = response.headers.get("ETag")
content = response.content

# Later: only download if changed
response = requests.get(
    f"http://localhost:8000/jobs/{job_id}/artifacts/{artifact_name}",
    headers={"If-None-Match": etag}
)

if response.status_code == 304:
    print("Artifact unchanged, using cached version")
elif response.status_code == 200:
    print("Artifact updated, downloading new version")
    content = response.content
```

### Download All Artifacts

```python
# Get job with full details to list artifacts
response = requests.get(f"http://localhost:8000/jobs/{job_id}?view=full")
job = response.json()

# Download each artifact
for artifact in job.get("artifacts", []):
    name = artifact["name"]
    response = requests.get(
        f"http://localhost:8000/jobs/{job_id}/artifacts/{name}"
    )
    with open(f"output/{name}", "wb") as f:
        f.write(response.content)
    print(f"Downloaded: {name} ({artifact['size']} bytes)")
```

## Queue Status

Check the queue status:

```python
response = requests.get("http://localhost:8000/health")
stats = response.json()["queue_stats"]

print(f"Pending: {stats['pending']}")
print(f"Running: {stats['running']}")
print(f"Max workers: {stats['max_workers']}")
```
