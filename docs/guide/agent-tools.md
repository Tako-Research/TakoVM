---
description: "Use Tako VM as a code-execution tool for AI agents — LangChain and OpenAI tool-calling integrations with sandboxed Python execution."
---

# Agent Integration

Tako VM is built to be the code-execution tool behind an AI agent: the model writes Python, Tako VM runs it in an isolated sandbox, and the agent gets stdout/stderr back as its observation. This guide wires that up for LangChain and OpenAI tool-calling.

The pattern is identical in every framework:

1. Expose one tool, e.g. `run_python(code, requirements)`.
2. The tool submits the model-generated code to Tako VM and waits for the result.
3. Return stdout on success and **return the error text on failure** — the model uses tracebacks to fix its own code, so never raise them away.

!!! warning "Treat AI-generated code as untrusted"
    Run agent workloads with `security_mode: strict` so jobs fail rather than silently falling back from gVisor to plain `runc`, and never pass secrets in `input_data` — anything the job receives is readable by the code. See the [threat model](../security/honest-assessment.md).

!!! note "Letting agents install packages"
    The `requirements` parameter needs `allow_runtime_requirements: true` in `tako_vm.yaml` (off by default). For tighter control, define [pre-built job types](environments.md) with the packages your agents need and drop the parameter entirely.

## The core tool function

Everything below is a thin wrapper around this:

```python
import tako_vm

tako_vm.configure("http://localhost:8000")  # add headers={"X-API-Key": ...} if auth is enabled


def run_python(code: str, requirements: list[str] | None = None) -> str:
    """Execute Python in a Tako VM sandbox and return its output."""
    job_id = tako_vm.submit_code(
        code,
        requirements=requirements or [],
        timeout=60,
    )
    record = tako_vm.get_result(job_id, timeout=120)

    if record["status"] == "succeeded":
        return record["stdout"] or "(no output)"
    # Surface the failure to the model — tracebacks are how agents self-correct.
    return (
        f"Execution {record['status']}.\n"
        f"stderr:\n{record.get('stderr', '')}\n"
        f"error: {record.get('error', '')}"
    )
```

`submit_code()` auto-generates an `idempotency_key` and retries transient failures with the same key, so a flaky network can't double-execute an agent's code.

## LangChain

```python
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class RunPythonArgs(BaseModel):
    code: str = Field(description="Python code to execute. Use print() for any output you need.")
    requirements: list[str] = Field(
        default_factory=list,
        description='PyPI packages the code imports, e.g. ["pandas", "numpy>=1.20"]',
    )


python_sandbox = StructuredTool.from_function(
    func=run_python,
    name="run_python",
    description=(
        "Execute Python code in an isolated sandbox (no network access). "
        "Returns stdout, or the error/traceback if execution failed."
    ),
    args_schema=RunPythonArgs,
)
```

Bind it to any tool-calling model with LangChain's LangGraph-based agent API:

```python
from langchain.agents import create_agent

agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[python_sandbox],
    system_prompt="You can execute Python via run_python. Compute, don't recall.",
)

result = agent.invoke({
    "messages": [{"role": "user", "content": "What is the 50th Fibonacci number?"}]
})
print(result["messages"][-1].content)
```

### Using it from asyncio

The SDK is `requests`-based and blocking, so don't call `run_python` directly inside an async agent loop — wrap it so the event loop stays free:

```python
import asyncio


async def arun_python(code: str, requirements: list[str] | None = None) -> str:
    return await asyncio.to_thread(run_python, code, requirements)
```

Register `arun_python` as the tool's coroutine (LangChain's `StructuredTool.from_function(coroutine=arun_python, ...)`) and `ainvoke` works without blocking. The block happens in `get_result`, which long-polls the server — cheap to park on a thread.

## OpenAI tool calling

```python
import json
from openai import OpenAI

client = OpenAI()

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_python",
        "description": "Execute Python code in an isolated sandbox (no network). "
                       "Returns stdout, or the error if execution failed.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code; print() the result"},
                "requirements": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["code"],
        },
    },
}]

messages = [{"role": "user", "content": "Compute the eigenvalues of [[2,1],[1,2]]."}]
response = client.chat.completions.create(model="gpt-5", messages=messages, tools=TOOLS)

call = response.choices[0].message.tool_calls[0]
args = json.loads(call.function.arguments)
output = run_python(args["code"], args.get("requirements"))

messages += [response.choices[0].message,
             {"role": "tool", "tool_call_id": call.id, "content": output}]
final = client.chat.completions.create(model="gpt-5", messages=messages, tools=TOOLS)
```

The same shape works for Anthropic tool use — define one `run_python` tool and feed `record["stdout"]`/`stderr` back as the `tool_result`.

## Concurrency and history

- **Parallel agents**: jobs queue automatically; the worker pool (default 4) drains them. Submit from as many agent sessions as you like — `submit_code()` returns immediately with a `job_id`.
- **Audit trail**: every execution an agent ever ran is persisted with code, stdout/stderr, and timing — `tako_vm.list_executions()` or `GET /executions`. Invaluable when an agent did something surprising last Tuesday.
- **Replay**: re-run an agent's past execution exactly (`tako_vm.rerun(job_id)`) or fork it with modified code (`fork`) while debugging.
- **No webhooks yet**: poll `get_result(job_id, timeout=...)` (it long-polls server-side via `?wait=true`, so this is cheap).

## Next steps

- [Async Jobs](async-jobs.md) — job lifecycle and status reference
- [Environments](environments.md) — pre-built job types so agents skip dependency install
- [Hardening Guide](../deployment/security.md) — production settings for untrusted code
