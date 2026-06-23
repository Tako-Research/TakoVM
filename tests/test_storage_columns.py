"""
Guards for the single-source execution_records upsert column descriptor
(tako_vm.storage._EXECUTION_COLUMNS).

The INSERT column list, the %s placeholders, the value tuple, and the ON
CONFLICT DO UPDATE SET clause are all generated from one ordered descriptor so
adding a column is a single edit and a placeholder miscount is impossible. These
tests freeze the column order and the per-column conflict policy, so an
accidental reorder or a policy flip (which would silently corrupt the
submission-identity preservation) fails loudly without needing a database.
"""

from tako_vm.models import ExecutionRecord, ExecutionTiming, ResourceUsage
from tako_vm.storage import (
    _EXECUTION_COLUMNS,
    _INSERT_COLUMN_SQL,
    _INSERT_PLACEHOLDER_SQL,
    _SET_COALESCE_EXCLUDED,
    _SET_COALESCE_EXISTING,
    _SET_EXCLUDED,
    _SET_KEEP_EXISTING,
    _SET_KEY,
)

# Frozen expectation: (column_name, conflict_policy) in physical order. If you
# add a column, add it here too — that is the point (one place, reviewed).
EXPECTED = [
    ("execution_id", _SET_KEY),
    ("status", _SET_EXCLUDED),
    ("job_type", _SET_EXCLUDED),
    ("job_ref", _SET_EXCLUDED),
    ("created_at", _SET_KEEP_EXISTING),
    ("queued_at", _SET_COALESCE_EXISTING),
    ("dequeued_at", _SET_COALESCE_EXISTING),
    ("started_at", _SET_EXCLUDED),
    ("ended_at", _SET_EXCLUDED),
    ("duration_ms", _SET_EXCLUDED),
    ("attempt", _SET_EXCLUDED),
    ("max_attempts", _SET_EXCLUDED),
    ("worker_id", _SET_COALESCE_EXCLUDED),
    ("idempotency_key", _SET_COALESCE_EXISTING),
    ("idempotency_fingerprint", _SET_COALESCE_EXISTING),
    ("code_hash", _SET_COALESCE_EXISTING),
    ("input_hash", _SET_COALESCE_EXISTING),
    ("params_hash", _SET_COALESCE_EXISTING),
    ("input_artifacts_hash", _SET_COALESCE_EXISTING),
    ("input_artifacts_json", _SET_EXCLUDED),
    ("exit_code", _SET_EXCLUDED),
    ("stdout", _SET_EXCLUDED),
    ("stderr", _SET_EXCLUDED),
    ("stdout_truncated", _SET_EXCLUDED),
    ("stderr_truncated", _SET_EXCLUDED),
    ("result_json", _SET_EXCLUDED),
    ("max_rss_mb", _SET_EXCLUDED),
    ("cpu_time_ms", _SET_EXCLUDED),
    ("wall_time_ms", _SET_EXCLUDED),
    ("timing_json", _SET_EXCLUDED),
    ("artifacts_json", _SET_EXCLUDED),
    ("error_json", _SET_EXCLUDED),
    ("client_ip", _SET_COALESCE_EXISTING),
    ("correlation_id", _SET_COALESCE_EXISTING),
    ("parent_execution_id", _SET_COALESCE_EXISTING),
    ("relationship", _SET_COALESCE_EXISTING),
    ("runtime", _SET_COALESCE_EXCLUDED),
]


def test_column_order_and_policy_match_frozen_expectation():
    actual = [(name, policy) for name, policy, _ in _EXECUTION_COLUMNS]
    assert actual == EXPECTED


def test_placeholder_count_matches_column_count():
    # The bug this whole refactor prevents: a placeholder miscount silently
    # shifting every column. They are generated from one list, so they agree.
    assert _INSERT_PLACEHOLDER_SQL.count("%s") == len(_EXECUTION_COLUMNS)
    assert len(_INSERT_COLUMN_SQL.split(",")) == len(_EXECUTION_COLUMNS)


def test_value_tuple_length_matches_columns():
    record = ExecutionRecord(
        execution_id="x",
        status="succeeded",
        code_hash="a" * 64,
        input_hash="b" * 64,
        runtime="runsc",
        resource_usage=ResourceUsage(max_rss_mb=1.0, cpu_time_ms=2, wall_time_ms=3),
        timing=ExecutionTiming(total_ms=5),
    )
    blobs = {
        "artifacts_json": [],
        "input_artifacts_json": [],
        "error_json": None,
        "result_json": None,
        "timing_json": record.timing.model_dump(),
    }
    values = tuple(extract(record, blobs) for _, _, extract in _EXECUTION_COLUMNS)
    assert len(values) == len(_EXECUTION_COLUMNS)
    # Spot-check that the runtime column extracts the record's runtime.
    runtime_index = [name for name, _, _ in _EXECUTION_COLUMNS].index("runtime")
    assert values[runtime_index] == "runsc"


def test_execution_id_has_no_set_line():
    # The ON CONFLICT key must not appear as its own SET assignment (substring
    # "execution_id =" also lives inside "parent_execution_id =", so match the
    # start of each stripped clause).
    from tako_vm.storage import _UPDATE_SET_SQL

    clauses = [c.strip() for c in _UPDATE_SET_SQL.split(",\n")]
    assert not any(c.startswith("execution_id =") for c in clauses)
