---
description: "Tako VM security documentation — vulnerability reporting, threat model, hardening guidance, and advisories."
---

# Security

Tako VM runs untrusted, often AI-generated, code. This section documents the isolation model, what it does and does not protect against, and how to harden a deployment.

!!! tip "Found a vulnerability?"
    Report it privately via [GitHub security advisories](https://github.com/Tako-Research/TakoVM/security/advisories/new) — never in a public issue. See the [security policy](policy.md) for response timelines.

## In this section

- **[Security Policy](policy.md)** — how to report vulnerabilities, response timelines, supported versions
- **[Threat Model](honest-assessment.md)** — practical security analysis: what's protected, what isn't, and for which workloads
- **[Hardening Guide](../deployment/security.md)** — production configuration: `security_mode: strict`, gVisor, seccomp, network policy
- **[Analysis: /proc exposure](proc-exposure-vulnerability.md)** — technical analysis of `/proc` filesystem access from sandboxed code
- **[Mitigations](mitigations.md)** — implementation options (AppArmor, gVisor, env var migration)

## Security Status

### ✅ Strong Protections (Already Implemented)

**Container Isolation:**
- Docker namespace isolation (PID, network, mount, IPC)
- Non-root execution (uid 1000, dropped via `gosu` after dependency install)
- Read-only root filesystem
- Capability dropping (`--cap-drop=ALL`)

!!! note "`no-new-privileges` trade-off"
    Tako VM deliberately does **not** set `--security-opt=no-new-privileges`: it would block the `gosu` setuid call that drops from root to the sandbox user after installing dependencies. The [hardening guide](../deployment/security.md) explains the privilege-drop flow and compensating controls.

**Seccomp Filtering:** (Enabled by default)
- Blocks dangerous syscalls: `ptrace`, `process_vm_readv/writev`, module loading
- Prevents most privilege escalation attempts
- Profile: [tako_vm/seccomp_profile.json](https://github.com/Tako-Research/TakoVM/blob/main/tako_vm/seccomp_profile.json)

**Resource Limits:**
- Memory, CPU, file size, process count
- Timeouts enforced
- Prevents resource exhaustion

**API Security:**
- Path traversal prevention on artifact downloads
- Input validation on all parameters
- Dockerfile injection prevention
- Artifact filename sanitization

### ⚠️ Expected Behaviors (Not Vulnerabilities)

**User code has access to:**
- Its own environment variables (via `os.environ` or `/proc/self/environ`)
- Process metadata (`/proc/self/exe`, `/proc/self/cmdline`)
- Input data (`/input/` directory)
- Output directory (`/output/`)

**Why this is OK:**
Code needs access to its configuration to function. If code requires an API key to call an API, it must have access to that key. The question isn't "can code access config?" but "should secrets be in job submission?"

### 🔵 Optional Enhancements (For Specific Threat Models)

**For untrusted/AI-generated code:**
- The built-in gVisor runtime (`container_runtime: runsc` + `security_mode: strict`) — see the [hardening guide](../deployment/security.md)
- External secret management (AWS Secrets Manager, HashiCorp Vault)
- AppArmor/SELinux to restrict `/proc` access (Linux only)
- Artifact scanning for leaked credentials

**For multi-tenant SaaS:**
- Per-tenant Docker networks
- Dedicated execution hosts
- Kata Containers for VM-level isolation
- Comprehensive audit logging

## Threat Model Decision Tree

```
Are you running your own code (trusted)?
├─ YES → Current security is GOOD
│         - Container isolation sufficient
│         - Env vars fine for config
│         - Focus on resource limits & access control
│
└─ NO → Are users writing code for your platform?
   ├─ YES (semi-trusted) → Add monitoring
   │        - Rate limiting per user
   │        - Audit logging
   │        - Output scanning
   │        - Network isolation (already default)
   │
   └─ NO → Running AI/untrusted code?
           - Don't pass secrets in submission
           - Use external secret manager
           - Consider gVisor runtime
           - Consider AppArmor/SELinux
           - Scan artifacts before download
```

## Common Questions

### Q: Is `/proc/self/environ` exposure a vulnerability?

**A:** Not for trusted code. It's expected behavior - code needs access to its environment. For untrusted code, use external secret management instead of passing secrets.

### Q: Should I migrate env vars to files?

**A:** Only if:
1. You have compliance requirements forbidding env var secrets
2. You want to prevent accidental logging (minor benefit)
3. You're building a multi-tenant platform (better audit trail)

For trusted code execution, env vars are simpler and equally secure.

### Q: Can seccomp block `/proc` reads?

**A:** No. Seccomp blocks syscalls, not file paths. It can't distinguish between `open("/proc/self/environ")` and `open("/input/data.json")` - both use the same syscall. You need AppArmor/SELinux or gVisor for path-based restrictions.

### Q: Is Tako VM safe for production?

**A:** Yes, for trusted code execution:
- ✅ CI/CD pipelines
- ✅ Data processing jobs
- ✅ Your own automation scripts
- ✅ Internal tooling

**With additional hardening for:**
- ⚠️ User-submitted code (add monitoring + rate limiting)
- ⚠️ AI-generated code (external secrets + gVisor)
- ⚠️ Multi-tenant SaaS (dedicated hosts + Kata)

### Q: How does Tako VM compare to AWS Lambda security?

**Tako VM:**
- Docker namespace isolation (shared kernel under `runc`)
- With gVisor enabled, syscalls hit a userspace kernel and `/proc` is a synthetic view
- Good for trusted code out of the box; enable strict gVisor mode for untrusted code

**AWS Lambda:**
- Firecracker microVMs (separate kernel per function)
- Fake `/proc` filesystem
- Stronger isolation (at higher cost)

Tako VM is closer to Docker-based platforms (Modal, Replit) than Lambda.

### Q: What about container escape vulnerabilities?

**A:** Container escapes via kernel vulnerabilities are:
1. Rare (few discovered per year)
2. Quickly patched
3. Require specific kernel versions
4. Mitigated by seccomp + capabilities

For paranoid deployments, use gVisor (user-space kernel) or Kata (VM isolation).

## Current State and Roadmap

**Shipped today:**

- gVisor (runsc) runtime support with `permissive`/`strict` security modes
- Seccomp filtering, enabled by default
- Container hardening: non-root execution, read-only filesystem, dropped capabilities
- Network isolation (`--network=none` by default), resource and input limits
- API-key authentication and rate limiting (auth off by default — [enable it in production](../deployment/security.md#api-security))

**Roadmap** (tracked in [GitHub issues](https://github.com/Tako-Research/TakoVM/issues?q=is%3Aissue+is%3Aopen+label%3Asecurity)):

- AppArmor profile for `/proc` path restrictions under runc
- Per-tenant quotas and audit logging
- Artifact scanning for leaked credentials

## References

- [Docker Security Best Practices](https://docs.docker.com/engine/security/)
- [Seccomp BPF](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
- [AppArmor Documentation](https://gitlab.com/apparmor/apparmor/-/wikis/Documentation)
- [gVisor](https://gvisor.dev/)
- [Equixly: AI Container Security](https://equixly.com/blog/2025/11/04/path-traversal-ai-containers/)
