import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .models import (
    Artifact,
    DeadLetterEntry,
    ExecutionError,
    ExecutionRecord,
    ExecutionTiming,
    InputArtifact,
    JobVersion,
    ResourceUsage,
    SessionEvent,
    SessionRecord,
)

logger = logging.getLogger(__name__)

MIGRATION_LOCK_ID = 94857231
RowMapping = Mapping[str, Any]

TERMINAL_STATUSES = ("succeeded", "failed", "timeout", "oom", "cancelled")

SESSION_TERMINAL_STATUSES = ("terminated", "failed", "expired")

_SAVE_RECORD_RETRY_DELAYS = (0.2, 0.8, 2.0)

MIN_DIGEST_PREFIX_LEN = 12

_DIGEST_RE = re.compile(rf"^[0-9a-f]{{{MIN_DIGEST_PREFIX_LEN},64}}$")


MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_initial",
        """
        CREATE TABLE IF NOT EXISTS execution_records (
            execution_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            job_type TEXT NOT NULL,
            job_ref TEXT NOT NULL DEFAULT 'default@latest',

            created_at TIMESTAMPTZ NOT NULL,
            queued_at TIMESTAMPTZ NOT NULL,
            dequeued_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            duration_ms INTEGER,

            attempt INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 1,
            worker_id TEXT,
            idempotency_key TEXT,
            idempotency_fingerprint TEXT,

            code_hash TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            params_hash TEXT,
            input_artifacts_hash TEXT,

            input_artifacts_json JSONB,

            exit_code INTEGER,
            stdout TEXT,
            stderr TEXT,
            stdout_truncated BOOLEAN DEFAULT FALSE,
            stderr_truncated BOOLEAN DEFAULT FALSE,
            result_json JSONB,

            max_rss_mb DOUBLE PRECISION,
            cpu_time_ms INTEGER,
            wall_time_ms INTEGER,

            timing_json JSONB,

            artifacts_json JSONB,
            error_json JSONB,

            client_ip TEXT,
            parent_execution_id TEXT,
            relationship TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_execution_status ON execution_records(status);
        CREATE INDEX IF NOT EXISTS idx_execution_job_type ON execution_records(job_type);
        CREATE INDEX IF NOT EXISTS idx_execution_created_at ON execution_records(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_execution_parent ON execution_records(parent_execution_id);

        CREATE INDEX IF NOT EXISTS idx_execution_status_created
            ON execution_records(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_execution_job_type_created
            ON execution_records(job_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_execution_status_job_type_created
            ON execution_records(status, job_type, created_at DESC);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_idempotency_unique
            ON execution_records(idempotency_key)
            WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS job_versions (
            digest TEXT PRIMARY KEY,
            job_type_name TEXT NOT NULL,
            version_tag TEXT,

            built_at TIMESTAMPTZ NOT NULL,
            built_by TEXT,
            dockerfile_hash TEXT NOT NULL,
            requirements_hash TEXT NOT NULL,
            image_ref TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_version_job_type ON job_versions(job_type_name);
        CREATE INDEX IF NOT EXISTS idx_version_tag ON job_versions(job_type_name, version_tag);
        CREATE INDEX IF NOT EXISTS idx_version_job_type_built_at
            ON job_versions(job_type_name, built_at DESC);

        CREATE TABLE IF NOT EXISTS dead_letter_queue (
            id BIGSERIAL PRIMARY KEY,
            job_id TEXT NOT NULL,
            job_data_json JSONB NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            client_ip TEXT,
            correlation_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_dlq_created_at ON dead_letter_queue(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_dlq_error_type ON dead_letter_queue(error_type);
        CREATE INDEX IF NOT EXISTS idx_dlq_job_id ON dead_letter_queue(job_id);
        """,
    ),
    (
        "0002_correlation_id",
        """
        ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS correlation_id TEXT
        """,
    ),
    (
        "0003_redact_dlq_job_data",
        """
        UPDATE dead_letter_queue
        SET job_data_json = (
                job_data_json - 'code' - 'input_data'
                - 'idempotency_key' - 'idempotency_fingerprint'
            )
            || jsonb_build_object('redacted_by_migration', true)
        WHERE job_data_json ?| array[
            'code', 'input_data', 'idempotency_key', 'idempotency_fingerprint'
        ]
        """,
    ),
    (
        "0004_runtime",
        """
        ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS runtime TEXT
        """,
    ),
    (
        "0005_sessions",
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'creating'
                CHECK (status IN (
                    'creating', 'running', 'idle', 'suspended',
                    'terminated', 'failed', 'expired'
                )),

            container_name TEXT NOT NULL,
            container_id TEXT,
            image_name TEXT,
            runtime TEXT,
            workspace_dir TEXT NOT NULL,

            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            last_activity_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,

            idle_timeout_seconds INTEGER NOT NULL,
            ttl_seconds INTEGER NOT NULL,

            metadata_json JSONB
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
        CREATE INDEX IF NOT EXISTS idx_sessions_last_activity
            ON sessions(last_activity_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

        CREATE TABLE IF NOT EXISTS session_events (
            id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL
                REFERENCES sessions(session_id) ON DELETE CASCADE,
            direction TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json JSONB,
            created_at TIMESTAMPTZ NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_session_events_session_id
            ON session_events(session_id, id);
        """,
    ),
    (
        "0006_execution_session_fk",
        """
        ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS session_id TEXT;

        CREATE INDEX IF NOT EXISTS idx_execution_session_id
            ON execution_records(session_id);
        """,
    ),
]


_SET_EXCLUDED = "excluded"
_SET_KEEP_EXISTING = "keep_existing"
_SET_COALESCE_EXISTING = "coalesce_existing"
_SET_COALESCE_EXCLUDED = "coalesce_excluded"
_SET_KEY = "key"


def _set_clause_for(name: str, policy: str) -> str:
    if policy == _SET_EXCLUDED:
        return f"{name} = EXCLUDED.{name}"
    if policy == _SET_KEEP_EXISTING:
        return f"{name} = execution_records.{name}"
    if policy == _SET_COALESCE_EXISTING:
        return f"{name} = COALESCE(execution_records.{name}, EXCLUDED.{name})"
    if policy == _SET_COALESCE_EXCLUDED:
        return f"{name} = COALESCE(EXCLUDED.{name}, execution_records.{name})"
    raise ValueError(f"unknown conflict policy {policy!r} for column {name!r}")


_EXECUTION_COLUMNS: list = [
    ("execution_id", _SET_KEY, lambda r, b: r.execution_id),
    ("status", _SET_EXCLUDED, lambda r, b: r.status),
    ("job_type", _SET_EXCLUDED, lambda r, b: r.job_type),
    ("job_ref", _SET_EXCLUDED, lambda r, b: r.job_ref),
    ("created_at", _SET_KEEP_EXISTING, lambda r, b: r.created_at),
    ("queued_at", _SET_COALESCE_EXISTING, lambda r, b: r.queued_at),
    ("dequeued_at", _SET_COALESCE_EXISTING, lambda r, b: r.dequeued_at),
    ("started_at", _SET_EXCLUDED, lambda r, b: r.started_at),
    ("ended_at", _SET_EXCLUDED, lambda r, b: r.ended_at),
    ("duration_ms", _SET_EXCLUDED, lambda r, b: r.duration_ms),
    ("attempt", _SET_EXCLUDED, lambda r, b: r.attempt),
    ("max_attempts", _SET_EXCLUDED, lambda r, b: r.max_attempts),
    ("worker_id", _SET_COALESCE_EXCLUDED, lambda r, b: r.worker_id),
    ("idempotency_key", _SET_COALESCE_EXISTING, lambda r, b: r.idempotency_key),
    ("idempotency_fingerprint", _SET_COALESCE_EXISTING, lambda r, b: r.idempotency_fingerprint),
    ("code_hash", _SET_COALESCE_EXISTING, lambda r, b: r.code_hash),
    ("input_hash", _SET_COALESCE_EXISTING, lambda r, b: r.input_hash),
    ("params_hash", _SET_COALESCE_EXISTING, lambda r, b: r.params_hash),
    ("input_artifacts_hash", _SET_COALESCE_EXISTING, lambda r, b: r.input_artifacts_hash),
    ("input_artifacts_json", _SET_EXCLUDED, lambda r, b: Jsonb(b["input_artifacts_json"])),
    ("exit_code", _SET_EXCLUDED, lambda r, b: r.exit_code),
    ("stdout", _SET_EXCLUDED, lambda r, b: r.stdout),
    ("stderr", _SET_EXCLUDED, lambda r, b: r.stderr),
    ("stdout_truncated", _SET_EXCLUDED, lambda r, b: r.stdout_truncated),
    ("stderr_truncated", _SET_EXCLUDED, lambda r, b: r.stderr_truncated),
    (
        "result_json",
        _SET_EXCLUDED,
        lambda r, b: Jsonb(b["result_json"]) if b["result_json"] is not None else None,
    ),
    (
        "max_rss_mb",
        _SET_EXCLUDED,
        lambda r, b: r.resource_usage.max_rss_mb if r.resource_usage else None,
    ),
    (
        "cpu_time_ms",
        _SET_EXCLUDED,
        lambda r, b: r.resource_usage.cpu_time_ms if r.resource_usage else None,
    ),
    (
        "wall_time_ms",
        _SET_EXCLUDED,
        lambda r, b: r.resource_usage.wall_time_ms if r.resource_usage else None,
    ),
    (
        "timing_json",
        _SET_EXCLUDED,
        lambda r, b: Jsonb(b["timing_json"]) if b["timing_json"] is not None else None,
    ),
    ("artifacts_json", _SET_EXCLUDED, lambda r, b: Jsonb(b["artifacts_json"])),
    (
        "error_json",
        _SET_EXCLUDED,
        lambda r, b: Jsonb(b["error_json"]) if b["error_json"] is not None else None,
    ),
    ("client_ip", _SET_COALESCE_EXISTING, lambda r, b: r.client_ip),
    ("correlation_id", _SET_COALESCE_EXISTING, lambda r, b: r.correlation_id),
    ("parent_execution_id", _SET_COALESCE_EXISTING, lambda r, b: r.parent_execution_id),
    ("relationship", _SET_COALESCE_EXISTING, lambda r, b: r.relationship),
    ("runtime", _SET_COALESCE_EXCLUDED, lambda r, b: r.runtime),
    ("session_id", _SET_COALESCE_EXISTING, lambda r, b: r.session_id),
]

_INSERT_COLUMN_SQL = ", ".join(name for name, _, _ in _EXECUTION_COLUMNS)
_INSERT_PLACEHOLDER_SQL = ", ".join(["%s"] * len(_EXECUTION_COLUMNS))
_UPDATE_SET_SQL = ",\n                    ".join(
    _set_clause_for(name, policy) for name, policy, _ in _EXECUTION_COLUMNS if policy != _SET_KEY
)


_VERSION_KEY_COLUMN = "digest"
_VERSION_COLUMNS: list = [
    ("digest", lambda v: v.digest),
    ("job_type_name", lambda v: v.job_type_name),
    ("version_tag", lambda v: v.version_tag),
    ("built_at", lambda v: v.built_at),
    ("built_by", lambda v: v.built_by),
    ("dockerfile_hash", lambda v: v.dockerfile_hash),
    ("requirements_hash", lambda v: v.requirements_hash),
    ("image_ref", lambda v: v.image_ref),
]

_VERSION_INSERT_COLUMN_SQL = ", ".join(name for name, _ in _VERSION_COLUMNS)
_VERSION_INSERT_PLACEHOLDER_SQL = ", ".join(["%s"] * len(_VERSION_COLUMNS))
_VERSION_UPDATE_SET_SQL = ",\n                    ".join(
    f"{name} = EXCLUDED.{name}" for name, _ in _VERSION_COLUMNS if name != _VERSION_KEY_COLUMN
)


class ExecutionStorage:
    def __init__(self, database_url: str, min_pool_size: int = 1, max_pool_size: int = 10):
        self.database_url = database_url
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self._pool: Optional[AsyncConnectionPool] = None

    async def init(self) -> None:
        if self._pool is not None:
            return

        pool: AsyncConnectionPool = AsyncConnectionPool(
            conninfo=self.database_url,
            min_size=self.min_pool_size,
            max_size=self.max_pool_size,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await pool.open(wait=True, timeout=10.0)
        try:
            await self._run_migrations(pool)
        except Exception:
            await pool.close()
            raise

        self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _get_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("ExecutionStorage not initialized")
        return self._pool

    async def _run_migrations(self, pool) -> None:
        async with pool.connection() as conn:
            lock_acquired = False
            for _ in range(300):
                cursor = await conn.execute(
                    "SELECT pg_try_advisory_lock(%s) AS locked", (MIGRATION_LOCK_ID,)
                )
                row = await cursor.fetchone()
                if row and row.get("locked"):
                    lock_acquired = True
                    break
                await asyncio.sleep(0.1)

            if not lock_acquired:
                raise TimeoutError("Timed out acquiring migration advisory lock")

            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )

                for version, sql in MIGRATIONS:
                    async with conn.transaction():
                        cursor = await conn.execute(
                            """
                            INSERT INTO schema_migrations (version)
                            VALUES (%s)
                            ON CONFLICT (version) DO NOTHING
                            RETURNING version
                            """,
                            (version,),
                        )
                        inserted = await cursor.fetchone()
                        if not inserted:
                            continue

                        await conn.execute(sql)
                        logger.info("Applied database migration %s", version)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))

    async def save_record(self, record: ExecutionRecord) -> None:
        attempts = len(_SAVE_RECORD_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                rowcount = await self._save_record_once(record)
                break
            except psycopg.OperationalError as e:
                if attempt >= attempts - 1:
                    raise
                delay = _SAVE_RECORD_RETRY_DELAYS[attempt]
                logger.warning(
                    "Transient error saving execution record %s (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    record.execution_id,
                    attempt + 1,
                    attempts,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

        if rowcount == 0:
            logger.warning(
                "Refused to overwrite terminal execution record %s with status %s",
                record.execution_id,
                record.status,
            )

    async def _save_record_once(self, record: ExecutionRecord) -> int:
        blobs = {
            "artifacts_json": [a.model_dump() for a in record.artifacts],
            "input_artifacts_json": [a.model_dump() for a in record.input_artifacts],
            "error_json": record.error.model_dump() if record.error else None,
            "result_json": record.result_json,
            "timing_json": record.timing.model_dump() if record.timing else None,
        }

        terminal_placeholders = ", ".join(["%s"] * len(TERMINAL_STATUSES))

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                INSERT INTO execution_records (
                    {_INSERT_COLUMN_SQL}
                ) VALUES (
                    {_INSERT_PLACEHOLDER_SQL}
                )
                ON CONFLICT (execution_id) DO UPDATE SET
                    {_UPDATE_SET_SQL}
                WHERE execution_records.status NOT IN ({terminal_placeholders})
                """,
                (
                    *(extract(record, blobs) for _, _, extract in _EXECUTION_COLUMNS),
                    *TERMINAL_STATUSES,
                ),
            )
            return cursor.rowcount

    async def get_record(self, execution_id: str) -> Optional[ExecutionRecord]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM execution_records WHERE execution_id = %s", (execution_id,)
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_record(cast(RowMapping, row))

    async def get_by_idempotency_key(self, key: str) -> Optional[ExecutionRecord]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM execution_records WHERE idempotency_key = %s", (key,)
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_record(cast(RowMapping, row))

    async def list_records(
        self,
        status: Optional[str] = None,
        job_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ExecutionRecord]:
        query = "SELECT * FROM execution_records WHERE 1=1"
        params: List[Any] = []

        if status:
            query += " AND status = %s"
            params.append(status)

        if job_type:
            query += " AND job_type = %s"
            params.append(job_type)

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

        return [self._row_to_record(cast(RowMapping, row)) for row in rows]

    async def reconcile_stale_records(self) -> int:
        now = datetime.now(timezone.utc)
        error_json = ExecutionError(
            type="interrupted",
            message="job interrupted by server restart before completion",
        ).model_dump()

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE execution_records
                SET status = 'failed',
                    ended_at = %s,
                    error_json = COALESCE(error_json, %s)
                WHERE status IN ('queued', 'running')
                """,
                (now, Jsonb(error_json)),
            )
            return cursor.rowcount

    async def mark_record_running(self, execution_id: str, worker_id: Optional[str] = None) -> bool:
        now = datetime.now(timezone.utc)
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE execution_records
                SET status = 'running',
                    dequeued_at = %s,
                    worker_id = %s
                WHERE execution_id = %s AND status = 'queued'
                """,
                (now, worker_id, execution_id),
            )
            return cursor.rowcount > 0

    async def cleanup_old_records(self, ttl_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM execution_records WHERE created_at < %s", (cutoff,)
            )
            return cursor.rowcount

    def _row_to_record(self, row: RowMapping) -> ExecutionRecord:
        resource_usage = None
        if any(
            row.get(column) is not None for column in ("max_rss_mb", "cpu_time_ms", "wall_time_ms")
        ):
            resource_usage = ResourceUsage(
                max_rss_mb=row.get("max_rss_mb"),
                cpu_time_ms=row.get("cpu_time_ms"),
                wall_time_ms=row.get("wall_time_ms"),
            )

        input_artifacts = []
        input_artifacts_data = row.get("input_artifacts_json")
        if input_artifacts_data:
            try:
                input_artifacts = [InputArtifact(**a) for a in input_artifacts_data]
            except (TypeError, ValueError) as e:
                logger.error(
                    "Corrupt stored field input_artifacts_json for execution %s; "
                    "degrading to empty list: %s",
                    row["execution_id"],
                    e,
                )

        artifacts = []
        artifacts_data = row.get("artifacts_json")
        if artifacts_data:
            try:
                artifacts = [Artifact(**a) for a in artifacts_data]
            except (TypeError, ValueError) as e:
                logger.error(
                    "Corrupt stored field artifacts_json for execution %s; "
                    "degrading to empty list: %s",
                    row["execution_id"],
                    e,
                )

        error = None
        error_data = row.get("error_json")
        if error_data:
            try:
                error = ExecutionError(**error_data)
            except (TypeError, ValueError) as e:
                logger.error(
                    "Corrupt stored field error_json for execution %s; "
                    "substituting fallback internal_error: %s",
                    row["execution_id"],
                    e,
                )
                error = ExecutionError(
                    type="internal_error",
                    message="stored error payload could not be decoded",
                )

        result_json = row.get("result_json")

        timing = None
        timing_data = row.get("timing_json")
        if timing_data:
            try:
                timing = ExecutionTiming(**timing_data)
            except (TypeError, ValueError) as e:
                logger.error(
                    "Corrupt stored field timing_json for execution %s; degrading to None: %s",
                    row["execution_id"],
                    e,
                )

        return ExecutionRecord(
            execution_id=row["execution_id"],
            status=row["status"],
            job_type=row["job_type"],
            job_ref=row["job_ref"],
            created_at=row["created_at"],
            queued_at=row["queued_at"],
            dequeued_at=row.get("dequeued_at"),
            started_at=row.get("started_at"),
            ended_at=row.get("ended_at"),
            duration_ms=row.get("duration_ms"),
            attempt=row.get("attempt", 0),
            max_attempts=row.get("max_attempts", 1),
            worker_id=row.get("worker_id"),
            idempotency_key=row.get("idempotency_key"),
            idempotency_fingerprint=row.get("idempotency_fingerprint"),
            code_hash=row["code_hash"],
            input_hash=row["input_hash"],
            params_hash=row.get("params_hash") or "",
            input_artifacts_hash=row.get("input_artifacts_hash") or "",
            input_artifacts=input_artifacts,
            exit_code=row.get("exit_code"),
            stdout=row.get("stdout") or "",
            stderr=row.get("stderr") or "",
            stdout_truncated=bool(row.get("stdout_truncated", False)),
            stderr_truncated=bool(row.get("stderr_truncated", False)),
            result_json=result_json,
            resource_usage=resource_usage,
            timing=timing,
            artifacts=artifacts,
            error=error,
            client_ip=row.get("client_ip"),
            correlation_id=row.get("correlation_id"),
            parent_execution_id=row.get("parent_execution_id"),
            relationship=row.get("relationship"),
            runtime=row.get("runtime"),
            session_id=row.get("session_id"),
        )

    async def save_session(self, session: SessionRecord) -> None:
        metadata_json = Jsonb(session.metadata) if session.metadata is not None else None

        pool = self._get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (
                    session_id, status,
                    container_name, container_id, image_name, runtime, workspace_dir,
                    created_at, started_at, last_activity_at, expires_at, ended_at,
                    idle_timeout_seconds, ttl_seconds,
                    metadata_json
                ) VALUES (
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s
                )
                ON CONFLICT (session_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    container_name = EXCLUDED.container_name,
                    container_id = EXCLUDED.container_id,
                    image_name = EXCLUDED.image_name,
                    runtime = EXCLUDED.runtime,
                    workspace_dir = EXCLUDED.workspace_dir,
                    created_at = sessions.created_at,
                    started_at = EXCLUDED.started_at,
                    last_activity_at = EXCLUDED.last_activity_at,
                    expires_at = EXCLUDED.expires_at,
                    ended_at = EXCLUDED.ended_at,
                    idle_timeout_seconds = EXCLUDED.idle_timeout_seconds,
                    ttl_seconds = EXCLUDED.ttl_seconds,
                    metadata_json = EXCLUDED.metadata_json
                """,
                (
                    session.session_id,
                    session.status,
                    session.container_name,
                    session.container_id,
                    session.image_name,
                    session.runtime,
                    session.workspace_dir,
                    session.created_at,
                    session.started_at,
                    session.last_activity_at,
                    session.expires_at,
                    session.ended_at,
                    session.idle_timeout_seconds,
                    session.ttl_seconds,
                    metadata_json,
                ),
            )

    async def get_session(self, session_id: str) -> Optional[SessionRecord]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = %s", (session_id,)
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_session(cast(RowMapping, row))

    async def list_sessions(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[SessionRecord]:
        query = "SELECT * FROM sessions WHERE 1=1"
        params: List[Any] = []

        if status:
            query += " AND status = %s"
            params.append(status)

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

        return [self._row_to_session(cast(RowMapping, row)) for row in rows]

    async def touch_session(self, session_id: str, touched_at: Optional[datetime] = None) -> bool:
        now = touched_at or datetime.now(timezone.utc)
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE sessions SET last_activity_at = %s WHERE session_id = %s",
                (now, session_id),
            )
            return cursor.rowcount > 0

    async def save_session_event(self, event: SessionEvent) -> SessionEvent:
        payload_json = Jsonb(event.payload) if event.payload is not None else None

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO session_events (
                    session_id, direction, event_type, payload_json, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    event.session_id,
                    event.direction,
                    event.event_type,
                    payload_json,
                    event.created_at,
                ),
            )
            row = await cursor.fetchone()

        return self._row_to_session_event(cast(RowMapping, row))

    async def list_session_events(
        self,
        session_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> List[SessionEvent]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM session_events
                WHERE session_id = %s AND id > %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (session_id, after_id, limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_session_event(cast(RowMapping, row)) for row in rows]

    def _row_to_session(self, row: RowMapping) -> SessionRecord:
        metadata = row.get("metadata_json") or {}
        return SessionRecord(
            session_id=row["session_id"],
            status=row["status"],
            container_name=row["container_name"],
            container_id=row.get("container_id"),
            image_name=row.get("image_name"),
            runtime=row.get("runtime"),
            workspace_dir=row["workspace_dir"],
            created_at=row["created_at"],
            started_at=row.get("started_at"),
            last_activity_at=row["last_activity_at"],
            expires_at=row.get("expires_at"),
            ended_at=row.get("ended_at"),
            idle_timeout_seconds=row["idle_timeout_seconds"],
            ttl_seconds=row["ttl_seconds"],
            metadata=metadata,
        )

    def _row_to_session_event(self, row: RowMapping) -> SessionEvent:
        return SessionEvent(
            id=row["id"],
            session_id=row["session_id"],
            direction=row["direction"],
            event_type=row["event_type"],
            payload=row.get("payload_json"),
            created_at=row["created_at"],
        )

    async def save_version(self, version: JobVersion) -> None:
        pool = self._get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                INSERT INTO job_versions (
                    {_VERSION_INSERT_COLUMN_SQL}
                ) VALUES (
                    {_VERSION_INSERT_PLACEHOLDER_SQL}
                )
                ON CONFLICT ({_VERSION_KEY_COLUMN}) DO UPDATE SET
                    {_VERSION_UPDATE_SET_SQL}
                """,
                tuple(extract(version) for _, extract in _VERSION_COLUMNS),
            )

    async def get_version_by_digest(self, job_type_name: str, digest: str) -> Optional[JobVersion]:
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError(
                f"Invalid digest {digest!r}: must be {MIN_DIGEST_PREFIX_LEN}-64 "
                "lowercase hex characters"
            )

        pool = self._get_pool()
        async with pool.connection() as conn:
            if len(digest) < 64:
                cursor = await conn.execute(
                    """
                    SELECT * FROM job_versions
                    WHERE job_type_name = %s AND digest LIKE %s
                    LIMIT 2
                    """,
                    (job_type_name, digest + "%"),
                )
                rows = await cursor.fetchall()
                if len(rows) > 1:
                    raise ValueError(
                        f"Ambiguous digest prefix {digest!r} for job type "
                        f"{job_type_name!r}: matches multiple versions; "
                        "provide a longer prefix or the full digest"
                    )
                row = rows[0] if rows else None
            else:
                cursor = await conn.execute(
                    "SELECT * FROM job_versions WHERE job_type_name = %s AND digest = %s",
                    (job_type_name, digest),
                )
                row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_version(cast(RowMapping, row))

    async def get_version_by_tag(
        self, job_type_name: str, version_tag: str
    ) -> Optional[JobVersion]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM job_versions WHERE job_type_name = %s AND version_tag = %s",
                (job_type_name, version_tag),
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_version(cast(RowMapping, row))

    async def get_latest_version(self, job_type_name: str) -> Optional[JobVersion]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM job_versions
                WHERE job_type_name = %s
                ORDER BY built_at DESC
                LIMIT 1
                """,
                (job_type_name,),
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_version(cast(RowMapping, row))

    async def list_versions(self, job_type_name: str) -> List[JobVersion]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM job_versions
                WHERE job_type_name = %s
                ORDER BY built_at DESC
                """,
                (job_type_name,),
            )
            rows = await cursor.fetchall()

        return [self._row_to_version(cast(RowMapping, row)) for row in rows]

    def _row_to_version(self, row: RowMapping) -> JobVersion:
        return JobVersion(
            digest=row["digest"],
            job_type_name=row["job_type_name"],
            version_tag=row.get("version_tag"),
            built_at=row["built_at"],
            built_by=row.get("built_by"),
            dockerfile_hash=row["dockerfile_hash"],
            requirements_hash=row["requirements_hash"],
            image_ref=row["image_ref"],
        )

    async def add_to_dlq(self, entry: DeadLetterEntry) -> int:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO dead_letter_queue (
                    job_id, job_data_json, error_type, error_message,
                    retry_count, created_at, client_ip, correlation_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    entry.job_id,
                    Jsonb(entry.job_data),
                    entry.error_type,
                    entry.error_message,
                    entry.retry_count,
                    entry.created_at,
                    entry.client_ip,
                    entry.correlation_id,
                ),
            )
            row = await cursor.fetchone()
            row_map = cast(RowMapping, row) if row else None
            return int(row_map["id"]) if row_map else 0

    async def get_dlq_entry(self, entry_id: int) -> Optional[DeadLetterEntry]:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM dead_letter_queue WHERE id = %s", (entry_id,)
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_dlq_entry(cast(RowMapping, row))

    async def list_dlq_entries(
        self, error_type: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[DeadLetterEntry]:
        query = "SELECT * FROM dead_letter_queue WHERE 1=1"
        params: List[Any] = []

        if error_type:
            query += " AND error_type = %s"
            params.append(error_type)

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

        return [self._row_to_dlq_entry(cast(RowMapping, row)) for row in rows]

    async def remove_from_dlq(self, entry_id: int) -> bool:
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM dead_letter_queue WHERE id = %s RETURNING id", (entry_id,)
            )
            row = await cursor.fetchone()
            return row is not None

    async def get_dlq_stats(self) -> dict:
        pool = self._get_pool()
        async with pool.connection() as conn:
            total_cursor = await conn.execute("SELECT COUNT(*) AS count FROM dead_letter_queue")
            total_row = await total_cursor.fetchone()
            total_row_map = cast(RowMapping, total_row) if total_row else None
            total = int(total_row_map["count"]) if total_row_map else 0

            by_error_cursor = await conn.execute(
                """
                SELECT error_type, COUNT(*) AS count
                FROM dead_letter_queue
                GROUP BY error_type
                ORDER BY count DESC
                """
            )
            by_error_rows = await by_error_cursor.fetchall()

        return {
            "total": total,
            "by_error_type": {
                cast(str, cast(RowMapping, row)["error_type"]): int(cast(RowMapping, row)["count"])
                for row in by_error_rows
            },
        }

    async def cleanup_old_dlq_entries(self, ttl_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM dead_letter_queue WHERE created_at < %s", (cutoff,)
            )
            return cursor.rowcount

    def _row_to_dlq_entry(self, row: RowMapping) -> DeadLetterEntry:
        job_data = row.get("job_data_json") or {}
        return DeadLetterEntry(
            id=row["id"],
            job_id=row["job_id"],
            job_data=job_data,
            error_type=row["error_type"],
            error_message=row.get("error_message"),
            retry_count=row.get("retry_count", 0),
            created_at=row["created_at"],
            client_ip=row.get("client_ip"),
            correlation_id=row.get("correlation_id"),
        )
