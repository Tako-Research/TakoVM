"""
Correlation ID support for request tracing.

Provides:
- Context variable for correlation ID propagation
- FastAPI middleware for automatic correlation ID handling
- Logging filter for adding correlation ID to log records
"""

import logging
import uuid
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Context variable to hold correlation ID for current request
correlation_id_var: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)

# Header name for correlation ID (standard header)
CORRELATION_ID_HEADER = "X-Correlation-ID"


def get_correlation_id() -> Optional[str]:
    """
    Get the current correlation ID from context.

    Returns:
        Correlation ID or None if not set
    """
    return correlation_id_var.get()


def set_correlation_id(correlation_id: str) -> None:
    """
    Set the correlation ID in context.

    Args:
        correlation_id: Correlation ID to set
    """
    correlation_id_var.set(correlation_id)


def clear_correlation_id() -> None:
    """
    Clear the correlation ID from context.

    Call this at the start of work that is not tied to an HTTP request (e.g.
    each queue worker iteration) so logs and DLQ entries never inherit a
    previous request's correlation ID.
    """
    correlation_id_var.set(None)


def generate_correlation_id() -> str:
    """
    Generate a new correlation ID.

    Returns:
        New UUID-based correlation ID
    """
    return str(uuid.uuid4())


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware that handles correlation IDs for all requests.

    - Reads X-Correlation-ID header from incoming request (if present)
    - Generates new correlation ID if not provided
    - Sets correlation ID in context for downstream use
    - Adds correlation ID to response headers
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Get correlation ID from header or generate new one
        correlation_id = request.headers.get(CORRELATION_ID_HEADER)
        if not correlation_id:
            correlation_id = generate_correlation_id()

        # Set in context
        set_correlation_id(correlation_id)

        # Process request
        response = await call_next(request)

        # Add to response headers
        response.headers[CORRELATION_ID_HEADER] = correlation_id

        return response


class CorrelationIdFilter(logging.Filter):
    """
    Logging filter that adds correlation ID to log records.

    Usage:
        handler.addFilter(CorrelationIdFilter())

    Then in format string use %(correlation_id)s
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Add correlation_id attribute to log record."""
        record.correlation_id = get_correlation_id() or "-"
        return True


def configure_logging_with_correlation():
    """
    Configure the root logger to include correlation IDs.

    Call this during application startup.
    """
    # Create formatter with correlation ID
    formatter = logging.Formatter(
        "%(asctime)s [%(correlation_id)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Add filter and formatter to root logger handlers
    root_logger = logging.getLogger()

    for handler in root_logger.handlers:
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(formatter)

    # If no handlers, add a default one
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
