"""
Tests for API response-model construction in tako_vm.server.app.

Guards the consolidation of ExecutionRecordFullResponse.from_record onto the
base ExecutionRecordResponse.from_record: the previous hand-copied override
silently dropped `timing` (and later `runtime`) from the ?view=full payload
while the slim view kept them. These assert the two views stay in lockstep.
"""

from tako_vm.models import Artifact, ExecutionRecord, ExecutionTiming, ResourceUsage
from tako_vm.server.app import (
    ArtifactResponse,
    ExecutionRecordFullResponse,
    ExecutionRecordResponse,
)


def _record_with_detail():
    return ExecutionRecord(
        execution_id="rec-full",
        status="succeeded",
        job_type="default",
        code_hash="a" * 64,
        input_hash="b" * 64,
        runtime="runsc",
        timing=ExecutionTiming(startup_ms=100, dep_install_ms=10, execution_ms=200, total_ms=300),
        resource_usage=ResourceUsage(max_rss_mb=12.5, cpu_time_ms=50, wall_time_ms=300),
        artifacts=[
            Artifact(
                name="out.png",
                size_bytes=10,
                sha256="c" * 64,
                content_type="image/png",
                storage_key="runs/rec-full/artifacts/out.png",
            )
        ],
    )


class TestFullViewMatchesSlimView:
    def test_full_view_includes_timing(self):
        # Regression: full view used to return timing=null even when slim did not.
        full = ExecutionRecordFullResponse.from_record(_record_with_detail())
        assert full.timing is not None
        assert full.timing.execution_ms == 200

    def test_full_view_includes_runtime(self):
        # Regression: the runtime field (issue #99) was also dropped by the
        # hand-copied override.
        full = ExecutionRecordFullResponse.from_record(_record_with_detail())
        assert full.runtime == "runsc"

    def test_full_and_slim_agree_on_base_fields(self):
        record = _record_with_detail()
        slim = ExecutionRecordResponse.from_record(record)
        full = ExecutionRecordFullResponse.from_record(record)
        assert full.runtime == slim.runtime
        assert (full.timing is None) == (slim.timing is None)
        assert full.execution_id == slim.execution_id
        assert full.status == slim.status

    def test_full_view_maps_artifacts(self):
        full = ExecutionRecordFullResponse.from_record(_record_with_detail())
        assert len(full.artifacts) == 1
        assert full.artifacts[0].name == "out.png"
        assert full.resource_usage is not None
        assert full.resource_usage.max_rss_mb == 12.5


class TestArtifactResponseFromModel:
    def test_from_model_copies_fields(self):
        art = Artifact(
            name="x.bin",
            size_bytes=3,
            sha256="d" * 64,
            content_type="application/octet-stream",
            storage_key="runs/x/artifacts/x.bin",
        )
        resp = ArtifactResponse.from_model(art)
        assert resp.name == "x.bin"
        assert resp.size_bytes == 3
        assert resp.sha256 == "d" * 64
        assert resp.content_type == "application/octet-stream"
