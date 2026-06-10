"""
PostgreSQL storage for Tako VM execution records.

Provides async CRUD operations for ExecutionRecords and JobVersions.
"""

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
)

logger = logging.getLogger(__name__)

MIGRATION_LOCK_ID = 94857231
RowMapping = Mapping[str, Any]

# Statuses from which an execution record must never transition away. Once a
# record is terminal, later writes (e.g. a stale executor result racing a
# cancellation, or vice versa) are refused by the save_record upsert guard.
TERMINAL_STATUSES = ("succeeded", "failed", "timeout", "oom", "cancelled")

# Backoff schedule (seconds) for retrying save_record on transient connection
# errors. Tests may monkeypatch this to avoid real sleeps.
_SAVE_RECORD_RETRY_DELAYS = (0.2, 0.8, 2.0)

# Minimum digest prefix length accepted by get_version_by_digest. Shorter
# prefixes are too ambiguous to resolve safely (and an empty prefix would
# match every stored version).
MIN_DIGEST_PREFIX_LEN = 12

# Full digests and digest prefixes must be lowercase hex (sha256 hexdigest).
# This also guarantees the value cannot contain SQL LIKE wildcards ('%'/'_').
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
]


def _decode_json_field(value: Any) -> Any:
    """Normalize JSONB values returned by psycopg."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return value


class ExecutionStorage:
    """PostgreSQL storage for execution records."""

    def __init__(self, database_url: str, min_pool_size: int = 1, max_pool_size: int = 10):
        self.database_url = database_url
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self._pool: Optional[AsyncConnectionPool] = None

    async def init(self) -> None:
        """Initialize connection pool and run schema migrations."""
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
        """Close connection pool."""
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
        """
        Insert or update execution record.

        Durability behavior:
        - Transient connection errors (psycopg.OperationalError, which includes
          psycopg_pool.PoolTimeout) are retried with backoff so a brief DB blip
          cannot lose the record of code that already ran. Non-transient errors
          (e.g. IntegrityError) are raised immediately.
        - The upsert refuses to modify a record that is already in a terminal
          status (see TERMINAL_STATUSES); such writes are logged and dropped.
        """
        attempts = len(_SAVE_RECORD_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                rowcount = await self._save_record_once(record)
                break
            except psycopg.OperationalError as e:
                # Connection-level failure (includes psycopg_pool.PoolTimeout,
                # which subclasses OperationalError). Retry with backoff;
                # IntegrityError and friends derive from DatabaseError instead
                # and propagate immediately.
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
            # The ON CONFLICT ... WHERE guard skipped the update: the existing
            # row is already terminal and must not be regressed (e.g. a
            # cancel/complete race).
            logger.warning(
                "Refused to overwrite terminal execution record %s with status %s",
                record.execution_id,
                record.status,
            )

    async def _save_record_once(self, record: ExecutionRecord) -> int:
        """Execute the upsert once; return affected rowcount (0 = guard skip)."""
        resource_usage = record.resource_usage
        artifacts_json = [a.model_dump() for a in record.artifacts]
        input_artifacts_json = [a.model_dump() for a in record.input_artifacts]
        error_json = record.error.model_dump() if record.error else None
        result_json = record.result_json
        timing_json = record.timing.model_dump() if record.timing else None

        terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)

        # Upsert column policy:
        # - Submission-identity fields (created_at, queued_at, code_hash,
        #   input_hash, params_hash, input_artifacts_hash, client_ip,
        #   correlation_id, idempotency_key, idempotency_fingerprint,
        #   parent_execution_id, relationship) describe WHEN/HOW the job was
        #   submitted. They are
        #   written by queue.submit() and must survive later writes from the
        #   executor, which rebuilds its record at execution start and does not
        #   know the true submission timestamps. We keep the existing row's
        #   value (COALESCE(existing, new) for nullables; plain existing for
        #   NOT NULL created_at/queued_at, where COALESCE degenerates to the
        #   existing value anyway).
        # - dequeued_at is set once by mark_record_running(); COALESCE keeps it.
        # - worker_id is also set by mark_record_running(); prefer the new
        #   value when the writer knows it, otherwise keep the existing one.
        # - Execution-outcome fields (status, started_at, ended_at,
        #   duration_ms, attempt, exit_code, stdout/stderr, result/timing/
        #   artifacts/error JSON, resource usage) take EXCLUDED: later writes
        #   know more about the outcome. input_artifacts_json also takes
        #   EXCLUDED because the executor enriches it with replay artifacts
        #   that the preliminary queued record does not have.
        # - The WHERE guard makes the whole update a no-op when the existing
        #   row is already terminal, so terminal status can never regress.
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                INSERT INTO execution_records (
                    execution_id, status, job_type, job_ref,
                    created_at, queued_at, dequeued_at, started_at, ended_at, duration_ms,
                    attempt, max_attempts, worker_id, idempotency_key, idempotency_fingerprint,
                    code_hash, input_hash, params_hash, input_artifacts_hash,
                    input_artifacts_json,
                    exit_code, stdout, stderr, stdout_truncated, stderr_truncated, result_json,
                    max_rss_mb, cpu_time_ms, wall_time_ms,
                    timing_json,
                    artifacts_json, error_json,
                    client_ip, correlation_id, parent_execution_id, relationship
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (execution_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    job_type = EXCLUDED.job_type,
                    job_ref = EXCLUDED.job_ref,
                    created_at = execution_records.created_at,
                    queued_at = COALESCE(execution_records.queued_at, EXCLUDED.queued_at),
                    dequeued_at = COALESCE(execution_records.dequeued_at, EXCLUDED.dequeued_at),
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    duration_ms = EXCLUDED.duration_ms,
                    attempt = EXCLUDED.attempt,
                    max_attempts = EXCLUDED.max_attempts,
                    worker_id = COALESCE(EXCLUDED.worker_id, execution_records.worker_id),
                    idempotency_key =
                        COALESCE(execution_records.idempotency_key, EXCLUDED.idempotency_key),
                    idempotency_fingerprint = COALESCE(
                        execution_records.idempotency_fingerprint,
                        EXCLUDED.idempotency_fingerprint
                    ),
                    code_hash = COALESCE(execution_records.code_hash, EXCLUDED.code_hash),
                    input_hash = COALESCE(execution_records.input_hash, EXCLUDED.input_hash),
                    params_hash = COALESCE(execution_records.params_hash, EXCLUDED.params_hash),
                    input_artifacts_hash = COALESCE(
                        execution_records.input_artifacts_hash,
                        EXCLUDED.input_artifacts_hash
                    ),
                    input_artifacts_json = EXCLUDED.input_artifacts_json,
                    exit_code = EXCLUDED.exit_code,
                    stdout = EXCLUDED.stdout,
                    stderr = EXCLUDED.stderr,
                    stdout_truncated = EXCLUDED.stdout_truncated,
                    stderr_truncated = EXCLUDED.stderr_truncated,
                    result_json = EXCLUDED.result_json,
                    max_rss_mb = EXCLUDED.max_rss_mb,
                    cpu_time_ms = EXCLUDED.cpu_time_ms,
                    wall_time_ms = EXCLUDED.wall_time_ms,
                    timing_json = EXCLUDED.timing_json,
                    artifacts_json = EXCLUDED.artifacts_json,
                    error_json = EXCLUDED.error_json,
                    client_ip = COALESCE(execution_records.client_ip, EXCLUDED.client_ip),
                    correlation_id = COALESCE(
                        execution_records.correlation_id,
                        EXCLUDED.correlation_id
                    ),
                    parent_execution_id = COALESCE(
                        execution_records.parent_execution_id,
                        EXCLUDED.parent_execution_id
                    ),
                    relationship =
                        COALESCE(execution_records.relationship, EXCLUDED.relationship)
                WHERE execution_records.status NOT IN ({terminal_list})
                """,
                (
                    record.execution_id,
                    record.status,
                    record.job_type,
                    record.job_ref,
                    record.created_at,
                    record.queued_at,
                    record.dequeued_at,
                    record.started_at,
                    record.ended_at,
                    record.duration_ms,
                    record.attempt,
                    record.max_attempts,
                    record.worker_id,
                    record.idempotency_key,
                    record.idempotency_fingerprint,
                    record.code_hash,
                    record.input_hash,
                    record.params_hash,
                    record.input_artifacts_hash,
                    Jsonb(input_artifacts_json),
                    record.exit_code,
                    record.stdout,
                    record.stderr,
                    record.stdout_truncated,
                    record.stderr_truncated,
                    Jsonb(result_json) if result_json is not None else None,
                    resource_usage.max_rss_mb if resource_usage else None,
                    resource_usage.cpu_time_ms if resource_usage else None,
                    resource_usage.wall_time_ms if resource_usage else None,
                    Jsonb(timing_json) if timing_json is not None else None,
                    Jsonb(artifacts_json),
                    Jsonb(error_json) if error_json is not None else None,
                    record.client_ip,
                    record.correlation_id,
                    record.parent_execution_id,
                    record.relationship,
                ),
            )
            return cursor.rowcount

    async def get_record(self, execution_id: str) -> Optional[ExecutionRecord]:
        """Retrieve execution record by ID."""
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
        """Retrieve execution record by idempotency key."""
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
        """List execution records with optional filters."""
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
        """
        Mark stale 'queued'/'running' records as failed after a restart.

        The job queue is in-memory (asyncio.Queue), so it does not survive a
        process restart: any record still marked 'queued' or 'running' when the
        server starts belongs to a job that was lost in a crash or shutdown and
        will never complete. Without this reconciliation, clients poll those
        records forever.

        NOTE: This assumes a single-server deployment (the only supported
        topology). There is no multi-server coordination anywhere in Tako VM,
        so no other live server process can legitimately own in-flight records
        when this runs at startup.

        Returns:
            Number of records transitioned to 'failed'.
        """
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
                    error_json = %s
                WHERE status IN ('queued', 'running')
                """,
                (now, Jsonb(error_json)),
            )
            return cursor.rowcount

    async def mark_record_running(self, execution_id: str, worker_id: Optional[str] = None) -> bool:
        """
        Persist the queued -> running transition for an execution record.

        Targeted UPDATE so the in-flight state is visible in the database
        during execution (and reconcilable after a crash) without rewriting
        the whole row.

        Returns:
            True if a queued record was transitioned to running.
        """
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
        """Delete records older than TTL."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM execution_records WHERE created_at < %s", (cutoff,)
            )
            return cursor.rowcount

    def _row_to_record(self, row: RowMapping) -> ExecutionRecord:
        """Convert database row to ExecutionRecord."""
        # All ResourceUsage fields are independently nullable, so rebuild the
        # model if ANY metric column was stored — gating on wall_time_ms alone
        # would silently drop records saved with only max_rss_mb/cpu_time_ms.
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
        input_artifacts_data = _decode_json_field(row.get("input_artifacts_json"))
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
        artifacts_data = _decode_json_field(row.get("artifacts_json"))
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
        error_data = _decode_json_field(row.get("error_json"))
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
                # Surface the corruption loudly rather than presenting the
                # record as error-free: a stored error payload existed but no
                # longer matches the ExecutionError model.
                error = ExecutionError(
                    type="internal_error",
                    message="stored error payload could not be decoded",
                )

        result_json = _decode_json_field(row.get("result_json"))

        timing = None
        timing_data = _decode_json_field(row.get("timing_json"))
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
        )

    async def save_version(self, version: JobVersion) -> None:
        """Save job version record."""
        pool = self._get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO job_versions (
                    digest, job_type_name, version_tag,
                    built_at, built_by, dockerfile_hash,
                    requirements_hash, image_ref
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (digest) DO UPDATE SET
                    job_type_name = EXCLUDED.job_type_name,
                    version_tag = EXCLUDED.version_tag,
                    built_at = EXCLUDED.built_at,
                    built_by = EXCLUDED.built_by,
                    dockerfile_hash = EXCLUDED.dockerfile_hash,
                    requirements_hash = EXCLUDED.requirements_hash,
                    image_ref = EXCLUDED.image_ref
                """,
                (
                    version.digest,
                    version.job_type_name,
                    version.version_tag,
                    version.built_at,
                    version.built_by,
                    version.dockerfile_hash,
                    version.requirements_hash,
                    version.image_ref,
                ),
            )

    async def get_version_by_digest(self, job_type_name: str, digest: str) -> Optional[JobVersion]:
        """
        Get version by full digest or an unambiguous digest prefix.

        Args:
            job_type_name: Job type name.
            digest: Full 64-character hex digest, or a prefix of at least
                MIN_DIGEST_PREFIX_LEN hex characters.

        Returns:
            Matching JobVersion, or None if no version matches.

        Raises:
            ValueError: If the digest is empty, shorter than
                MIN_DIGEST_PREFIX_LEN, not lowercase hex (which also rejects
                SQL LIKE wildcards), or if a prefix matches more than one
                stored version.
        """
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError(
                f"Invalid digest {digest!r}: must be {MIN_DIGEST_PREFIX_LEN}-64 "
                "lowercase hex characters"
            )

        pool = self._get_pool()
        async with pool.connection() as conn:
            if len(digest) < 64:
                # The digest is validated as hex above, so it cannot contain
                # LIKE wildcards ('%'/'_'); the prefix match is literal.
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
        """Get version by tag."""
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
        """Get most recent version for job type."""
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
        """List all versions for job type."""
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
        """Convert database row to JobVersion."""
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
        """Add a failed job to the dead letter queue.

        ``entry.job_data`` is persisted verbatim as JSONB, so callers must
        pass a redacted forensic summary (DeadLetterEntry.build_job_summary),
        never raw code/input_data/idempotency keys.
        """
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
        """Get a DLQ entry by ID."""
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
        """List dead letter queue entries."""
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
        """Remove an entry from the dead letter queue."""
        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM dead_letter_queue WHERE id = %s RETURNING id", (entry_id,)
            )
            row = await cursor.fetchone()
            return row is not None

    async def get_dlq_stats(self) -> dict:
        """Get statistics about the dead letter queue."""
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
        """Delete DLQ entries older than TTL."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

        pool = self._get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM dead_letter_queue WHERE created_at < %s", (cutoff,)
            )
            return cursor.rowcount

    def _row_to_dlq_entry(self, row: RowMapping) -> DeadLetterEntry:
        """Convert database row to DeadLetterEntry."""
        job_data = _decode_json_field(row.get("job_data_json")) or {}
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
