"""Tests for API-layer request protection middleware."""

from contextlib import contextmanager

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tako_vm.config import TakoVMConfig
from tako_vm.server.limits import ApiProtectionMiddleware


def create_client(
    *,
    api_max_payload_bytes: int = 1024,
    api_auth_enabled: bool = False,
    api_keys: list[str] | None = None,
    api_auth_header: str = "X-API-Key",
    api_rate_limit_enabled: bool = True,
    api_rate_limit_requests: int = 2,
    api_rate_limit_window_seconds: int = 60,
) -> TestClient:
    """Create a minimal app client with API protection middleware."""
    config = TakoVMConfig(
        api_max_payload_bytes=api_max_payload_bytes,
        api_auth_enabled=api_auth_enabled,
        api_keys=api_keys or [],
        api_auth_header=api_auth_header,
        api_rate_limit_enabled=api_rate_limit_enabled,
        api_rate_limit_requests=api_rate_limit_requests,
        api_rate_limit_window_seconds=api_rate_limit_window_seconds,
    )

    app = FastAPI()
    app.add_middleware(ApiProtectionMiddleware, config_getter=lambda: config)

    @app.get("/limited")
    async def limited_endpoint():
        return {"ok": True}

    @app.post("/echo")
    async def echo_endpoint(request: Request):
        body = await request.body()
        return {"size": len(body)}

    @app.get("/ignore-body")
    async def ignore_body_endpoint():
        return {"ok": True}

    return TestClient(app)


@contextmanager
def create_server_app_client(config: TakoVMConfig):
    """Create client against the real server app with injected config."""
    from tako_vm.server.app import app as server_app
    from tako_vm.server.app import state as server_state

    had_config = hasattr(server_state, "config")
    previous_config = getattr(server_state, "config", None)
    server_state.config = config
    client = TestClient(server_app)
    try:
        yield client
    finally:
        client.close()
        if had_config:
            assert previous_config is not None
            server_state.config = previous_config
        else:
            delattr(server_state, "config")


class TestApiRateLimiting:
    """Rate limiting behavior."""

    def test_rate_limit_enforced(self):
        """Requests over the configured threshold return 429."""
        with create_client(api_rate_limit_requests=2, api_rate_limit_window_seconds=60) as client:
            assert client.get("/limited").status_code == 200
            assert client.get("/limited").status_code == 200

            blocked = client.get("/limited")
            assert blocked.status_code == 429
            assert "Retry-After" in blocked.headers
            assert int(blocked.headers["Retry-After"]) >= 1

            payload = blocked.json()
            assert "rate limit" in payload["detail"].lower()
            assert isinstance(payload.get("correlation_id"), str)
            assert payload["correlation_id"]

    def test_docs_endpoints_are_exempt(self):
        """Docs and schema endpoints bypass rate limiting."""
        with create_client(api_rate_limit_requests=1, api_rate_limit_window_seconds=60) as client:
            for _ in range(3):
                assert client.get("/docs").status_code == 200
                assert client.get("/openapi.json").status_code == 200

            # Exempt endpoints do not consume the normal quota.
            assert client.get("/limited").status_code == 200
            assert client.get("/limited").status_code == 429

    def test_rate_limit_can_be_disabled(self):
        """Disabling rate limiting allows repeated requests."""
        with create_client(api_rate_limit_enabled=False, api_rate_limit_requests=1) as client:
            for _ in range(5):
                assert client.get("/limited").status_code == 200

    def test_rate_limit_uses_api_key_identity_when_authenticated(self):
        """Authenticated clients have independent rate-limit buckets per API key."""
        keys = ["a" * 16, "b" * 16]
        with create_client(
            api_auth_enabled=True,
            api_keys=keys,
            api_rate_limit_requests=1,
            api_rate_limit_window_seconds=60,
        ) as client:
            assert client.get("/limited", headers={"X-API-Key": keys[0]}).status_code == 200
            assert client.get("/limited", headers={"X-API-Key": keys[0]}).status_code == 429
            assert client.get("/limited", headers={"X-API-Key": keys[1]}).status_code == 200


class TestApiAuthentication:
    """API key authentication behavior."""

    def test_auth_disabled_by_default(self):
        """Existing deployments remain unauthenticated unless auth is enabled."""
        with create_client(api_auth_enabled=False) as client:
            assert client.get("/limited").status_code == 200

    def test_missing_api_key_rejected_when_auth_enabled(self):
        """Protected routes reject unauthenticated requests."""
        with create_client(api_auth_enabled=True, api_keys=["a" * 16]) as client:
            response = client.get("/limited")

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        payload = response.json()
        assert "api key" in payload["detail"].lower()
        assert payload["correlation_id"]

    def test_invalid_api_key_rejected(self):
        """Protected routes reject invalid API keys."""
        with create_client(api_auth_enabled=True, api_keys=["a" * 16]) as client:
            response = client.get("/limited", headers={"X-API-Key": "b" * 16})

        assert response.status_code == 401

    def test_configured_header_api_key_allowed(self):
        """The configured API key header authenticates requests."""
        with create_client(api_auth_enabled=True, api_keys=["a" * 16]) as client:
            response = client.get("/limited", headers={"X-API-Key": "a" * 16})

        assert response.status_code == 200

    def test_authorization_bearer_api_key_allowed(self):
        """Bearer tokens are accepted for clients that prefer Authorization."""
        with create_client(api_auth_enabled=True, api_keys=["a" * 16]) as client:
            response = client.get("/limited", headers={"Authorization": f"Bearer {'a' * 16}"})

        assert response.status_code == 200

    def test_docs_and_health_are_auth_exempt(self):
        """Operational/docs endpoints remain available without API keys."""
        with create_client(api_auth_enabled=True, api_keys=["a" * 16]) as client:
            assert client.get("/docs").status_code == 200
            assert client.get("/openapi.json").status_code == 200


class TestApiPayloadLimit:
    """Payload size enforcement behavior."""

    def test_oversized_payload_rejected(self):
        """Payloads above configured max bytes return 413."""
        with create_client(api_max_payload_bytes=1024) as client:
            response = client.post(
                "/echo",
                content=b"x" * 1025,
                headers={"Content-Type": "application/octet-stream"},
            )

            assert response.status_code == 413
            payload = response.json()
            assert "payload too large" in payload["detail"].lower()
            assert "1024" in payload["detail"]
            assert isinstance(payload.get("correlation_id"), str)
            assert payload["correlation_id"]

    def test_payload_at_limit_allowed(self):
        """Payloads at configured max bytes are accepted."""
        with create_client(api_max_payload_bytes=1024) as client:
            response = client.post(
                "/echo",
                content=b"x" * 1024,
                headers={"Content-Type": "application/octet-stream"},
            )

            assert response.status_code == 200
            assert response.json()["size"] == 1024

    def test_oversized_chunked_payload_rejected_when_body_ignored(self):
        """Chunked oversized payloads are rejected even if route does not read body."""
        with create_client(api_max_payload_bytes=1024) as client:
            response = client.request(
                "GET", "/ignore-body", content=(b"x" * 1500 for _ in range(1))
            )

            assert response.status_code == 413
            payload = response.json()
            assert "payload too large" in payload["detail"].lower()
            assert isinstance(payload.get("correlation_id"), str)
            assert payload["correlation_id"]


class TestApiProtectionAppIntegration:
    """Integration checks against the real server app wiring."""

    def test_real_app_docs_exempt_and_non_exempt_limited(self):
        """Docs routes stay exempt while normal routes are rate-limited."""
        config = TakoVMConfig(
            api_rate_limit_enabled=True,
            api_rate_limit_requests=1,
            api_rate_limit_window_seconds=137,
        )

        with create_server_app_client(config) as client:
            for _ in range(3):
                assert client.get("/docs").status_code == 200
                assert client.get("/openapi.json").status_code == 200

            first = client.get("/does-not-exist")
            second = client.get("/does-not-exist")

            assert first.status_code == 404
            assert second.status_code == 429
