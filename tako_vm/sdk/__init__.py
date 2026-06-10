"""
Tako VM SDK - Python client for typed function execution.

Provides a typed interface for executing functions in isolated containers.
"""

from tako_vm.sdk.client import (
    APIError,
    ClientError,
    ExecutionError,
    ExecutionResult,
    MalformedResponseError,
    SDKExecutionError,
    ServerError,
    TakoVM,
    TakoVMError,
    TransportError,
    ValidationError,
    build_job_type,
    cancel,
    configure,
    dlq_stats,
    download_artifact,
    fork,
    get_execution,
    get_job_type,
    get_result,
    get_status,
    health,
    list_executions,
    list_job_types,
    pool_stats,
    rerun,
    send,
    send_raw,
    submit,
    submit_code,
)

__all__ = [
    # Typed function execution
    "send",
    "send_raw",
    "configure",
    # Async job lifecycle
    "submit",
    "submit_code",
    "get_status",
    "get_result",
    "cancel",
    "rerun",
    "fork",
    "download_artifact",
    # History & metadata
    "list_executions",
    "get_execution",
    "pool_stats",
    "dlq_stats",
    "health",
    "list_job_types",
    "get_job_type",
    "build_job_type",
    # Classes
    "TakoVM",
    "ExecutionResult",
    # Exceptions
    "TakoVMError",
    "TransportError",
    "APIError",
    "ServerError",
    "ClientError",
    "SDKExecutionError",
    "ExecutionError",
    "MalformedResponseError",
    "ValidationError",
]
