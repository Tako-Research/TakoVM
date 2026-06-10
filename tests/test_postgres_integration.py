"""Integration tests for PostgreSQL-backed storage behavior."""

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg
import pytest

import tako_vm.storage as storage_module
from tako_vm.models import ExecutionRecord, sha256_content, sha256_json
from tako_vm.storage import ExecutionStorage

# Never skip in CI: temp_data_dir provides the database URL there and fails
# loudly if postgres is unreachable, so DB coverage cannot silently vanish.
pytestmark = pytest.mark.skipif(
    "TAKO_VM_DATABASE_URL" not in os.environ
    and not (os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")),
    reason="Set TAKO_VM_DATABASE_URL to run PostgreSQL integration tests",
)


def _make_record(execution_id: str, idempotency_key: Optional[str] = None) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=execution_id,
        status="queued",
        job_type="default",
        job_ref="default@latest",
        created_at=datetime.now(timezone.utc),
        queued_at=datetime.now(timezone.utc),
        code_hash=sha256_content("print('hi')"),
        input_hash=sha256_json({"x": 1}),
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_schema_migrations_written(temp_data_dir):
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()
    await storage.close()

    with psycopg.connect(os.environ["TAKO_VM_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [row[0] for row in cur.fetchall()]

    assert "0001_initial" in versions


@pytest.mark.asyncio
async def test_idempotency_unique_index_enforced(temp_data_dir):
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()

    await storage.save_record(_make_record("job-1", idempotency_key="same-key"))

    with pytest.raises(psycopg.IntegrityError):
        await storage.save_record(_make_record("job-2", idempotency_key="same-key"))

    await storage.close()


@pytest.mark.asyncio
async def test_jsonb_roundtrip_for_result_and_artifacts(temp_data_dir):
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()

    record = _make_record("job-json")
    record.result_json = {"ok": True, "items": [1, 2, 3]}
    await storage.save_record(record)

    loaded = await storage.get_record("job-json")
    await storage.close()

    assert loaded is not None
    assert loaded.result_json == {"ok": True, "items": [1, 2, 3]}


@pytest.mark.asyncio
async def test_concurrent_idempotency_conflict_allows_single_write(temp_data_dir):
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    await storage.init()

    async def write_record(execution_id: str):
        try:
            await storage.save_record(_make_record(execution_id, idempotency_key="race-key"))
            return "ok"
        except psycopg.IntegrityError:
            return "integrity_error"

    results = await asyncio.gather(write_record("race-1"), write_record("race-2"))
    await storage.close()

    assert sorted(results) == ["integrity_error", "ok"]

    with psycopg.connect(os.environ["TAKO_VM_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM execution_records WHERE idempotency_key = %s", ("race-key",)
            )
            row = cur.fetchone()
            count = row[0] if row else 0
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_storage_init_is_safe(temp_data_dir):
    storages = [ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"]) for _ in range(4)]
    try:
        results = await asyncio.gather(
            *(storage.init() for storage in storages), return_exceptions=True
        )
        failures = [r for r in results if isinstance(r, Exception)]
        assert failures == []
    finally:
        await asyncio.gather(*(storage.close() for storage in storages), return_exceptions=True)


@pytest.mark.asyncio
async def test_init_clears_pool_when_migrations_fail(temp_data_dir):
    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    original_migrations = list(storage_module.MIGRATIONS)
    try:
        storage_module.MIGRATIONS = [("0000_broken", "SELECT FROM")]

        with pytest.raises(Exception):
            await storage.init()

        storage_module.MIGRATIONS = original_migrations
        await storage.init()
        assert await storage.list_records(limit=1, offset=0) == []
    finally:
        storage_module.MIGRATIONS = original_migrations
        await storage.close()
