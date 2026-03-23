# Tutorial: Build a Data Processing Pipeline

This tutorial walks through building a complete application with Tako VM — from setup to processing results.

## What We'll Build

A simple service that:

1. Accepts CSV data
2. Processes it in an isolated container (computes statistics)
3. Returns the results

## Prerequisites

```bash
pip install "tako-vm[server]" requests
tako-vm setup
tako-vm server  # leave running in a terminal
```

## Step 1: Basic Execution

Start with a simple test to verify everything works:

```python
import requests

response = requests.post("http://localhost:8000/execute", json={
    "code": "print('Tako VM is working!')"
})
result = response.json()
assert result["success"], f"Failed: {result.get('stderr')}"
print(result["stdout"])  # "Tako VM is working!"
```

## Step 2: Process Input Data

Pass data in, get structured results back:

```python
import requests

code = """
import json

with open("/input/data.json") as f:
    data = json.load(f)

items = data["items"]
stats = {
    "count": len(items),
    "sum": sum(items),
    "mean": sum(items) / len(items),
    "min": min(items),
    "max": max(items),
}

with open("/output/result.json", "w") as f:
    json.dump(stats, f)

print(f"Processed {len(items)} items")
"""

response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {"items": [10, 20, 30, 40, 50]}
})

result = response.json()
print(f"Success: {result['success']}")
print(f"Stats: {result['output']}")
# Stats: {'count': 5, 'sum': 150, 'mean': 30.0, 'min': 10, 'max': 50}
print(f"Stdout: {result['stdout']}")
# Stdout: Processed 5 items
```

## Step 3: Use Async for Longer Jobs

For jobs that take more than a few seconds, use async execution:

```python
import requests
import time

# Submit the job (returns immediately)
response = requests.post("http://localhost:8000/execute/async", json={
    "code": """
import time, json
time.sleep(3)  # simulate heavy processing
with open("/output/result.json", "w") as f:
    json.dump({"status": "done", "processed": 1000}, f)
""",
    "input_data": {}
})
job = response.json()
job_id = job["job_id"]
print(f"Job submitted: {job_id}")

# Poll for completion
while True:
    status = requests.get(f"http://localhost:8000/jobs/{job_id}").json()
    if status["status"] in ("succeeded", "failed", "timeout", "oom"):
        break
    print(f"  Status: {status['status']}")
    time.sleep(1)

# Get the result
result = requests.get(f"http://localhost:8000/jobs/{job_id}/result").json()
print(f"Result: {result['output']}")
```

## Step 4: Handle Errors

Always check for failures:

```python
import requests

response = requests.post("http://localhost:8000/execute", json={
    "code": "raise ValueError('something went wrong')",
    "input_data": {}
})

result = response.json()
if not result["success"]:
    print(f"Job failed (exit code {result['exit_code']})")
    print(f"Error: {result['stderr']}")
else:
    print(f"Output: {result['output']}")
```

## Step 5: Use Dependencies

Need third-party packages? Pass them in `requirements`:

```python
import requests

code = """
import pandas as pd
import json

with open("/input/data.json") as f:
    data = json.load(f)

df = pd.DataFrame(data["records"])
summary = df.describe().to_dict()

with open("/output/result.json", "w") as f:
    json.dump(summary, f)
"""

response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {
        "records": [
            {"name": "Alice", "score": 85},
            {"name": "Bob", "score": 92},
            {"name": "Charlie", "score": 78},
        ]
    },
    "requirements": ["pandas"]
})

result = response.json()
print(result["output"])
```

## Step 6: Batch Processing

Process multiple items by submitting async jobs and collecting results:

```python
import requests
import time

# Submit batch of jobs
items = [
    {"x": 1, "y": 2},
    {"x": 10, "y": 20},
    {"x": 100, "y": 200},
]

job_ids = []
for item in items:
    resp = requests.post("http://localhost:8000/execute/async", json={
        "code": """
import json
with open("/input/data.json") as f:
    data = json.load(f)
result = {"sum": data["x"] + data["y"], "product": data["x"] * data["y"]}
with open("/output/result.json", "w") as f:
    json.dump(result, f)
""",
        "input_data": item
    })
    job_ids.append(resp.json()["job_id"])

print(f"Submitted {len(job_ids)} jobs")

# Wait for all to complete
results = []
for job_id in job_ids:
    result = requests.get(
        f"http://localhost:8000/jobs/{job_id}/result?wait=true&timeout=30"
    ).json()
    results.append(result["output"])

print("Results:", results)
# Results: [{'sum': 3, 'product': 2}, {'sum': 30, 'product': 200}, {'sum': 300, 'product': 20000}]
```

## Next Steps

- [REST API Reference](../api/rest.md) — all endpoints and options
- [Environments](environments.md) — pre-configured job types
- [Error Handling](error-handling.md) — robust error handling patterns
- [Async Jobs](async-jobs.md) — advanced async patterns
