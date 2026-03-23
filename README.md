<p align="center">
  <img src="assets/logo.png" alt="Tako VM" width="256">
</p>

<p align="center">
  <strong>Run untrusted Python safely. Job queues and Docker isolation built-in.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/tako-vm/"><img src="https://img.shields.io/pypi/v/tako-vm" alt="PyPI"></a>
  <a href="https://github.com/las7/TakoVM/actions"><img src="https://github.com/las7/TakoVM/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/las7/TakoVM/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License"></a>
</p>

Run AI-generated code in isolated Docker containers with optional gVisor sandboxing.
Job queues, retries, and execution history included.

<p align="center">
  <a href="https://las7.github.io/TakoVM/"><strong>Documentation</strong></a> · <a href="https://las7.github.io/TakoVM/getting-started/quickstart/"><strong>Quick Start</strong></a> · <a href="https://las7.github.io/TakoVM/api/rest/"><strong>API Reference</strong></a>
</p>

```bash
# Install (requires Docker + Python 3.9+)
pip install "tako-vm[server]"
tako-vm setup                   # pull the executor Docker image
tako-vm server                  # start server (auto-starts PostgreSQL via Docker)
```

```bash
# Execute code
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(1 + 1)"}'
```

## Why Tako VM?

Sandbox solutions like [e2b](https://e2b.dev) and [microsandbox](https://github.com/microsandbox/microsandbox) give you isolated code execution—but that's it. You still need to build:

| You build | With sandbox-only | With Tako VM |
|-----------|-------------------|--------------|
| Job queue | Redis + Celery/Bull | Built-in |
| Execution history | Postgres + schema | PostgreSQL included |
| Retry logic | Custom code | Automatic |
| Idempotency | Deduplication logic | `idempotency_key` |
| Replay/debugging | Custom tooling | Rerun/fork API |

**Tako VM is the complete package:**

- **Job queue + workers** - Async execution with worker pool, no Redis/Celery setup
- **Execution history** - Every job persisted with stdout, stderr, timing, artifacts
- **Replay to debug** - Rerun past jobs with exact same code and inputs
- **Docker isolation** - Each job in its own container with seccomp filtering
- **Network isolation** - No network by default, optional allowlist per job type
- **Self-hosted** - Your machine, offline-capable, zero per-execution cost

## CLI

```bash
tako-vm setup                     # Pull executor image and verify Docker
tako-vm server                    # Start the API server
tako-vm server --port 9000        # Custom port
tako-vm dev up                    # Start local PostgreSQL for development
tako-vm dev up --with-server      # Start PostgreSQL + API server
tako-vm dev status                # Check local PostgreSQL status
tako-vm dev down                  # Stop local PostgreSQL
tako-vm config                    # Show current configuration
tako-vm config --json             # Output as JSON
tako-vm validate                  # Validate current config
tako-vm validate my.yaml          # Validate specific file
tako-vm status                    # Check server health
tako-vm version                   # Show version
tako-vm --config my.yaml server   # Use specific config file
```

## Documentation

| Topic | Link |
|-------|------|
| Installation | [docs/getting-started/installation.md](docs/getting-started/installation.md) |
| Quick Start | [docs/getting-started/quickstart.md](docs/getting-started/quickstart.md) |
| Configuration | [docs/getting-started/configuration.md](docs/getting-started/configuration.md) |
| REST API | [docs/api/rest.md](docs/api/rest.md) |
| Python SDK | [docs/api/sdk.md](docs/api/sdk.md) |
| Job Types & Environments | [docs/guide/environments.md](docs/guide/environments.md) |
| Security | [docs/deployment/security.md](docs/deployment/security.md) |
| Deployment | [docs/deployment/how-to-deploy.md](docs/deployment/how-to-deploy.md) |
| Config Reference | [tako_vm.yaml.example](tako_vm.yaml.example) |

## License

Apache License 2.0
