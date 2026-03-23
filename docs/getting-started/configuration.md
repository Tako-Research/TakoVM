# Configuration

Tako VM uses YAML configuration with sensible defaults.

## Quick Start

Create `tako_vm.yaml` in your project directory:

```yaml
production_mode: false    # strict mode (no auto-build)
max_workers: 4            # concurrent execution slots
```

That's it for basic setup. Everything else has sensible defaults.

## Full Configuration

```yaml
# tako_vm.yaml

# ==============================================================================
# QUICK START
# ==============================================================================
production_mode: false
max_workers: 4

# ==============================================================================
# SERVER & LOGGING
# ==============================================================================
log_level: INFO            # DEBUG, INFO, WARNING, ERROR, CRITICAL
server_host: "0.0.0.0"     # Host to bind to
server_port: 8000          # Port to bind to
database_url: "postgresql://postgres:postgres@localhost:5432/tako_vm"

# ==============================================================================
# API PROTECTION
# ==============================================================================
api_max_payload_bytes: 2097152       # 2MB max HTTP request body
api_rate_limit_enabled: true         # Enable per-client-IP rate limiting
api_rate_limit_requests: 120         # Requests allowed per window
api_rate_limit_window_seconds: 60    # Rate limit window in seconds

# ==============================================================================
# EXECUTION LIMITS
# ==============================================================================
default_timeout: 30       # seconds
max_timeout: 300          # maximum allowed

max_code_bytes: 102400    # 100KB
max_input_bytes: 1048576  # 1MB
max_stdout_bytes: 65536   # 64KB
max_stderr_bytes: 65536   # 64KB

# ==============================================================================
# RETRY CONFIGURATION
# ==============================================================================
max_retry_attempts: 2     # Retries for transient failures
retry_base_delay: 1.0     # Base delay between retries (seconds)
queue_wait_timeout: 1.0   # Queue wait timeout (seconds)

# ==============================================================================
# CONTAINER SECURITY
# ==============================================================================
docker_image: code-executor:latest
enable_seccomp: true           # syscall filtering
enable_cap_restrictions: true  # capability restrictions (--cap-drop=ALL)
enable_userns: false           # user namespace (disabled for gosu compatibility)

# gVisor runtime (strong isolation)
container_runtime: runsc  # 'runsc' (gVisor) or 'runc' (standard Docker)
security_mode: permissive # 'permissive' (fallback to runc) or 'strict' (require gVisor)

container_limits:
  pids_limit: 100
  nofile_soft: 256
  nofile_hard: 256
  nproc_soft: 50
  nproc_hard: 50
  fsize: 104857600        # 100MB max file size
  tmpfs_size: "100m"

# ==============================================================================
# JOB TYPES
# ==============================================================================
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
    memory_limit: "256m"
    timeout: 30
    network_enabled: true

# ==============================================================================
# OTHER
# ==============================================================================
max_queue_size: 100
max_artifact_bytes: 10485760
max_total_artifacts_bytes: 52428800
execution_record_ttl_days: 30
```

## Config File Search Order

Tako VM searches for configuration in this order:

1. `TAKO_VM_CONFIG` environment variable
2. `./tako_vm.yaml` (current directory)
3. `./config/tako_vm.yaml`
4. `~/.tako_vm/config.yaml`
5. `/etc/tako_vm/config.yaml`

## Environment Variables

Tako VM supports the following environment variables for configuration:

```bash
# Override config file location
export TAKO_VM_CONFIG=/path/to/config.yaml

# Override data directory
export TAKO_VM_DATA_DIR=/var/lib/tako_vm

# Override database URL
export TAKO_VM_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/tako_vm

# Security mode (strict or permissive)
export TAKO_VM_SECURITY_MODE=permissive  # Allow fallback to runc if gVisor unavailable

# Container runtime (runsc or runc)
export TAKO_VM_CONTAINER_RUNTIME=runsc   # Use gVisor for strong isolation

# API protection (front-door safeguards)
export TAKO_VM_API_MAX_PAYLOAD_BYTES=2097152
export TAKO_VM_API_RATE_LIMIT_ENABLED=true
export TAKO_VM_API_RATE_LIMIT_REQUESTS=120
export TAKO_VM_API_RATE_LIMIT_WINDOW_SECONDS=60

# Security features (true/false/1/0/yes/no)
export TAKO_VM_ENABLE_SECCOMP=true       # Enable syscall filtering
export TAKO_VM_ENABLE_CAP_RESTRICTIONS=true  # Enable capability restrictions

# XDG Base Directory support
export XDG_DATA_HOME=/custom/data/path  # Tako VM will use $XDG_DATA_HOME/tako_vm
```

| Variable | Description | Default |
|----------|-------------|---------|
| `TAKO_VM_CONFIG` | Config file path | Search in standard locations |
| `TAKO_VM_DATA_DIR` | Data directory | `~/.tako_vm` or `$XDG_DATA_HOME/tako_vm` |
| `TAKO_VM_DATABASE_URL` | PostgreSQL connection URL | `postgresql://postgres:postgres@localhost:5432/tako_vm` |
| `TAKO_VM_SECURITY_MODE` | Security mode (`strict` or `permissive`) | `strict` |
| `TAKO_VM_CONTAINER_RUNTIME` | Container runtime (`runsc` or `runc`) | `runsc` |
| `TAKO_VM_API_MAX_PAYLOAD_BYTES` | Max HTTP request body size in bytes | `2097152` |
| `TAKO_VM_API_RATE_LIMIT_ENABLED` | Enable API rate limiting | `true` |
| `TAKO_VM_API_RATE_LIMIT_REQUESTS` | Requests allowed per rate-limit window | `120` |
| `TAKO_VM_API_RATE_LIMIT_WINDOW_SECONDS` | Rate-limit window duration (seconds) | `60` |
| `TAKO_VM_ENABLE_SECCOMP` | Enable seccomp syscall filtering | `true` |
| `TAKO_VM_ENABLE_CAP_RESTRICTIONS` | Enable capability restrictions | `true` |
| `XDG_DATA_HOME` | XDG base data directory | `~/.local/share` |

## Job Types

Job types define pre-configured execution environments:

| Field | Description | Default |
|-------|-------------|---------|
| `name` | Unique identifier | required |
| `requirements` | pip packages | `[]` |
| `memory_limit` | Container memory | `"512m"` |
| `cpu_limit` | CPU cores | `1.0` |
| `timeout` | Default timeout | `30` |
| `network_enabled` | Allow network | `false` |

When you define a job type, Tako VM builds a Docker image with the specified packages pre-installed (`tako-vm-{name}:latest`).

!!! warning "Security: Environment Variables"
    If your job type config includes `environment` variables, be aware that user code can read them via `/proc/self/environ`. Never put secrets (API keys, tokens) in job type environment variables. See [Security Mitigations](../security/mitigations.md).

## Container Limits

| Limit | Description | Default | Range |
|-------|-------------|---------|-------|
| `pids_limit` | Max processes | 100 | 10-1000 |
| `nofile_soft/hard` | File descriptors | 256 | 64-65536 |
| `nproc_soft/hard` | Process count | 50 | 10-1000 |
| `fsize` | Max file size | 100MB | 1MB-1GB |
| `tmpfs_size` | /tmp size | 100m | 10m-2g |

## Validating Configuration

```bash
# Validate config file
tako-vm validate

# Show current configuration
tako-vm config

# Show as JSON
tako-vm config --json
```

Or in Python:

```python
from tako_vm.config import load_config, validate_config_file

# Validate without loading
errors = validate_config_file(Path("tako_vm.yaml"))
if errors:
    print(f"Invalid: {errors}")

# Load and inspect
config = load_config()
print(f"Workers: {config.max_workers}")
print(f"Job types: {len(config.job_types)}")
```

## Production Configuration

```yaml
production_mode: true     # require pre-built images
log_level: WARNING        # reduce log verbosity
max_workers: 8
max_timeout: 60
default_timeout: 15
enable_seccomp: true
enable_userns: true

# Retry configuration for production
max_retry_attempts: 3
retry_base_delay: 2.0

container_limits:
  pids_limit: 50
  nofile_soft: 128
  nofile_hard: 128
  tmpfs_size: "64m"
```

## Timeout Configuration

Tako VM separates startup time from code execution time:

| Option | Description | Default | Range |
|--------|-------------|---------|-------|
| `default_timeout` | Code execution timeout (seconds) | `30` | 1-3600 |
| `max_timeout` | Maximum allowed execution timeout | `300` | 1-86400 |
| `default_startup_timeout` | Container startup + dep install timeout | `120` | 10-600 |
| `max_startup_timeout` | Maximum allowed startup timeout | `600` | 30-1800 |

## gVisor and Security Modes

Tako VM supports gVisor (runsc) for strong container isolation:

| Option | Description | Default |
|--------|-------------|---------|
| `container_runtime` | Container runtime: `runsc` (gVisor) or `runc` (standard) | `runsc` |
| `security_mode` | `permissive` (fallback to runc) or `strict` (require gVisor) | `permissive` |

**Security modes:**

- **permissive** (default): Falls back to standard runc runtime with a warning if gVisor is unavailable. Works on all platforms including macOS and Windows.
- **strict**: Fails with `RuntimeUnavailableError` if gVisor is not installed. Use this in production for guaranteed strong isolation.

```yaml
# Development (allow fallback to runc)
security_mode: permissive
container_runtime: runsc

# Production (require gVisor)
security_mode: strict
container_runtime: runsc
```

To install gVisor, see the [gVisor installation guide](https://gvisor.dev/docs/user_guide/install/).

For macOS/Windows development, use the included Lima VM configuration:

```bash
limactl start lima-gvisor.yaml
limactl shell tako-gvisor
```

## Configuration Options Reference

| Option | Description | Default |
|--------|-------------|---------|
| `log_level` | Logging level | `INFO` |
| `server_host` | Server bind host | `0.0.0.0` |
| `server_port` | Server bind port | `8000` |
| `api_max_payload_bytes` | Max HTTP request body size (bytes) | `2097152` |
| `api_rate_limit_enabled` | Enable API rate limiting | `true` |
| `api_rate_limit_requests` | Requests allowed per rate-limit window | `120` |
| `api_rate_limit_window_seconds` | Rate-limit window duration (seconds) | `60` |
| `max_retry_attempts` | Max retries for transient failures | `2` |
| `retry_base_delay` | Base delay between retries (seconds) | `1.0` |
| `queue_wait_timeout` | Queue wait timeout (seconds) | `1.0` |
| `container_runtime` | Container runtime (`runsc` or `runc`) | `runsc` |
| `security_mode` | Security mode (`permissive` or `strict`) | `permissive` |
| `enable_seccomp` | Enable seccomp syscall filtering | `true` |
| `enable_cap_restrictions` | Enable capability restrictions (`--cap-drop=ALL`) | `true` |
| `enable_userns` | Enable user namespace isolation | `false` |
