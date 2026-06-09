"""
Tests for the ExecutionStorage class (PostgreSQL persistence).

Tests CRUD operations for ExecutionRecords, JobVersions, and DeadLetterQueue.
"""

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
    """Reuse shared Postgres-backed storage fixture from conftest."""
    return test_storage


class TestExecutionStorageInit:
    """Tests for storage initialization."""

    def test_init_creates_database(self, storage):
        """Storage init creates tables and accepts connections."""
        records = storage.list_records(limit=1, offset=0)
        assert records == []

    def test_init_idempotent(self, storage):
        """Multiple init calls are safe."""
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
    """Tests for ExecutionRecord save/get/list operations."""

    def test_save_and_get_record(self, storage):
        """Can save and retrieve an execution record."""
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
        """Get returns None for nonexistent record."""
        result = storage.get_record("nonexistent")
        assert result is None

    def test_save_record_with_all_fields(self, storage):
        """Can save record with all optional fields populated."""
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
        )

        storage.save_record(record)
        retrieved = storage.get_record("full-record")

        assert retrieved is not None
        assert retrieved.job_ref == "custom-job@sha256:abc123"
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
        """Can update an existing record."""
        record = ExecutionRecord(
            execution_id="update-test",
            status="queued",
            code_hash="a" * 64,
            input_hash="b" * 64,
        )
        storage.save_record(record)

        # Update status
        record.status = "running"
        storage.save_record(record)

        retrieved = storage.get_record("update-test")
        assert retrieved.status == "running"

    def test_list_records_empty(self, storage):
        """List returns empty list when no records."""
        records = storage.list_records()
        assert records == []

    def test_list_records_pagination(self, storage):
        """List supports pagination."""
        for i in range(10):
            storage.save_record(
                ExecutionRecord(
                    execution_id=f"record-{i:02d}",
                    status="succeeded",
                    code_hash="a" * 64,
                    input_hash="b" * 64,
                )
            )

        # Get first page
        page1 = storage.list_records(limit=3, offset=0)
        assert len(page1) == 3

        # Get second page
        page2 = storage.list_records(limit=3, offset=3)
        assert len(page2) == 3

        # Verify different records
        ids1 = {r.execution_id for r in page1}
        ids2 = {r.execution_id for r in page2}
        assert ids1.isdisjoint(ids2)

    def test_list_records_filter_by_status(self, storage):
        """List can filter by status."""
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
        """List can filter by job type."""
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
        """Can retrieve record by idempotency key."""
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
        """Returns None for nonexistent idempotency key."""
        result = storage.get_by_idempotency_key("nonexistent")
        assert result is None


class TestCleanup:
    """Tests for cleanup operations."""

    def test_cleanup_old_records(self, storage):
        """Cleanup deletes records older than TTL."""
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
    """Tests for JobVersion operations."""

    def test_save_and_get_version(self, storage):
        """Can save and retrieve a job version."""
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
        """Can retrieve version with short digest prefix."""
        version = JobVersion(
            digest="abcdef1234567890" + "0" * 48,
            job_type_name="test-job",
            version_tag="v1.0.0",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v1",
        )
        storage.save_version(version)

        retrieved = storage.get_version_by_digest("test-job", "abcdef")
        assert retrieved is not None
        assert retrieved.version_tag == "v1.0.0"

    def test_get_version_by_tag(self, storage):
        """Can retrieve version by tag."""
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
        """Get latest version returns most recent."""
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
        """Can list all versions for a job type."""
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
    """Tests for Dead Letter Queue operations."""

    def test_add_and_get_dlq_entry(self, storage):
        """Can add and retrieve DLQ entry."""
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
        """Can list DLQ entries with pagination."""
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
        """Can filter DLQ by error type."""
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
        """Can remove entry from DLQ."""
        entry_id = storage.add_to_dlq(
            DeadLetterEntry(job_id="job-1", job_data={}, error_type="timeout", error_message="Err")
        )

        removed = storage.remove_from_dlq(entry_id)
        assert removed is True

        # Verify removed
        assert storage.get_dlq_entry(entry_id) is None

    def test_remove_nonexistent_dlq_entry(self, storage):
        """Remove returns False for nonexistent entry."""
        removed = storage.remove_from_dlq(99999)
        assert removed is False

    def test_dlq_stats(self, storage):
        """Can get DLQ statistics."""
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
        """Cleanup deletes old DLQ entries."""
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
    """Tests for startup reconciliation of stale queued/running records."""

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
        """Stale queued/running records become failed with an interrupted error."""
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
        """Records already in a terminal state are not modified."""
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
        """Reconcile returns 0 when nothing is stale."""
        storage.save_record(self._make_record("done", "succeeded", exit_code=0))
        assert storage.reconcile_stale_records() == 0


class TestMarkRecordRunning:
    """Tests for persisting the queued -> running transition."""

    def test_mark_running_updates_queued_record(self, storage):
        """A queued record transitions to running with dequeued_at/worker_id set."""
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
        """Terminal records are never moved back to running."""
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
        """Marking a nonexistent record returns False."""
        assert storage.mark_record_running("nope") is False
