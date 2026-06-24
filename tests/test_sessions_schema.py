"""
Tests for the session persistence scaffolding (phase 0, feature-flagged off).

Covers the additive, zero-runtime-change schema work:
- SessionStatus / SessionEventDirection / SessionRecord / SessionEvent models
- ExecutionRecord.session_id nullable FK
- the 0005_sessions / 0006_execution_session_fk migrations (shape only)
- the session config fields, bounds, cross-field validator, and env wiring
- storage round-trips (Postgres-backed; skip cleanly when DB unavailable)

The model, migration-shape, and config tests are pure (no DB). The storage
round-trip tests reuse the shared ``test_storage`` fixture from conftest, which
skips when no Postgres test database is reachable (and fails loudly in CI).
"""

from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from tako_vm.config import ConfigurationError, TakoVMConfig, load_config
from tako_vm.models import (
    ExecutionRecord,
    SessionEvent,
    SessionEventDirection,
    SessionRecord,
    SessionStatus,
)
from tako_vm.storage import MIGRATIONS, SESSION_TERMINAL_STATUSES

# ---------------------------------------------------------------------------
# Model unit tests (pure, no DB)
# ---------------------------------------------------------------------------


class TestSessionRecordModel:
    """SessionRecord defaults, bounds, and serialization."""

    def test_minimal_session_defaults(self):
        """Required fields only; everything else takes documented defaults."""
        session = SessionRecord(
            container_name="tako-session-abc",
            workspace_dir="/workspaces/abc",
        )
        assert session.status == "creating"
        assert session.container_id is None
        assert session.image_name is None
        assert session.runtime is None
        assert session.idle_timeout_seconds == 1800
        assert session.ttl_seconds == 86400
        assert session.metadata == {}
        assert session.started_at is None
        assert session.expires_at is None
        assert session.ended_at is None
        # session_id is a uuid4 by default.
        assert isinstance(session.session_id, str)
        assert len(session.session_id) <= 64
        # Activity/creation timestamps default to "now".
        assert isinstance(session.created_at, datetime)
        assert isinstance(session.last_activity_at, datetime)

    def test_container_name_required(self):
        with pytest.raises(ValidationError):
            SessionRecord(workspace_dir="/workspaces/abc")

    def test_workspace_dir_required(self):
        with pytest.raises(ValidationError):
            SessionRecord(container_name="tako-session-abc")

    @pytest.mark.parametrize("status", list(get_args(SessionStatus)))
    def test_all_seven_statuses_accepted(self, status):
        """All 7 SessionStatus values must be accepted by the model."""
        session = SessionRecord(
            container_name="c",
            workspace_dir="/w",
            status=status,
        )
        assert session.status == status

    def test_session_status_has_all_seven_states(self):
        assert set(get_args(SessionStatus)) == {
            "creating",
            "running",
            "idle",
            "suspended",
            "terminated",
            "failed",
            "expired",
        }

    def test_idle_timeout_lower_bound(self):
        SessionRecord(container_name="c", workspace_dir="/w", idle_timeout_seconds=30)
        with pytest.raises(ValidationError):
            SessionRecord(container_name="c", workspace_dir="/w", idle_timeout_seconds=29)

    def test_idle_timeout_upper_bound(self):
        SessionRecord(container_name="c", workspace_dir="/w", idle_timeout_seconds=86400)
        with pytest.raises(ValidationError):
            SessionRecord(container_name="c", workspace_dir="/w", idle_timeout_seconds=86401)

    def test_ttl_lower_bound(self):
        SessionRecord(container_name="c", workspace_dir="/w", ttl_seconds=60)
        with pytest.raises(ValidationError):
            SessionRecord(container_name="c", workspace_dir="/w", ttl_seconds=59)

    def test_ttl_upper_bound(self):
        SessionRecord(container_name="c", workspace_dir="/w", ttl_seconds=604800)
        with pytest.raises(ValidationError):
            SessionRecord(container_name="c", workspace_dir="/w", ttl_seconds=604801)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            SessionRecord(container_name="c", workspace_dir="/w", bogus="x")

    def test_serialization_round_trip(self):
        """model_dump -> model_validate preserves every field."""
        session = SessionRecord(
            session_id="sess-1",
            status="running",
            container_name="tako-session-1",
            container_id="deadbeef",
            image_name="code-executor:latest",
            runtime="runsc",
            workspace_dir="/workspaces/1",
            idle_timeout_seconds=600,
            ttl_seconds=7200,
            metadata={"source": "test"},
        )
        dumped = session.model_dump()
        restored = SessionRecord.model_validate(dumped)
        assert restored == session
        assert restored.metadata["source"] == "test"


class TestSessionEventModel:
    """SessionEvent defaults, direction enum, and serialization."""

    def test_defaults(self):
        event = SessionEvent(session_id="sess-1", direction="in")
        assert event.id is None
        assert event.event_type == "message"
        assert event.payload is None
        assert isinstance(event.created_at, datetime)

    def test_session_id_required(self):
        with pytest.raises(ValidationError):
            SessionEvent(direction="in")

    def test_direction_required(self):
        with pytest.raises(ValidationError):
            SessionEvent(session_id="sess-1")

    @pytest.mark.parametrize("direction", list(get_args(SessionEventDirection)))
    def test_all_directions_accepted(self, direction):
        event = SessionEvent(session_id="sess-1", direction=direction)
        assert event.direction == direction

    def test_invalid_direction_rejected(self):
        with pytest.raises(ValidationError):
            SessionEvent(session_id="sess-1", direction="sideways")

    def test_serialization_round_trip(self):
        event = SessionEvent(
            id=5,
            session_id="sess-1",
            direction="out",
            event_type="message",
            payload={"value": 1},
        )
        restored = SessionEvent.model_validate(event.model_dump())
        assert restored == event


class TestExecutionRecordSessionId:
    """ExecutionRecord gains a nullable session_id FK."""

    def test_defaults_none(self):
        record = ExecutionRecord()
        assert record.session_id is None

    def test_session_id_serializes(self):
        record = ExecutionRecord(session_id="sess-42")
        dumped = record.model_dump()
        assert dumped["session_id"] == "sess-42"
        restored = ExecutionRecord.model_validate(dumped)
        assert restored.session_id == "sess-42"

    def test_none_serializes(self):
        record = ExecutionRecord()
        assert record.model_dump()["session_id"] is None

    def test_max_length_enforced(self):
        with pytest.raises(ValidationError):
            ExecutionRecord(session_id="x" * 65)


# ---------------------------------------------------------------------------
# Migration shape tests (pure, no DB)
# ---------------------------------------------------------------------------


class TestSessionMigrationsShape:
    """The new migrations are present, ordered, unique, and non-empty."""

    def test_new_migrations_present(self):
        versions = [v for v, _ in MIGRATIONS]
        assert "0005_sessions" in versions
        assert "0006_execution_session_fk" in versions

    def test_versions_unique(self):
        versions = [v for v, _ in MIGRATIONS]
        assert len(versions) == len(set(versions))

    def test_versions_sorted_and_ordered(self):
        versions = [v for v, _ in MIGRATIONS]
        assert versions == sorted(versions)
        # The two new migrations come last, in order.
        assert versions[-2:] == ["0005_sessions", "0006_execution_session_fk"]

    def test_all_sql_non_empty_strings(self):
        for version, sql in MIGRATIONS:
            assert isinstance(sql, str)
            assert sql.strip(), f"migration {version} has empty SQL"

    def test_sessions_migration_pins_full_status_vocabulary(self):
        sql = dict(MIGRATIONS)["0005_sessions"]
        assert "CREATE TABLE IF NOT EXISTS sessions" in sql
        assert "CREATE TABLE IF NOT EXISTS session_events" in sql
        # CHECK constraint covers all 7 states (so it never needs widening).
        for status in get_args(SessionStatus):
            assert f"'{status}'" in sql

    def test_session_fk_migration_is_additive(self):
        sql = dict(MIGRATIONS)["0006_execution_session_fk"]
        assert "ADD COLUMN IF NOT EXISTS session_id" in sql
        assert "idx_execution_session_id" in sql

    def test_session_terminal_statuses_constant(self):
        assert set(SESSION_TERMINAL_STATUSES) == {"terminated", "failed", "expired"}
        # Every terminal status is a real SessionStatus.
        assert set(SESSION_TERMINAL_STATUSES).issubset(set(get_args(SessionStatus)))


# ---------------------------------------------------------------------------
# Config tests (pure, no DB)
# ---------------------------------------------------------------------------


class TestSessionConfig:
    """Session config fields, defaults, bounds, validator, and env wiring."""

    def test_defaults(self):
        config = TakoVMConfig()
        assert config.sessions_enabled is False
        assert config.session_idle_timeout_seconds == 1800
        assert config.session_max_ttl_seconds == 86400
        assert config.session_max_concurrent == 50

    def test_idle_le_ttl_validator_accepts_equal(self):
        config = TakoVMConfig(
            session_idle_timeout_seconds=3600,
            session_max_ttl_seconds=3600,
        )
        assert config.session_idle_timeout_seconds == 3600

    def test_idle_le_ttl_validator_rejects_idle_greater_than_ttl(self):
        with pytest.raises(ValidationError):
            TakoVMConfig(
                session_idle_timeout_seconds=10000,
                session_max_ttl_seconds=5000,
            )

    def test_idle_timeout_bounds(self):
        TakoVMConfig(session_idle_timeout_seconds=30)
        with pytest.raises(ValidationError):
            TakoVMConfig(session_idle_timeout_seconds=29)
        with pytest.raises(ValidationError):
            TakoVMConfig(session_idle_timeout_seconds=86401)

    def test_max_ttl_bounds(self):
        # Lower idle alongside ttl so the idle<=ttl cross-field validator isn't
        # what trips; we're exercising the ttl field bounds here.
        TakoVMConfig(session_idle_timeout_seconds=60, session_max_ttl_seconds=60)
        with pytest.raises(ValidationError):
            TakoVMConfig(session_max_ttl_seconds=59)
        with pytest.raises(ValidationError):
            TakoVMConfig(session_max_ttl_seconds=604801)

    def test_max_concurrent_lower_bound(self):
        TakoVMConfig(session_max_concurrent=1)
        with pytest.raises(ValidationError):
            TakoVMConfig(session_max_concurrent=0)

    def test_env_var_parsing(self, monkeypatch):
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")
        monkeypatch.setenv("TAKO_VM_SESSIONS_ENABLED", "true")
        monkeypatch.setenv("TAKO_VM_SESSION_IDLE_TIMEOUT_SECONDS", "900")
        monkeypatch.setenv("TAKO_VM_SESSION_MAX_TTL_SECONDS", "43200")
        monkeypatch.setenv("TAKO_VM_SESSION_MAX_CONCURRENT", "7")

        config = load_config()
        assert config.sessions_enabled is True
        assert config.session_idle_timeout_seconds == 900
        assert config.session_max_ttl_seconds == 43200
        assert config.session_max_concurrent == 7

    def test_env_sessions_enabled_falsey(self, monkeypatch):
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")
        monkeypatch.setenv("TAKO_VM_SESSIONS_ENABLED", "no")
        config = load_config()
        assert config.sessions_enabled is False

    def test_env_non_integer_raises(self, monkeypatch):
        monkeypatch.setenv("TAKO_VM_SECURITY_MODE", "permissive")
        monkeypatch.setenv("TAKO_VM_SESSION_MAX_CONCURRENT", "not-an-int")
        with pytest.raises(ConfigurationError):
            load_config()


# ---------------------------------------------------------------------------
# Storage round-trip tests (Postgres-backed; skip cleanly without a DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(test_storage):
    """Reuse the shared Postgres-backed storage fixture from conftest."""
    return test_storage


class TestSessionStorage:
    """save/get/list/touch + session events round-trip through Postgres."""

    @staticmethod
    def _make_session(session_id: str, status: SessionStatus = "running") -> SessionRecord:
        now = datetime.now(timezone.utc)
        return SessionRecord(
            session_id=session_id,
            status=status,
            container_name=f"tako-session-{session_id}",
            image_name="code-executor:latest",
            runtime="runc",
            workspace_dir=f"/workspaces/{session_id}",
            created_at=now,
            last_activity_at=now,
            idle_timeout_seconds=1800,
            ttl_seconds=86400,
            metadata={"source": "test"},
        )

    def test_save_and_get_session(self, storage):
        session = self._make_session("session-1")
        storage.save_session(session)

        loaded = storage.get_session("session-1")
        assert loaded is not None
        assert loaded.session_id == "session-1"
        assert loaded.status == "running"
        assert loaded.container_name == "tako-session-session-1"
        assert loaded.workspace_dir == "/workspaces/session-1"
        assert loaded.metadata["source"] == "test"

    def test_get_missing_session_returns_none(self, storage):
        assert storage.get_session("does-not-exist") is None

    def test_save_session_upsert_updates(self, storage):
        session = self._make_session("session-upsert", status="creating")
        storage.save_session(session)

        session.status = "running"
        session.container_id = "container-xyz"
        storage.save_session(session)

        loaded = storage.get_session("session-upsert")
        assert loaded.status == "running"
        assert loaded.container_id == "container-xyz"

    def test_list_sessions_with_status_filter(self, storage):
        storage.save_session(self._make_session("running-1", status="running"))
        storage.save_session(self._make_session("terminated-1", status="terminated"))

        running = storage.list_sessions(status="running")
        assert len(running) == 1
        assert running[0].session_id == "running-1"

    def test_list_sessions_no_filter(self, storage):
        storage.save_session(self._make_session("a", status="running"))
        storage.save_session(self._make_session("b", status="idle"))
        assert len(storage.list_sessions()) == 2

    def test_touch_session_updates_last_activity(self, storage):
        session = self._make_session("session-touch")
        storage.save_session(session)

        touched_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        updated = storage.touch_session("session-touch", touched_at=touched_at)
        assert updated is True

        loaded = storage.get_session("session-touch")
        assert loaded is not None
        assert loaded.last_activity_at == touched_at

    def test_touch_missing_session_returns_false(self, storage):
        assert storage.touch_session("nope") is False

    def test_save_and_list_session_events(self, storage):
        storage.save_session(self._make_session("session-events"))

        first = storage.save_session_event(
            SessionEvent(
                session_id="session-events",
                direction="out",
                event_type="message",
                payload={"value": 1},
            )
        )
        second = storage.save_session_event(
            SessionEvent(
                session_id="session-events",
                direction="in",
                payload={"value": 2},
            )
        )

        assert first.id is not None
        assert second.id is not None
        assert second.id != first.id

        events = storage.list_session_events("session-events")
        assert len(events) == 2
        assert events[0].payload == {"value": 1}
        assert events[1].direction == "in"

    def test_list_session_events_after_cursor(self, storage):
        storage.save_session(self._make_session("session-cursor"))

        event1 = storage.save_session_event(
            SessionEvent(session_id="session-cursor", direction="in", payload={"n": 1})
        )
        event2 = storage.save_session_event(
            SessionEvent(session_id="session-cursor", direction="out", payload={"n": 2})
        )

        assert event1.id is not None
        assert event2.id is not None

        later = storage.list_session_events("session-cursor", after_id=event1.id)
        assert len(later) == 1
        assert later[0].id == event2.id

    def test_execution_record_session_id_round_trips(self, storage):
        """ExecutionRecord.session_id persists and reloads through the upsert."""
        record = ExecutionRecord(
            execution_id="exec-with-session",
            status="succeeded",
            session_id="sess-linked",
        )
        storage.save_record(record)

        loaded = storage.get_record("exec-with-session")
        assert loaded is not None
        assert loaded.session_id == "sess-linked"

    def test_execution_record_session_id_defaults_none_in_db(self, storage):
        record = ExecutionRecord(execution_id="exec-no-session", status="succeeded")
        storage.save_record(record)

        loaded = storage.get_record("exec-no-session")
        assert loaded is not None
        assert loaded.session_id is None
