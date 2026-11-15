import logging
import uuid
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

correlation_id_var: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)

CORRELATION_ID_HEADER = "X-Correlation-ID"


def get_correlation_id() -> Optional[str]:
    return correlation_id_var.get()


def set_correlation_id(correlation_id: str) -> None:
    correlation_id_var.set(correlation_id)


def clear_correlation_id() -> None:
    correlation_id_var.set(None)


def generate_correlation_id() -> str:
    return str(uuid.uuid4())


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER)
        if not correlation_id:
            correlation_id = generate_correlation_id()

        set_correlation_id(correlation_id)

        response = await call_next(request)

        response.headers[CORRELATION_ID_HEADER] = correlation_id

        return response


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


def configure_logging_with_correlation():
    formatter = logging.Formatter(
        "%(asctime)s [%(correlation_id)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()

    for handler in root_logger.handlers:
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(formatter)

    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
