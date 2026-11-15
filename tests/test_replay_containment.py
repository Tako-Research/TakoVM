import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from tako_vm.server.app import _resolve_under_data_dir  # noqa: E402


class TestResolveUnderDataDir:
    def test_normal_key_resolves(self, tmp_path):
        (tmp_path / "runs" / "abc").mkdir(parents=True)
        resolved = _resolve_under_data_dir(tmp_path.resolve(), "runs/abc/_code.py")
        assert resolved == (tmp_path / "runs" / "abc" / "_code.py").resolve()

    def test_dotdot_traversal_rejected(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            _resolve_under_data_dir(tmp_path.resolve(), "../../../etc/passwd")
        assert exc.value.status_code == 400

    def test_absolute_key_rejected(self, tmp_path):
        with pytest.raises(HTTPException):
            _resolve_under_data_dir(tmp_path.resolve(), "/etc/passwd")

    def test_symlink_escape_rejected(self, tmp_path):
        data = tmp_path.resolve()
        (data / "runs").mkdir()
        (data / "runs" / "evil").symlink_to(tmp_path.parent)
        with pytest.raises(HTTPException):
            _resolve_under_data_dir(data, "runs/evil/secret")
