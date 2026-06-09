"""Tests for job type schema and merge behavior."""

import json
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

import tako_vm.job_types as job_types_module
from tako_vm.config import JobTypeConfig, JobTypeGPUConfig
from tako_vm.job_types import JobType, JobTypeRegistry, merge_config_job_types


def test_job_type_from_dict_reads_nested_gpu_config():
    """from_dict supports nested gpu object in JSON config."""
    job_type = JobType.from_dict(
        {
            "name": "gpu-job",
            "session_enabled": True,
            "gpu": {
                "enabled": True,
                "vendor": "nvidia",
                "count": 2,
                "device_ids": [],
            },
        }
    )

    assert job_type.name == "gpu-job"
    assert job_type.session_enabled is True
    assert job_type.gpu_enabled is True
    assert job_type.gpu_vendor == "nvidia"
    assert job_type.gpu_count == 2


def test_job_type_from_dict_supports_legacy_gpu_top_level_fields():
    """Top-level gpu_* fields remain supported for compatibility."""
    job_type = JobType.from_dict(
        {
            "name": "legacy-gpu",
            "gpu_enabled": True,
            "gpu_vendor": "amd",
            "gpu_device_ids": ["0", "1"],
        }
    )

    assert job_type.gpu_enabled is True
    assert job_type.gpu_vendor == "amd"
    assert job_type.gpu_device_ids == ["0", "1"]


def test_job_type_config_roundtrip_preserves_gpu_and_session_fields():
    """Dataclass <-> pydantic conversion preserves new fields."""
    original = JobType(
        name="roundtrip",
        session_enabled=True,
        gpu_enabled=True,
        gpu_vendor="nvidia",
        gpu_count=1,
        gpu_device_ids=[],
    )

    config_model = original.to_config()
    restored = JobType.from_config(config_model)

    assert restored.name == "roundtrip"
    assert restored.session_enabled is True
    assert restored.gpu_enabled is True
    assert restored.gpu_vendor == "nvidia"
    assert restored.gpu_count == 1


def test_merge_config_job_types_overrides_in_memory_without_persistence():
    """Config merge updates runtime registry without rewriting job_types.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "job_types.json"
        config_path.write_text(
            json.dumps(
                {
                    "job_types": [
                        {
                            "name": "default",
                            "timeout": 30,
                            "session_enabled": False,
                            "gpu": {
                                "enabled": False,
                                "vendor": None,
                                "count": None,
                                "device_ids": [],
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        registry = JobTypeRegistry(config_path=config_path)
        original_on_disk = config_path.read_text(encoding="utf-8")

        merged_count = merge_config_job_types(
            registry,
            [
                JobTypeConfig(
                    name="default",
                    timeout=99,
                    session_enabled=True,
                    gpu=JobTypeGPUConfig(enabled=True, vendor="nvidia", count=1),
                )
            ],
        )

        assert merged_count == 1
        merged = registry.get("default")
        assert merged is not None
        assert merged.timeout == 99
        assert merged.session_enabled is True
        assert merged.gpu_enabled is True
        assert merged.gpu_vendor == "nvidia"

        # Persist=False means disk config should be untouched.
        assert config_path.read_text(encoding="utf-8") == original_on_disk


def test_default_registry_path_is_under_data_dir(tmp_path, monkeypatch):
    """Default registry lives in the data dir, never in site-packages."""
    monkeypatch.setenv("TAKO_VM_DATA_DIR", str(tmp_path))

    registry = JobTypeRegistry()

    assert registry.config_path == tmp_path / "job_types.json"

    registry.register(JobType(name="persisted"))

    assert (tmp_path / "job_types.json").exists()
    # Saving never touches the legacy site-packages location.
    legacy_path = job_types_module._legacy_registry_path()
    if legacy_path is not None and legacy_path.exists():
        legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
        assert "persisted" not in {jt["name"] for jt in legacy_data["job_types"]}


def test_default_registry_migrates_from_legacy_site_packages_file(tmp_path, monkeypatch):
    """If only the legacy site-packages file exists, it is read (not written)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy_path = tmp_path / "legacy" / "job_types.json"
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        json.dumps({"job_types": [{"name": "legacy-entry", "timeout": 42}]}),
        encoding="utf-8",
    )
    original_legacy_content = legacy_path.read_text(encoding="utf-8")

    monkeypatch.setenv("TAKO_VM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(job_types_module, "_legacy_registry_path", lambda: legacy_path)

    registry = JobTypeRegistry()

    migrated = registry.get("legacy-entry")
    assert migrated is not None
    assert migrated.timeout == 42

    # Persisting writes to the data dir; the legacy file is left untouched.
    registry.register(JobType(name="new-entry"))
    assert (data_dir / "job_types.json").exists()
    assert legacy_path.read_text(encoding="utf-8") == original_legacy_content

    # Once the data-dir file exists, the legacy file is no longer consulted.
    legacy_path.write_text(
        json.dumps({"job_types": [{"name": "should-not-load"}]}), encoding="utf-8"
    )
    reloaded = JobTypeRegistry()
    assert reloaded.get("should-not-load") is None
    assert reloaded.get("new-entry") is not None


def test_save_is_atomic_and_leaves_valid_json(tmp_path):
    """_save writes via temp file + os.replace, leaving valid JSON and no temp litter."""
    config_path = tmp_path / "job_types.json"
    registry = JobTypeRegistry(config_path=config_path)

    registry.register(JobType(name="alpha"))
    registry.register(JobType(name="beta", memory_limit="1g", cpu_limit=2.0))

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert {jt["name"] for jt in data["job_types"]} == {"alpha", "beta"}

    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_load_skips_invalid_entries(tmp_path, caplog):
    """Tampered entries with out-of-bounds limits are rejected, valid ones kept."""
    config_path = tmp_path / "job_types.json"
    config_path.write_text(
        json.dumps(
            {
                "job_types": [
                    {"name": "valid", "timeout": 30},
                    {"name": "huge-cpu", "cpu_limit": 999},
                    {"name": "negative-timeout", "timeout": -5},
                    {"name": "bad-memory", "memory_limit": "lots"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("ERROR"):
        registry = JobTypeRegistry(config_path=config_path)

    assert registry.get("valid") is not None
    assert registry.get("huge-cpu") is None
    assert registry.get("negative-timeout") is None
    assert registry.get("bad-memory") is None
    assert "huge-cpu" in caplog.text


def test_from_dict_rejects_out_of_bounds_values():
    """from_dict enforces the same bounds as the YAML JobTypeConfig path."""
    with pytest.raises(ValidationError):
        JobType.from_dict({"name": "bad", "cpu_limit": 999})

    with pytest.raises(ValidationError):
        JobType.from_dict({"name": "bad", "timeout": -1})

    with pytest.raises(ValidationError):
        JobType.from_dict({"name": "bad", "memory_limit": "999999g"})
