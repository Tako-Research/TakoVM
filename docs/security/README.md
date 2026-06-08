---
description: "Tako VM security documentation index — threat-model assessment, /proc exposure analysis, and proposed mitigations."
---

# Tako VM Security Documentation

## Quick Links

- **[Honest Assessment](honest-assessment.md)** - Practical security analysis based on threat models
- **[Proc Exposure Analysis](proc-exposure-vulnerability.md)** - Technical details of `/proc` filesystem access
- **[Proposed Mitigations](mitigations.md)** - Implementation options (AppArmor, gVisor, env var migration)
- **[Solutions Summary](SOLUTIONS.md)** - What can/can't be fixed and why

## Security Status

### ✅ Strong Protections (Already Implemented)

**Container Isolation:**
- Docker namespace isolation (PID, network, mount, IPC)
- Non-root execution (uid 1000)
- Read-only root filesystem
- Capability dropping (`--cap-drop=ALL`)
- No privilege escalation (`--security-opt=no-new-privileges`)

**Seccomp Filtering:** (Enabled by default)
- Blocks dangerous syscalls: `ptrace`, `process_vm_readv/writev`, module loading
- Prevents most privilege escalation attempts
- Profile: [tako_vm/seccomp_profile.json](../../tako_vm/seccomp_profile.json)

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
- External secret management (AWS Secrets Manager, HashiCorp Vault)
- gVisor runtime for stronger isolation
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
- Docker namespace isolation (shared kernel)
- Can read `/proc` filesystem
- Good for trusted code

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

## Implementation Priorities

### Immediate (Already Done)
- ✅ Seccomp enabled by default
- ✅ Security documentation with honest assessment
- ✅ Container hardening (non-root, read-only FS, capabilities)

### Short-term (If Needed)
- 🔵 AppArmor profile for Linux deployments (optional)
- 🔵 Audit logging for job submissions
- 🔵 Artifact scanning for leaked credentials

### Long-term (Multi-Tenant)
- 🔵 gVisor runtime support
- 🔵 Per-tenant resource isolation
- 🔵 Kata Containers for VMs

## References

- [Docker Security Best Practices](https://docs.docker.com/engine/security/)
- [Seccomp BPF](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
- [AppArmor Documentation](https://gitlab.com/apparmor/apparmor/-/wikis/Documentation)
- [gVisor](https://gvisor.dev/)
- [Equixly: AI Container Security](https://equixly.com/blog/2025/11/04/path-traversal-ai-containers/)
