# Tako VM

Secure Python code execution in isolated Docker containers. Lets AI agents and platforms run untrusted code safely by sandboxing it behind a gVisor-backed container boundary. REST API for sync/async job execution, plus a Python SDK for library-mode usage.

## Why gVisor?

Docker alone shares the host kernel — a container escape gives root. gVisor intercepts syscalls in a userspace kernel (`runsc`), so even a kernel exploit stays inside the sandbox. This is the core security promise of Tako VM.

## Architecture

```
tako_vm/
├── server/
│   ├── app.py             # FastAPI routes
│   ├── queue.py           # WorkerPool, async jobs
│   ├── correlation.py     # Request correlation ID tracking
│   └── limits.py          # Rate limiting
├── execution/
│   ├── worker.py          # CodeExecutor (runs Docker containers)
│   ├── docker.py          # Docker utilities (container naming, cleanup)
│   ├── builder.py         # ContainerBuilder (for pre-built images)
│   ├── health.py          # Health checks
│   └── retry.py           # Retry logic
├── sdk/
│   └── client.py          # Python SDK (library-mode client)
├── cli.py                 # CLI entry point (`tako-vm` command)
├── config.py              # Pydantic config (TakoVMConfig)
├── constants.py           # Shared constants (UV_CACHE_VOLUME, etc.)
├── job_types.py           # Job type registry
├── models.py              # ExecutionRecord, JobStatus
├── sandbox.py             # Direct Docker sandbox (library mode, no server)
├── security.py            # Security utilities
└── storage.py             # PostgreSQL persistence
docker/
├── Dockerfile.executor    # Base executor image (uv + gosu)
├── Dockerfile.server      # API server image
└── entrypoint.sh          # Installs deps, runs code as sandbox user, writes timing
lima-gvisor.yaml           # Lima VM config for macOS dev with gVisor
```

## Key Concepts

- **Security modes**: `permissive` (default) falls back to runc; `strict` fails if gVisor unavailable
- **ExecutionRecord** status: `queued`, `running`, `succeeded`, `failed`, `timeout`, `oom`, `cancelled`
- **Queue job** status: `pending`, `running`, `completed` (different from ExecutionRecord)
- **Timeouts**: `startup_timeout` (dep install) vs `timeout` (code execution) — configured separately
- **Runtime deps**: Installed via `uv pip install` at container startup; `UV_CACHE_VOLUME` speeds repeats
- Tests use temp database via `TAKO_VM_DATA_DIR` env var for isolation

## Build & Test

```bash
# Build executor image (one-time)
docker build -t code-executor:latest -f docker/Dockerfile.executor .

# Run tests
TAKO_VM_SECURITY_MODE=permissive pytest tests/ -v

# Start server
tako-vm server --port 8000
```

## Code Quality

Linting is handled automatically via a PostToolUse hook (`.claude/hooks/lint.sh`) that runs `ruff check --fix` and `ruff format` on changed Python files. To run manually:

```bash
ruff check tako_vm/ tests/
ruff format tako_vm/ tests/
```

## References

- [docs/development/troubleshooting.md](docs/development/troubleshooting.md) — Dependency flow, common issues, debugging endpoints
- [docs/api/rest.md](docs/api/rest.md) — API reference
- [docs/api/sdk.md](docs/api/sdk.md) — Python SDK reference
- [tako_vm.yaml.example](tako_vm.yaml.example) — Config options
- [README.md](README.md) — Full docs, CLI commands, job types
