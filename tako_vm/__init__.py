# Copyright 2026 las7
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Tako VM - Secure Code Execution System

Core modules:
- server: FastAPI HTTP server with async job queue (server/app.py, server/queue.py)
- execution: Docker container execution orchestrator (execution/worker.py, execution/builder.py)
- sdk: Typed Python SDK for function execution (sdk/client.py)
- sandbox: Direct Docker execution without a server
- job_types: Execution environment configuration
- models: ExecutionRecord and other data models
- config: Configuration management
"""

from importlib.metadata import version as _pkg_version

__version__ = _pkg_version("tako-vm")

# Suppress LibreSSL warnings on macOS (urllib3 v2 requires OpenSSL 1.1.1+)
import warnings

try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

from tako_vm.config import TakoVMConfig, get_config
from tako_vm.constants import DEFAULT_IMAGE, MAX_REQUIREMENTS, UV_CACHE_VOLUME, WORKSPACE_DIR
from tako_vm.job_types import JobType, JobTypeRegistry
from tako_vm.models import Artifact, ExecutionRecord, JobVersion, ResourceUsage
from tako_vm.sandbox import Sandbox, SandboxResult
from tako_vm.sandbox import run as sandbox_run
from tako_vm.sdk.client import (
    ExecutionError,
    ExecutionResult,
    SDKExecutionError,
    TakoVM,
    TakoVMError,
    ValidationError,
    configure,
    get_job_type,
    list_job_types,
    send,
    send_raw,
)

__all__ = [
    # Sandbox (library-first interface)
    "Sandbox",
    "SandboxResult",
    "sandbox_run",
    # Main SDK functions
    "send",
    "send_raw",
    "configure",
    "list_job_types",
    "get_job_type",
    # SDK Classes
    "TakoVM",
    "ExecutionResult",
    # Job Types
    "JobType",
    "JobTypeRegistry",
    # Production Models
    "ExecutionRecord",
    "ResourceUsage",
    "Artifact",
    "JobVersion",
    # Configuration
    "TakoVMConfig",
    "get_config",
    # Constants
    "DEFAULT_IMAGE",
    "UV_CACHE_VOLUME",
    "WORKSPACE_DIR",
    "MAX_REQUIREMENTS",
    # Exceptions
    "TakoVMError",
    "SDKExecutionError",
    "ExecutionError",  # Backward compatibility alias for SDKExecutionError
    "ValidationError",
    # Version
    "__version__",
]
