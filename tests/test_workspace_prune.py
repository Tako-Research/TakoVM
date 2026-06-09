"""
Disk-hygiene sweeps: stranded workspace dirs and on-disk run artifacts past TTL.

Run temp dirs (job-*/sandbox-*) are normally removed in a finally block, but a
crash leaks them with no other reaper; and the DB record cleanup never deletes
the on-disk runs/<id> artifacts. These pure filesystem sweeps reclaim both.
No Docker required.
"""

import os
import time

import tako_vm.execution.worker as worker
from tako_vm.execution.worker import prune_old_run_dirs, prune_stale_workspaces


def _age(path, seconds_old: int) -> None:
    t = time.time() - seconds_old
    os.utime(path, (t, t))


class TestPruneStaleWorkspaces:
    def test_removes_only_old_run_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker, "WORKSPACE_DIR", str(tmp_path))
        old_job = tmp_path / "job-old"
        old_job.mkdir()
        (old_job / "f").write_text("x")
        _age(old_job, 10_000)
        old_sandbox = tmp_path / "sandbox-old"
        old_sandbox.mkdir()
        _age(old_sandbox, 10_000)
        fresh = tmp_path / "job-fresh"
        fresh.mkdir()  # mtime ~ now
        unrelated = tmp_path / "data"
        unrelated.mkdir()
        _age(unrelated, 10_000)  # old but not a job-/sandbox- dir

        removed = prune_stale_workspaces(max_age_seconds=3600)

        assert removed == 2
        assert not old_job.exists()
        assert not old_sandbox.exists()
        assert fresh.exists()  # too new
        assert unrelated.exists()  # wrong prefix, untouched

    def test_missing_workspace_dir_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker, "WORKSPACE_DIR", str(tmp_path / "nope"))
        assert prune_stale_workspaces(max_age_seconds=1) == 0

    def test_symlinked_entry_is_skipped(self, tmp_path, monkeypatch):
        # A workspace-named symlink must never be followed/removed by the sweep.
        monkeypatch.setattr(worker, "WORKSPACE_DIR", str(tmp_path))
        target = tmp_path / "target_dir"
        target.mkdir()
        (target / "keep").write_text("x")
        link = tmp_path / "job-link"
        link.symlink_to(target)

        removed = prune_stale_workspaces(max_age_seconds=1)

        assert removed == 0
        assert link.is_symlink()  # the symlink itself untouched
        assert (target / "keep").exists()  # and its target not deleted


class TestPruneOldRunDirs:
    def test_removes_runs_past_ttl(self, tmp_path):
        runs = tmp_path / "runs"
        runs.mkdir()
        old = runs / "abc"
        old.mkdir()
        (old / "_code.py").write_text("x")
        _age(old, 40 * 86400)
        fresh = runs / "def"
        fresh.mkdir()

        removed = prune_old_run_dirs(tmp_path, ttl_days=30)

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_no_runs_dir_is_noop(self, tmp_path):
        assert prune_old_run_dirs(tmp_path, ttl_days=30) == 0
