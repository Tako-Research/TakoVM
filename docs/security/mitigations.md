# Security Mitigations

This document describes mitigations for the [/proc filesystem exposure vulnerability](proc-exposure-vulnerability.md) identified in the [Equixly security research](https://equixly.com/blog/2025/11/04/path-traversal-ai-containers/).

## Summary

User code running inside Tako VM containers can read `/proc/self/environ` and other `/proc` paths, potentially leaking environment variables and process metadata. The mitigations below reduce this risk.

## Current Protections (Implemented)

These protections are active in the current release:

### Seccomp Profile

A seccomp profile (`tako_vm/seccomp_profile.json`) restricts dangerous syscalls like `ptrace`, `process_vm_readv`, and `process_vm_writev`. Enable it in your config:

```yaml
enable_seccomp: true
```

!!! warning "Seccomp does not block /proc reads"
    Seccomp filters syscalls, not file paths. The `open` and `read` syscalls must be allowed for Python to function, so `/proc` files remain readable. Seccomp blocks other dangerous operations like process debugging.

### Container Hardening

Every container runs with:

- `--cap-drop=ALL` — all Linux capabilities removed
- `--read-only` — read-only root filesystem
- `--network=none` — no network access (by default)
- Non-root execution (uid 1000 via gosu)
- Resource limits (memory, CPU, PIDs, file size)

### gVisor Runtime

When using gVisor (`container_runtime: runsc`), the userspace kernel provides an additional isolation boundary. gVisor intercepts syscalls and limits what `/proc` exposes compared to the host kernel.

## Recommended Actions

### Do Not Pass Secrets as Environment Variables

This is the single most important mitigation. User code can read `/proc/self/environ` to extract any environment variable set on the container.

**Instead of:**
```yaml
job_types:
  - name: api-client
    environment:
      API_KEY: "sk-secret-123"  # Readable via /proc!
```

**Use:**
- Pass secrets through `/input/data.json` (mounted read-only, scoped to one job)
- Use your platform's secret management (Vault, AWS Secrets Manager)
- Use pre-built images with secrets baked in at build time (not at runtime)

### Enable gVisor for Production

gVisor's `/proc` implementation is more restricted than the host kernel's. Set:

```yaml
container_runtime: runsc
security_mode: strict
```

## Planned Mitigations

These are tracked as future work:

| Mitigation | Description | Status |
|-----------|-------------|--------|
| `/proc` path masking | Use Docker `--security-opt=mask=` to hide sensitive `/proc` paths | Planned |
| AppArmor profile | Deny access to `/proc/*/environ` and `/proc/*/fd/` | Planned |
| Config file migration | Move job type environment vars from env to read-only files | Planned |
| Requirements via file | Pass `TAKO_REQUIREMENTS` via file instead of env var | Planned |

## References

- [/proc Exposure Vulnerability Analysis](proc-exposure-vulnerability.md)
- [Equixly: The False Security of AI Containers](https://equixly.com/blog/2025/11/04/path-traversal-ai-containers/)
- [Docker Security Best Practices](https://docs.docker.com/engine/security/)
