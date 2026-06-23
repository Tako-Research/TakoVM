---
description: "Learn the Tako VM execution model: how containers are created, files mounted read-only, code run, and results captured for each job."
---

# Basic Execution

This guide covers the fundamentals of executing code in Tako VM.

## The Execution Model

When you submit code to Tako VM:

1. **Container Created** - A fresh Docker container is spawned
2. **Files Mounted** - Input data and your code are mounted read-only
3. **Code Executes** - Your Python code runs with limited privileges
4. **Output Collected** - stdout, stderr, and `/output/` files are captured
5. **Container Destroyed** - Container is removed (ephemeral)

!!! tip "Key Paths"
    | Path | Access | Purpose |
    |------|--------|---------|
    | `/input/data.json` | Read | Your `input_data` as JSON |
    | `/output/result.json` | Write | Auto-parsed into response `output` field |
    | `/output/*` | Write | Other files saved as downloadable **artifacts** |
    | `/tmp/` | Write | Temporary files (not persisted) |

## Basic Request

```python
import requests

response = requests.post("http://localhost:8000/execute", json={
    "code": "print('Hello, World!')",
    "input_data": {}
})

print(response.json())
```

## Working with Input Data

Input data is available at `/input/data.json`:

```python
code = """
import json

with open("/input/data.json") as f:
    data = json.load(f)

name = data["name"]
items = data["items"]

print(f"Processing {len(items)} items for {name}")
"""

response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {
        "name": "Alice",
        "items": [1, 2, 3, 4, 5]
    }
})
```

## Returning Output

Write results to `/output/result.json`:

```python
code = """
import json

with open("/input/data.json") as f:
    data = json.load(f)

# Process
result = {
    "total": sum(data["values"]),
    "count": len(data["values"]),
    "average": sum(data["values"]) / len(data["values"])
}

# Write output
with open("/output/result.json", "w") as f:
    json.dump(result, f)
"""

response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {"values": [10, 20, 30, 40, 50]}
})

# result.json is automatically parsed
print(response.json()["output"])
# {'total': 150, 'count': 5, 'average': 30.0}
```

## Multiple Output Files

You can write multiple files to `/output/`:

```python
code = """
import json

with open("/input/data.json") as f:
    data = json.load(f)

# Write multiple outputs
with open("/output/result.json", "w") as f:
    json.dump({"status": "complete"}, f)

with open("/output/report.txt", "w") as f:
    f.write(f"Processed {len(data['items'])} items\\n")

with open("/output/data.csv", "w") as f:
    f.write("id,value\\n")
    for i, v in enumerate(data["items"]):
        f.write(f"{i},{v}\\n")
"""
```

!!! note
    Only `result.json` is automatically parsed. Other files are recorded as artifacts.

## Setting Timeout

Override the default timeout (30 seconds):

```python
response = requests.post("http://localhost:8000/execute", json={
    "code": "import time; time.sleep(60)",
    "input_data": {},
    "timeout": 120  # 2 minutes
})
```

## Using Print for Debugging

stdout is captured and returned:

```python
code = """
import json

print("Starting execution...")

with open("/input/data.json") as f:
    data = json.load(f)

print(f"Got {len(data)} keys")

for key, value in data.items():
    print(f"  {key}: {value}")

print("Done!")

with open("/output/result.json", "w") as f:
    json.dump({"processed": True}, f)
"""

response = requests.post("http://localhost:8000/execute", json={
    "code": code,
    "input_data": {"a": 1, "b": 2}
})

print(response.json()["stdout"])
# Starting execution...
# Got 2 keys
#   a: 1
#   b: 2
# Done!
```

## Limitations

| Limitation | Details |
|------------|---------|
| No Network | Cannot make HTTP requests or connect to databases |
| Read-only FS | Can only write to `/output/` and `/tmp/` |
| No Persistence | Container is destroyed after execution |
| Resource Limits | Memory, CPU, and time are limited |

## Next Steps

- [Async Jobs](async-jobs.md) - For long-running tasks
- [Environments](environments.md) - Use packages like numpy, pandas
- [Error Handling](error-handling.md) - Handle failures gracefully
