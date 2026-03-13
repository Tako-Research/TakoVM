"""Tests for SessionManager core behavior."""

import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

from tako_vm.config import TakoVMConfig
from tako_vm.execution.worker import CodeExecutor
from tako_vm.job_types import JobType, JobTypeRegistry
from tako_vm.models import SessionRecord
from tako_vm.server.sessions import SessionManager, SessionManagerError
from tako_vm.storage import ExecutionStorage


def _build_runtime_components(
    storage: ExecutionStorage,
    *,
    sessions_enabled: bool = True,
    session_max_events_per_poll: int = 100,
) -> tuple[TakoVMConfig, JobTypeRegistry, CodeExecutor, SessionManager]:
    config = TakoVMConfig.model_validate(
        {
            "container_runtime": "runc",
            "security_mode": "permissive",
            "sessions_enabled": sessions_enabled,
            "session_max_events_per_poll": session_max_events_per_poll,
        }
    )
    registry = JobTypeRegistry()
    registry.register(
        JobType(
            name="session-job",
            session_enabled=True,
            network_enabled=False,
        ),
        persist=False,
    )
    executor = CodeExecutor(registry=registry, config=config)
    manager = SessionManager(config=config, storage=storage, registry=registry, executor=executor)
    return config, registry, executor, manager


@pytest.mark.asyncio
async def test_create_session_persists_running_record_and_started_event(
    temp_data_dir, monkeypatch, tmp_path
):
    """Successful create_session stores running state and startup event."""
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()
    try:
        _, _, _, manager = _build_runtime_components(storage)
        monkeypatch.setattr("tako_vm.server.sessions.WORKSPACE_DIR", str(tmp_path))

        async def fake_run(cmd, timeout):
            del timeout
            return subprocess.CompletedProcess(cmd, 0, "container-abc\n", "")

        monkeypatch.setattr(manager, "_run_subprocess", fake_run)

        session = await manager.create_session(job_type_name="session-job")

        assert session.status == "running"
        assert session.container_id == "container-abc"

        loaded = await storage.get_session(session.session_id)
        assert loaded is not None
        assert loaded.status == "running"

        events = await storage.list_session_events(session.session_id)
        assert len(events) == 1
        assert events[0].event_type == "session_started"
        assert events[0].direction == "system"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_create_session_rejected_when_sessions_disabled(temp_data_dir):
    """Session creation is blocked by config when sessions_enabled=false."""
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()
    try:
        _, _, _, manager = _build_runtime_components(storage, sessions_enabled=False)
        with pytest.raises(SessionManagerError) as exc_info:
            await manager.create_session(job_type_name="session-job")
        assert exc_info.value.status_code == 403
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_send_event_writes_inbox_file_and_updates_last_activity(
    temp_data_dir, monkeypatch, tmp_path
):
    """send_event persists metadata and writes a mailbox envelope."""
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()
    try:
        _, _, _, manager = _build_runtime_components(storage)

        workspace = tmp_path / "session"
        inbox = workspace / "inbox"
        outbox = workspace / "outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        session = SessionRecord(
            session_id="session-send",
            status="running",
            job_type="session-job",
            created_at=now,
            last_activity_at=now,
            idle_timeout_seconds=1800,
            ttl_seconds=86400,
            container_name="tako-session-session-send",
            image_name="code-executor:latest",
            runtime="runc",
            workspace_dir=str(workspace),
        )
        await storage.save_session(session)

        async def fake_run(cmd, timeout):
            del timeout
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 0, "running\t0", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(manager, "_run_subprocess", fake_run)

        loaded_before = await storage.get_session(session.session_id)
        assert loaded_before is not None
        before = loaded_before.last_activity_at
        saved_event = await manager.send_event(
            session_id=session.session_id,
            payload={"message": "hello"},
            event_type="input",
        )
        loaded_after = await storage.get_session(session.session_id)
        assert loaded_after is not None
        after = loaded_after.last_activity_at

        assert saved_event.id is not None
        assert saved_event.direction == "in"
        assert after >= before

        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) == 1
        envelope = json.loads(inbox_files[0].read_text(encoding="utf-8"))
        assert envelope["event_type"] == "input"
        assert envelope["payload"] == {"message": "hello"}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_poll_events_ingests_outbox_and_handles_invalid_json(
    temp_data_dir, monkeypatch, tmp_path
):
    """poll_events ingests outbox files and normalizes malformed payloads."""
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()
    try:
        _, _, _, manager = _build_runtime_components(storage, session_max_events_per_poll=10)

        workspace = tmp_path / "session-poll"
        inbox = workspace / "inbox"
        outbox = workspace / "outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)

        session = SessionRecord(
            session_id="session-poll",
            status="running",
            job_type="session-job",
            idle_timeout_seconds=1800,
            ttl_seconds=86400,
            container_name="tako-session-session-poll",
            image_name="code-executor:latest",
            runtime="runc",
            workspace_dir=str(workspace),
        )
        await storage.save_session(session)

        (outbox / "event-good.json").write_text(
            json.dumps({"event_type": "reply", "text": "ok"}),
            encoding="utf-8",
        )
        (outbox / "event-bad.json").write_text("{not-json", encoding="utf-8")

        async def fake_run(cmd, timeout):
            del timeout
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 0, "running\t0", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(manager, "_run_subprocess", fake_run)

        _, events = await manager.poll_events(session_id=session.session_id, after_id=0, limit=100)

        assert len(events) == 2
        assert {event.event_type for event in events} == {"reply", "message"}

        bad_payload = next(event.payload for event in events if event.file_name == "event-bad.json")
        assert bad_payload["parse_error"] == "invalid_json"

        assert not list(outbox.glob("*.json"))
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_terminate_session_marks_status_and_cleans_workspace(
    temp_data_dir, monkeypatch, tmp_path
):
    """terminate_session updates status, emits event, and removes workspace."""
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()
    try:
        _, _, _, manager = _build_runtime_components(storage)

        workspace = tmp_path / "session-term"
        (workspace / "inbox").mkdir(parents=True)
        (workspace / "outbox").mkdir(parents=True)

        session = SessionRecord(
            session_id="session-term",
            status="running",
            job_type="session-job",
            idle_timeout_seconds=1800,
            ttl_seconds=86400,
            container_name="tako-session-session-term",
            image_name="code-executor:latest",
            runtime="runc",
            workspace_dir=str(workspace),
        )
        await storage.save_session(session)

        async def fake_run(cmd, timeout):
            del timeout
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(manager, "_run_subprocess", fake_run)

        terminated = await manager.terminate_session(session.session_id, reason="expired_idle")

        assert terminated.status == "expired"
        assert terminated.terminated_reason == "expired_idle"
        assert not workspace.exists()

        events = await storage.list_session_events(session.session_id)
        assert len(events) == 1
        assert events[0].event_type == "session_ended"
        assert events[0].payload["reason"] == "expired_idle"
    finally:
        await storage.close()
