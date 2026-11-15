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
    sha256_content,
    sha256_json,
)


class TestHashFunctions:
    def test_sha256_json_deterministic(self):
        obj1 = {"b": 2, "a": 1}
        obj2 = {"a": 1, "b": 2}

        assert sha256_json(obj1) == sha256_json(obj2)

    def test_sha256_json_different_objects(self):
        obj1 = {"a": 1}
        obj2 = {"a": 2}

        assert sha256_json(obj1) != sha256_json(obj2)

    def test_sha256_json_length(self):
        result = sha256_json({"test": "data"})
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_sha256_content(self):
        result = sha256_content("hello world")
        assert len(result) == 64


class TestResourceUsage:
    def test_resource_usage_defaults(self):
        usage = ResourceUsage()
        assert usage.max_rss_mb is None
        assert usage.cpu_time_ms is None
        assert usage.wall_time_ms is None

    def test_resource_usage_with_values(self):
        usage = ResourceUsage(max_rss_mb=128.5, cpu_time_ms=1000, wall_time_ms=2000)
        assert usage.max_rss_mb == 128.5
        assert usage.cpu_time_ms == 1000
        assert usage.wall_time_ms == 2000

    def test_resource_usage_bounds(self):
        with pytest.raises(ValidationError):
            ResourceUsage(max_rss_mb=-1)

        with pytest.raises(ValidationError):
            ResourceUsage(cpu_time_ms=86400001)

    def test_resource_usage_forbids_extra(self):
        with pytest.raises(ValidationError):
            ResourceUsage.model_validate({"unknown_field": "value"})


class TestExecutionTiming:
    def test_execution_timing_defaults(self):
        timing = ExecutionTiming()
        assert timing.startup_ms is None
        assert timing.execution_ms is None
        assert timing.dep_install_started is False

    def test_execution_timing_with_phases(self):
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
    def test_input_artifact_valid(self):
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
        with pytest.raises(ValidationError) as exc_info:
            InputArtifact(
                name="../etc/passwd",
                size_bytes=100,
                sha256="a" * 64,
                storage_key="key",
            )
        assert "path separators" in str(exc_info.value).lower()

    def test_input_artifact_rejects_dotdot(self):
        with pytest.raises(ValidationError):
            InputArtifact(
                name="..",
                size_bytes=100,
                sha256="a" * 64,
                storage_key="key",
            )

    def test_input_artifact_sha256_validation(self):
        with pytest.raises(ValidationError):
            InputArtifact(
                name="file.txt",
                size_bytes=100,
                sha256="invalid",
                storage_key="key",
            )


class TestArtifact:
    def test_artifact_valid(self):
        artifact = Artifact(
            name="output.png",
            size_bytes=2048,
            sha256="b" * 64,
            content_type="image/png",
            storage_key="runs/123/artifacts/output.png",
        )
        assert artifact.name == "output.png"

    def test_artifact_rejects_backslash_path(self):
        with pytest.raises(ValidationError):
            Artifact(
                name="path\\to\\file.txt",
                size_bytes=100,
                sha256="a" * 64,
                storage_key="key",
            )


class TestExecutionError:
    def test_execution_error_valid(self):
        error = ExecutionError(
            type="timeout",
            message="Execution exceeded time limit",
            phase="execution",
        )
        assert error.type == "timeout"
        assert error.phase == "execution"

    def test_execution_error_types(self):
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
        with pytest.raises(ValidationError):
            ExecutionError(type="unknown", message="x" * 4097)


class TestExecutionRecord:
    def test_execution_record_defaults(self):
        record = ExecutionRecord()
        assert record.status == "queued"
        assert record.job_type == "default"
        assert record.execution_id
        assert record.created_at is not None

    def test_execution_record_hash_validation(self):
        record = ExecutionRecord(code_hash="a" * 64, input_hash="b" * 64)
        assert record.code_hash == "a" * 64

        record = ExecutionRecord(code_hash="", input_hash="")
        assert record.code_hash == ""

        with pytest.raises(ValidationError):
            ExecutionRecord(code_hash="abc")

        with pytest.raises(ValidationError):
            ExecutionRecord(code_hash="g" * 64)

    def test_execution_record_status_values(self):
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
        record = ExecutionRecord(
            input_artifacts=[
                InputArtifact(name="b.txt", size_bytes=100, sha256="b" * 64, storage_key="k1"),
                InputArtifact(name="a.txt", size_bytes=200, sha256="a" * 64, storage_key="k2"),
            ]
        )
        hash1 = record.compute_input_artifacts_hash()

        record2 = ExecutionRecord(
            input_artifacts=[
                InputArtifact(name="a.txt", size_bytes=200, sha256="a" * 64, storage_key="k2"),
                InputArtifact(name="b.txt", size_bytes=100, sha256="b" * 64, storage_key="k1"),
            ]
        )
        hash2 = record2.compute_input_artifacts_hash()

        assert hash1 == hash2

    def test_execution_record_empty_artifacts_hash(self):
        record = ExecutionRecord()
        assert record.compute_input_artifacts_hash() == ""

    def test_execution_record_stdout_stderr_ensure_string(self):
        record = ExecutionRecord.model_validate({"stdout": None, "stderr": None})
        assert record.stdout == ""
        assert record.stderr == ""

    def test_execution_record_relationship(self):
        record = ExecutionRecord(
            parent_execution_id="parent-123",
            relationship="fork",
        )
        assert record.parent_execution_id == "parent-123"
        assert record.relationship == "fork"


class TestJobVersion:
    def test_job_version_valid(self):
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
        version = JobVersion(
            digest="abcdef1234567890" + "0" * 48,
            job_type_name="test-job",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v1",
        )
        assert version.full_ref == "test-job@sha256:abcdef123456"

    def test_job_version_short_digest(self):
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
        version = JobVersion(
            digest="a" * 64,
            job_type_name="test",
            dockerfile_hash="",
            requirements_hash="",
            image_ref="test:v1",
        )
        assert version.dockerfile_hash == ""

        with pytest.raises(ValidationError):
            JobVersion(
                digest="a" * 64,
                job_type_name="test",
                dockerfile_hash="invalid",
                requirements_hash="",
                image_ref="test:v1",
            )


class TestDeadLetterEntry:
    def test_dead_letter_entry_valid(self):
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
        entry = DeadLetterEntry(
            job_id="job-1",
            job_data={},
            error_type="unknown",
        )
        assert entry.retry_count == 0
        assert entry.id is None
        assert entry.created_at is not None

    def test_dead_letter_entry_error_types(self):
        valid_types: list[ErrorType] = ["timeout", "oom", "docker_error", "internal_error"]
        for error_type in valid_types:
            entry = DeadLetterEntry(job_id="job", job_data={}, error_type=error_type)
            assert entry.error_type == error_type


class TestDeadLetterJobSummary:
    def test_summary_keeps_fingerprints_and_metadata(self):
        code = "print('hello')"
        input_data = {"key": "value"}
        job_data = {
            "code": code,
            "input_data": input_data,
            "job_type": "svg-processing",
            "timeout": 45,
            "startup_timeout": 120,
            "requirements": ["numpy", "pillow==10.0.0"],
            "correlation_id": "corr-1",
            "parent_execution_id": "parent-1",
            "idempotency_key": "idem-secret",
        }

        summary = DeadLetterEntry.build_job_summary(job_data)

        assert summary["job_type"] == "svg-processing"
        assert summary["code_hash"] == sha256_content(code)
        assert summary["input_hash"] == sha256_json(input_data)
        assert summary["code_size_bytes"] == len(code.encode("utf-8"))
        assert summary["input_size_bytes"] == len(b'{"key":"value"}')
        assert summary["timeout"] == 45
        assert summary["startup_timeout"] == 120
        assert summary["requirements"] == ["numpy", "pillow==10.0.0"]
        assert summary["correlation_id"] == "corr-1"
        assert summary["parent_execution_id"] == "parent-1"
        assert summary["has_idempotency_key"] is True

    def test_summary_drops_raw_payloads(self):
        job_data = {
            "code": "import os; os.environ['SECRET']",
            "input_data": {"api_key": "sk-live-supersecret"},
            "idempotency_key": "idem-key-abc",
        }

        summary = DeadLetterEntry.build_job_summary(job_data)

        assert "code" not in summary
        assert "input_data" not in summary
        assert "idempotency_key" not in summary
        serialized = str(summary)
        assert "sk-live-supersecret" not in serialized
        assert "idem-key-abc" not in serialized
        assert "import os" not in serialized

    def test_summary_minimal_job(self):
        summary = DeadLetterEntry.build_job_summary({})

        assert summary["job_type"] == "default"
        assert summary["code_hash"] == sha256_content("")
        assert summary["input_hash"] == sha256_json({})
        assert summary["code_size_bytes"] == 0
        assert summary["has_idempotency_key"] is False
        assert "timeout" not in summary
        assert "startup_timeout" not in summary
        assert "requirements" not in summary
        assert "correlation_id" not in summary
        assert "parent_execution_id" not in summary
