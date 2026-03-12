<p align="center">
  <img src="assets/logo.png" alt="Tako VM" width="256">
</p>

<p align="center">
  <strong>Run untrusted Python safely. Job queues and Docker isolation built-in.</strong>
</p>

Run AI-generated code in isolated Docker containers with gVisor sandboxing.
Job queues, retries, and execution history included—no Redis or Postgres required.

```python
uv pip install tako-vm

from tako_vm import Sandbox

with Sandbox() as sb:
    result = sb.run("print(1 + 1)")
    print(result.stdout)  # "2"
```

## Why Tako VM?

Sandbox solutions like [e2b](https://e2b.dev) and [microsandbox](https://github.com/microsandbox/microsandbox) give you isolated code execution—but that's it. You still need to build:

| You build | With sandbox-only | With Tako VM |
|-----------|-------------------|--------------|
| Job queue | Redis + Celery/Bull | ✅ Built-in |
| Execution history | Postgres + schema | ✅ PostgreSQL included |
| Retry logic | Custom code | ✅ Automatic |
| Idempotency | Deduplication logic | ✅ `idempotency_key` |
| Replay/debugging | Custom tooling | ✅ Rerun/fork API |

**Tako VM is the complete package:**

- **Job queue + workers** - Async execution with worker pool, no Redis/Celery setup
- **Execution history** - Every job persisted with stdout, stderr, timing, artifacts
- **Replay to debug** - Rerun past jobs with exact same code and inputs
- **Docker isolation** - Each job in its own container with seccomp filtering
- **Network isolation** - No network by default, optional allowlist per job type
- **Self-hosted** - Your machine, offline-capable, zero per-execution cost

## Architecture

Tako VM executes Python code in isolated Docker containers with:
- **Security** - Network isolation, read-only filesystem, seccomp filtering, resource limits
- **Configurability** - Pydantic-validated YAML config with env var overrides
- **Job Types** - Pre-configured environments with network control per job type
- **Fast Dependencies** - Runtime package installation via [uv](https://github.com/astral-sh/uv) (~10x faster than pip)
- **Execution History** - Full job records with timing, artifacts, and error details

**Security layers applied to every container:**
- gVisor runtime (userspace kernel) by default
- `--network=none` (isolated by default)
- `--read-only` filesystem
- `--cap-drop=ALL` (except SETUID/SETGID for privilege dropping)
- Seccomp syscall filtering
- Non-root execution (uid 1000 via gosu)

## Installation

### Prerequisites

- Docker 20.10+
- Python 3.9+
- [uv](https://github.com/astral-sh/uv)

### Install

```zsh
uv venv && source .venv/bin/activate
uv pip install -e ".[server]"
docker build -t code-executor:latest -f docker/Dockerfile.executor .
```

## Quick Start (Library Mode)

```python
from tako_vm import Sandbox

# Basic execution
with Sandbox() as sb:
    result = sb.run("print('Hello from sandbox!')")
    print(result.stdout)

# With dependencies (installed via uv, cached for speed)
with Sandbox() as sb:
    result = sb.run("""
import pandas as pd
print(pd.__version__)
""", requirements=["pandas"])

# With local packages
sb = Sandbox(package_dirs=["./my_utils"])
result = sb.run("from my_utils import helper; helper.process()")

# With input/output data
with Sandbox() as sb:
    result = sb.run("""
import json
with open("/input/data.json") as f:
    data = json.load(f)
result = {"sum": data["x"] + data["y"]}
with open("/output/result.json", "w") as f:
    json.dump(result, f)
""", input_data={"x": 10, "y": 20})
    print(result.output)  # {"sum": 30}
```

Your code runs in a container with these paths:
- `/input/data.json` - Your `input_data` as JSON (read-only)
- `/output/result.json` - Write output here, returned as `result.output`
- `/tmp/` - Temporary files (read-write)

The first run builds the executor Docker image automatically (~30 seconds one-time setup).

## Quick Start (Server Mode)

For production workloads with job queuing, retries, and execution history:

```bash
# Zero-setup local development (starts managed PostgreSQL if needed)
tako-vm dev up --with-server

# Or start server directly (auto-starts local PostgreSQL when using defaults)
tako-vm server
```

```bash
# Execute code via API
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(1 + 1)", "requirements": ["requests"]}'
```

## SDK Usage

```python
from dataclasses import dataclass
import tako_vm

tako_vm.configure('http://localhost:8000')

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

## CLI Commands

```bash
tako-vm --help                    # Show all commands
tako-vm server                    # Start the API server
tako-vm server --port 9000        # Custom port
tako-vm dev up                    # Start local PostgreSQL for development
tako-vm dev up --with-server      # Start local PostgreSQL + API server
tako-vm dev status                # Check local PostgreSQL helper status
tako-vm dev down                  # Stop local PostgreSQL helper container
tako-vm --config my.yaml server   # Use specific config file

tako-vm config                    # Show current configuration
tako-vm config --json             # Output as JSON
tako-vm validate                  # Validate current config
tako-vm validate my.yaml          # Validate specific file

tako-vm status                    # Check server health
tako-vm version                   # Show version
```

## Configuration

Tako VM uses YAML configuration with Pydantic validation. All values have sensible defaults.

### Quick Setup

```yaml
# tako_vm.yaml
production_mode: false
max_workers: 4
default_timeout: 30
max_timeout: 300
```

### Config File Search Order

1. `TAKO_VM_CONFIG` environment variable
2. `./tako_vm.yaml`
3. `./config/tako_vm.yaml`
4. `~/.tako_vm/config.yaml`
5. `/etc/tako_vm/config.yaml`

### Environment Variables

Environment variables are optional overrides. If not set, Tako VM uses values from `tako_vm.yaml` (if present), then built-in defaults.

```bash
# Override config file location
export TAKO_VM_CONFIG=/path/to/config.yaml

# Override paths
export TAKO_VM_DATA_DIR=/var/lib/tako_vm
export TAKO_VM_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/tako_vm

# API protection (front-door safeguards)
export TAKO_VM_API_MAX_PAYLOAD_BYTES=2097152
export TAKO_VM_API_RATE_LIMIT_ENABLED=true
export TAKO_VM_API_RATE_LIMIT_REQUESTS=120
export TAKO_VM_API_RATE_LIMIT_WINDOW_SECONDS=60
```

### API Protection

Front-door safeguards to protect the API from oversized payloads and request bursts:

```yaml
api_max_payload_bytes: 2097152       # 2MB max HTTP request body
api_rate_limit_enabled: true         # Enable per-client-IP rate limiting
api_rate_limit_requests: 120         # Requests allowed per window
api_rate_limit_window_seconds: 60    # Rate limit window in seconds
```

### Container Limits

Fine-grained control over container resources:

```yaml
container_limits:
  pids_limit: 100        # max processes (10-1000)
  nofile_soft: 256       # file descriptors (64-65536)
  nofile_hard: 256
  nproc_soft: 50         # process limit (10-1000)
  nproc_hard: 50
  fsize: 104857600       # max file size: 100MB (1MB-1GB)
  tmpfs_size: "100m"     # /tmp size (10m-2g)
```

### Full Example

See [tako_vm.yaml.example](tako_vm.yaml.example) for all options with documentation.

## Job Types

Job types are pre-configured execution environments with specific dependencies and limits.

### How Dependencies Work

Tako VM uses **runtime dependency installation** with [uv](https://github.com/astral-sh/uv):

1. A single base image (`code-executor:latest`) handles all job types
2. When a job runs, dependencies are installed via `uv pip install` (fast!)
3. Dependencies are cached in a Docker volume for repeated installs

This approach is simpler than pre-building images for each job type, with minimal startup overhead.

### Built-in Types

| Type | Packages | Network | Use Case |
|------|----------|---------|----------|
| `default` | stdlib only | isolated | Simple scripts |
| `data-processing` | pandas, numpy | isolated | Data manipulation |
| `ml-inference` | numpy, scikit-learn | isolated | ML inference |
| `api-client` | requests, httpx | enabled | External API calls |

### Define Custom Job Types

```yaml
job_types:
  - name: data-processing
    requirements:
      - pandas
      - numpy
    memory_limit: "1g"
    cpu_limit: 2.0
    timeout: 60

  - name: api-client
    requirements:
      - requests
      - httpx
    network_enabled: true
```

### Ollama GPU Sessions (Minimal)

Use a persistent Docker volume for model files, then configure an Ollama GPU session job type:

```yaml
security_mode: permissive
sessions_enabled: true
session_model_cache_volume: "tako-ollama-models"

job_types:
  - name: ollama-nvidia
    base_image: "ollama/ollama:latest"
    network_enabled: true
    memory_limit: "8g"
    cpu_limit: 4.0
    timeout: 3600
    session_enabled: true
    environment:
      OLLAMA_MODELS: "/models"
      OLLAMA_HOST: "0.0.0.0:11434"
    gpu:
      enabled: true
      vendor: nvidia
```

Notes:
- GPU sessions/jobs require `security_mode: permissive`.
- `/sessions/{id}/send` writes to Tako's inbox contract; it does not proxy Ollama HTTP endpoints.

### Network Control

By default, containers have **no network access** (`--network=none`).

To enable network:
```yaml
job_types:
  - name: my-api-job
    network_enabled: true      # Allow outbound connections
```

> **Note:** Jobs with runtime requirements need network access to install packages. If `network_enabled: false` with requirements, Tako VM temporarily uses bridge network for installation, then runs code with isolation. For true network isolation with dependencies, use pre-built images (see below).

### Pre-built Images (Optional)

For high-throughput production or true network isolation, you can pre-build images:

```bash
# Build a specific job type image with dependencies baked in
tako-vm build job-type data-processing

# Then configure to use the pre-built image
job_types:
  - name: data-processing
    base_image: tako-vm-data-processing:latest
    requirements: []  # Already installed in image
    network_enabled: false  # True isolation now possible
```

### Custom Libraries

For internal/proprietary packages not on PyPI, drop `.whl` files into `tako_vm/custom_libs/` and rebuild the executor image:

```bash
cp your_lib-1.0.0-py3-none-any.whl tako_vm/custom_libs/
docker build -t code-executor:latest -f docker/Dockerfile.executor .
```

See [Custom Libraries](docs/guide/custom-libraries.md) for details.

## API Usage

### Execute Code

```bash
# Simple execution
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(1 + 1)", "input_data": {}}'

# With job type
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import pandas as pd; print(pd.__version__)",
    "input_data": {},
    "job_type": "data-processing"
  }'

# Async execution
curl -X POST http://localhost:8000/execute/async \
  -H "Content-Type: application/json" \
  -d '{"code": "...", "input_data": {}}'
# Returns: {"job_id": "abc123"}

# Get result
curl http://localhost:8000/jobs/abc123/result
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/execute` | POST | Execute code synchronously |
| `/execute/async` | POST | Submit job, returns job ID |
| `/jobs/{id}` | GET | Get job status |
| `/jobs/{id}/result` | GET | Wait for job result |
| `/jobs/{id}/cancel` | POST | Cancel pending/running job |
| `/sessions` | POST | Create long-running session |
| `/sessions/{id}` | GET | Get session status |
| `/sessions/{id}/send` | POST | Send message to session inbox |
| `/sessions/{id}/events` | GET | Poll session output events |
| `/sessions/{id}/terminate` | POST | Terminate session |
| `/job-types` | GET | List available job types |
| `/health` | GET | Health check |

### Advanced Features

Tako VM provides job-native runtime capabilities for production workflows:

**Idempotent Execution** - Submit jobs with `idempotency_key` for safe retries:
```bash
curl -X POST http://localhost:8000/execute/async \
  -H "Content-Type: application/json" \
  -d '{"code": "...", "input_data": {}, "idempotency_key": "my-unique-key"}'
```

**Rerun/Fork** - "Time machine" debugging to reproduce or modify past executions:
```bash
# Rerun with exact same code and inputs
curl -X POST http://localhost:8000/jobs/{job_id}/rerun

# Fork with new code, same inputs
curl -X POST http://localhost:8000/jobs/{job_id}/fork \
  -d '{"code": "print(\"modified version\")"}'
```

**Artifact Downloads** - Direct artifact retrieval with ETag caching:
```bash
curl http://localhost:8000/jobs/{job_id}/artifacts/result.json
```

**Complete Job Records** - Use `?view=full` for full execution details including pinned environment references (`job_ref`), content hashes, lineage tracking, and resource metrics:
```bash
curl http://localhost:8000/jobs/{job_id}/result?view=full
```

### Timing & Timeouts

Tako VM separates startup time from code execution time, giving you precise control and visibility:

**Two timeout parameters:**
- `startup_timeout` - Time limit for container startup + dependency installation (default: 120s, max: 600s)
- `timeout` - Time limit for your code execution only (default: 30s, max: 300s)

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import pandas; print(pandas.__version__)",
    "requirements": ["pandas"],
    "startup_timeout": 180,
    "timeout": 60
  }'
```

**Timing breakdown in responses:**

Every response includes a `timing` field showing where time was spent:

```json
{
  "status": "succeeded",
  "timing": {
    "startup_ms": 2500,
    "dep_install_ms": 2100,
    "execution_ms": 150,
    "total_ms": 2650,
    "phase_at_exit": "completed"
  }
}
```

| Field | Description |
|-------|-------------|
| `startup_ms` | Container init + dependency installation time |
| `dep_install_ms` | Just the `uv pip install` portion (subset of startup) |
| `execution_ms` | Your code execution time |
| `total_ms` | Total container runtime |
| `phase_at_exit` | Phase when container exited: `startup`, `execution`, `completed`, or `failed` |

**Phase-specific timeout errors:**

When a timeout occurs, the error tells you which phase timed out:

```json
{
  "status": "timeout",
  "error": {
    "type": "startup_timeout",
    "message": "Startup phase exceeded 120s limit (dependency installation)",
    "phase": "startup"
  },
  "timing": {
    "startup_ms": 120000,
    "dep_install_ms": 119500,
    "execution_ms": null,
    "phase_at_exit": "startup"
  }
}
```

| Error Type | Description |
|------------|-------------|
| `startup_timeout` | Container startup or dependency installation took too long |
| `execution_timeout` | Your code exceeded the execution time limit |

This separation helps you diagnose issues: if startup times out, increase `startup_timeout` or use pre-built images; if execution times out, optimize your code or increase `timeout`.

See [docs/api/rest.md](docs/api/rest.md) for complete API reference and [docs/architecture.md](docs/architecture.md) for system diagrams.

## Security Features

| Feature | Description |
|---------|-------------|
| gVisor Runtime | Userspace kernel isolation (default, `runsc`) |
| Security Modes | `strict` (require gVisor) or `permissive` (fallback to runc) |
| Network Isolation | `--network=none` by default (bridge for dep install) |
| Read-Only Filesystem | `--read-only` with tmpfs for /tmp |
| Seccomp Filtering | Hardened allowlist blocking 47+ dangerous syscalls (see below) |
| Resource Limits | Memory, CPU, file size, process count |
| Non-Root Execution | Code runs as uid 1000 (sandbox user) via gosu |
| Capability Drop | `--cap-drop=ALL` (except SETUID/SETGID for gosu) |
| Dependency Caching | Shared uv cache volume across containers |

### Seccomp Profile

Tako VM uses a hardened seccomp profile ([tako_vm/seccomp_profile.json](tako_vm/seccomp_profile.json)) that blocks syscalls commonly used in container escape and privilege escalation:

**Blocked syscalls include:**
- **Privilege escalation**: `setuid`, `setgid`, `setresuid`, `setresgid`, `capset`
- **Permission manipulation**: `chmod`, `chown`, `fchmod`, `fchown`
- **Container escape**: `unshare`, `clone3`, `ptrace`, `process_vm_readv/writev`
- **Kernel interfaces**: `bpf`, `perf_event_open`, `io_uring_*`, `userfaultfd`
- **Module loading**: `init_module`, `finit_module`, `delete_module`
- **Kernel keyring**: `add_key`, `request_key`, `keyctl`

The profile allows ~200 syscalls required for normal Python execution (file I/O, networking, memory management, signals) while blocking privileged operations. This defense-in-depth approach complements Docker's other isolation mechanisms.

### Security Considerations

**What Tako VM protects against:**
- ✅ **Container escape** - Docker namespace isolation + seccomp profile
- ✅ **Resource exhaustion** - Memory, CPU, file size limits enforced
- ✅ **API-level path traversal** - Strong path validation on artifact downloads
- ✅ **Privilege escalation** - Non-root user + capability dropping + no-new-privileges

**What user code has access to (by design):**
- ⚠️ **Its own environment** - Configuration passed via env vars or files
- ⚠️ **Process metadata** - Can read `/proc/self/` (standard Linux behavior)
- ⚠️ **Input data** - Full access to `/input/` directory
- ⚠️ **Output writes** - Full write access to `/output/`

**This is expected behavior.** If code needs an API key to call an API, it will have access to that key. The question is not "can code access secrets?" but "should secrets be passed in job submission?"

**Recommended security practices:**
- ✅ **For your own code:** Current isolation is strong, env vars are fine
- ✅ **For user-submitted code:** Add rate limiting and audit logging
- ✅ **For untrusted/AI code:** Use external secret manager (AWS Secrets Manager, Vault), don't pass secrets in job submission

**gVisor is the default runtime:**
Tako VM uses gVisor (`runsc`) by default for strong isolation. Configure security modes:

```yaml
# Production (require gVisor)
security_mode: strict
container_runtime: runsc

# Development (allow fallback if gVisor not available)
security_mode: permissive
container_runtime: runsc
```

Or via environment variable for testing:
```bash
TAKO_VM_SECURITY_MODE=permissive pytest tests/ -v
```

**GPU policy:**
- GPU workloads (NVIDIA/AMD) run with `runc` and do not use gVisor.
- In `security_mode: strict`, GPU sessions/jobs are rejected.

**Additional isolation options:**
- **AppArmor/SELinux** (Linux only) - Can block `/proc` reads if needed
- **Kata Containers** - VM-level isolation for multi-tenant deployments

See [docs/security/honest-assessment.md](docs/security/honest-assessment.md) for detailed threat model analysis.

## Project Structure

```
tako-vm/
├── tako_vm/
│   ├── server/              # HTTP API layer
│   │   ├── app.py           # FastAPI application
│   │   └── queue.py         # Worker pool & job queue
│   ├── execution/           # Docker execution layer
│   │   ├── worker.py        # Container executor
│   │   └── builder.py       # Image builder (for pre-built images)
│   ├── sdk/                 # Python SDK
│   │   └── client.py        # TakoVM client
│   ├── cli.py               # CLI entry point
│   ├── config.py            # Pydantic configuration
│   ├── models.py            # Data models
│   ├── storage.py           # PostgreSQL persistence
│   └── job_types.py         # Job type definitions
├── docker/
│   ├── Dockerfile.executor  # Base executor image (with uv)
│   ├── Dockerfile.server    # API server image
│   └── entrypoint.sh        # Container entrypoint (installs deps, runs code)
├── tako_vm.yaml.example     # Example configuration
├── demo.sh                  # Interactive demo script
└── pyproject.toml           # Package definition
```

## License

Apache License 2.0
