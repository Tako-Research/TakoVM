# Installation

## Prerequisites

Before installing Tako VM, verify you have the following:

| Requirement | Minimum Version | Check Command | Required? |
|-------------|----------------|---------------|-----------|
| **Docker** | 20.10+ | `docker --version` | Yes |
| **Python** | 3.9+ | `python3 --version` | Yes |
| **[uv](https://github.com/astral-sh/uv)** | any | `uv --version` | Recommended (pip also works) |
| **gVisor** | any | `docker run --runtime=runsc --rm hello-world` | Production only |

!!! note "Quick pre-flight check"
    ```bash
    docker info > /dev/null 2>&1 && echo "Docker: OK" || echo "Docker: NOT RUNNING"
    python3 --version
    ```
    Docker must be **running** (not just installed). On macOS/Windows, start Docker Desktop first.

## Install Tako VM

### Install from PyPI

```bash
# Create virtual environment and install
uv venv && source .venv/bin/activate

# SDK client only - for connecting to an existing Tako VM server
uv pip install tako-vm

# Full installation with server components
uv pip install tako-vm[server]

# All dependencies including dev tools
uv pip install tako-vm[all]
```

### Install from Source

```bash
# Clone the repository
git clone https://github.com/las7/TakoVM.git
cd tako-vm
uv venv && source .venv/bin/activate

# Install in development mode
uv pip install -e .              # SDK only
uv pip install -e ".[server]"    # With server
uv pip install -e ".[dev]"       # With dev dependencies
```

## Build the Docker Image

The Docker image is required to execute code:

```bash
docker build -t code-executor:latest -f docker/Dockerfile.executor .
```

This builds the base execution container with Python 3.11 and uv.

## Verify Installation

### Check the CLI

```bash
tako-vm version
# tako-vm 0.1.4

tako-vm --help
# Shows all available commands
```

### Start the Server

```bash
tako-vm server
# or with options
tako-vm server --port 9000
tako-vm --config my-config.yaml server
```

### Check Server Health

```bash
tako-vm status
# or
curl http://localhost:8000/health
```

Expected output:
```json
{
  "status": "healthy",
  "docker_available": true,
  "version": "0.1.4"
}
```

## Installation Extras

| Extra | Command | Description |
|-------|---------|-------------|
| `server` | `uv pip install tako-vm[server]` | FastAPI server, uvicorn, YAML config |
| `dev` | `uv pip install tako-vm[dev]` | pytest, ruff, development tools |
| `docs` | `uv pip install tako-vm[docs]` | MkDocs for documentation |
| `all` | `uv pip install tako-vm[all]` | All production dependencies |

## Directory Structure

After installation, your directory should look like:

```
tako-vm/
├── tako_vm/              # Main package
│   ├── cli.py            # CLI entry point
│   ├── config.py         # Configuration (Pydantic)
│   ├── server/           # API server
│   └── execution/        # Docker execution
├── docs/                 # Documentation
├── docker/               # Container images
├── pyproject.toml        # Package configuration
├── tako_vm.yaml.example  # Config template
└── demo.sh               # Interactive demo
```

## Troubleshooting

### Docker Not Found

If you see "Docker not available" errors:

```bash
# Check Docker is running
docker info

# On macOS/Windows, ensure Docker Desktop is running
```

### Permission Denied

If you get permission errors with Docker:

```bash
# Add your user to the docker group (Linux)
sudo usermod -aG docker $USER
# Log out and back in for changes to take effect
```

### Port Already in Use

If port 8000 is busy:

```bash
# Use a different port
tako-vm server --port 8001
```

### gVisor Not Available

If you see "RuntimeUnavailableError: gVisor not available" errors:

```bash
# Option 1: Install gVisor (recommended for production)
# See: https://gvisor.dev/docs/user_guide/install/

# Option 2: Use permissive mode for development
export TAKO_VM_SECURITY_MODE=permissive
tako-vm server

# Or in config file:
# security_mode: permissive
```

For macOS/Windows development, use the included Lima VM with gVisor pre-installed:

```bash
limactl start lima-gvisor.yaml
limactl shell tako-gvisor
```

### Config Validation Errors

If you get config errors:

```bash
# Validate your config file
tako-vm validate my-config.yaml

# Show current configuration
tako-vm config
```

## Next Steps

- Continue to [Quick Start](quickstart.md) to run your first code
- See [Configuration](configuration.md) for customization options
