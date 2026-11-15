from importlib.metadata import version as _pkg_version

__version__ = _pkg_version("tako-vm")

import warnings

try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

from tako_vm.config import TakoVMConfig, get_config
from tako_vm.constants import (
    DEFAULT_IMAGE,
    MAX_REQUIREMENTS,
    UV_CACHE_VOLUME,
    get_workspace_dir,
)
from tako_vm.job_types import JobType, JobTypeRegistry
from tako_vm.models import Artifact, ExecutionRecord, JobVersion, ResourceUsage
from tako_vm.sandbox import Sandbox, SandboxResult
from tako_vm.sandbox import run as sandbox_run
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
    "Sandbox",
    "SandboxResult",
    "sandbox_run",
    "send",
    "send_raw",
    "configure",
    "submit",
    "submit_code",
    "get_status",
    "get_result",
    "cancel",
    "rerun",
    "fork",
    "download_artifact",
    "list_executions",
    "get_execution",
    "pool_stats",
    "dlq_stats",
    "health",
    "list_job_types",
    "get_job_type",
    "build_job_type",
    "TakoVM",
    "ExecutionResult",
    "JobType",
    "JobTypeRegistry",
    "ExecutionRecord",
    "ResourceUsage",
    "Artifact",
    "JobVersion",
    "TakoVMConfig",
    "get_config",
    "DEFAULT_IMAGE",
    "UV_CACHE_VOLUME",
    "get_workspace_dir",
    "MAX_REQUIREMENTS",
    "TakoVMError",
    "TransportError",
    "APIError",
    "ServerError",
    "ClientError",
    "SDKExecutionError",
    "ExecutionError",
    "MalformedResponseError",
    "ValidationError",
    "__version__",
]
