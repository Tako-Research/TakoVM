---
description: "Tako VM security solutions and limitations: what is already fixed (seccomp, path-traversal protection) and what cannot be fully mitigated."
---

# Security Solutions & Limitations

## Current Status

✅ **Already Fixed:**
- Seccomp enabled by default (`enable_seccomp: bool = Field(default=True)`)
- Seccomp profile blocks `ptrace`, `process_vm_readv`, `process_vm_writev`
- API-level path traversal protection (strong)

⚠️ **Partially Fixable:**
- Environment variable secrets (can move to files)
- Requirements exposure (can move to files)

❌ **Cannot Fix (Fundamental Limitations):**
- User code can always read `/proc/self/` files
- User code can always read `/input/` files
- Seccomp cannot block file reads

---

## What We CAN Fix

### 1. Move Secrets from Env Vars to Files ✅

**Why it helps:**
- Removes secrets from `/proc/self/environ`
- Makes config access explicit and auditable
- Easier to scan for secrets before job execution

**Implementation:** [env-var-mitigation.md](env-var-mitigation.md)

**Result:**
```python
# Before: Vulnerable
os.environ['API_KEY']  # Works, exposed via /proc

# After: Secure from /proc
with open('/input/_config.json') as f:
    config['environment']['API_KEY']  # Works, but explicit
```

**Does NOT prevent:** Malicious code from reading `/input/_config.json`

### 2. Add AppArmor Profile (Linux Only) ✅

**Why it helps:**
- Blocks `/proc/self/environ`, `/proc/self/exe`, `/proc/self/fd/` at kernel level
- User code gets `PermissionError` when trying to access

**Implementation:**
```bash
sudo cp tako_vm/apparmor_profile.txt /etc/apparmor.d/tako-vm
sudo apparmor_parser -r /etc/apparmor.d/tako-vm

# In worker.py:
cmd.append("--security-opt=apparmor=tako-vm")
```

**Limitations:**
- ❌ Only works on Linux with AppArmor
- ❌ Not available on macOS
- ❌ Requires root access to install profile
- ❌ Some distros don't have AppArmor (use SELinux instead)

### 3. Add gVisor Runtime (Strong Isolation) ✅

**Why it helps:**
- User-space kernel intercepts all syscalls
- Can block or fake `/proc` reads
- Stronger isolation than Docker alone

**Implementation:**
```yaml
# In tako_vm.yaml:
docker_runtime: runsc  # Use gVisor instead of runc
```

**Limitations:**
- ⚠️ ~50-100ms overhead per job
- ⚠️ Requires gVisor installation
- ⚠️ Some syscalls not supported (rare edge cases)

---

## What We CANNOT Fix

### 1. Complete /proc Blocking ❌

**Why it's impossible:**
- Seccomp can only block syscalls, not file paths
- The `open()` syscall is needed for legitimate file access
- Cannot selectively block `open("/proc/self/environ")` vs `open("/input/data.json")`

**Alternatives:**
- Use AppArmor/SELinux (Linux only)
- Use gVisor (all platforms, performance cost)
- Accept it as a limitation and document it

### 2. Prevent User Code from Reading Config Files ❌

**Why it's impossible:**
- User code NEEDS to read `/input/` for input data
- Cannot distinguish between:
  - Reading `/input/data.json` (intended)
  - Reading `/input/_config.json` (also intended, but contains secrets)

**The Real Solution:**
User code is SUPPOSED to have access to config. The problem is:
- ❌ **Wrong approach:** Try to hide secrets from user code (impossible in same container)
- ✅ **Right approach:** Don't put secrets in job submission at all

**How users should handle secrets:**

```python
# ❌ BAD: Pass secret in job submission
job = {
    "code": my_code,
    "job_type": "api-call",  # job_type has API_KEY in environment
}

# ✅ GOOD: User code fetches secret from external source
code = """
import boto3
secrets = boto3.client('secretsmanager')
api_key = secrets.get_secret_value(SecretId='prod/api_key')['SecretString']

# Use api_key here...
"""
```

---

## Architecture Comparison

### Current: Docker Only
```
┌─────────────────────────────────────┐
│  User Code                          │
│  - Can read /proc/self/environ      │
│  - Can read /proc/self/exe          │
│  - Can read /input/_config.json     │
│  - Run as uid 1000 (non-root)       │
└─────────────────────────────────────┘
           │
           │ Docker isolation
           ▼
┌─────────────────────────────────────┐
│  Host Kernel (shared)               │
│  - /proc is mounted from host       │
│  - Seccomp blocks dangerous syscalls│
└─────────────────────────────────────┘
```

**Protection Level:** ⚠️ Medium
- ✅ API keys in env vars exposed
- ✅ Binary extraction possible
- ✅ Process enumeration possible
- ❌ No container escape (with proper hardening)

### With Env Var Migration
```
┌─────────────────────────────────────┐
│  User Code                          │
│  - /proc/self/environ is clean ✅   │
│  - Can read /input/_config.json ⚠️  │
└─────────────────────────────────────┘
```

**Protection Level:** 🟢 Good
- ✅ Env vars don't contain secrets
- ⚠️ Secrets still in container (via files)
- ✅ Easier to audit

### With AppArmor (Linux Only)
```
┌─────────────────────────────────────┐
│  User Code                          │
│  - /proc/self/environ → DENIED ✅   │
│  - /proc/self/exe → DENIED ✅       │
│  - /proc/self/fd/* → DENIED ✅      │
└─────────────────────────────────────┘
           │
           │ AppArmor MAC
           ▼
┌─────────────────────────────────────┐
│  Kernel enforces profile            │
│  - PermissionError on /proc reads   │
└─────────────────────────────────────┘
```

**Protection Level:** 🟢 Good
- ✅ /proc exposure blocked
- ✅ Works transparently
- ❌ Linux-only

### With gVisor
```
┌─────────────────────────────────────┐
│  User Code                          │
│  - Syscalls intercepted by gVisor   │
└─────────────────────────────────────┘
           │
           │ All syscalls go through gVisor
           ▼
┌─────────────────────────────────────┐
│  gVisor (User-space kernel)         │
│  - Can fake /proc reads             │
│  - Stronger isolation               │
└─────────────────────────────────────┘
           │
           │ Only safe syscalls reach host
           ▼
┌─────────────────────────────────────┐
│  Host Kernel                        │
└─────────────────────────────────────┘
```

**Protection Level:** 🟢 Excellent
- ✅ Strong syscall filtering
- ✅ Can fake/block /proc
- ✅ Protects against kernel exploits
- ⚠️ Performance overhead (~50-100ms)

---

## Recommended Implementation Priority

### Phase 1: Immediate (This Week) ✅
1. ✅ Seccomp already enabled by default
2. **Migrate env vars to files** (see [env-var-mitigation.md](env-var-mitigation.md))
3. **Document /proc limitations** in README.md and security docs
4. **Add warning logging** when env vars contain "key", "token", "password", "secret"

### Phase 2: Enhanced (This Month) 🟡
1. **AppArmor profile** (optional, for Linux deployments)
2. **Secret scanning** on artifact downloads
3. **Audit logging** for suspicious /proc access patterns

### Phase 3: Advanced (Future) 🔵
1. **gVisor support** (`docker_runtime: runsc` config option)
2. **Kata Containers** for VM-level isolation
3. **Multi-tenant isolation** (separate Docker networks per tenant)

---

## The Honest Answer

**Can we completely solve /proc exposure?**

**No, not without:**
1. AppArmor/SELinux (Linux-only) **OR**
2. gVisor/Kata (performance cost) **OR**
3. Accepting it as a documented limitation

**Can we solve env var secrets leakage?**

**Yes, by:**
1. Moving secrets to files (prevents /proc exposure)
2. Documenting that user code can read files
3. Recommending external secret management

**Bottom line:**
- For trusted code: Current Docker isolation is fine
- For semi-trusted code: Migrate env vars to files
- For untrusted code: Use AppArmor or gVisor
- For multi-tenant: Use dedicated execution hosts with Kata Containers

---

## Next Steps

1. Read [env-var-mitigation.md](env-var-mitigation.md) for implementation guide
2. Decide on AppArmor vs accepting /proc limitation
3. Update user documentation with security best practices
4. Consider gVisor for future "enhanced security" tier
