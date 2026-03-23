# Job Types (Environments)

Job types are pre-configured execution environments with specific packages and resource limits.

## Why Job Types?

Installing packages at runtime is slow. Job types pre-install packages so execution is fast:

```
WITHOUT Job Types (slower)          WITH Job Types (fast)
──────────────────────────          ─────────────────────
Start container                     Start container (pre-built)
uv pip install numpy pandas         Execute code
Execute code                        Return results
Return results
```

## Defining Job Types

Define job types in your `tako_vm.yaml`:

```yaml
job_types:
  - name: data-processing
    requirements:
      - pandas
      - numpy
    memory_limit: "1g"
    cpu_limit: 2.0
    timeout: 60

  - name: ml-inference
    requirements:
      - numpy
      - scikit-learn
      - joblib
    memory_limit: "2g"
    cpu_limit: 2.0
    timeout: 120

  - name: api-client
    requirements:
      - requests
      - httpx
    memory_limit: "256m"
    timeout: 30
    network_enabled: true
```

## Using a Job Type

Specify the `job_type` parameter in your request:

```python
import requests

response = requests.post("http://localhost:8000/execute", json={
    "code": '''
import json
import numpy as np

with open("/input/data.json") as f:
    data = json.load(f)

values = np.array(data["values"])
result = {"mean": float(np.mean(values)), "std": float(np.std(values))}

with open("/output/result.json", "w") as f:
    json.dump(result, f)
''',
    "input_data": {"values": [1, 2, 3, 4, 5]},
    "job_type": "data-processing"
})

print(response.json()["output"])
# {'mean': 3.0, 'std': 1.414...}
```

## Job Type Options

| Field | Description | Default |
|-------|-------------|---------|
| `name` | Unique identifier | required |
| `requirements` | pip packages to install | `[]` |
| `python_version` | Python version | `"3.11"` |
| `base_image` | Custom Docker base image | `python:{version}-slim` |
| `memory_limit` | Container memory limit | `"512m"` |
| `cpu_limit` | CPU cores | `1.0` |
| `timeout` | Default timeout (seconds) | `30` |
| `network_enabled` | Allow outbound network | `false` |
| `environment` | Environment variables | `{}` |
| `shared_code` | Python files to include | `[]` |

## Network Access

By default, containers have **no network access** for security. To enable network for specific job types:

```yaml
job_types:
  - name: api-caller
    requirements:
      - requests
    network_enabled: true           # Enable network
```

When `network_enabled: true`, containers can access any external host. For strict egress control in production, use external firewalls or Kubernetes NetworkPolicy.

## List Job Types

```python
response = requests.get("http://localhost:8000/job-types")

for jt in response.json():
    print(f"{jt['name']}:")
    print(f"  Packages: {jt['requirements']}")
    print(f"  Memory: {jt['memory_limit']}")
    print(f"  Network: {'yes' if jt.get('network_enabled') else 'no'}")
```

## Building Images

In development mode, images are auto-built on first use. To pre-build:

```bash
# Build images via the REST API (requires a running server)
curl -X POST http://localhost:8000/job-types/data-processing/build
```

!!! note "CLI support for building images is planned — see [GitHub #30](https://github.com/las7/TakoVM/issues/30)."

Images are named `tako-vm-{name}:latest`.

## Programmatic Registration

You can also register job types in code:

```python
from tako_vm.job_types import JobType, JobTypeRegistry

registry = JobTypeRegistry()

registry.register(JobType(
    name="custom-env",
    requirements=["numpy", "pandas>=2.0"],
    memory_limit="2g",
    cpu_limit=2.0,
    timeout=60,
    environment={
        "NUMBA_CACHE_DIR": "/tmp",
        "OMP_NUM_THREADS": "2"
    },
))
```

## Production Mode

In production mode (`production_mode: true`):

- Auto-build is disabled
- All job types must be pre-built
- Requests for missing job types fail with an error

```yaml
production_mode: true
```

This ensures consistent, tested images in production.
