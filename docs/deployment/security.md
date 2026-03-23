# Security

Tako VM implements defense-in-depth to safely execute untrusted code.

## Security Layers

```
┌─────────────────────────────────────────────────────────────┐
│                      Input Validation                        │
│                  (Size limits, sanitization)                 │
├─────────────────────────────────────────────────────────────┤
│                    Container Isolation                       │
│              (Docker with security restrictions)             │
├─────────────────────────────────────────────────────────────┤
│                     Syscall Filtering                        │
│                    (Seccomp whitelist)                       │
├─────────────────────────────────────────────────────────────┤
│                    Resource Limits                           │
│               (Memory, CPU, time, file size)                 │
├─────────────────────────────────────────────────────────────┤
│                    Output Sanitization                       │
│              (Capped output, error filtering)                │
└─────────────────────────────────────────────────────────────┘
```

## Container Security

### Network Isolation

By default, containers have no network access:

```bash
docker run --network=none ...
```

This prevents:
- Data exfiltration
- Command & control communication
- Attacks on internal services
- Cryptocurrency mining pools

**Selective Network Access**

For jobs that need network (e.g., API calls), configure per job type:

```yaml
job_types:
  - name: api-client
    network_enabled: true
```

When `network_enabled: true`, containers can access any external host. For strict egress control, use external firewalls or Kubernetes NetworkPolicy.

### Read-Only Filesystem

```bash
docker run --read-only ...
```

Writable locations:
- `/output/` - For results
- `/tmp/` - Temporary files (noexec)

### Capability Dropping

All Linux capabilities are dropped except those required for privilege dropping:

```bash
docker run --cap-drop=ALL --cap-add=SETUID --cap-add=SETGID ...
```

**Note on `no-new-privileges`:** Tako VM does NOT use `--security-opt=no-new-privileges` because it conflicts with `gosu`, which is used to drop from root to the sandbox user after installing dependencies. The privilege drop flow is:

1. Container starts as root (required for dependency installation)
2. `gosu` drops privileges to sandbox user (uid 1000)
3. User code executes as unprivileged sandbox user

This trade-off is necessary because:
- Dependencies may require root to install (e.g., system packages)
- `gosu` uses setuid to switch users securely
- `no-new-privileges` blocks setuid, breaking the privilege drop

The risk is mitigated by:
- gVisor runtime (userspace kernel) blocks most privilege escalation
- Seccomp profile restricts dangerous syscalls
- Code runs as non-root after the privilege drop

### Non-Root Execution

Code runs as unprivileged user (uid 1000) inside the container:

```dockerfile
# In Dockerfile
USER sandbox
```

```bash
# At runtime (enforced by Tako VM)
docker run --user=1000:1000 ...
```

This is controlled by `enable_userns: true` (default). Even if container code somehow modifies the Dockerfile or image, the `--user` flag at runtime ensures non-root execution.

### Ephemeral Containers

Containers are destroyed after each execution:

```bash
docker run --rm ...
```

No persistent state between executions.

## Seccomp Filtering

Seccomp (Secure Computing Mode) restricts available syscalls.

### Enabled Syscalls

The whitelist includes safe operations:
- File I/O (read, write, open, close)
- Memory (mmap, brk)
- Process (exit, getpid)
- Time (clock_gettime)

### Blocked Syscalls

Dangerous syscalls are blocked:
- `ptrace` - Process debugging
- `mount` - Filesystem mounting
- `reboot` - System reboot
- `sethostname` - Hostname changes
- `init_module` - Kernel modules

### Custom Profile

The profile is at `tako_vm/seccomp_profile.json`:

```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "syscalls": [
    {
      "names": ["read", "write", "open", ...],
      "action": "SCMP_ACT_ALLOW"
    }
  ]
}
```

## Resource Limits

### Memory Limits

```bash
docker run --memory=512m --memory-swap=512m ...
```

Prevents:
- Memory exhaustion attacks
- Fork bombs consuming RAM

### CPU Limits

```bash
docker run --cpus=1.0 ...
```

Prevents CPU starvation of other processes.

### Process Limits

```bash
docker run --pids-limit=100 ...
```

Prevents fork bombs.

### File Size Limits

```bash
docker run --ulimit=fsize=104857600 ...  # 100MB
```

Prevents disk filling attacks.

### Time Limits

Enforced timeout kills long-running processes:

```python
timeout = job.get("timeout", 30)
subprocess.run(..., timeout=timeout)
```

## Input Validation

### Size Limits

| Input | Limit | Configuration |
|-------|-------|---------------|
| Code | 100KB | `max_code_bytes` |
| Input data | 1MB | `max_input_bytes` |
| Timeout | 300s | `max_timeout` |

### Output Limits

| Output | Limit | Configuration |
|--------|-------|---------------|
| stdout | 64KB | `max_stdout_bytes` |
| stderr | 64KB | `max_stderr_bytes` |
| Single artifact | 10MB | `max_artifact_bytes` |
| Total artifacts | 50MB | `max_total_artifacts_bytes` |

### Dockerfile Build Validation

When building job type containers, Tako VM validates all inputs to prevent injection attacks:

| Validation | Function | Description |
|------------|----------|-------------|
| Docker image | `validate_docker_image()` | Rejects shell injection, newlines, special characters |
| Python version | `validate_python_version()` | Only allows `3.8`, `3.9`, `3.10`, `3.11`, `3.12`, etc. |
| Pip packages | `validate_pip_requirement()` | Rejects URLs, path specifiers, shell characters |
| Environment keys | `validate_env_key()` | POSIX-compliant variable names only |
| Environment values | `validate_env_value()` | Rejects control characters, backticks, `$` |
| Shared code paths | Path validation | Prevents directory traversal |

**Example attack prevention:**

```python
# These malicious inputs are rejected:

# Docker image injection
base_image = "python:3.11\nRUN rm -rf /"  # ❌ Rejected

# Python version injection
python_version = "3.11; apt install malware"  # ❌ Rejected

# Pip package injection
requirements = ["numpy; rm -rf /"]  # ❌ Rejected

# Environment variable injection
environment = {"PATH": "$HOME/malware"}  # ❌ Rejected
```

### Artifact Filename Validation

Output artifacts are validated before collection:

```python
# is_safe_filename() rejects:
- Path separators (/, \)
- Parent directory references (..)
- Hidden files (.filename)
```

This prevents containers from creating artifacts that could overwrite or read unauthorized files.

## Error Sanitization

Stack traces are sanitized to prevent information leakage:

```python
# Internal path: /var/lib/tako-vm/workspace/job-123/code/main.py
# Sanitized:     /code/main.py
```

## API Security

### HTTPS

Always use TLS in production:

```nginx
listen 443 ssl http2;
ssl_protocols TLSv1.2 TLSv1.3;
```

## Threat Model

### In Scope

Tako VM protects against:

| Threat | Mitigation |
|--------|------------|
| Code execution escape | Container isolation, seccomp |
| Resource exhaustion | Memory, CPU, time limits |
| Data exfiltration | Network isolation |
| Disk filling | File size limits |
| Information leakage | Output sanitization |

### Out of Scope

Tako VM does NOT protect against:

| Threat | Reason |
|--------|--------|
| Docker daemon compromise | Requires Docker access |
| Host kernel exploits | Containers share kernel |
| Side-channel attacks | Shared CPU/memory |
| Timing attacks | Execution time visible |

For higher security, consider:
- gVisor (supported by Tako VM)
- Kata Containers
- Dedicated execution hosts
- VM-based isolation

## gVisor Runtime

Tako VM supports gVisor (runsc) for strong container isolation. gVisor provides a userspace kernel that intercepts and emulates syscalls, adding a significant security boundary beyond standard Docker. By default, Tako VM runs in `permissive` mode, which falls back to runc if gVisor is not installed.

### Why gVisor?

| Benefit | Description |
|---------|-------------|
| Userspace kernel | Syscalls handled in userspace, not host kernel |
| Reduced attack surface | Most kernel vulnerabilities don't affect gVisor |
| Container escape prevention | Much harder to escape to host |
| Production-tested | Used by Google Cloud Run, GKE Sandbox |

### Installation

gVisor is required for `strict` security mode. Install it following the [official gVisor installation guide](https://gvisor.dev/docs/user_guide/install/).

**Ubuntu/Debian:**
```bash
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install
sudo systemctl restart docker
```

**Verify installation:**
```bash
docker run --runtime=runsc --rm hello-world
```

### Configuration

```yaml
# tako_vm.yaml
container_runtime: runsc   # 'runsc' (gVisor) or 'runc' (standard Docker)
security_mode: strict      # 'strict' (require gVisor) or 'permissive' (fallback)
```

**Security modes:**

- **permissive** (default): Falls back to standard runc runtime with a warning. Works on all platforms.
- **strict**: Fails with `RuntimeUnavailableError` if gVisor is not available. Recommended for production.

**Environment variable override (useful for testing):**
```bash
TAKO_VM_SECURITY_MODE=permissive pytest tests/ -v
```

### Development on macOS/Windows

gVisor only runs on Linux. For macOS/Windows development, Tako VM includes a Lima VM configuration with gVisor pre-installed:

```bash
# Start the VM
limactl start lima-gvisor.yaml

# Enter the VM
limactl shell tako-gvisor

# Run Tako VM with gVisor
cd ~/tako-vm
pytest tests/ -v
```

The Lima VM provides:
- Ubuntu 24.04 with Docker and gVisor pre-installed
- 4 CPUs, 8GB RAM, 50GB disk
- Home directory mounted for code access

### gVisor vs runc Trade-offs

| Aspect | gVisor (runsc) | Standard (runc) |
|--------|----------------|-----------------|
| Security | Strong (userspace kernel) | Good (kernel namespaces) |
| Performance | ~5-15% overhead | Native speed |
| Compatibility | Most Python code works | Full compatibility |
| Kernel exploits | Protected | Vulnerable |
| Setup complexity | Requires installation | Built into Docker |

**Recommendation:** Use gVisor (`strict` mode) for production and any environment running untrusted or AI-generated code. Use `permissive` mode only for development when gVisor is not available.

## Docker Isolation Limitations

Docker containers share the host kernel, which has security implications:

### What Docker Provides

| Protection | Level | Notes |
|------------|-------|-------|
| Filesystem isolation | Good | Separate root filesystem |
| Process isolation | Good | Separate PID namespace |
| Network isolation | Good | `--network=none` blocks all |
| User isolation | Moderate | UID mapping available |
| Syscall filtering | Good | Seccomp whitelist |

### What Docker Does NOT Provide

| Risk | Description | Mitigation |
|------|-------------|------------|
| Kernel exploits | Container escapes via kernel bugs | Keep kernel updated, use gVisor |
| Resource side-channels | CPU cache timing attacks | Dedicated hosts |
| `/proc` information | Process info leakage | Restrict `/proc` access |
| Device access | Hardware access if not restricted | `--cap-drop=ALL` |

### Stronger Isolation Options

For high-security environments:

**1. gVisor** (Google)
- User-space kernel that intercepts syscalls
- Significant performance overhead
- Strong isolation without VMs

```bash
docker run --runtime=runsc ...
```

**2. Kata Containers**
- Lightweight VMs with container UX
- Hardware-level isolation
- Higher resource overhead

**3. Firecracker** (AWS)
- MicroVMs for serverless
- Used by AWS Lambda
- Sub-second boot times

**4. Dedicated Hosts**
- Run Tako VM on isolated machines
- Network segmentation
- Physical separation

### Recommendation

| Use Case | Recommended Isolation |
|----------|----------------------|
| Development | Docker (default) |
| Internal tools | Docker + seccomp |
| Multi-tenant SaaS | gVisor or Kata |
| High-security | Firecracker or dedicated VMs |

## Architecture Considerations

### Does Containerizing the API Server Add Security?

**Short answer: No.** The API server needs Docker socket access to spawn executor containers. Docker socket access effectively grants root privileges on the host, so containerizing the server doesn't create a meaningful security boundary.

```
Current Model (adequate for most cases):
┌─────────────────────────────────────────┐
│              Host/VM                     │
│  ┌──────────────┐    ┌───────────────┐  │
│  │  Tako VM     │───▶│   Executor    │  │
│  │  Server      │    │   Container   │  │
│  │  (trusted)   │    │  (untrusted)  │  │
│  └──────────────┘    └───────────────┘  │
└─────────────────────────────────────────┘
```

**Why containerize the server anyway?**

- Easier deployment (Docker Compose, Kubernetes)
- Consistent environment across machines
- Simpler updates and rollbacks

**For true separation (future consideration):**

```
High-Security Model (separate hosts):
┌─────────────┐     ┌─────────────────────────────┐
│   Host A    │     │          Host B             │
│  ┌───────┐  │     │  ┌───────┐   ┌──────────┐  │
│  │ API   │──┼────▶│  │Docker │──▶│ Executor │  │
│  │Server │  │ RPC │  │ Agent │   │Container │  │
│  └───────┘  │     │  └───────┘   └──────────┘  │
└─────────────┘     └─────────────────────────────┘
```

This separates the API server from the execution environment entirely, but adds significant complexity.

## Security Checklist

- [ ] Install gVisor and use `security_mode: strict`
- [ ] Enable `enable_seccomp: true`
- [ ] Use HTTPS in production
- [ ] Set appropriate resource limits
- [ ] Keep Docker and gVisor updated
- [ ] Minimize use of `network_enabled: true` jobs
- [ ] Monitor for anomalies
- [ ] Review execution logs
- [ ] Test security controls regularly

### gVisor-Specific Checks

- [ ] Verify gVisor is working: `docker run --runtime=runsc --rm hello-world`
- [ ] Set `container_runtime: runsc` in config
- [ ] Set `security_mode: strict` for production
- [ ] Test your workloads with gVisor (some edge cases may differ)
