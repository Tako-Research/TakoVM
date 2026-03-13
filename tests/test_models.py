"""
Tests for Tako VM data models (Pydantic models).

Tests validation, serialization, and utility methods.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tako_vm.models import (
    Artifact,
    DeadLetterEntry,
    ErrorType,
    ExecutionError,
    ExecutionRecord,
    ExecutionTiming,
    InputArtifact,
    JobStatus,
    JobVersion,
    ResourceUsage,
    SessionEvent,
    SessionRecord,
    sha256_content,
    sha256_json,
)


class TestHashFunctions:
    """Tests for hash utility functions."""

    def test_sha256_json_deterministic(self):
        """sha256_json produces deterministic hashes."""
        obj1 = {"b": 2, "a": 1}
        obj2 = {"a": 1, "b": 2}

        # Order shouldn't matter
        assert sha256_json(obj1) == sha256_json(obj2)

    def test_sha256_json_different_objects(self):
        """sha256_json produces different hashes for different objects."""
        obj1 = {"a": 1}
        obj2 = {"a": 2}

        assert sha256_json(obj1) != sha256_json(obj2)

    def test_sha256_json_length(self):
        """sha256_json produces 64-character hex strings."""
        result = sha256_json({"test": "data"})
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_sha256_content(self):
        """sha256_content hashes string content."""
        result = sha256_content("hello world")
        assert len(result) == 64


class TestResourceUsage:
    """Tests for ResourceUsage model."""

    def test_resource_usage_defaults(self):
        """ResourceUsage has None defaults."""
        usage = ResourceUsage()
        assert usage.max_rss_mb is None
        assert usage.cpu_time_ms is None
        assert usage.wall_time_ms is None

    def test_resource_usage_with_values(self):
        """ResourceUsage accepts valid values."""
        usage = ResourceUsage(max_rss_mb=128.5, cpu_time_ms=1000, wall_time_ms=2000)
        assert usage.max_rss_mb == 128.5
        assert usage.cpu_time_ms == 1000
        assert usage.wall_time_ms == 2000

    def test_resource_usage_bounds(self):
        """ResourceUsage validates bounds."""
        with pytest.raises(ValidationError):
            ResourceUsage(max_rss_mb=-1)  # Must be >= 0

        with pytest.raises(ValidationError):
            ResourceUsage(cpu_time_ms=86400001)  # Max 24h in ms

    def test_resource_usage_forbids_extra(self):
        """ResourceUsage rejects unknown fields."""
        with pytest.raises(ValidationError):
            ResourceUsage.model_validate({"unknown_field": "value"})


class TestExecutionTiming:
    """Tests for ExecutionTiming model."""

    def test_execution_timing_defaults(self):
        """ExecutionTiming has sensible defaults."""
        timing = ExecutionTiming()
        assert timing.startup_ms is None
        assert timing.execution_ms is None
        assert timing.dep_install_started is False

    def test_execution_timing_with_phases(self):
        """ExecutionTiming tracks phase information."""
        timing = ExecutionTiming(
            startup_ms=1000,
            dep_install_ms=500,
            execution_ms=2000,
            total_ms=3000,
            phase_at_exit="completed",
            dep_install_started=True,
        )
        assert timing.startup_ms == 1000
        assert timing.phase_at_exit == "completed"


class TestInputArtifact:
    """Tests for InputArtifact model."""

    def test_input_artifact_valid(self):
        """InputArtifact accepts valid data."""
        artifact = InputArtifact(
            name="input.svg",
            size_bytes=1024,
            sha256="a" * 64,
            content_type="image/svg+xml",
            storage_key="runs/123/inputs/input.svg",
        )
        assert artifact.name == "input.svg"
        assert artifact.size_bytes == 1024

    def test_input_artifact_rejects_path_traversal(self):
        """InputArtifact rejects path traversal in filename."""
        with pytest.raises(ValidationError) as exc_info:
            InputArtifact(
                name="../etc/passwd",
                size_bytes=100,
                sha256="a" * 64,
                storage_key="key",
            )
        assert "path separators" in str(exc_info.value).lower()

    def test_input_artifact_rejects_dotdot(self):
        """InputArtifact rejects '..' as filename."""
        with pytest.raises(ValidationError):
            InputArtifact(
                name="..",
                size_bytes=100,
                sha256="a" * 64,
                storage_key="key",
            )

    def test_input_artifact_sha256_validation(self):
        """InputArtifact validates SHA256 format."""
        with pytest.raises(ValidationError):
            InputArtifact(
                name="file.txt",
                size_bytes=100,
                sha256="invalid",  # Not 64 hex chars
                storage_key="key",
            )


class TestArtifact:
    """Tests for Artifact model."""

    def test_artifact_valid(self):
        """Artifact accepts valid data."""
        artifact = Artifact(
            name="output.png",
            size_bytes=2048,
            sha256="b" * 64,
            content_type="image/png",
            storage_key="runs/123/artifacts/output.png",
        )
        assert artifact.name == "output.png"

    def test_artifact_rejects_backslash_path(self):
        """Artifact rejects Windows-style path separators."""
        with pytest.raises(ValidationError):
            Artifact(
                name="path\\to\\file.txt",
                size_bytes=100,
                sha256="a" * 64,
                storage_key="key",
            )


class TestExecutionError:
    """Tests for ExecutionError model."""

    def test_execution_error_valid(self):
        """ExecutionError accepts valid error types."""
        error = ExecutionError(
            type="timeout",
            message="Execution exceeded time limit",
            phase="execution",
        )
        assert error.type == "timeout"
        assert error.phase == "execution"

    def test_execution_error_types(self):
        """ExecutionError accepts all defined error types."""
        valid_types: list[ErrorType] = [
            "timeout",
            "oom",
            "cancelled",
            "syntax_error",
            "runtime_error",
            "unknown",
        ]
        for error_type in valid_types:
            error = ExecutionError(type=error_type, message="Test")
            assert error.type == error_type

    def test_execution_error_message_length(self):
        """ExecutionError enforces message length limits."""
        # Max 4096 chars
        with pytest.raises(ValidationError):
            ExecutionError(type="unknown", message="x" * 4097)


class TestExecutionRecord:
    """Tests for ExecutionRecord model."""

    def test_execution_record_defaults(self):
        """ExecutionRecord has sensible defaults."""
        record = ExecutionRecord()
        assert record.status == "queued"
        assert record.job_type == "default"
        assert record.execution_id  # Auto-generated UUID
        assert record.created_at is not None

    def test_execution_record_hash_validation(self):
        """ExecutionRecord validates hash fields."""
        # Valid 64-char hex
        record = ExecutionRecord(code_hash="a" * 64, input_hash="b" * 64)
        assert record.code_hash == "a" * 64

        # Empty is allowed
        record = ExecutionRecord(code_hash="", input_hash="")
        assert record.code_hash == ""

        # Invalid length
        with pytest.raises(ValidationError):
            ExecutionRecord(code_hash="abc")  # Too short

        # Invalid characters
        with pytest.raises(ValidationError):
            ExecutionRecord(code_hash="g" * 64)  # 'g' not in hex

    def test_execution_record_status_values(self):
        """ExecutionRecord accepts valid status values."""
        valid_statuses: list[JobStatus] = [
            "queued",
            "running",
            "succeeded",
            "failed",
            "timeout",
            "oom",
            "cancelled",
        ]
        for status in valid_statuses:
            record = ExecutionRecord(status=status)
            assert record.status == status

    def test_execution_record_to_summary(self):
        """ExecutionRecord.to_summary returns expected fields."""
        record = ExecutionRecord(
            execution_id="test-123",
            status="succeeded",
            job_type="default",
            duration_ms=1000,
            exit_code=0,
        )
        summary = record.to_summary()

        assert summary["execution_id"] == "test-123"
        assert summary["status"] == "succeeded"
        assert summary["duration_ms"] == 1000
        assert "created_at" in summary

    def test_execution_record_compute_input_artifacts_hash(self):
        """ExecutionRecord computes consistent artifact hash."""
        record = ExecutionRecord(
            input_artifacts=[
                InputArtifact(name="b.txt", size_bytes=100, sha256="b" * 64, storage_key="k1"),
                InputArtifact(name="a.txt", size_bytes=200, sha256="a" * 64, storage_key="k2"),
            ]
        )
        hash1 = record.compute_input_artifacts_hash()

        # Same artifacts in different order should produce same hash
        record2 = ExecutionRecord(
            input_artifacts=[
                InputArtifact(name="a.txt", size_bytes=200, sha256="a" * 64, storage_key="k2"),
                InputArtifact(name="b.txt", size_bytes=100, sha256="b" * 64, storage_key="k1"),
            ]
        )
        hash2 = record2.compute_input_artifacts_hash()

        assert hash1 == hash2

    def test_execution_record_empty_artifacts_hash(self):
        """ExecutionRecord returns empty hash for no artifacts."""
        record = ExecutionRecord()
        assert record.compute_input_artifacts_hash() == ""

    def test_execution_record_stdout_stderr_ensure_string(self):
        """ExecutionRecord ensures stdout/stderr are strings."""
        record = ExecutionRecord.model_validate({"stdout": None, "stderr": None})
        assert record.stdout == ""
        assert record.stderr == ""

    def test_execution_record_relationship(self):
        """ExecutionRecord supports lineage tracking."""
        record = ExecutionRecord(
            parent_execution_id="parent-123",
            relationship="fork",
        )
        assert record.parent_execution_id == "parent-123"
        assert record.relationship == "fork"


class TestJobVersion:
    """Tests for JobVersion model."""

    def test_job_version_valid(self):
        """JobVersion accepts valid data."""
        version = JobVersion(
            digest="a" * 64,
            job_type_name="svg-processing",
            version_tag="v1.0.0",
            built_at=datetime.now(timezone.utc),
            dockerfile_hash="b" * 64,
            requirements_hash="c" * 64,
            image_ref="svg-processing:v1.0.0",
        )
        assert version.job_type_name == "svg-processing"
        assert version.digest == "a" * 64

    def test_job_version_full_ref(self):
        """JobVersion.full_ref returns formatted reference."""
        version = JobVersion(
            digest="abcdef1234567890" + "0" * 48,
            job_type_name="test-job",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v1",
        )
        assert version.full_ref == "test-job@sha256:abcdef123456"

    def test_job_version_short_digest(self):
        """JobVersion.short_digest returns truncated digest."""
        version = JobVersion(
            digest="a" * 64,
            job_type_name="test",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v1",
        )
        assert version.short_digest == "a" * 12
        assert len(version.short_digest) == 12

    def test_job_version_hash_validation(self):
        """JobVersion validates hash fields."""
        # Empty is allowed
        version = JobVersion(
            digest="a" * 64,
            job_type_name="test",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v1",
        )
        assert version.dockerfile_hash == ""

        # Invalid hash
        with pytest.raises(ValidationError):
            JobVersion(
                digest="a" * 64,
                job_type_name="test",
                dockerfile_hash="invalid",  # Not 64 hex chars
                requirements_hash="",
                image_ref="test:v1",
            )


class TestDeadLetterEntry:
    """Tests for DeadLetterEntry model."""

    def test_dead_letter_entry_valid(self):
        """DeadLetterEntry accepts valid data."""
        entry = DeadLetterEntry(
            job_id="failed-job-123",
            job_data={"code": "print('fail')", "input_data": {}},
            error_type="timeout",
            error_message="Execution exceeded time limit",
            retry_count=3,
            client_ip="192.168.1.1",
            correlation_id="corr-456",
        )
        assert entry.job_id == "failed-job-123"
        assert entry.error_type == "timeout"
        assert entry.retry_count == 3

    def test_dead_letter_entry_defaults(self):
        """DeadLetterEntry has sensible defaults."""
        entry = DeadLetterEntry(
            job_id="job-1",
            job_data={},
            error_type="unknown",
        )
        assert entry.retry_count == 0
        assert entry.id is None
        assert entry.created_at is not None

    def test_dead_letter_entry_error_types(self):
        """DeadLetterEntry accepts all error types."""
        valid_types: list[ErrorType] = ["timeout", "oom", "docker_error", "internal_error"]
        for error_type in valid_types:
            entry = DeadLetterEntry(job_id="job", job_data={}, error_type=error_type)
            assert entry.error_type == error_type


class TestSessionModels:
    """Tests for session-related models."""

    def test_session_record_defaults(self):
        """SessionRecord initializes with expected defaults."""
        record = SessionRecord(
            container_name="tako-session-1",
            image_name="code-executor:latest",
            runtime="runc",
            workspace_dir="/tmp/session-1",
        )

        assert record.status == "creating"
        assert record.job_type == "default"
        assert record.gpu_enabled is False
        assert record.metadata == {}

    def test_session_record_validates_status_literal(self):
        """SessionRecord enforces allowed status values."""
        with pytest.raises(ValidationError):
            SessionRecord(
                status="unknown",  # type: ignore[arg-type]
                container_name="tako-session-1",
                image_name="code-executor:latest",
                runtime="runc",
                workspace_dir="/tmp/session-1",
            )

    def test_session_event_defaults(self):
        """SessionEvent defaults event type to message."""
        event = SessionEvent(session_id="session-1", direction="out", payload={"ok": True})
        assert event.event_type == "message"
        assert event.file_name is None

    def test_session_event_validates_direction_literal(self):
        """SessionEvent enforces direction literals."""
        with pytest.raises(ValidationError):
            SessionEvent(
                session_id="session-1",
                direction="sideways",  # type: ignore[arg-type]
                payload={"bad": True},
            )
