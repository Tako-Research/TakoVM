# Tako VM Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TAKO VM                                        │
│                     Secure Python Code Execution                            │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────┐     ┌──────────────────────────────────────────────────────┐
│              │     │                   Tako VM Server                      │
│   Client     │     │  ┌────────────┐  ┌─────────────┐  ┌──────────────┐  │
│  ─────────   │     │  │            │  │             │  │              │  │
│  curl/SDK    │────▶│  │  FastAPI   │─▶│ WorkerPool  │─▶│ CodeExecutor │  │
│  Python      │◀────│  │  (app.py)  │◀─│ (queue.py)  │◀─│ (worker.py)  │  │
│              │     │  │            │  │             │  │              │  │
└──────────────┘     │  └────────────┘  └─────────────┘  └──────┬───────┘  │
                     │        │                                  │          │
                     │        ▼                                  ▼          │
                     │  ┌────────────┐              ┌────────────────────┐  │
                     │  │ PostgreSQL │              │   Docker Engine    │  │
                     │  │  Storage   │              │                    │  │
                     │  │ (history)  │              │  ┌──────────────┐  │  │
                     │  └────────────┘              │  │  Executor    │  │  │
                     │                              │  │  Container   │  │  │
                     └──────────────────────────────│  │  (isolated)  │  │──┘
                                                    │  └──────────────┘  │
                                                    └────────────────────┘
```

## Execution Flow

### Synchronous Execution (`POST /execute`)

```
  Client                    Server                     Docker
    │                         │                          │
    │  POST /execute          │                          │
    │  {code, input_data}     │                          │
    │────────────────────────▶│                          │
    │                         │                          │
    │                         │  Create container        │
    │                         │─────────────────────────▶│
    │                         │                          │
    │                         │  Install deps (uv)       │
    │                         │  Run code                │
    │                         │  Collect output          │
    │                         │◀─────────────────────────│
    │                         │                          │
    │  {success, output,      │  Remove container        │
    │   stdout, stderr}       │─────────────────────────▶│
    │◀────────────────────────│                          │
    │                         │                          │
```

### Asynchronous Execution (`POST /execute/async`)

```
  Client                    Server                     Docker
    │                         │                          │
    │  POST /execute/async    │                          │
    │  {code, input_data}     │                          │
    │────────────────────────▶│                          │
    │                         │                          │
    │  {job_id, status:       │  Queue job               │
    │   "queued"}             │                          │
    │◀────────────────────────│                          │
    │                         │                          │
    │                         │  Worker picks up job     │
    │  GET /jobs/{id}         │─────────────────────────▶│
    │────────────────────────▶│                          │
    │                         │  Execute in container    │
    │  {status: "running"}    │                          │
    │◀────────────────────────│                          │
    │                         │◀─────────────────────────│
    │  GET /jobs/{id}/result  │                          │
    │────────────────────────▶│                          │
    │                         │                          │
    │  {status: "succeeded",  │                          │
    │   output: {...}}        │                          │
    │◀────────────────────────│                          │
```

## Container Execution Detail

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Docker Container Lifecycle                            │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│   1. STARTUP     │    │   2. INSTALL     │    │    3. EXECUTE    │
│   (as root)      │───▶│   (as root)      │───▶│   (as sandbox)   │
└──────────────────┘    └──────────────────┘    └──────────────────┘
        │                       │                       │
        ▼                       ▼                       ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ entrypoint.sh    │    │ uv pip install   │    │ gosu sandbox     │
│ starts           │    │ $TAKO_REQUIREMENTS│   │ python main.py   │
└──────────────────┘    └──────────────────┘    └──────────────────┘
                               │
                               ▼
                        ┌──────────────────┐
                        │ Cached in        │
                        │ tako-uv-cache    │
                        │ volume           │
                        └──────────────────┘

Security Layers:
┌─────────────────────────────────────────────────────────────────────────────┐
│  --network=none     No network access (or bridge for deps)                  │
│  --read-only        Read-only filesystem                                    │
│  --cap-drop=ALL     No Linux capabilities                                   │
│  --security-opt     No privilege escalation                                 │
│  uid 1000           Non-root execution via gosu                             │
│  seccomp            Syscall filtering                                       │
│  resource limits    Memory, CPU, PIDs, file size                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Dependency Installation Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Runtime Dependencies (Default)                          │
└─────────────────────────────────────────────────────────────────────────────┘

  Job Config                    Container                    Result
      │                             │                           │
      │  requirements:              │                           │
      │    - pandas                 │                           │
      │    - numpy                  │                           │
      │                             │                           │
      ▼                             ▼                           ▼
┌──────────────┐          ┌─────────────────┐         ┌──────────────┐
│ TAKO_        │─────────▶│ uv pip install  │────────▶│ Code runs    │
│ REQUIREMENTS │          │ pandas numpy    │         │ with deps    │
│ env var      │          │ (~1-2 seconds)  │         │ available    │
└──────────────┘          └─────────────────┘         └──────────────┘
                                  │
                                  ▼
                          ┌─────────────────┐
                          │ tako-uv-cache   │  Cached for
                          │ Docker volume   │  future runs
                          └─────────────────┘


┌─────────────────────────────────────────────────────────────────────────────┐
│                     Pre-built Images (Production)                           │
└─────────────────────────────────────────────────────────────────────────────┘

  Build Phase                                      Run Phase
      │                                                │
      ▼                                                ▼
┌──────────────────┐                         ┌──────────────────┐
│ Pre-build image  │                         │ Job submitted    │
│ via REST API or  │                         │ with job_type    │
│ docker build     │                         │                  │
└────────┬─────────┘                         └────────┬─────────┘
         │                                            │
         ▼                                            ▼
┌──────────────────┐                         ┌──────────────────┐
│ Docker image     │                         │ Container starts │
│ with pandas,     │────────────────────────▶│ instantly        │
│ numpy baked in   │                         │ --network=none   │
└──────────────────┘                         │ (true isolation) │
                                             └──────────────────┘
```

## Job Types

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Job Type Selection                               │
└─────────────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────────────────┐
                    │      POST /execute          │
                    │  job_type: "data-processing"│
                    └──────────────┬──────────────┘
                                   │
                                   ▼
              ┌────────────────────────────────────────┐
              │           JobTypeRegistry              │
              │                                        │
              │  ┌──────────┐ ┌──────────┐ ┌────────┐ │
              │  │ default  │ │  data-   │ │  api-  │ │
              │  │          │ │processing│ │ client │ │
              │  │ stdlib   │ │ pandas   │ │requests│ │
              │  │ only     │ │ numpy    │ │ httpx  │ │
              │  │          │ │          │ │        │ │
              │  │ network: │ │ network: │ │network:│ │
              │  │ none     │ │ none*    │ │ bridge │ │
              │  └──────────┘ └──────────┘ └────────┘ │
              └────────────────────────────────────────┘

              * Uses bridge network for dependency installation,
                then isolates for code execution.
                Use pre-built images for true network=none.
```

## Quick Start Workflow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Getting Started                                    │
└─────────────────────────────────────────────────────────────────────────────┘

Step 1: Build Base Image
────────────────────────
    $ docker build -t code-executor:latest -f docker/Dockerfile.executor .

    Creates:
    ┌────────────────────────────────┐
    │  code-executor:latest          │
    │  ├── Python 3.11               │
    │  ├── uv (fast installer)       │
    │  ├── gosu (privilege drop)     │
    │  └── entrypoint.sh             │
    └────────────────────────────────┘


Step 2: Start Server
────────────────────
    $ tako-vm server --port 8000

    ┌──────────────────────────────────────────────┐
    │  Tako VM Server                              │
    │  ├── FastAPI on :8000                        │
    │  ├── WorkerPool (4 workers default)          │
    │  └── PostgreSQL for execution history        │
    └──────────────────────────────────────────────┘


Step 3: Execute Code
────────────────────
    $ curl -X POST http://localhost:8000/execute \
        -H "Content-Type: application/json" \
        -d '{"code": "print(1+1)", "input_data": {}}'

    ┌──────────────────────────────────────────────┐
    │  Response:                                   │
    │  {                                           │
    │    "success": true,                          │
    │    "output": null,                           │
    │    "stdout": "2\n",                          │
    │    "exit_code": 0                            │
    │  }                                           │
    └──────────────────────────────────────────────┘


Step 4: Use Job Types for Dependencies
──────────────────────────────────────
    $ curl -X POST http://localhost:8000/execute \
        -d '{"code": "import pandas; print(pandas.__version__)",
             "input_data": {},
             "job_type": "data-processing"}'

    Container startup:
    ┌──────────────────────────────────────────────┐
    │  1. uv pip install pandas numpy  (~1-2s)    │
    │  2. python main.py                           │
    │  3. Return output                            │
    └──────────────────────────────────────────────┘
```

## API Endpoints Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            API Endpoints                                     │
└─────────────────────────────────────────────────────────────────────────────┘

Execution
─────────
POST /execute              Execute code (sync, blocks until done)
POST /execute/async        Submit job (async, returns job_id)

Jobs
────
GET  /jobs/{id}            Get job status
GET  /jobs/{id}/result     Get result (with optional wait)
POST /jobs/{id}/cancel     Cancel pending/running job
POST /jobs/{id}/rerun      Re-execute with same code/inputs
POST /jobs/{id}/fork       Execute new code with same inputs
GET  /jobs/{id}/artifacts/{name}   Download output file

Job Types
─────────
GET  /job-types            List available environments
GET  /job-types/{name}     Get specific job type config
POST /job-types/{name}/build   Build pre-configured image

Operations
──────────
GET  /health               Server health + circuit breaker status
GET  /pool/stats           Worker pool statistics
GET  /executions           List execution history
GET  /dlq/stats            Dead letter queue stats
```

## File Structure

```
tako-vm/
├── tako_vm/
│   ├── server/
│   │   ├── app.py           # FastAPI routes, request handling
│   │   ├── queue.py         # WorkerPool, async job management
│   │   └── correlation.py   # Correlation ID middleware for tracing
│   ├── execution/
│   │   ├── worker.py        # CodeExecutor, Docker commands
│   │   ├── builder.py       # ContainerBuilder (pre-built images)
│   │   ├── docker.py        # Docker utilities (container naming, cleanup)
│   │   ├── health.py        # Circuit breaker, startup cleanup
│   │   └── retry.py         # Retry logic for transient failures
│   ├── config.py            # Pydantic settings (TakoVMConfig), YAML loading
│   ├── models.py            # ExecutionRecord, JobStatus, ErrorType
│   ├── storage.py           # PostgreSQL persistence
│   ├── security.py          # Validation, error sanitization
│   ├── job_types.py         # JobType, JobTypeRegistry
│   └── version.py           # VersionManager for job type versioning
├── docker/
│   ├── Dockerfile.executor  # Base image (Python + uv + gosu)
│   ├── Dockerfile.server    # Server image (for containerized deploy)
│   └── entrypoint.sh        # Install deps, drop privileges, run code
│                            # Writes timing to /output/.tako_phase
├── tako_vm.yaml.example     # Configuration reference
├── lima-gvisor.yaml         # Lima VM config for macOS/Windows with gVisor
└── docs/
    ├── api/rest.md          # API reference
    ├── api/sdk.md           # Python SDK reference
    └── guide/               # User guides
```
