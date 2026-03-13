"""
Integration tests for Tako VM API.

Tests job submission, status checking, and result retrieval
using FastAPI's TestClient (no running server required).
"""

import asyncio
import importlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from tako_vm.models import SessionStatus


@pytest.fixture
def client(temp_data_dir):
    """Create test client with lifespan management and temp database."""
    from tako_vm.config import reset_config

    reset_config()

    # Import app after setting env vars
    from tako_vm.server.app import app

    with TestClient(app) as test_client:
        yield test_client

    # Cleanup
    reset_config()


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_check(self, client):
        """Health endpoint returns status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "docker_available" in data
        assert "gvisor_available" in data
        assert "version" in data


class TestSyncExecution:
    """Tests for synchronous /execute endpoint."""

    def test_execute_simple_code(self, client):
        """Execute simple code that writes output."""
        code = """
import json
with open("/input/data.json") as f:
    data = json.load(f)
result = {"sum": data["a"] + data["b"]}
with open("/output/result.json", "w") as f:
    json.dump(result, f)
print("Done!")
"""
        response = client.post("/execute", json={"code": code, "input_data": {"a": 10, "b": 20}})

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True, (
            f"Execution failed: stderr={data.get('stderr')}, error={data.get('error')}"
        )
        assert data["output"] == {"sum": 30}
        assert "Done!" in data["stdout"]
        assert data["exit_code"] == 0

    def test_execute_syntax_error(self, client):
        """Execute code with syntax error."""
        response = client.post("/execute", json={"code": "def broken(", "input_data": {}})

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["exit_code"] != 0

    def test_execute_empty_code_rejected(self, client):
        """Empty code is rejected."""
        response = client.post("/execute", json={"code": "", "input_data": {}})

        assert response.status_code == 422  # Validation error


class TestAsyncExecution:
    """Tests for async /execute/async endpoint."""

    def test_async_submit_returns_job_id(self, client):
        """Async submit returns job ID and queued status."""
        response = client.post("/execute/async", json={"code": "print('hello')", "input_data": {}})

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "queued"

    def test_async_job_status(self, client):
        """Can check status of submitted job."""
        # Submit job
        submit_response = client.post(
            "/execute/async", json={"code": "print('hello')", "input_data": {}}
        )
        job_id = submit_response.json()["job_id"]

        # Check status
        status_response = client.get(f"/jobs/{job_id}")
        assert status_response.status_code == 200
        data = status_response.json()
        assert data["job_id"] == job_id
        assert "status" in data

    def test_async_job_result_wait(self, client):
        """Can wait for and retrieve job result."""
        code = """
import json
with open("/output/result.json", "w") as f:
    json.dump({"message": "async complete"}, f)
"""
        # Submit job
        submit_response = client.post("/execute/async", json={"code": code, "input_data": {}})
        job_id = submit_response.json()["job_id"]

        # Wait for result
        result_response = client.get(f"/jobs/{job_id}/result", params={"wait": True, "timeout": 60})

        assert result_response.status_code == 200
        data = result_response.json()
        assert data["execution_id"] == job_id
        # Status should be a terminal state
        assert data["status"] in ["succeeded", "failed", "timeout", "oom", "cancelled"]

    def test_job_not_found(self, client):
        """Non-existent job returns 404."""
        response = client.get("/jobs/nonexistent-job-id")
        assert response.status_code == 404


class TestJobTypes:
    """Tests for job type endpoints."""

    def test_list_job_types(self, client):
        """Can list available job types."""
        response = client.get("/job-types")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Should have at least the default job type
        names = [jt["name"] for jt in data]
        assert "default" in names

    def test_get_job_type(self, client):
        """Can get specific job type."""
        response = client.get("/job-types/default")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "default"
        assert "requirements" in data
        assert "python_version" in data
        assert "session_enabled" in data
        assert "gpu_enabled" in data
        assert "gpu_vendor" in data

    def test_job_type_not_found(self, client):
        """Non-existent job type returns 404."""
        response = client.get("/job-types/nonexistent")
        assert response.status_code == 404


class TestExecutionRecords:
    """Tests for execution record endpoints."""

    def test_list_executions(self, client):
        """Can list execution records (paginated)."""
        response = client.get("/executions")
        assert response.status_code == 200
        data = response.json()
        # Response is now paginated
        assert "items" in data
        assert "limit" in data
        assert "offset" in data
        assert "has_more" in data
        assert "count" in data
        assert isinstance(data["items"], list)

    def test_list_executions_with_filters(self, client):
        """Can filter execution records."""
        response = client.get("/executions", params={"status": "succeeded", "limit": 10})
        assert response.status_code == 200


class TestPoolStats:
    """Tests for worker pool stats."""

    def test_pool_stats(self, client):
        """Can get pool statistics."""
        response = client.get("/pool/stats")
        assert response.status_code == 200
        data = response.json()
        assert "pending" in data
        assert "running" in data
        assert "max_workers" in data


class TestDLQ:
    """Tests for dead letter queue endpoints."""

    def test_dlq_stats(self, client):
        """Can get DLQ statistics."""
        response = client.get("/dlq/stats")
        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "by_error_type" in data

    def test_dlq_list(self, client):
        """Can list DLQ entries (paginated)."""
        response = client.get("/dlq")
        assert response.status_code == 200
        data = response.json()
        # Response is now paginated
        assert "items" in data
        assert "limit" in data
        assert "offset" in data
        assert "has_more" in data
        assert "count" in data
        assert isinstance(data["items"], list)


class TestRerun:
    """Tests for /jobs/{job_id}/rerun endpoint."""

    def test_rerun_completed_job(self, client):
        """Can rerun a completed job with same code and inputs."""
        # First, create and complete a job
        code = """
import json
with open("/input/data.json") as f:
    data = json.load(f)
result = {"value": data["x"] * 2}
with open("/output/result.json", "w") as f:
    json.dump(result, f)
"""
        submit_response = client.post(
            "/execute/async", json={"code": code, "input_data": {"x": 21}}
        )
        assert submit_response.status_code == 200
        original_job_id = submit_response.json()["job_id"]

        # Wait for completion
        result_response = client.get(
            f"/jobs/{original_job_id}/result", params={"wait": True, "timeout": 60}
        )
        assert result_response.status_code == 200
        assert result_response.json()["status"] == "succeeded"
        assert result_response.json()["output"] == {"value": 42}

        # Rerun the job
        rerun_response = client.post(
            f"/jobs/{original_job_id}/rerun",
            json={},
        )
        assert rerun_response.status_code == 200
        rerun_data = rerun_response.json()
        assert "job_id" in rerun_data
        assert rerun_data["status"] == "queued"
        new_job_id = rerun_data["job_id"]
        assert new_job_id != original_job_id

        # Wait for rerun to complete and verify same result
        rerun_result = client.get(
            f"/jobs/{new_job_id}/result", params={"wait": True, "timeout": 60}
        )
        assert rerun_result.status_code == 200
        rerun_result_data = rerun_result.json()
        assert rerun_result_data["status"] == "succeeded"
        assert rerun_result_data["output"] == {"value": 42}

    def test_rerun_with_full_view_shows_lineage(self, client):
        """Rerun job has parent_execution_id and relationship in full view."""
        # Create and complete original job
        code = "print('original')"
        submit_response = client.post("/execute/async", json={"code": code})
        original_job_id = submit_response.json()["job_id"]

        client.get(f"/jobs/{original_job_id}/result", params={"wait": True, "timeout": 60})

        # Rerun
        rerun_response = client.post(f"/jobs/{original_job_id}/rerun", json={})
        new_job_id = rerun_response.json()["job_id"]

        # Wait and get full view
        client.get(f"/jobs/{new_job_id}/result", params={"wait": True, "timeout": 60})
        full_response = client.get(f"/jobs/{new_job_id}/result", params={"view": "full"})

        assert full_response.status_code == 200
        data = full_response.json()
        assert data["parent_execution_id"] == original_job_id
        assert data["relationship"] == "rerun"

    def test_rerun_nonexistent_job(self, client):
        """Rerun of non-existent job returns 404."""
        response = client.post("/jobs/nonexistent-id/rerun", json={})
        assert response.status_code == 404

    def test_rerun_pending_job_fails(self, client):
        """Cannot rerun a job that hasn't completed yet."""
        # Submit a long-running job
        code = "import time; time.sleep(60)"
        submit_response = client.post("/execute/async", json={"code": code})
        job_id = submit_response.json()["job_id"]

        # Try to rerun immediately (job still running)
        import time

        time.sleep(0.5)  # Give it time to start
        rerun_response = client.post(f"/jobs/{job_id}/rerun", json={})

        # Should fail because job hasn't completed
        assert rerun_response.status_code == 400

        # Cancel the job to clean up
        client.post(f"/jobs/{job_id}/cancel")


class TestFork:
    """Tests for /jobs/{job_id}/fork endpoint."""

    def test_fork_with_new_code(self, client):
        """Can fork a job with new code but same inputs."""
        # Create and complete original job
        original_code = """
import json
with open("/input/data.json") as f:
    data = json.load(f)
result = {"operation": "multiply", "value": data["x"] * 2}
with open("/output/result.json", "w") as f:
    json.dump(result, f)
"""
        submit_response = client.post(
            "/execute/async", json={"code": original_code, "input_data": {"x": 10}}
        )
        original_job_id = submit_response.json()["job_id"]

        # Wait for completion
        result_response = client.get(
            f"/jobs/{original_job_id}/result", params={"wait": True, "timeout": 60}
        )
        assert result_response.status_code == 200
        assert result_response.json()["output"] == {"operation": "multiply", "value": 20}

        # Fork with different code (add instead of multiply)
        new_code = """
import json
with open("/input/data.json") as f:
    data = json.load(f)
result = {"operation": "add", "value": data["x"] + 100}
with open("/output/result.json", "w") as f:
    json.dump(result, f)
"""
        fork_response = client.post(
            f"/jobs/{original_job_id}/fork",
            json={"code": new_code},
        )
        assert fork_response.status_code == 200
        fork_data = fork_response.json()
        assert "job_id" in fork_data
        assert fork_data["status"] == "queued"
        forked_job_id = fork_data["job_id"]

        # Wait for fork to complete
        fork_result = client.get(
            f"/jobs/{forked_job_id}/result", params={"wait": True, "timeout": 60}
        )
        assert fork_result.status_code == 200
        fork_result_data = fork_result.json()
        assert fork_result_data["status"] == "succeeded"
        # Same input (x=10) but different operation
        assert fork_result_data["output"] == {"operation": "add", "value": 110}

    def test_fork_shows_lineage(self, client):
        """Fork job has parent_execution_id and relationship='fork' in full view."""
        # Create original job
        code = "print('v1')"
        submit_response = client.post("/execute/async", json={"code": code})
        original_job_id = submit_response.json()["job_id"]

        client.get(f"/jobs/{original_job_id}/result", params={"wait": True, "timeout": 60})

        # Fork
        fork_response = client.post(f"/jobs/{original_job_id}/fork", json={"code": "print('v2')"})
        forked_job_id = fork_response.json()["job_id"]

        # Wait and get full view
        client.get(f"/jobs/{forked_job_id}/result", params={"wait": True, "timeout": 60})
        full_response = client.get(f"/jobs/{forked_job_id}/result", params={"view": "full"})

        assert full_response.status_code == 200
        data = full_response.json()
        assert data["parent_execution_id"] == original_job_id
        assert data["relationship"] == "fork"

    def test_fork_nonexistent_job(self, client):
        """Fork of non-existent job returns 404."""
        response = client.post("/jobs/nonexistent-id/fork", json={"code": "print('test')"})
        assert response.status_code == 404

    def test_fork_requires_code(self, client):
        """Fork requires new code in request body."""
        # Create and complete a job
        submit_response = client.post("/execute/async", json={"code": "print(1)"})
        job_id = submit_response.json()["job_id"]
        client.get(f"/jobs/{job_id}/result", params={"wait": True, "timeout": 60})

        # Try to fork without code
        response = client.post(f"/jobs/{job_id}/fork", json={})
        assert response.status_code == 422  # Validation error


class TestCancel:
    """Tests for /jobs/{job_id}/cancel endpoint."""

    def test_cancel_running_job(self, client):
        """Can cancel a running job."""
        # Submit a long-running job
        code = """
import time
for i in range(60):
    print(f"iteration {i}")
    time.sleep(1)
"""
        submit_response = client.post("/execute/async", json={"code": code})
        assert submit_response.status_code == 200
        job_id = submit_response.json()["job_id"]

        # Give it time to start
        import time

        time.sleep(1)

        # Cancel the job
        cancel_response = client.post(f"/jobs/{job_id}/cancel")
        assert cancel_response.status_code == 200
        cancel_data = cancel_response.json()
        assert cancel_data["status"] == "cancelled"
        assert cancel_data["job_id"] == job_id

    def test_cancel_nonexistent_job(self, client):
        """Cancel of non-existent job returns 404."""
        response = client.post("/jobs/nonexistent-id/cancel")
        assert response.status_code == 404

    def test_cancel_completed_job_fails(self, client):
        """Cannot cancel an already completed job."""
        # Create and wait for job to complete
        submit_response = client.post("/execute/async", json={"code": "print('quick')"})
        job_id = submit_response.json()["job_id"]

        # Wait for completion
        client.get(f"/jobs/{job_id}/result", params={"wait": True, "timeout": 60})

        # Try to cancel - should fail
        cancel_response = client.post(f"/jobs/{job_id}/cancel")
        assert cancel_response.status_code == 404


class TestArtifacts:
    """Tests for /jobs/{job_id}/artifacts/{name} endpoint."""

    def test_download_artifact(self, client):
        """Can download an artifact from a completed job."""
        code = """
with open("/output/report.txt", "w") as f:
    f.write("Hello from Tako VM!")
"""
        submit_response = client.post("/execute/async", json={"code": code})
        job_id = submit_response.json()["job_id"]

        # Wait for completion
        result_response = client.get(f"/jobs/{job_id}/result", params={"wait": True, "timeout": 60})
        assert result_response.status_code == 200
        assert result_response.json()["status"] == "succeeded"

        # Download the artifact
        artifact_response = client.get(f"/jobs/{job_id}/artifacts/report.txt")
        assert artifact_response.status_code == 200
        assert artifact_response.text == "Hello from Tako VM!"
        assert "ETag" in artifact_response.headers

    def test_artifact_head_request(self, client):
        """HEAD request returns artifact metadata without content."""
        code = """
with open("/output/data.csv", "w") as f:
    f.write("id,value\\n1,100\\n2,200\\n")
"""
        submit_response = client.post("/execute/async", json={"code": code})
        job_id = submit_response.json()["job_id"]

        client.get(f"/jobs/{job_id}/result", params={"wait": True, "timeout": 60})

        # HEAD request
        head_response = client.head(f"/jobs/{job_id}/artifacts/data.csv")
        assert head_response.status_code == 200
        assert "Content-Length" in head_response.headers
        assert "ETag" in head_response.headers
        assert head_response.content == b""  # No body

    def test_artifact_not_found(self, client):
        """Non-existent artifact returns 404."""
        code = "print('no artifacts')"
        submit_response = client.post("/execute/async", json={"code": code})
        job_id = submit_response.json()["job_id"]

        client.get(f"/jobs/{job_id}/result", params={"wait": True, "timeout": 60})

        response = client.get(f"/jobs/{job_id}/artifacts/nonexistent.txt")
        assert response.status_code == 404

    def test_artifact_job_not_found(self, client):
        """Artifact request for non-existent job returns 404."""
        response = client.get("/jobs/nonexistent-id/artifacts/file.txt")
        assert response.status_code == 404

    def test_artifact_persisted_to_storage(self, client):
        """Artifacts are copied to permanent storage, surviving temp cleanup."""
        from tako_vm.config import get_config

        code = """
with open("/output/persisted.txt", "w") as f:
    f.write("I should persist!")
"""
        submit_response = client.post("/execute/async", json={"code": code})
        job_id = submit_response.json()["job_id"]

        # Wait for completion (temp workspace is cleaned up after this)
        result_response = client.get(f"/jobs/{job_id}/result", params={"wait": True, "timeout": 60})
        assert result_response.status_code == 200
        assert result_response.json()["status"] == "succeeded"

        # Verify artifact exists at permanent storage location
        config = get_config()
        artifact_path = config.data_dir / "runs" / job_id / "artifacts" / "persisted.txt"
        assert artifact_path.exists(), "Artifact should be copied to permanent storage"
        assert artifact_path.read_text() == "I should persist!"

        # Also verify it's still downloadable via API
        artifact_response = client.get(f"/jobs/{job_id}/artifacts/persisted.txt")
        assert artifact_response.status_code == 200
        assert artifact_response.text == "I should persist!"


class TestIdempotency:
    """Tests for idempotency key behavior."""

    def test_idempotent_request_returns_same_job(self, client):
        """Same idempotency key returns the same job ID."""
        idempotency_key = "test-idem-key-12345"

        # First request
        first_response = client.post(
            "/execute/async",
            json={"code": "print('hello')", "idempotency_key": idempotency_key},
        )
        assert first_response.status_code == 200
        first_job_id = first_response.json()["job_id"]

        # Second request with same key
        second_response = client.post(
            "/execute/async",
            json={"code": "print('hello')", "idempotency_key": idempotency_key},
        )
        assert second_response.status_code == 200
        second_job_id = second_response.json()["job_id"]

        # Should be the same job
        assert first_job_id == second_job_id

    def test_idempotency_key_reuse_with_different_payload_fails(self, client):
        """Reusing idempotency key with different payload returns 409 Conflict."""
        idempotency_key = "test-conflict-key-67890"

        # First request
        first_response = client.post(
            "/execute/async",
            json={"code": "print('first')", "idempotency_key": idempotency_key},
        )
        assert first_response.status_code == 200

        # Second request with same key but different code
        second_response = client.post(
            "/execute/async",
            json={"code": "print('different')", "idempotency_key": idempotency_key},
        )
        assert second_response.status_code == 409

    def test_different_idempotency_keys_create_different_jobs(self, client):
        """Different idempotency keys create separate jobs."""
        # First request
        first_response = client.post(
            "/execute/async",
            json={"code": "print('test')", "idempotency_key": "key-one-abc"},
        )
        first_job_id = first_response.json()["job_id"]

        # Second request with different key
        second_response = client.post(
            "/execute/async",
            json={"code": "print('test')", "idempotency_key": "key-two-xyz"},
        )
        second_job_id = second_response.json()["job_id"]

        # Should be different jobs
        assert first_job_id != second_job_id

    def test_idempotency_key_validation(self, client):
        """Idempotency key must be alphanumeric and 8-255 chars."""
        # Too short
        response = client.post(
            "/execute/async",
            json={"code": "print(1)", "idempotency_key": "short"},
        )
        assert response.status_code == 422

        # Invalid characters
        response = client.post(
            "/execute/async",
            json={"code": "print(1)", "idempotency_key": "invalid key with spaces"},
        )
        assert response.status_code == 422


class TestSessionsApi:
    """Tests for session API endpoint behavior."""

    def _make_session_record(self, session_id: str, status: "SessionStatus" = "running"):
        from tako_vm.models import SessionRecord

        now = datetime.now(timezone.utc)
        return SessionRecord(
            session_id=session_id,
            status=status,
            job_type="session-job",
            created_at=now,
            started_at=now,
            last_activity_at=now,
            idle_timeout_seconds=1800,
            ttl_seconds=86400,
            container_name=f"tako-session-{session_id}",
            image_name="code-executor:latest",
            runtime="runc",
            workspace_dir=f"/tmp/{session_id}",
        )

    def test_create_session_success(self, client, monkeypatch):
        """POST /sessions returns serialized session record."""
        app_module = importlib.import_module("tako_vm.server.app")

        async def fake_create_session(*args, **kwargs):
            del args, kwargs
            return self._make_session_record("session-123", status="running")

        session_manager = cast(Any, app_module.state).session_manager
        monkeypatch.setattr(session_manager, "create_session", fake_create_session)

        response = client.post("/sessions", json={"job_type": "session-job"})
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "session-123"
        assert data["status"] == "running"
        assert data["runtime"] == "runc"

    def test_create_session_error_mapping(self, client, monkeypatch):
        """SessionManagerError is surfaced as HTTPException with status code."""
        app_module = importlib.import_module("tako_vm.server.app")
        session_manager_error_cls = importlib.import_module(
            "tako_vm.server.sessions"
        ).SessionManagerError

        async def fake_create_session(*args, **kwargs):
            del args, kwargs
            raise session_manager_error_cls("Sessions are disabled", status_code=403)

        session_manager = cast(Any, app_module.state).session_manager
        monkeypatch.setattr(session_manager, "create_session", fake_create_session)

        response = client.post("/sessions", json={"job_type": "session-job"})
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()

    def test_list_sessions_pagination(self, client, monkeypatch):
        """GET /sessions applies has_more logic with limit+1 fetch."""
        app_module = importlib.import_module("tako_vm.server.app")

        async def fake_list_sessions(*args, **kwargs):
            del args, kwargs
            return [
                self._make_session_record("session-a", status="running"),
                self._make_session_record("session-b", status="running"),
            ]

        session_manager = cast(Any, app_module.state).session_manager
        monkeypatch.setattr(session_manager, "list_sessions", fake_list_sessions)

        response = client.get("/sessions", params={"limit": 1, "offset": 0})
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["has_more"] is True
        assert len(data["items"]) == 1

    def test_send_and_poll_session_events(self, client, monkeypatch):
        """Session send and poll endpoints serialize mailbox events."""
        app_module = importlib.import_module("tako_vm.server.app")
        session_event_cls = importlib.import_module("tako_vm.models").SessionEvent

        now = datetime.now(timezone.utc)

        async def fake_send_event(*args, **kwargs):
            del args, kwargs
            return session_event_cls(
                id=7,
                session_id="session-abc",
                direction="in",
                event_type="input",
                payload={"message": "hi"},
                created_at=now,
            )

        async def fake_poll_events(*args, **kwargs):
            del args, kwargs
            session = self._make_session_record("session-abc", status="running")
            events = [
                session_event_cls(
                    id=8,
                    session_id="session-abc",
                    direction="out",
                    event_type="reply",
                    payload={"text": "hello"},
                    created_at=now,
                )
            ]
            return session, events

        session_manager = cast(Any, app_module.state).session_manager
        monkeypatch.setattr(session_manager, "send_event", fake_send_event)
        monkeypatch.setattr(session_manager, "poll_events", fake_poll_events)

        send_response = client.post(
            "/sessions/session-abc/send",
            json={"event_type": "input", "payload": {"message": "hi"}},
        )
        assert send_response.status_code == 200
        assert send_response.json()["event_id"] == 7
        assert send_response.json()["status"] == "queued"

        poll_response = client.get("/sessions/session-abc/events", params={"after": 0, "limit": 10})
        assert poll_response.status_code == 200
        poll_data = poll_response.json()
        assert poll_data["status"] == "running"
        assert poll_data["next_cursor"] == 8
        assert len(poll_data["events"]) == 1
        assert poll_data["events"][0]["event_type"] == "reply"


class TestSessionsApiIntegration:
    """Integration-style session API tests with minimal mocking."""

    def _configure_real_session_manager(self, monkeypatch, tmp_path):
        app_module = importlib.import_module("tako_vm.server.app")
        job_type_cls = importlib.import_module("tako_vm.job_types").JobType
        state = cast(Any, app_module.state)

        state.config.sessions_enabled = True
        state.config.session_max_events_per_poll = 10
        state.registry.register(
            job_type_cls(
                name="session-live",
                session_enabled=True,
                base_image="code-executor:latest",
            ),
            persist=False,
        )

        monkeypatch.setattr("tako_vm.server.sessions.WORKSPACE_DIR", str(tmp_path))

        async def fake_run(cmd, timeout):
            del timeout
            if cmd[:2] == ["docker", "run"]:
                return subprocess.CompletedProcess(cmd, 0, "container-live\n", "")
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 0, "running\t0", "")
            if cmd[:2] == ["docker", "rm"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(state.session_manager, "_run_subprocess", fake_run)
        return state

    def test_session_lifecycle_endpoints_with_real_manager(self, client, monkeypatch, tmp_path):
        """Create/send/poll/terminate endpoints work end-to-end via SessionManager."""
        state = self._configure_real_session_manager(monkeypatch, tmp_path)

        create_response = client.post(
            "/sessions",
            json={"job_type": "session-live", "metadata": {"flow": "integration"}},
        )
        assert create_response.status_code == 200
        session_data = create_response.json()
        session_id = session_data["session_id"]
        assert session_data["status"] == "running"

        get_response = client.get(f"/sessions/{session_id}")
        assert get_response.status_code == 200
        assert get_response.json()["status"] == "running"

        send_response = client.post(
            f"/sessions/{session_id}/send",
            json={"event_type": "input", "payload": {"prompt": "hello"}},
        )
        assert send_response.status_code == 200
        assert send_response.json()["status"] == "queued"

        record = asyncio.run(state.storage.get_session(session_id))
        assert record is not None
        outbox_file = Path(record.workspace_dir) / "outbox" / "event-1.json"
        outbox_file.write_text(
            json.dumps({"event_type": "reply", "text": "world"}),
            encoding="utf-8",
        )

        poll_response = client.get(
            f"/sessions/{session_id}/events", params={"after": 0, "limit": 20}
        )
        assert poll_response.status_code == 200
        poll_data = poll_response.json()
        assert len(poll_data["events"]) >= 1
        assert any(event["event_type"] == "reply" for event in poll_data["events"])

        terminate_response = client.post(f"/sessions/{session_id}/terminate")
        assert terminate_response.status_code == 200
        assert terminate_response.json()["status"] == "terminated"

        final_response = client.get(f"/sessions/{session_id}")
        assert final_response.status_code == 200
        assert final_response.json()["status"] == "terminated"

    def test_send_session_event_enforces_payload_limit(self, client, monkeypatch, tmp_path):
        """Session send endpoint returns 413 for oversized payloads."""
        state = self._configure_real_session_manager(monkeypatch, tmp_path)
        state.config.session_max_message_bytes = 16

        create_response = client.post(
            "/sessions",
            json={"job_type": "session-live"},
        )
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]

        send_response = client.post(
            f"/sessions/{session_id}/send",
            json={"event_type": "input", "payload": {"message": "x" * 200}},
        )
        assert send_response.status_code == 413
        assert "session_max_message_bytes" in send_response.json()["detail"]
