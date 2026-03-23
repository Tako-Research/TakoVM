# Development Guide

## Dependency Flow

1. Job submitted with `job_type: "data-processing"` (has `requirements: [pandas, numpy]`)
2. Worker passes requirements via `TAKO_REQUIREMENTS` env var
3. Container starts, `entrypoint.sh` runs `uv pip install pandas numpy`
4. Code runs as `sandbox` user (uid 1000) via `gosu`

For network-isolated jobs with deps, use pre-built images via the REST API:
```bash
curl -X POST http://localhost:8000/job-types/data-processing/build
# Then set base_image in job type config
```

## Common Issues

- **RuntimeUnavailableError: gVisor not available** -> Install gVisor: https://gvisor.dev/docs/user_guide/install/ or set `security_mode: permissive` for dev
- **ImageNotFound: code-executor:latest** -> Build image first: `docker build -t code-executor:latest -f docker/Dockerfile.executor .`
- **PostgreSQL migration/connection errors** -> Verify `database_url` and database reachability
- **Test isolation** -> Use `reset_config()` and temp database pattern from `tests/test_api.py`
- **Dep install fails (network)** -> Jobs with requirements need network; use pre-built for true isolation

## Debugging

```bash
curl http://localhost:8000/health           # Health check
curl http://localhost:8000/pool/stats       # Worker pool status
curl http://localhost:8000/dlq/stats        # Dead letter queue
```
