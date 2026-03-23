# Tako VM Scaling Guide

This document covers scaling Tako VM. The first section describes what works today; later sections describe planned features that are not yet implemented.

## What Works Today

Tako VM supports **single-node vertical scaling** by increasing workers and queue size. Multi-node horizontal scaling is not yet available.

| Capability | Status |
|-----------|--------|
| Increase workers (up to 64) | Available |
| Increase queue size | Available |
| gVisor runtime | Available |
| Container pooling | Planned |
| Distributed workers (multi-node) | Planned |
| Lighter isolation (nsjail, Firecracker) | Planned |

## Current Configuration

```yaml
# tako_vm.yaml
max_workers: 16      # 4x default (was 4)
max_queue_size: 500  # 5x default (was 100)
```

**Expected throughput**: ~48 jobs/second with 16 workers (vs ~12 with 4 workers)

---

## Scaling Strategies

### 1. Increase Workers (Quick Win)

**Current status**: Implemented (16 workers)

Workers process jobs in parallel. More workers = higher throughput, limited by:
- CPU cores (each container uses CPU during execution)
- Memory (each container needs ~256MB-1GB depending on workload)

```yaml
# For a 32-core machine with 64GB RAM:
max_workers: 32
max_queue_size: 1000
```

**Limits**: max_workers can go up to 64 in config validation.

---

### 2. Container Pooling (High Impact)

!!! warning "Not yet implemented"
    Container pooling is a planned feature. The design below is aspirational.

**Status**: Planned

Pre-create containers at startup and reuse them instead of creating/destroying per job.

#### How It Works

**Current** (cold start each job):
```
Job → Create container (~300ms) → Run code (~50ms) → Destroy (~100ms)
                                  Total: ~450ms per job
```

**With pooling** (warm containers):
```
Startup: Create N containers, keep running

Job → Grab warm container → Run code (~50ms) → Reset filesystem → Return to pool
                            Total: ~60ms per job
```

#### Implementation Outline

```python
# tako_vm/execution/pool.py

class ContainerPool:
    """Pool of pre-warmed Docker containers."""

    def __init__(self, size: int = 20, image: str = "code-executor:latest"):
        self.size = size
        self.image = image
        self.available: asyncio.Queue[Container] = asyncio.Queue()
        self.all_containers: List[Container] = []

    async def start(self):
        """Pre-create containers at startup."""
        for i in range(self.size):
            container = await self._create_container(f"pool-{i}")
            self.all_containers.append(container)
            await self.available.put(container)

    async def acquire(self) -> Container:
        """Get a warm container from pool."""
        return await self.available.get()

    async def release(self, container: Container):
        """Reset and return container to pool."""
        await self._reset_container(container)
        await self.available.put(container)

    async def _create_container(self, name: str) -> Container:
        """Create a long-running container that waits for work."""
        # Container runs a small HTTP server or watches for job files
        pass

    async def _reset_container(self, container: Container):
        """Reset filesystem state between jobs."""
        # Clear /tmp, /output, reset environment
        pass
```

#### Expected Improvement

| Metric | Without Pooling | With Pooling | Improvement |
|--------|-----------------|--------------|-------------|
| Cold start overhead | ~400ms | ~5ms | 80x |
| Jobs/second (16 workers) | ~48 | ~200-300 | 5-6x |
| Memory usage | Variable | Fixed (pool size) | Predictable |

#### Considerations

- Pool size must match max_workers
- Container health monitoring needed
- Cleanup must be thorough (security)
- Need graceful degradation if container becomes unhealthy

---

### 3. Distributed Workers (Horizontal Scaling)

!!! warning "Not yet implemented"
    Distributed workers require Redis and external PostgreSQL. This is a planned feature.

**Status**: Planned

Run multiple Tako VM instances behind a load balancer with shared state.

#### Architecture

```
                    ┌──────────────────┐
                    │   Load Balancer  │
                    │   (nginx/HAProxy)│
                    └────────┬─────────┘
         ┌───────────────────┼───────────────────┐
         │                   │                   │
    ┌────┴────┐        ┌────┴────┐        ┌────┴────┐
    │ Node 1  │        │ Node 2  │        │ Node 3  │
    │ Tako VM │        │ Tako VM │        │ Tako VM │
    │ 16 wkrs │        │ 16 wkrs │        │ 16 wkrs │
    └────┬────┘        └────┬────┘        └────┬────┘
         │                   │                   │
         └───────────────────┼───────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
         ┌────┴────┐                  ┌────┴────┐
         │ Redis   │                  │ Postgres│
         │ (queue) │                  │ (state) │
         └─────────┘                  └─────────┘
```

#### Required Changes

| Component | Current | Distributed |
|-----------|---------|-------------|
| Job Queue | In-memory asyncio.Queue | Redis or RabbitMQ |
| Database | PostgreSQL (single node) | PostgreSQL (clustered/managed) |
| Artifact Storage | Local filesystem | S3 or shared NFS |
| Config | Local file | Consul/etcd or env vars |

#### Implementation Phases

**Phase 1: PostgreSQL storage foundation**
```python
# tako_vm/storage.py
# Use psycopg async pool

from psycopg_pool import AsyncConnectionPool

class PostgresStorage:
    async def connect(self, dsn: str):
        self.pool = AsyncConnectionPool(conninfo=dsn, open=False)
        await self.pool.open()
```

**Phase 2: Redis Job Queue**
```python
# tako_vm/server/redis_queue.py

import redis.asyncio as redis

class RedisJobQueue:
    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url)

    async def submit(self, job_data: dict) -> str:
        job_id = str(uuid.uuid4())
        await self.redis.lpush("tako:jobs", json.dumps({
            "job_id": job_id,
            **job_data
        }))
        return job_id

    async def get_next(self) -> Optional[dict]:
        result = await self.redis.brpop("tako:jobs", timeout=5)
        if result:
            return json.loads(result[1])
        return None
```

**Phase 3: S3 Artifact Storage**
```python
# tako_vm/storage/s3.py

import aioboto3

class S3ArtifactStorage:
    async def save_artifact(self, job_id: str, name: str, data: bytes):
        async with self.session.client('s3') as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=f"artifacts/{job_id}/{name}",
                Body=data
            )
```

#### Expected Improvement

| Nodes | Workers | Theoretical Jobs/sec |
|-------|---------|---------------------|
| 1 | 16 | ~48 |
| 3 | 48 | ~144 |
| 10 | 160 | ~480 |

With container pooling + distribution: **1000+ jobs/sec**

---

### 4. Lighter Isolation Options

!!! warning "Not yet implemented"
    Alternative isolation backends are a planned feature. Only Docker and gVisor are supported today.

**Status**: Planned

For even higher throughput, consider lighter isolation than full Docker containers:

| Technology | Startup Time | Isolation Level | Use Case |
|------------|--------------|-----------------|----------|
| Docker | ~300ms | High | Default, untrusted code |
| gVisor (runsc) | ~50ms | High | Faster, still secure |
| Firecracker | ~125ms | Very High | MicroVMs |
| nsjail | ~10ms | Medium | Trusted code, max speed |

#### gVisor Integration (Recommended)

```yaml
# tako_vm.yaml
docker_runtime: runsc  # Use gVisor instead of runc
```

```python
# In worker.py _run_container():
if self.config.docker_runtime:
    cmd.append(f"--runtime={self.config.docker_runtime}")
```

---

## Monitoring for Scale

When scaling, monitor:

| Metric | Tool | Warning Threshold |
|--------|------|-------------------|
| Queue depth | `/health` endpoint | > max_workers × 2 |
| Memory per node | Prometheus | > 80% |
| P99 latency | Stress test | > 500ms |
| 503 rate | Logs | > 5% |
| Container health | Docker stats | Any restarts |

---

## Quick Reference

| Target | Configuration | Requirements |
|--------|--------------|--------------|
| 50 jobs/sec | 16 workers (current) | 16GB RAM, 8 cores |
| 200 jobs/sec | 32 workers + pooling | 32GB RAM, 16 cores |
| 500 jobs/sec | Distributed (3 nodes) | PostgreSQL, Redis |
| 1000+ jobs/sec | Distributed + pooling + gVisor | Full cluster |

---

## Next Steps

- [Production Setup](production.md) - Production deployment checklist
- [Security](security.md) - Hardening for production
- [Configuration](../getting-started/configuration.md) - Fine-tune settings
