"""
API middleware for request rate limiting and payload size enforcement.

This middleware provides lightweight API-layer protections:
- In-memory fixed-window rate limiting per client IP
- Maximum request payload size checks (header and streamed body)
"""

from __future__ import annotations

import hmac
import logging
import math
import time
from threading import Lock
from typing import Callable, Dict, Optional, Tuple

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tako_vm.config import TakoVMConfig
from tako_vm.server.correlation import (
    CORRELATION_ID_HEADER,
    generate_correlation_id,
    get_correlation_id,
    set_correlation_id,
)

logger = logging.getLogger(__name__)

_CORRELATION_ID_HEADER_BYTES = CORRELATION_ID_HEADER.lower().encode("ascii")
_RATE_LIMIT_EXEMPT_PATHS = {
    "/health",
    "/openapi.json",
    "/docs/oauth2-redirect",
}
_RATE_LIMIT_EXEMPT_PREFIXES = ("/docs", "/redoc")


class PayloadTooLargeError(Exception):
    """Raised when request payload exceeds configured limit."""


class FixedWindowRateLimiter:
    """In-memory fixed-window limiter keyed by client identifier."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: Dict[str, Tuple[float, int]] = {}
        self._lock = Lock()
        self._last_cleanup = time.monotonic()

    def allow(self, key: str) -> tuple[bool, int]:
        """
        Check whether key can proceed.

        Returns:
            (allowed, retry_after_seconds)
        """
        now = time.monotonic()

        with self._lock:
            window_start, count = self._buckets.get(key, (now, 0))
            elapsed = now - window_start

            if elapsed >= self.window_seconds:
                window_start = now
                count = 0

            count += 1
            self._buckets[key] = (window_start, count)
            self._cleanup_if_needed(now)

            if count <= self.max_requests:
                return True, 0

            remaining = self.window_seconds - (now - window_start)
            return False, max(1, math.ceil(remaining))

    def _cleanup_if_needed(self, now: float) -> None:
        """Periodically evict stale entries to bound memory usage."""
        if now - self._last_cleanup < self.window_seconds:
            return

        cutoff = now - (self.window_seconds * 2)
        self._buckets = {key: value for key, value in self._buckets.items() if value[0] >= cutoff}
        self._last_cleanup = now


class ApiProtectionMiddleware:
    """
    Enforce API request protections.

    - Optionally requires API key auth (excluding health/docs/schema routes)
    - Rate limits requests by client IP or authenticated API key
    - Rejects oversized payloads with HTTP 413
    """

    def __init__(self, app: ASGIApp, config_getter: Callable[[], TakoVMConfig]):
        self.app = app
        self._config_getter = config_getter
        self._rate_limiter: Optional[FixedWindowRateLimiter] = None
        self._limiter_settings: Optional[tuple[int, int]] = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # This middleware wraps the app *outside* FastAPI's exception handlers,
        # so an unexpected error in the protection logic would otherwise escape
        # as an opaque 500 with no correlated log line. Catch it, log loudly,
        # and emit a sanitized 500, unless the response has already started
        # streaming, in which case we can only re-raise.
        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._dispatch(scope, receive, tracked_send)
        except Exception as e:
            logger.error(
                "Unhandled error in API protection middleware (correlation_id=%s): %s",
                get_correlation_id(),
                e,
                exc_info=True,
            )
            if response_started:
                raise
            await self._send_error_response(
                scope,
                receive,
                send,
                status_code=500,
                detail="Internal server error.",
            )

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        config = self._config_getter()
        path = scope.get("path", "")
        authenticated_identity = None

        if config.api_auth_enabled and not self._is_auth_exempt(path):
            authenticated_identity = self._authenticate_request(scope, config)
            if authenticated_identity is None:
                await self._send_error_response(
                    scope,
                    receive,
                    send,
                    status_code=401,
                    detail="Missing or invalid API key.",
                    extra_headers={"WWW-Authenticate": "Bearer"},
                )
                return

        if config.api_rate_limit_enabled and not self._is_rate_limit_exempt(path):
            limiter = self._get_rate_limiter(config)
            client_id = self._get_client_identifier(scope, authenticated_identity)
            allowed, retry_after = limiter.allow(client_id)

            if not allowed:
                await self._send_error_response(
                    scope,
                    receive,
                    send,
                    status_code=429,
                    detail="Rate limit exceeded. Try again later.",
                    extra_headers={"Retry-After": str(retry_after)},
                )
                return

        content_length = self._get_content_length(scope)
        if content_length is not None and content_length > config.api_max_payload_bytes:
            await self._send_error_response(
                scope,
                receive,
                send,
                status_code=413,
                detail=(
                    "Payload too large. "
                    f"Maximum allowed size is {config.api_max_payload_bytes} bytes."
                ),
            )
            return

        try:
            buffered_messages = await self._buffer_and_validate_body(
                receive, config.api_max_payload_bytes
            )
        except PayloadTooLargeError:
            await self._send_error_response(
                scope,
                receive,
                send,
                status_code=413,
                detail=(
                    "Payload too large. "
                    f"Maximum allowed size is {config.api_max_payload_bytes} bytes."
                ),
            )
            return

        buffered_messages_iter = iter(buffered_messages)

        async def replay_receive() -> Message:
            try:
                return next(buffered_messages_iter)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    async def _buffer_and_validate_body(
        self, receive: Receive, max_payload_bytes: int
    ) -> list[Message]:
        """Read and validate request body size even if downstream ignores it."""
        buffered_messages: list[Message] = []
        body_bytes_seen = 0

        while True:
            message = await receive()
            buffered_messages.append(message)

            message_type = message["type"]
            if message_type == "http.request":
                body_bytes_seen += len(message.get("body", b""))
                if body_bytes_seen > max_payload_bytes:
                    raise PayloadTooLargeError

                if not message.get("more_body", False):
                    break
            else:
                # Any non-request message (e.g. http.disconnect) ends the body.
                break

        return buffered_messages

    def _get_rate_limiter(self, config: TakoVMConfig) -> FixedWindowRateLimiter:
        """Create or refresh rate limiter if settings changed."""
        settings = (config.api_rate_limit_requests, config.api_rate_limit_window_seconds)

        if self._rate_limiter is None or self._limiter_settings != settings:
            self._rate_limiter = FixedWindowRateLimiter(
                max_requests=config.api_rate_limit_requests,
                window_seconds=config.api_rate_limit_window_seconds,
            )
            self._limiter_settings = settings

        return self._rate_limiter

    def _authenticate_request(self, scope: Scope, config: TakoVMConfig) -> Optional[str]:
        """Return a non-secret API key identity if the request is authenticated."""
        provided_key = self._extract_api_key(scope, config.api_auth_header)
        if provided_key is None:
            return None

        for index, configured_key in enumerate(config.api_keys):
            if hmac.compare_digest(provided_key, configured_key):
                return f"api_key:{index}"
        return None

    @staticmethod
    def _extract_api_key(scope: Scope, api_auth_header: str) -> Optional[str]:
        """Extract API key from configured header or Authorization bearer token."""
        configured_header = api_auth_header.lower().encode("ascii")
        for header_name, header_value in scope.get("headers", []):
            header_name_lower = header_name.lower()
            if header_name_lower == configured_header:
                try:
                    value = header_value.decode("utf-8").strip()
                except UnicodeDecodeError:
                    return None
                return value or None
            if header_name_lower == b"authorization":
                try:
                    value = header_value.decode("utf-8").strip()
                except UnicodeDecodeError:
                    return None
                prefix = "bearer "
                if value.lower().startswith(prefix):
                    token = value[len(prefix) :].strip()
                    return token or None
        return None

    @staticmethod
    def _get_client_identifier(scope: Scope, authenticated_identity: Optional[str] = None) -> str:
        """Use API key identity when present, otherwise client host."""
        if authenticated_identity:
            return authenticated_identity
        client = scope.get("client")
        if client and client[0]:
            return str(client[0])
        return "unknown"

    @staticmethod
    def _is_rate_limit_exempt(path: str) -> bool:
        """Allow docs/schema endpoints to bypass rate limiting."""
        if path in _RATE_LIMIT_EXEMPT_PATHS:
            return True

        return any(
            path == prefix or path.startswith(f"{prefix}/")
            for prefix in _RATE_LIMIT_EXEMPT_PREFIXES
        )

    @classmethod
    def _is_auth_exempt(cls, path: str) -> bool:
        """Allow health/docs/schema endpoints to bypass API key auth."""
        return cls._is_rate_limit_exempt(path)

    @staticmethod
    def _get_content_length(scope: Scope) -> Optional[int]:
        """Parse Content-Length header if available and valid."""
        for header_name, header_value in scope.get("headers", []):
            if header_name.lower() != b"content-length":
                continue

            try:
                value = int(header_value.decode("ascii"))
                return value if value >= 0 else None
            except (ValueError, UnicodeDecodeError):
                return None

        return None

    async def _send_error_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Send standardized error response with correlation ID."""
        correlation_id = get_correlation_id()
        if not correlation_id:
            correlation_id = (
                self._extract_incoming_correlation_id(scope) or generate_correlation_id()
            )
            set_correlation_id(correlation_id)

        headers = {
            CORRELATION_ID_HEADER: correlation_id,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "X-XSS-Protection": "1; mode=block",
        }
        if extra_headers:
            headers.update(extra_headers)

        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail, "correlation_id": correlation_id},
            headers=headers,
        )
        await response(scope, receive, send)

    @staticmethod
    def _extract_incoming_correlation_id(scope: Scope) -> Optional[str]:
        """Read correlation ID from inbound headers if present."""
        for header_name, header_value in scope.get("headers", []):
            if header_name.lower() == _CORRELATION_ID_HEADER_BYTES:
                try:
                    return header_value.decode("ascii")
                except UnicodeDecodeError:
                    return None
        return None
