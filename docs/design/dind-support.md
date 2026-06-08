---
description: "Docker-in-Docker (DinD) with Tako VM — how containers are spawned today and what would be required to support nested Docker execution."
---

# Docker-in-Docker (DinD) Support

This document explains the Docker-in-Docker deployment pattern and what Tako VM would need to support it.

## Background: How Tako VM Spawns Containers

Tako VM executes user code by:

1. Creating a temporary directory with the code and input files
2. Calling Docker to start an executor container
3. Mounting the temp directory into the executor via bind mount
4. Reading results from the mounted output directory

```python
# Simplified from tako_vm/execution/worker.py
docker_cmd = [
    "docker", "run",
    "-v", f"{workspace}/code:/code:ro",      # Bind mount
    "-v", f"{workspace}/output:/output",
    "code-executor:latest"
]
```

**Key assumption**: Tako VM talks to a Docker daemon, and that daemon can see the filesystem paths Tako VM uses.

---

## Deployment Scenario 1: Direct on Host

```
┌─────────────────────────────────────────────────────────┐
│  HOST                                                    │
│                                                          │
│  Tako VM (process)                                       │
│     │                                                    │
│     │ creates /tmp/job-123/code/main.py                 │
│     │                                                    │
│     ▼                                                    │
│  Docker Daemon                                           │
│     │                                                    │
│     │ mounts /tmp/job-123/code → ✅ exists on host      │
│     ▼                                                    │
│  ┌─────────────────┐                                    │
│  │ Executor        │                                    │
│  │ Container       │                                    │
│  └─────────────────┘                                    │
└─────────────────────────────────────────────────────────┘
```

**This works** because Tako VM and Docker daemon share the same filesystem.

---

## Deployment Scenario 2: Tako VM in Container (Socket Mount)

```
┌─────────────────────────────────────────────────────────┐
│  HOST                                                    │
│                                                          │
│  Docker Daemon ◄─── /var/run/docker.sock                │
│     │                     ▲                              │
│     │                     │ (mounted into Tako VM)       │
│     │              ┌──────┴──────────────────────┐      │
│     │              │  Tako VM Container          │      │
│     │              │                             │      │
│     │              │  creates /tmp/job-123/...   │      │
│     │              │  (inside container only!)   │      │
│     │              └─────────────────────────────┘      │
│     │                                                    │
│     │ mounts /tmp/job-123/code → ❌ doesn't exist       │
│     ▼                        on HOST                     │
│  ┌─────────────────┐                                    │
│  │ Executor        │                                    │
│  │ Container       │                                    │
│  └─────────────────┘                                    │
└─────────────────────────────────────────────────────────┘
```

**Problem**: Docker daemon runs on host, looks for paths on host filesystem.

**Solution**: Shared workspace (documented in how-to-deploy.md):
- Mount `/tmp/tako-workspace` from host into Tako VM at same path
- Set `TAKO_VM_WORKSPACE=/tmp/tako-workspace`
- Now both Tako VM and Docker daemon see the same files

---

## Deployment Scenario 3: Docker-in-Docker (DinD)

Instead of sharing the host's Docker daemon, run a **separate Docker daemon inside** (or alongside) Tako VM:

```
┌─────────────────────────────────────────────────────────┐
│  HOST                                                    │
│                                                          │
│  Host Docker Daemon (only runs Tako VM, nothing else)   │
│     │                                                    │
│     ▼                                                    │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Tako VM Container                                │   │
│  │                                                   │   │
│  │  Tako VM (process)                               │   │
│  │     │                                            │   │
│  │     │ creates /tmp/job-123/code/main.py          │   │
│  │     │                                            │   │
│  │     ▼                                            │   │
│  │  DinD Docker Daemon (nested)                     │   │
│  │     │                                            │   │
│  │     │ mounts /tmp/job-123/code → ✅ exists       │   │
│  │     ▼               (inside Tako VM container)   │   │
│  │  ┌─────────────────┐                             │   │
│  │  │ Executor        │                             │   │
│  │  │ Container       │                             │   │
│  │  └─────────────────┘                             │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**Why this works**: The nested Docker daemon runs inside Tako VM's container, so it sees Tako VM's filesystem. No shared host directories needed.

### DinD Sidecar Pattern (Kubernetes/Compose)

In practice, "dind" usually means running the Docker daemon as a **sidecar container** that shares a network/volume with Tako VM:

```yaml
# Kubernetes pod with dind sidecar
spec:
  containers:
  - name: tako-vm
    image: tako-vm:latest
    env:
    - name: DOCKER_HOST
      value: tcp://localhost:2375  # Connect to sidecar

  - name: dind
    image: docker:dind
    securityContext:
      privileged: true  # Required for nested Docker
    env:
    - name: DOCKER_TLS_CERTDIR
      value: ""  # Disable TLS for localhost
```

Tako VM connects to the sidecar's Docker daemon via `DOCKER_HOST=tcp://localhost:2375` instead of the Unix socket.

---

## What Tako VM Needs to Support DinD

### Current Assumptions (that break with DinD)

1. **Executor image exists**: Tako VM assumes `code-executor:latest` is available. With dind, the nested daemon starts empty.

2. **UV cache volume exists when enabled**: If `enable_runtime_dependency_cache` is true, Tako VM mounts the `tako-uv-cache` volume for faster dependency installs. This volume doesn't exist in a fresh dind daemon.

### Required Changes

#### 1. Image Bootstrap Logic

Tako VM needs to ensure the executor image exists in whatever Docker daemon it connects to:

```python
# Pseudocode for new startup check
def ensure_executor_image(image_name: str, registry: str | None):
    """Ensure executor image exists in connected Docker daemon."""
    try:
        docker_client.images.get(image_name)
        logger.info(f"Executor image {image_name} found")
    except ImageNotFound:
        if registry:
            logger.info(f"Pulling {image_name} from {registry}")
            docker_client.images.pull(registry)
            docker_client.images.tag(registry, image_name)
        else:
            raise RuntimeError(
                f"Executor image {image_name} not found. "
                "Either build it or configure executor_image_registry."
            )
```

#### 2. Configuration Options

```yaml
# tako_vm.yaml - new options
executor:
  image: code-executor:latest

  # If image not found locally, pull from this registry
  registry: ghcr.io/your-org/code-executor:latest

  # Or: build from bundled Dockerfile if not found
  build_if_missing: true
```

#### 3. Volume Bootstrap

For the uv cache volume, either:
- Create it automatically on first use (Docker does this)
- Or document that dind deployments won't have persistent cache across restarts

### Code Changes Summary

| File | Change |
|------|--------|
| `tako_vm/config.py` | Add `executor.registry` and `executor.build_if_missing` options |
| `tako_vm/execution/worker.py` | Add `ensure_executor_image()` call on startup |
| `tako_vm/server/app.py` | Call bootstrap check on server startup |

### No Changes Needed

- **DOCKER_HOST**: The Docker SDK already respects this env var
- **Workspace paths**: DinD sees Tako VM's filesystem, so no `TAKO_VM_WORKSPACE` needed
- **Network**: Executor containers inherit dind's network by default

---

## Comparison: Socket Mount vs DinD

| Aspect | Socket Mount | DinD |
|--------|--------------|------|
| Setup complexity | Simple | More complex |
| Host isolation | Shared workspace on host | Full isolation |
| Security | Tako VM has host Docker access | Nested, `--privileged` required |
| Image availability | Uses host's images | Must pull/build in dind |
| Cache persistence | Host volume persists | Lost on dind restart (unless volume mounted) |
| Performance | Native | Slight overhead |

---

## Implementation Priority

1. **MVP**: Document the dind pattern (already in k8s example)
2. **Phase 1**: Add `executor.registry` config to pull image if missing
3. **Phase 2**: Add `executor.build_if_missing` to build from bundled Dockerfile
4. **Phase 3**: Auto-create uv cache volume if needed

---

## Open Questions

1. Should Tako VM bundle the executor Dockerfile for self-building?
2. Should we support loading executor image from a tar file?
3. How to handle uv cache in ephemeral dind (accept cold starts, or mount volume)?
