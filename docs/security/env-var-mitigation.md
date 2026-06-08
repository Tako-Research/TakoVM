---
description: "Mitigating environment-variable exposure in Tako VM — why /proc/self/environ leaks secrets and how to reduce the risk."
---

# Environment Variable Mitigation

## Problem

Environment variables are exposed via `/proc/self/environ`. Any malicious code can read:
```python
with open('/proc/self/environ', 'rb') as f:
    secrets = f.read()  # Gets ALL env vars including API keys
```

## Solution: Use Configuration Files Instead

Instead of passing secrets via `--env` flags, write them to read-only files in `/input/`.

### Current Implementation

#### Step 1: Update worker.py

Add this helper method to the `CodeExecutor` class:

```python
def _prepare_config_file(self, job_type: JobType, input_dir: Path) -> None:
    """
    Write job type configuration to read-only file instead of env vars.

    This prevents secrets from being exposed via /proc/self/environ.
    """
    if job_type.environment:
        config_data = {
            "environment": job_type.environment,
        }

        config_file = input_dir / "_config.json"
        config_file.write_text(json.dumps(config_data))
        config_file.chmod(0o444)  # Read-only for all users
        logger.debug(f"Wrote config to {config_file}")

def _prepare_requirements_file(self, requirements: List[str], input_dir: Path) -> None:
    """
    Write requirements to file instead of an environment variable.
    """
    if requirements:
        reqs_file = input_dir / "_requirements.txt"
        reqs_file.write_text("\n".join(requirements))
        reqs_file.chmod(0o444)  # Read-only
        logger.debug(f"Wrote {len(requirements)} requirements to {reqs_file}")
```

Then in `_run_container`, REPLACE this:

```python
# OLD: Passing via env vars (INSECURE)
for key, value in job_type.environment.items():
    cmd.append(f"--env={key}={value}")

if validated_reqs:
    cmd.append("--env=DEPENDENCIES=<comma-separated requirements>")
```

With this:

```python
# NEW: Write to files (SECURE - not in /proc)
self._prepare_config_file(job_type, input_dir)
self._prepare_requirements_file(validated_reqs, input_dir)
# No --env flags for secrets!
```

#### Step 2: Update entrypoint.sh

Replace the env var reading:

```bash
# OLD: Read from env var
if [ -n "$DEPENDENCIES" ]; then
    echo "$DEPENDENCIES" | tr ',' '\n' > /tmp/requirements.txt
    uv pip install -r /tmp/requirements.txt
fi
```

With file reading:

```bash
# NEW: Read from file
if [ -f /input/_requirements.txt ]; then
    uv pip install --target /tmp/site-packages -r /input/_requirements.txt
fi
```

#### Step 3: Update User Documentation

Tell users to read config from files:

```python
# In user-submitted code:
import json

# Read configuration (replaces os.environ access)
with open('/input/_config.json') as f:
    config = json.load(f)

api_key = config['environment'].get('API_KEY')
database_url = config['environment'].get('DATABASE_URL')
```

### Why This Works

1. **Files are not in /proc** - User code can still read `/input/_config.json`, but:
   - It's explicit (not hidden in env vars)
   - You can scan these files for secrets before job submission
   - It's documented as the intended way to pass config

2. **Still accessible, but controlled** - You're not trying to hide secrets from user code (impossible), but:
   - Secrets aren't accidentally logged via env dumps
   - Debugging tools don't expose env vars
   - Attack surface is more obvious

3. **Works everywhere** - No Linux-specific features needed

### Migration Path

**Phase 1: Add file support (backward compatible)**
- Add `_prepare_config_file()` method
- Write files in addition to env vars
- Update docs to recommend files

**Phase 2: Deprecation warning**
- Log warnings when `job_type.environment` is used
- Encourage migration to file-based config

**Phase 3: Remove env var support**
- Stop passing `job_type.environment` via `--env`
- Only use files

### Limitations

**This does NOT prevent user code from reading the config file.** The goal is:
- ✅ Remove secrets from `/proc/self/environ`
- ✅ Make config access explicit and auditable
- ✅ Allow scanning for secrets before job execution
- ❌ Does NOT prevent malicious code from reading `/input/_config.json`

**For true secret isolation,** users must:
1. Use external secret management (Vault, AWS Secrets Manager)
2. User code fetches secrets at runtime
3. Never pass secrets in job submission

### Testing

Add to `tests/test_security_mitigations.py`:

```python
def test_config_file_instead_of_env_vars(executor):
    """Verify secrets are in files, not env vars."""
    code = """
import json
import os

# Check env vars don't contain secrets
has_api_key_in_env = 'API_KEY' in os.environ

# Check config file exists
try:
    with open('/input/_config.json') as f:
        config = json.load(f)
    has_api_key_in_file = 'API_KEY' in config.get('environment', {})
except FileNotFoundError:
    has_api_key_in_file = False

with open('/output/result.json', 'w') as f:
    json.dump({
        'api_key_in_env': has_api_key_in_env,
        'api_key_in_file': has_api_key_in_file
    }, f)
"""

    from tako_vm.job_types import JobType
    job_type = JobType(
        name="test-config-file",
        requirements=[],
        environment={"API_KEY": "secret-key-12345"}
    )
    executor.registry.register(job_type)

    job = {
        "code": code,
        "input_data": {},
        "job_type": "test-config-file"
    }

    result = executor.execute_job(job)
    assert result["success"]

    # PASS: Secret is in file, NOT in env var
    assert result["output"]["api_key_in_file"] is True
    assert result["output"]["api_key_in_env"] is False
```

## Summary

**Before:**
```
User Code → reads /proc/self/environ → gets all secrets
```

**After:**
```
User Code → reads /input/_config.json → gets expected config
User Code → tries /proc/self/environ → only system vars, no secrets
```

Not perfect, but significantly better:
- ✅ Secrets not in env vars
- ✅ Config access is explicit
- ✅ Works on all platforms
- ✅ Backward compatible migration path
