"""
Tako VM Server - HTTP API layer.

Provides FastAPI application and job queue.
"""

from tako_vm.server.app import app
from tako_vm.server.correlation import (
    CorrelationIdMiddleware,
    get_correlation_id,
    set_correlation_id,
)
from tako_vm.server.queue import QueuedJob, WorkerPool
from tako_vm.server.sessions import SessionManager, SessionManagerError

__all__ = [
    # App
    "app",
    # Queue
    "WorkerPool",
    "QueuedJob",
    "SessionManager",
    "SessionManagerError",
    # Correlation ID utilities
    "get_correlation_id",
    "set_correlation_id",
    "CorrelationIdMiddleware",
]
