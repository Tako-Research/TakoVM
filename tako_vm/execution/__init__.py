"""
Tako VM Execution - Docker container execution layer.

Provides code execution in isolated containers with gVisor isolation by default.
"""

from tako_vm.execution.builder import ContainerBuilder
from tako_vm.execution.docker import (
    base_isolation_args,
    generate_container_name,
    is_native_linux,
    kill_container,
)
from tako_vm.execution.worker import (
    CodeExecutor,
    RuntimeUnavailableError,
    check_gvisor_available,
    reset_gvisor_check,
    resolve_runtime,
)

__all__ = [
    # Worker
    "CodeExecutor",
    "RuntimeUnavailableError",
    "check_gvisor_available",
    "reset_gvisor_check",
    "resolve_runtime",
    # Builder
    "ContainerBuilder",
    # Docker utilities
    "generate_container_name",
    "kill_container",
    "is_native_linux",
    "base_isolation_args",
]
