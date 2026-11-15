import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from tako_vm.models import (
    Artifact,
    DeadLetterEntry,
    ExecutionError,
    ExecutionRecord,
    ExecutionTiming,
    InputArtifact,
    JobVersion,
    ResourceUsage,
)
from tako_vm.storage import ExecutionStorage


@pytest.fixture
def storage(test_storage):
    return test_storage


class TestExecutionStorageInit:
    def test_init_creates_database(self, storage):
        records = storage.list_records(limit=1, offset=0)
        assert records == []

    def test_init_idempotent(self, storage):
        db_url = os.environ.get(
            "TAKO_VM_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/tako_vm_test"
        )
        store = ExecutionStorage(db_url)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(store.init())
        loop.run_until_complete(store.init())
        loop.run_until_complete(store.close())
        loop.close()


class TestExecutionRecordCRUD:
    def test_save_and_get_record(self, storage):
        record = ExecutionRecord(
            execution_id="test-123",
            status="succeeded",
            job_type="default",
            code_hash="a" * 64,
            input_hash="b" * 64,
            exit_code=0,
            stdout="Hello, World!",
            stderr="",
        )

        storage.save_record(record)
        retrieved = storage.get_record("test-123")

        assert retrieved is not None
        assert retrieved.execution_id == "test-123"
        assert retrieved.status == "succeeded"
        assert retrieved.stdout == "Hello, World!"
        assert retrieved.exit_code == 0

    def test_get_nonexistent_record(self, storage):
        result = storage.get_record("nonexistent")
        assert result is None

    def test_save_record_with_all_fields(self, storage):
        now = datetime.now(timezone.utc)
        record = ExecutionRecord(
            execution_id="full-record",
            status="failed",
            job_type="custom-job",
            job_ref="custom-job@sha256:abc123",
            created_at=now,
            queued_at=now,
            dequeued_at=now + timedelta(seconds=1),
            started_at=now + timedelta(seconds=2),
            ended_at=now + timedelta(seconds=5),
            duration_ms=3000,
            attempt=1,
            max_attempts=3,
            worker_id="worker-1",
            idempotency_key="idem-key-123",
            idempotency_fingerprint="c" * 64,
            code_hash="d" * 64,
            input_hash="e" * 64,
            params_hash="f" * 64,
            input_artifacts_hash="",
            input_artifacts=[
                InputArtifact(
                    name="input.svg",
                    size_bytes=1024,
                    sha256="a" * 64,
                    content_type="image/svg+xml",
                    storage_key="runs/full-record/inputs/input.svg",
                )
            ],
            exit_code=1,
            stdout="output",
            stderr="error",
            stdout_truncated=False,
            stderr_truncated=True,
            result_json={"key": "value"},
            resource_usage=ResourceUsage(max_rss_mb=128.5, cpu_time_ms=500, wall_time_ms=3000),
            timing=ExecutionTiming(
                startup_ms=1000,
                dep_install_ms=500,
                execution_ms=2000,
                total_ms=3000,
                phase_at_exit="failed",
            ),
            artifacts=[
                Artifact(
                    name="output.png",
                    size_bytes=2048,
                    sha256="b" * 64,
                    content_type="image/png",
                    storage_key="runs/full-record/artifacts/output.png",
                )
            ],
            error=ExecutionError(type="runtime_error", message="Something went wrong"),
            client_ip="192.168.1.1",
            parent_execution_id="parent-123",
            relationship="rerun",
            runtime="runsc",
        )

        storage.save_record(record)
        retrieved = storage.get_record("full-record")

        assert retrieved is not None
        assert retrieved.job_ref == "custom-job@sha256:abc123"
        assert retrieved.runtime == "runsc"
        assert retrieved.duration_ms == 3000
        assert retrieved.worker_id == "worker-1"
        assert retrieved.resource_usage is not None
        assert retrieved.resource_usage.max_rss_mb == 128.5
        assert retrieved.timing is not None
        assert retrieved.timing.startup_ms == 1000
        assert len(retrieved.artifacts) == 1
        assert retrieved.artifacts[0].name == "output.png"
        assert retrieved.error is not None
        assert retrieved.error.type == "runtime_error"
        assert retrieved.parent_execution_id == "parent-123"
        assert retrieved.relationship == "rerun"

    def test_update_record(self, storage):
        record = ExecutionRecord(
            execution_id="update-test",
            status="queued",
            code_hash="a" * 64,
            input_hash="b" * 64,
        )
        storage.save_record(record)

        record.status = "running"
        storage.save_record(record)

        retrieved = storage.get_record("update-test")
        assert retrieved.status == "running"

    def test_list_records_empty(self, storage):
        records = storage.list_records()
        assert records == []

    def test_list_records_pagination(self, storage):
        for i in range(10):
            storage.save_record(
                ExecutionRecord(
                    execution_id=f"record-{i:02d}",
                    status="succeeded",
                    code_hash="a" * 64,
                    input_hash="b" * 64,
                )
            )

        page1 = storage.list_records(limit=3, offset=0)
        assert len(page1) == 3

        page2 = storage.list_records(limit=3, offset=3)
        assert len(page2) == 3

        ids1 = {r.execution_id for r in page1}
        ids2 = {r.execution_id for r in page2}
        assert ids1.isdisjoint(ids2)

    def test_list_records_filter_by_status(self, storage):
        storage.save_record(
            ExecutionRecord(
                execution_id="rec-1", status="succeeded", code_hash="a" * 64, input_hash="b" * 64
            )
        )
        storage.save_record(
            ExecutionRecord(
                execution_id="rec-2", status="failed", code_hash="a" * 64, input_hash="b" * 64
            )
        )
        storage.save_record(
            ExecutionRecord(
                execution_id="rec-3", status="succeeded", code_hash="a" * 64, input_hash="b" * 64
            )
        )

        succeeded = storage.list_records(status="succeeded")
        assert len(succeeded) == 2
        assert all(r.status == "succeeded" for r in succeeded)

    def test_list_records_filter_by_job_type(self, storage):
        storage.save_record(
            ExecutionRecord(
                execution_id="rec-1",
                job_type="type-a",
                status="succeeded",
                code_hash="a" * 64,
                input_hash="b" * 64,
            )
        )
        storage.save_record(
            ExecutionRecord(
                execution_id="rec-2",
                job_type="type-b",
                status="succeeded",
                code_hash="a" * 64,
                input_hash="b" * 64,
            )
        )

        type_a = storage.list_records(job_type="type-a")
        assert len(type_a) == 1
        assert type_a[0].job_type == "type-a"

    def test_get_by_idempotency_key(self, storage):
        storage.save_record(
            ExecutionRecord(
                execution_id="idem-test",
                status="succeeded",
                idempotency_key="my-unique-key",
                code_hash="a" * 64,
                input_hash="b" * 64,
            )
        )

        retrieved = storage.get_by_idempotency_key("my-unique-key")
        assert retrieved is not None
        assert retrieved.execution_id == "idem-test"

    def test_get_by_idempotency_key_not_found(self, storage):
        result = storage.get_by_idempotency_key("nonexistent")
        assert result is None


class TestCleanup:
    def test_cleanup_old_records(self, storage):
        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        recent_time = datetime.now(timezone.utc)

        storage.save_record(
            ExecutionRecord(
                execution_id="old-record",
                status="succeeded",
                created_at=old_time,
                code_hash="a" * 64,
                input_hash="b" * 64,
            )
        )
        storage.save_record(
            ExecutionRecord(
                execution_id="recent-record",
                status="succeeded",
                created_at=recent_time,
                code_hash="a" * 64,
                input_hash="b" * 64,
            )
        )

        deleted = storage.cleanup_old_records(ttl_days=5)

        assert deleted == 1
        assert storage.get_record("old-record") is None
        assert storage.get_record("recent-record") is not None


class TestJobVersions:
    def test_save_and_get_version(self, storage):
        version = JobVersion(
            digest="a" * 64,
            job_type_name="test-job",
            version_tag="v1.0.0",
            built_at=datetime.now(timezone.utc),
            built_by="test",
            dockerfile_hash="b" * 64,
            requirements_hash="c" * 64,
            image_ref="test-job:v1.0.0",
        )

        storage.save_version(version)
        retrieved = storage.get_version_by_digest("test-job", "a" * 64)

        assert retrieved is not None
        assert retrieved.job_type_name == "test-job"
        assert retrieved.version_tag == "v1.0.0"

    def test_get_version_by_short_digest(self, storage):
        version = JobVersion(
            digest="abcdef1234567890" + "0" * 48,
            job_type_name="test-job",
            version_tag="v1.0.0",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v1",
        )
        storage.save_version(version)

        retrieved = storage.get_version_by_digest("test-job", "abcdef123456")
        assert retrieved is not None
        assert retrieved.version_tag == "v1.0.0"

    def test_get_version_by_tag(self, storage):
        version = JobVersion(
            digest="a" * 64,
            job_type_name="test-job",
            version_tag="v2.0.0",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v2",
        )
        storage.save_version(version)

        retrieved = storage.get_version_by_tag("test-job", "v2.0.0")
        assert retrieved is not None
        assert retrieved.digest == "a" * 64

    def test_get_latest_version(self, storage):
        old_time = datetime.now(timezone.utc) - timedelta(days=1)
        new_time = datetime.now(timezone.utc)

        storage.save_version(
            JobVersion(
                digest="a" * 64,
                job_type_name="test-job",
                version_tag="v1.0.0",
                built_at=old_time,
                dockerfile_hash="",
                requirements_hash="",
                image_ref="test:v1",
            )
        )
        storage.save_version(
            JobVersion(
                digest="b" * 64,
                job_type_name="test-job",
                version_tag="v2.0.0",
                built_at=new_time,
                dockerfile_hash="",
                requirements_hash="",
                image_ref="test:v2",
            )
        )

        latest = storage.get_latest_version("test-job")
        assert latest is not None
        assert latest.version_tag == "v2.0.0"

    def test_list_versions(self, storage):
        for i in range(3):
            storage.save_version(
                JobVersion(
                    digest=f"{i}" * 64,
                    job_type_name="test-job",
                    version_tag=f"v{i}.0.0",
                    dockerfile_hash="",
                    requirements_hash="",
                    image_ref=f"test:v{i}",
                )
            )

        versions = storage.list_versions("test-job")
        assert len(versions) == 3


class TestDeadLetterQueue:
    def test_add_and_get_dlq_entry(self, storage):
        entry = DeadLetterEntry(
            job_id="failed-job-123",
            job_data={"code": "print('fail')", "input_data": {}},
            error_type="runtime_error",
            error_message="Something went wrong",
            retry_count=2,
            client_ip="10.0.0.1",
            correlation_id="corr-123",
        )

        entry_id = storage.add_to_dlq(entry)
        assert entry_id > 0

        retrieved = storage.get_dlq_entry(entry_id)
        assert retrieved is not None
        assert retrieved.job_id == "failed-job-123"
        assert retrieved.job_data["code"] == "print('fail')"
        assert retrieved.error_type == "runtime_error"
        assert retrieved.retry_count == 2

    def test_list_dlq_entries(self, storage):
        for i in range(5):
            storage.add_to_dlq(
                DeadLetterEntry(
                    job_id=f"job-{i}",
                    job_data={},
                    error_type="timeout" if i % 2 == 0 else "oom",
                    error_message="Error",
                )
            )

        all_entries = storage.list_dlq_entries()
        assert len(all_entries) == 5

        page = storage.list_dlq_entries(limit=2, offset=0)
        assert len(page) == 2

    def test_list_dlq_filter_by_error_type(self, storage):
        storage.add_to_dlq(
            DeadLetterEntry(job_id="job-1", job_data={}, error_type="timeout", error_message="Err")
        )
        storage.add_to_dlq(
            DeadLetterEntry(job_id="job-2", job_data={}, error_type="oom", error_message="Err")
        )
        storage.add_to_dlq(
            DeadLetterEntry(job_id="job-3", job_data={}, error_type="timeout", error_message="Err")
        )

        timeout_entries = storage.list_dlq_entries(error_type="timeout")
        assert len(timeout_entries) == 2
        assert all(e.error_type == "timeout" for e in timeout_entries)

    def test_remove_from_dlq(self, storage):
        entry_id = storage.add_to_dlq(
            DeadLetterEntry(job_id="job-1", job_data={}, error_type="timeout", error_message="Err")
        )

        removed = storage.remove_from_dlq(entry_id)
        assert removed is True

        assert storage.get_dlq_entry(entry_id) is None

    def test_remove_nonexistent_dlq_entry(self, storage):
        removed = storage.remove_from_dlq(99999)
        assert removed is False

    def test_dlq_stats(self, storage):
        storage.add_to_dlq(
            DeadLetterEntry(job_id="job-1", job_data={}, error_type="timeout", error_message="Err")
        )
        storage.add_to_dlq(
            DeadLetterEntry(job_id="job-2", job_data={}, error_type="timeout", error_message="Err")
        )
        storage.add_to_dlq(
            DeadLetterEntry(job_id="job-3", job_data={}, error_type="oom", error_message="Err")
        )

        stats = storage.get_dlq_stats()
        assert stats["total"] == 3
        assert stats["by_error_type"]["timeout"] == 2
        assert stats["by_error_type"]["oom"] == 1

    def test_cleanup_old_dlq_entries(self, storage):
        old_entry = DeadLetterEntry(
            job_id="old-job",
            job_data={},
            error_type="timeout",
            error_message="Err",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        recent_entry = DeadLetterEntry(
            job_id="recent-job",
            job_data={},
            error_type="timeout",
            error_message="Err",
            created_at=datetime.now(timezone.utc),
        )

        storage.add_to_dlq(old_entry)
        storage.add_to_dlq(recent_entry)

        deleted = storage.cleanup_old_dlq_entries(ttl_days=5)
        assert deleted == 1

        remaining = storage.list_dlq_entries()
        assert len(remaining) == 1
        assert remaining[0].job_id == "recent-job"


class TestReconcileStaleRecords:
    @staticmethod
    def _make_record(execution_id: str, status: str, **kwargs) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=execution_id,
            status=status,
            code_hash="a" * 64,
            input_hash="b" * 64,
            **kwargs,
        )

    def test_reconcile_marks_queued_and_running_as_failed(self, storage):
        storage.save_record(self._make_record("stale-queued", "queued"))
        storage.save_record(self._make_record("stale-running", "running"))

        count = storage.reconcile_stale_records()
        assert count == 2

        for execution_id in ("stale-queued", "stale-running"):
            record = storage.get_record(execution_id)
            assert record is not None
            assert record.status == "failed"
            assert record.ended_at is not None
            assert record.error is not None
            assert record.error.type == "interrupted"
            assert "interrupted by server restart" in record.error.message

    def test_reconcile_leaves_terminal_records_untouched(self, storage):
        storage.save_record(self._make_record("done-ok", "succeeded", exit_code=0, stdout="ok"))
        storage.save_record(
            self._make_record(
                "done-cancelled",
                "cancelled",
                error=ExecutionError(type="cancelled", message="cancelled by user"),
            )
        )
        storage.save_record(self._make_record("stale-queued-2", "queued"))

        count = storage.reconcile_stale_records()
        assert count == 1

        succeeded = storage.get_record("done-ok")
        assert succeeded.status == "succeeded"
        assert succeeded.error is None

        cancelled = storage.get_record("done-cancelled")
        assert cancelled.status == "cancelled"
        assert cancelled.error.type == "cancelled"

    def test_reconcile_with_no_stale_records(self, storage):
        storage.save_record(self._make_record("done", "succeeded", exit_code=0))
        assert storage.reconcile_stale_records() == 0


class TestMarkRecordRunning:
    def test_mark_running_updates_queued_record(self, storage):
        record = ExecutionRecord(
            execution_id="run-me",
            status="queued",
            code_hash="a" * 64,
            input_hash="b" * 64,
        )
        storage.save_record(record)

        updated = storage.mark_record_running("run-me", worker_id="worker-3")
        assert updated is True

        retrieved = storage.get_record("run-me")
        assert retrieved.status == "running"
        assert retrieved.dequeued_at is not None
        assert retrieved.worker_id == "worker-3"

    def test_mark_running_skips_non_queued_records(self, storage):
        record = ExecutionRecord(
            execution_id="already-done",
            status="succeeded",
            code_hash="a" * 64,
            input_hash="b" * 64,
            exit_code=0,
        )
        storage.save_record(record)

        updated = storage.mark_record_running("already-done", worker_id="worker-1")
        assert updated is False
        assert storage.get_record("already-done").status == "succeeded"

    def test_mark_running_missing_record(self, storage):
        assert storage.mark_record_running("nope") is False


class TestRecordHydrationRobustness:
    def test_resource_usage_round_trip_with_only_max_rss(self, storage):
        record = ExecutionRecord(
            execution_id="rss-only",
            status="succeeded",
            code_hash="a" * 64,
            input_hash="b" * 64,
            exit_code=0,
            resource_usage=ResourceUsage(max_rss_mb=64.5),
        )
        storage.save_record(record)

        retrieved = storage.get_record("rss-only")
        assert retrieved is not None
        assert retrieved.resource_usage is not None
        assert retrieved.resource_usage.max_rss_mb == 64.5
        assert retrieved.resource_usage.cpu_time_ms is None
        assert retrieved.resource_usage.wall_time_ms is None

    def test_resource_usage_absent_stays_none(self, storage):
        record = ExecutionRecord(
            execution_id="no-usage",
            status="succeeded",
            code_hash="a" * 64,
            input_hash="b" * 64,
            exit_code=0,
        )
        storage.save_record(record)

        retrieved = storage.get_record("no-usage")
        assert retrieved is not None
        assert retrieved.resource_usage is None

    def test_corrupted_error_json_loads_with_fallback(self, storage):
        from psycopg.types.json import Jsonb

        record = ExecutionRecord(
            execution_id="corrupt-error",
            status="failed",
            code_hash="a" * 64,
            input_hash="b" * 64,
            exit_code=1,
            error=ExecutionError(type="runtime_error", message="boom"),
        )
        storage.save_record(record)

        async def _corrupt():
            pool = storage._inner._get_pool()
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE execution_records SET error_json = %s WHERE execution_id = %s",
                    (Jsonb({"type": "no-such-error-type", "bogus": True}), "corrupt-error"),
                )

        storage._run(_corrupt())

        retrieved = storage.get_record("corrupt-error")
        assert retrieved is not None
        assert retrieved.error is not None
        assert retrieved.error.type == "internal_error"
        assert "stored error payload could not be decoded" in retrieved.error.message


class TestDigestResolutionStrictness:
    @staticmethod
    def _make_version(digest: str, tag: str, built_at=None) -> JobVersion:
        kwargs = {"built_at": built_at} if built_at is not None else {}
        return JobVersion(
            digest=digest,
            job_type_name="digest-job",
            version_tag=tag,
            dockerfile_hash="",
            requirements_hash="",
            image_ref=f"digest-job:{tag}",
            **kwargs,
        )

    def test_empty_digest_rejected(self, storage):
        storage.save_version(self._make_version("a" * 64, "v1"))

        with pytest.raises(ValueError, match="Invalid digest"):
            storage.get_version_by_digest("digest-job", "")

    def test_short_prefix_rejected(self, storage):
        storage.save_version(self._make_version("abcdef1234567890" + "0" * 48, "v1"))

        with pytest.raises(ValueError, match="Invalid digest"):
            storage.get_version_by_digest("digest-job", "abcdef12345")

    def test_non_hex_digest_rejected(self, storage):
        storage.save_version(self._make_version("abcdef1234567890" + "0" * 48, "v1"))

        with pytest.raises(ValueError, match="Invalid digest"):
            storage.get_version_by_digest("digest-job", "abc%def12345")

        with pytest.raises(ValueError, match="Invalid digest"):
            storage.get_version_by_digest("digest-job", "abcdef_2345678")

    def test_valid_prefix_resolves(self, storage):
        storage.save_version(self._make_version("abcdef1234567890" + "0" * 48, "v1"))

        retrieved = storage.get_version_by_digest("digest-job", "abcdef123456")
        assert retrieved is not None
        assert retrieved.version_tag == "v1"

    def test_unmatched_prefix_returns_none(self, storage):
        storage.save_version(self._make_version("abcdef1234567890" + "0" * 48, "v1"))

        assert storage.get_version_by_digest("digest-job", "f" * 12) is None

    def test_ambiguous_prefix_raises(self, storage):
        old_time = datetime.now(timezone.utc) - timedelta(days=1)
        new_time = datetime.now(timezone.utc)
        shared_prefix = "deadbeef0123"
        storage.save_version(self._make_version(shared_prefix + "a" * 52, "v1", built_at=old_time))
        storage.save_version(self._make_version(shared_prefix + "b" * 52, "v2", built_at=new_time))

        with pytest.raises(ValueError, match="Ambiguous digest prefix"):
            storage.get_version_by_digest("digest-job", shared_prefix)

        retrieved = storage.get_version_by_digest("digest-job", shared_prefix + "a" * 52)
        assert retrieved is not None
        assert retrieved.version_tag == "v1"


class TestSaveRecordUpsertIntegrity:
    @staticmethod
    def _make_record(execution_id: str, status: str, **kwargs) -> ExecutionRecord:
        kwargs.setdefault("code_hash", "a" * 64)
        kwargs.setdefault("input_hash", "b" * 64)
        return ExecutionRecord(execution_id=execution_id, status=status, **kwargs)

    def test_resave_preserves_submission_fields(self, storage):
        original_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        storage.save_record(
            self._make_record(
                "upsert-submission",
                "queued",
                created_at=original_time,
                queued_at=original_time,
                client_ip="10.0.0.1",
                idempotency_key="idem-upsert-1",
                idempotency_fingerprint="f" * 64,
                parent_execution_id="parent-1",
            )
        )

        storage.save_record(
            self._make_record(
                "upsert-submission",
                "succeeded",
                created_at=datetime.now(timezone.utc),
                queued_at=datetime.now(timezone.utc),
                code_hash="c" * 64,
                input_hash="d" * 64,
                stdout="done",
                exit_code=0,
            )
        )

        retrieved = storage.get_record("upsert-submission")
        assert retrieved.status == "succeeded"
        assert retrieved.stdout == "done"
        assert retrieved.exit_code == 0
        assert retrieved.created_at == original_time
        assert retrieved.queued_at == original_time
        assert retrieved.code_hash == "a" * 64
        assert retrieved.input_hash == "b" * 64
        assert retrieved.client_ip == "10.0.0.1"
        assert retrieved.idempotency_key == "idem-upsert-1"
        assert retrieved.idempotency_fingerprint == "f" * 64
        assert retrieved.parent_execution_id == "parent-1"

    def test_dequeued_at_and_worker_id_survive_final_save(self, storage):
        storage.save_record(self._make_record("upsert-dequeue", "queued"))
        assert storage.mark_record_running("upsert-dequeue", worker_id="worker-7")
        intermediate = storage.get_record("upsert-dequeue")
        assert intermediate.dequeued_at is not None

        storage.save_record(self._make_record("upsert-dequeue", "succeeded", stdout="ok"))

        retrieved = storage.get_record("upsert-dequeue")
        assert retrieved.status == "succeeded"
        assert retrieved.stdout == "ok"
        assert retrieved.dequeued_at == intermediate.dequeued_at
        assert retrieved.worker_id == "worker-7"

    def test_terminal_record_not_overwritten(self, storage, caplog):
        import logging

        storage.save_record(self._make_record("upsert-terminal", "succeeded", stdout="ok"))

        with caplog.at_level(logging.WARNING, logger="tako_vm.storage"):
            storage.save_record(self._make_record("upsert-terminal", "cancelled"))

        retrieved = storage.get_record("upsert-terminal")
        assert retrieved.status == "succeeded"
        assert retrieved.stdout == "ok"
        assert any("Refused to overwrite terminal" in m for m in caplog.messages)

    def test_non_terminal_to_terminal_transition_allowed(self, storage):
        storage.save_record(self._make_record("upsert-transition", "queued"))
        storage.save_record(self._make_record("upsert-transition", "running"))
        storage.save_record(self._make_record("upsert-transition", "failed", stderr="boom"))

        retrieved = storage.get_record("upsert-transition")
        assert retrieved.status == "failed"
        assert retrieved.stderr == "boom"


class TestSaveRecordRetry:
    @staticmethod
    def _fake_pool(attempts: dict, failures: int, exc_factory):

        class FakeCursor:
            rowcount = 1

        class FakeConn:
            async def execute(self, *args, **kwargs):
                return FakeCursor()

        class FakeConnCtx:
            async def __aenter__(self):
                attempts["n"] += 1
                if attempts["n"] <= failures:
                    raise exc_factory()
                return FakeConn()

            async def __aexit__(self, *exc_info):
                return False

        class FakePool:
            def connection(self):
                return FakeConnCtx()

        return FakePool()

    @staticmethod
    def _record() -> ExecutionRecord:
        return ExecutionRecord(
            execution_id="retry-test",
            status="succeeded",
            code_hash="a" * 64,
            input_hash="b" * 64,
        )

    def test_transient_error_retried_then_succeeds(self, monkeypatch, caplog):
        import logging

        import psycopg

        from tako_vm import storage as storage_module

        store = ExecutionStorage("postgresql://unused")
        attempts = {"n": 0}
        monkeypatch.setattr(
            store,
            "_get_pool",
            lambda: self._fake_pool(
                attempts, 2, lambda: psycopg.OperationalError("connection refused")
            ),
        )
        monkeypatch.setattr(storage_module, "_SAVE_RECORD_RETRY_DELAYS", (0, 0, 0))

        with caplog.at_level(logging.WARNING, logger="tako_vm.storage"):
            asyncio.run(store.save_record(self._record()))

        assert attempts["n"] == 3
        retry_warnings = [m for m in caplog.messages if "Transient error saving" in m]
        assert len(retry_warnings) == 2

    def test_persistent_transient_error_raises(self, monkeypatch):
        import psycopg

        from tako_vm import storage as storage_module

        store = ExecutionStorage("postgresql://unused")
        attempts = {"n": 0}
        monkeypatch.setattr(
            store,
            "_get_pool",
            lambda: self._fake_pool(
                attempts, 100, lambda: psycopg.OperationalError("connection refused")
            ),
        )
        monkeypatch.setattr(storage_module, "_SAVE_RECORD_RETRY_DELAYS", (0, 0, 0))

        with pytest.raises(psycopg.OperationalError):
            asyncio.run(store.save_record(self._record()))

        assert attempts["n"] == len(storage_module._SAVE_RECORD_RETRY_DELAYS) + 1

    def test_non_transient_error_not_retried(self, monkeypatch):
        import psycopg

        store = ExecutionStorage("postgresql://unused")
        attempts = {"n": 0}
        monkeypatch.setattr(
            store,
            "_get_pool",
            lambda: self._fake_pool(attempts, 100, lambda: psycopg.IntegrityError("duplicate key")),
        )

        with pytest.raises(psycopg.IntegrityError):
            asyncio.run(store.save_record(self._record()))

        assert attempts["n"] == 1


class TestCorrelationIdPersistence:
    def test_correlation_id_round_trip(self, storage):
        record = ExecutionRecord(
            execution_id="corr-round-trip",
            status="succeeded",
            code_hash="a" * 64,
            input_hash="b" * 64,
            correlation_id="req-abc-123",
        )

        storage.save_record(record)
        retrieved = storage.get_record("corr-round-trip")

        assert retrieved is not None
        assert retrieved.correlation_id == "req-abc-123"

    def test_correlation_id_defaults_to_none(self, storage):
        record = ExecutionRecord(
            execution_id="corr-none",
            status="succeeded",
            code_hash="a" * 64,
            input_hash="b" * 64,
        )

        storage.save_record(record)
        retrieved = storage.get_record("corr-none")

        assert retrieved is not None
        assert retrieved.correlation_id is None

    def test_correlation_id_survives_executor_rewrite(self, storage):
        queued = ExecutionRecord(
            execution_id="corr-upsert",
            status="queued",
            code_hash="a" * 64,
            input_hash="b" * 64,
            correlation_id="req-keep-me",
        )
        storage.save_record(queued)

        final = ExecutionRecord(
            execution_id="corr-upsert",
            status="succeeded",
            code_hash="a" * 64,
            input_hash="b" * 64,
            correlation_id=None,
            exit_code=0,
        )
        storage.save_record(final)

        retrieved = storage.get_record("corr-upsert")
        assert retrieved is not None
        assert retrieved.status == "succeeded"
        assert retrieved.correlation_id == "req-keep-me"


class TestRedactDLQMigration:
    def test_migration_0003_redacts_legacy_dlq_rows(self, storage):
        from tako_vm.storage import MIGRATIONS

        legacy = DeadLetterEntry(
            job_id="legacy-raw-job",
            job_data={
                "job_type": "data-processing",
                "code": "import os; print(os.environ)",
                "input_data": {"api_token": "super-secret"},
                "idempotency_key": "idem-raw-123",
                "idempotency_fingerprint": "f" * 64,
                "timeout": 30,
            },
            error_type="internal_error",
            error_message="boom",
        )
        legacy_id = storage.add_to_dlq(legacy)

        clean = DeadLetterEntry(
            job_id="already-redacted-job",
            job_data={"job_type": "default", "code_sha256": "a" * 64},
            error_type="timeout",
            error_message="slow",
        )
        clean_id = storage.add_to_dlq(clean)

        migration_sql = dict(MIGRATIONS)["0003_redact_dlq_job_data"]

        async def apply_migration():
            pool = storage._inner._get_pool()
            async with pool.connection() as conn:
                await conn.execute(migration_sql)

        storage._run(apply_migration())

        redacted = storage.get_dlq_entry(legacy_id)
        assert redacted is not None
        for key in ("code", "input_data", "idempotency_key", "idempotency_fingerprint"):
            assert key not in redacted.job_data
        assert redacted.job_data["redacted_by_migration"] is True
        assert redacted.job_data["job_type"] == "data-processing"
        assert redacted.job_data["timeout"] == 30
        assert redacted.error_type == "internal_error"

        untouched = storage.get_dlq_entry(clean_id)
        assert untouched is not None
        assert "redacted_by_migration" not in untouched.job_data
        assert untouched.job_data == {"job_type": "default", "code_sha256": "a" * 64}
