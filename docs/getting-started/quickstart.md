# Quick Start

Get Tako VM running and execute your first code.

## Install and Start

```bash
pip install "tako-vm[server]"
tako-vm setup                   # pull the executor Docker image
tako-vm server                  # starts on http://localhost:8000
```

`tako-vm server` auto-starts a PostgreSQL container on port 55432 for job persistence. Use `--port` to change the server port:

```bash
tako-vm server --port 9000
```

!!! note "gVisor on Linux"
    Tako VM defaults to `permissive` mode, which falls back to runc if gVisor is not installed. For production, set `security_mode: strict` to require gVisor. See [Security](../deployment/security.md#gvisor-runtime) for installation instructions.

!!! warning "Security: Environment Variables"
    Do not pass secrets (API keys, tokens, passwords) as job type environment variables. User code can read them via `/proc/self/environ`. Pass sensitive data through `input_data` instead, which is scoped to a single job. See [Security Mitigations](../security/mitigations.md) for details.

## Execute Code

### Using curl

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(1 + 1)"}'
```

### Using Python

```python
import requests

code = """
import json

# Read input from /input/data.json
with open("/input/data.json") as f:
    data = json.load(f)

# Process
result = {
    "sum": data["x"] + data["y"],
    "product": data["x"] * data["y"]
}

# Write output to /output/result.json
with open("/output/result.json", "w") as f:
    json.dump(result, f)

print("Done!")
"""

response = requests.post(
    "http://localhost:8000/execute",
    json={
        "code": code,
        "input_data": {"x": 10, "y": 20},
        "requirements": ["numpy"]  # Optional: ad-hoc dependencies
    }
)

result = response.json()
print(f"Success: {result['success']}")
print(f"Output: {result['output']}")
print(f"Stdout: {result['stdout']}")
```

## Sync vs Async Execution

| Endpoint | Behavior | Best For |
|----------|----------|----------|
| `POST /execute` | **Blocks** until complete, returns result directly | Quick jobs (<30s), simple scripts |
| `POST /execute/async` | Returns `job_id` immediately, poll for result | Long jobs, queued workloads, production |

### Sync (Blocking)

```python
# Blocks until done - simple but ties up the connection
response = requests.post("http://localhost:8000/execute", json={
    "code": "print('hello')"
})
print(response.json()["stdout"])  # "hello"
```

### Async (Non-blocking)

```python
# Returns immediately with job_id
response = requests.post("http://localhost:8000/execute/async", json={
    "code": "import time; time.sleep(10); print('done')"
})
job_id = response.json()["job_id"]

# Option 1: Poll for status
status = requests.get(f"http://localhost:8000/jobs/{job_id}").json()
print(status["status"])  # "running" or "succeeded"

# Option 2: Block until complete (with timeout)
result = requests.get(
    f"http://localhost:8000/jobs/{job_id}/result?wait=true&timeout=30"
).json()
print(result["stdout"])  # "done"
```

!!! tip "When to use async"
    Use async (`/execute/async`) when:

    - Jobs may take more than a few seconds
    - You need idempotency keys for safe retries
    - You want to submit multiple jobs and collect results later
    - You're building a production system with job queuing

## Understanding the Execution Model

Your code runs in an isolated Docker container with three mounted directories:

| Path | Permission | Purpose |
|------|------------|---------|
| `/input/data.json` | Read-only | Input data (JSON) |
| `/output/` | Read-write | Output files |
| `/code/main.py` | Read-only | Your code |

```
Container Filesystem
├── /input/
│   └── data.json      ← Your input_data as JSON
├── /output/
│   └── result.json    ← Write your output here
├── /code/
│   └── main.py        ← Your code
└── /tmp/              ← Temporary files (writable)
```

!!! important "Output Files (Artifacts)"
    **All output files must be written to `/output/`** - this is the only writable directory besides `/tmp/`.

    - `/output/result.json` is **special**: it's automatically parsed and returned in the `output` field
    - Any other files in `/output/` (e.g., `report.txt`, `data.csv`) are saved as **artifacts**
    - Download artifacts via `GET /jobs/{job_id}/artifacts/{filename}`
    - Files written elsewhere are lost when the container is destroyed

## Response Format

```json
{
  "success": true,
  "output": {"sum": 30, "product": 200},
  "stdout": "Done!\n",
  "stderr": "",
  "exit_code": 0,
  "execution_time": 0.35,
  "job_type": "default"
}
```

| Field | Description |
|-------|-------------|
| `success` | Whether execution completed without errors |
| `output` | Contents of `/output/result.json` (parsed) |
| `stdout` | Standard output from your code |
| `stderr` | Standard error from your code |
| `exit_code` | Process exit code (0 = success) |
| `execution_time` | Wall clock time in seconds |
| `job_type` | Environment used |

## Next Steps

- [Basic Execution](../guide/basic-execution.md) - Input/output patterns, multiple artifacts
- [Configuration](configuration.md) - Customize Tako VM settings
- [Environments](../guide/environments.md) - Use different packages
- [Async Jobs](../guide/async-jobs.md) - Long-running tasks with artifact downloads
