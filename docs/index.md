# Tako VM

**Job queue infrastructure for AI agents. Not just a sandbox.**

Other tools give you isolated code execution. Tako VM gives you the complete job system: queue, workers, execution history, retries, and replay—all in one package.

## Quick Start

```bash
pip install "tako-vm[server]"
tako-vm setup                   # pull the executor Docker image
tako-vm server                  # start server (auto-starts PostgreSQL via Docker)
```

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(1 + 1)"}'
```

## Why Tako VM?

Sandbox-only tools (e2b, microsandbox) give you isolated execution. You still need to build the job system yourself.

| Feature | Sandbox-only | Tako VM |
|---------|--------------|---------|
| Job queue | Build with Redis/Celery | Built-in |
| Execution history | Build with Postgres | PostgreSQL included |
| Retries | Write retry logic | Automatic |
| Replay/debugging | Build custom tooling | Rerun/fork API |
| Idempotency | Implement deduplication | `idempotency_key` |

**Tako VM includes:**

- **Job queue + workers** - No Redis/Celery setup needed
- **Execution history** - Every job persisted with timing and artifacts
- **Replay to debug** - Rerun past jobs with exact same inputs
- **Docker isolation** - Seccomp filtering, network isolation
- **Self-hosted** - Zero per-execution cost, works offline

## Server Mode

For production workloads with job queuing, retries, and audit trails:

```python
import requests

response = requests.post('http://localhost:8000/execute', json={
    'code': 'print(1 + 1)',
    'requirements': ['requests']
})
print(response.json())
```

## Library Mode

For development and simple scripts, use the Sandbox class directly (no server needed):

```bash
pip install tako-vm
```

```python
from tako_vm import Sandbox

with Sandbox() as sb:
    result = sb.run("print(1 + 1)")
    print(result.stdout)  # "2"
```

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────────────┐
│   Client    │────▶│  Tako VM    │────▶│   Docker + gVisor   │
│  (HTTP/SDK) │     │   Server    │     │   (Isolated)        │
└─────────────┘     └─────────────┘     └─────────────────────┘
                          │
                          ▼
                    ┌─────────────┐
                    │ PostgreSQL  │
                    │  (Records)  │
                    └─────────────┘
```

**Security:** Tako VM supports gVisor (runsc) for strong container isolation with a userspace kernel. Defaults to `permissive` mode, which falls back to standard Docker (runc) if gVisor is not installed.

## Next Steps

- [Installation](getting-started/installation.md) - Set up Tako VM
- [Quick Start](getting-started/quickstart.md) - Run your first code
- [Architecture](architecture.md) - How Tako VM works
- [REST API](api/rest.md) - Full API documentation
- [Tutorial](guide/tutorial.md) - Build a data processing pipeline
