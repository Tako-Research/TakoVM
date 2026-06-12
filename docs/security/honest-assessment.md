---
description: "An honest security assessment of Tako VM — strong isolation for trusted code, and the real-world limits for untrusted AI-generated code."
---

# Honest Security Assessment: Tako VM

**TL;DR:** Tako VM provides strong isolation for **trusted code execution**. If you're running your own code, CI/CD pipelines, or data processing jobs, the current security is good. The `/proc` exposure and env var concerns are mostly relevant for **untrusted AI-generated code**, which is a different threat model.

---

## What You're Actually Protected Against

### ✅ API-Level Path Traversal (STRONG)
```python
# Attacker tries: GET /artifacts?path=../../etc/passwd
# Result: BLOCKED by .is_relative_to() validation
```

**Why it matters:** External attackers can't escape artifact directories via API.

**Implementation:** [app.py:1036-1042](https://github.com/las7/TakoVM/blob/main/tako_vm/server/app.py#L1036-L1042)

### ✅ Container Escape (GOOD with proper config)
```python
# Docker provides namespace isolation
# - Separate PID namespace
# - Separate network namespace
# - Separate mount namespace
# - Non-root user (uid 1000)
# - Capability dropping
```

**Why it matters:** User code can't access host system or other containers.

**Limitations:**
- Kernel vulnerabilities could allow escape (rare, quickly patched)
- Shared kernel with host (use gVisor/Kata for VM-level isolation)

### ✅ Resource Exhaustion (GOOD)
```yaml
# Memory limits, CPU limits, timeout, ulimits all enforced
# User code can't DoS the system
```

**Why it matters:** Prevents one job from affecting others.

### ✅ Dangerous Syscalls (GOOD with seccomp)
```json
// Blocks: ptrace, process_vm_readv, kernel module loading
// Status: Enabled by default (enable_seccomp: true)
```

**Why it matters:** Prevents container introspection and privilege escalation attempts.

---

## What You're NOT Protected Against (And Why That's OK)

### ⚠️ User Code Reading /proc

**The "Vulnerability":**
```python
# User code can do this:
with open('/proc/self/environ', 'rb') as f:
    env_vars = f.read()  # Gets environment variables
```

**Is this bad?** **Depends on your threat model:**

#### Threat Model 1: Running Your Own Code
```python
# Your own Python script for data processing
job = {
    "code": "import pandas as pd; df = pd.read_csv('/input/data.csv')",
    "job_type": "data-processing"
}
```

**Answer:** `/proc` access is **NOT A CONCERN**
- You wrote the code, you trust it
- Code is supposed to have access to its environment
- This is like being worried that your code can read its own memory

#### Threat Model 2: Running Untrusted AI-Generated Code
```python
# AI agent generated code, user didn't review it
job = {
    "code": gpt4_generated_code,  # Could be malicious
    "job_type": "ai-assistant"
}
```

**Answer:** `/proc` access **IS A CONCERN**
- Code might exfiltrate secrets
- BUT: The real problem is passing secrets in the first place
- Solution: Don't pass secrets, use external secret manager

**The Honest Truth:** If code needs secrets to function, it will have access to them whether via:
- `os.environ['API_KEY']` (env var)
- `open('/input/_config.json')` (file)
- `open('/proc/self/environ')` (proc)

All three are equally accessible. The solution isn't hiding secrets better, it's **not passing them at all** for untrusted code.

### ⚠️ Binary Extraction via /proc/self/exe

**The "Vulnerability":**
```python
shutil.copy('/proc/self/exe', '/output/python_binary')  # Can extract Python runtime
```

**Is this bad?** **No, not really:**
- Python interpreter is open source anyway
- Knowing the Python version doesn't help escape the container
- This is more of a curiosity than a security issue

**When it matters:**
- If you have proprietary compiled extensions loaded
- If you're using a custom Python build with vulnerabilities
- Even then, knowledge doesn't equal exploitation

### ⚠️ File Descriptor Enumeration

**The "Vulnerability":**
```python
for fd in range(256):
    target = os.readlink(f'/proc/self/fd/{fd}')
    print(target)  # See what files are open
```

**Is this bad?** **Mildly concerning, but limited:**
- Only shows FDs for the current process (not other containers)
- In Tako VM, containers are isolated - no shared state
- Worst case: User sees their own open files (which they already know)

**Real concern:** If Tako VM opened a database connection in the same process (it doesn't)

---

## Environment Variables: The Real Story

### The Equixly Concern

The Equixly article warns about `/proc/self/environ` exposure. But let's be honest:

**If user code NEEDS the API key to function**, then hiding it doesn't help:

```python
# Scenario: Data processing job that calls an API

# Approach 1: Env var
api_key = os.environ['API_KEY']
requests.get('https://api.example.com', headers={'Authorization': api_key})

# Approach 2: File
with open('/input/_config.json') as f:
    api_key = json.load(f)['environment']['API_KEY']
requests.get('https://api.example.com', headers={'Authorization': api_key})

# Approach 3: Proc
with open('/proc/self/environ', 'rb') as f:
    api_key = extract_from_proc(f.read())
requests.get('https://api.example.com', headers={'Authorization': api_key})
```

**All three work identically.** Moving secrets to files just changes the API.

### When Env Var → File Migration Actually Helps

**1. Accidental Logging**
Many frameworks dump `os.environ` in error messages. Files are less likely to leak.

**2. Cross-Container Attacks**
If containers share PID namespace (Tako VM doesn't), `/proc/<other_pid>/environ` could leak. Not applicable here.

**3. Compliance Requirements**
Some standards prohibit secrets in env vars (regardless of practicality).

**4. Third-Party Package Scanning**
Malicious dependencies might scan env vars. Files require explicit reads.

### When It Doesn't Matter

**If you're running your own code:**
- You trust the code anyway
- Code is supposed to access its config
- Env vars are simpler and standard

**If you're running untrusted code:**
- Files don't solve the problem
- Code can read files just as easily
- Real solution: external secret manager

---

## Practical Security Recommendations

### For Trusted Code (Your Team's Scripts)

**Current Tako VM security is GOOD:**
- ✅ Seccomp enabled by default
- ✅ Container isolation
- ✅ Resource limits
- ✅ Non-root execution
- ✅ Read-only filesystem

**What you should do:**
- ✅ Keep using env vars for config (simpler)
- ✅ Rely on Docker isolation
- ✅ Monitor resource usage
- ⚠️ Don't overthink `/proc` access - it's expected behavior

### For Semi-Trusted Code (User Scripts on Your Platform)

**Add these protections:**
- ✅ Rate limiting per user
- ✅ Audit logging of job submissions
- ✅ Artifact scanning for secrets
- ✅ Network isolation (already default)
- ⚠️ Consider env var → file migration (marginal benefit)

### For Untrusted Code (AI Agents, Public Code Execution)

**You need stronger isolation:**
- ✅ **Don't pass secrets in job submission**
- ✅ Use external secret manager (AWS Secrets Manager, Vault)
- ✅ Consider gVisor runtime (`docker_runtime: runsc`)
- ✅ Consider AppArmor/SELinux to block `/proc` reads
- ✅ Artifact scanning before download
- ✅ Separate execution hosts per tenant

**Architecture for untrusted code:**
```python
# ❌ BAD: Pass secrets in job
job = {
    "code": untrusted_ai_code,
    "job_type": "api-caller",  # Has API_KEY in environment
}

# ✅ GOOD: Code fetches secrets via IAM role
code = """
import boto3
secrets = boto3.client('secretsmanager')
api_key = secrets.get_secret_value(SecretId='prod/api-key')['SecretString']
# Container has IAM role, no secrets in job submission
"""
```

---

## What Should You Actually Do?

### Priority 1: Document Honestly (This Week) ✅

Update README.md and docs to say:
- User code has access to its environment (expected)
- `/proc` is readable (Linux behavior, not a bug)
- Env vars are accessible to code (by design)
- For untrusted code, use external secrets

### Priority 2: Add Logging (Optional)

Log when code reads sensitive paths (for audit trail):
```python
# In AppArmor profile (Linux only)
audit /proc/*/environ r,  # Log reads, don't block
```

### Priority 3: Consider Advanced Isolation (If Running Untrusted Code)

Only if you're building a code execution service for untrusted users:
- gVisor for stronger isolation
- AppArmor to block `/proc`
- Per-tenant Docker networks
- External secret management

### DON'T Do These (Waste of Time)

❌ Migrate env vars to files for your own code (no benefit)
❌ Try to "hide" `/proc` from user code (impossible without AppArmor/gVisor)
❌ Treat expected behavior as vulnerabilities

---

## Comparison to Other Systems

### AWS Lambda
- ✅ Better isolation (Firecracker microVMs)
- ❌ Can't access `/proc` at all (fake proc)
- ✅ IAM roles for secret access
- ❌ More expensive

### Modal Labs
- ✅ Similar Docker isolation
- ⚠️ Same `/proc` exposure as Tako VM
- ✅ Secret management via platform

### Replit
- ⚠️ Weaker isolation (shared kernel)
- ⚠️ Similar `/proc` concerns
- ✅ Secrets via environment variables

**Tako VM is on par with industry standard code execution services.**

---

## Final Verdict

**For your use case (trusted code execution):**
- Current security is **GOOD ENOUGH**
- `/proc` access is expected behavior, not a vulnerability
- Env vars are fine for configuration
- Focus on container isolation, resource limits, and access control

**If you want to offer untrusted code execution:**
- Don't pass secrets in job submission (fundamental architecture change)
- Add gVisor for stronger isolation
- Add per-tenant resource isolation
- This is a different product tier

**Bottom line:** The Equixly article is about AI code execution platforms where users don't trust the code they're running. If you trust your code, the current security is solid.
