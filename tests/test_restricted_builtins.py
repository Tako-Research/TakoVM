"""Tests for the restricted builtins module."""

import pytest

from tako_vm.restricted_builtins import (
    RestrictedLimits,
    _DENIED_BUILTIN_NAMES,
    _SAFE_BUILTIN_NAMES,
    build_safe_builtins,
    wrap_code_with_restricted_builtins,
)


class TestBuildSafeBuiltins:
    def test_includes_allowed_builtins(self):
        safe = build_safe_builtins()
        for name in ("len", "sum", "min", "max", "sorted", "print"):
            assert name in safe, f"{name} missing from safe builtins"

    def test_excludes_denied_builtins(self):
        safe = build_safe_builtins()
        for name in _DENIED_BUILTIN_NAMES:
            assert name not in safe, f"{name} should be denied"

    def test_no_open(self):
        safe = build_safe_builtins()
        assert "open" not in safe

    def test_no_import(self):
        safe = build_safe_builtins()
        assert "__import__" not in safe

    def test_no_eval_exec_compile(self):
        safe = build_safe_builtins()
        assert "eval" not in safe
        assert "exec" not in safe
        assert "compile" not in safe

    def test_no_input(self):
        safe = build_safe_builtins()
        assert "input" not in safe

    def test_no_help_dir(self):
        safe = build_safe_builtins()
        assert "help" not in safe
        assert "dir" not in safe

    def test_no_reflection(self):
        safe = build_safe_builtins()
        assert "getattr" not in safe
        assert "setattr" not in safe
        assert "delattr" not in safe
        assert "globals" not in safe
        assert "locals" not in safe
        assert "vars" not in safe

    def test_range_is_safe_variant(self):
        safe = build_safe_builtins()
        assert "range" in safe
        assert safe["range"] is not range

    def test_includes_exception_types(self):
        safe = build_safe_builtins()
        assert safe["ValueError"] is ValueError
        assert safe["TypeError"] is TypeError
        assert safe["KeyError"] is KeyError

    def test_includes_type_constructors(self):
        safe = build_safe_builtins()
        assert safe["int"] is int
        assert safe["float"] is float
        assert safe["str"] is str
        assert safe["list"] is list
        assert safe["dict"] is dict

    def test_denied_and_safe_are_disjoint(self):
        overlap = _SAFE_BUILTIN_NAMES & _DENIED_BUILTIN_NAMES
        assert not overlap, f"Overlap between safe and denied: {overlap}"


class TestSafeRange:
    def test_basic_range(self):
        safe = build_safe_builtins()
        assert list(safe["range"](5)) == [0, 1, 2, 3, 4]

    def test_range_with_start_stop(self):
        safe = build_safe_builtins()
        assert list(safe["range"](2, 6)) == [2, 3, 4, 5]

    def test_range_with_step(self):
        safe = build_safe_builtins()
        assert list(safe["range"](0, 10, 3)) == [0, 3, 6, 9]

    def test_negative_step(self):
        safe = build_safe_builtins()
        assert list(safe["range"](5, 0, -1)) == [5, 4, 3, 2, 1]

    def test_empty_range(self):
        safe = build_safe_builtins()
        assert list(safe["range"](0)) == []

    def test_zero_step_raises(self):
        safe = build_safe_builtins()
        with pytest.raises(ValueError, match="must not be zero"):
            safe["range"](0, 10, 0)

    def test_exceeds_limit_raises(self):
        limits = RestrictedLimits(max_range=100)
        safe = build_safe_builtins(limits)
        with pytest.raises(ValueError, match="exceeding the limit"):
            safe["range"](101)

    def test_at_limit_ok(self):
        limits = RestrictedLimits(max_range=100)
        safe = build_safe_builtins(limits)
        r = safe["range"](100)
        assert len(r) == 100

    def test_too_many_args(self):
        safe = build_safe_builtins()
        with pytest.raises(TypeError, match="at most 3"):
            safe["range"](1, 2, 3, 4)

    def test_default_limit_allows_reasonable_range(self):
        safe = build_safe_builtins()
        r = safe["range"](1_000_000)
        assert len(r) == 1_000_000


class TestRestrictedExecution:
    """Integration tests: exec user code with restricted builtins."""

    def _exec_restricted(self, code: str, limits=None):
        safe = build_safe_builtins(limits)
        g = {"__builtins__": safe, "__name__": "__restricted__"}
        exec(compile(code, "<test>", "exec"), g)
        return g

    def test_basic_arithmetic(self):
        g = self._exec_restricted("result = 2 + 3")
        assert g["result"] == 5

    def test_list_operations(self):
        g = self._exec_restricted("result = sorted([3, 1, 2])")
        assert g["result"] == [1, 2, 3]

    def test_len_sum_min_max(self):
        code = """\
data = [10, 20, 30]
length = len(data)
total = sum(data)
lo = min(data)
hi = max(data)
"""
        g = self._exec_restricted(code)
        assert g["length"] == 3
        assert g["total"] == 60
        assert g["lo"] == 10
        assert g["hi"] == 30

    def test_print_works(self, capsys):
        self._exec_restricted("print('hello')")
        assert "hello" in capsys.readouterr().out

    def test_import_blocked(self):
        with pytest.raises((ImportError, NameError)):
            self._exec_restricted("import os")

    def test_open_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("open('/etc/passwd')")

    def test_eval_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("eval('1+1')")

    def test_exec_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("exec('x=1')")

    def test_dunder_import_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("__import__('os')")

    def test_input_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("input('>')")

    def test_dir_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("dir()")

    def test_help_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("help()")

    def test_globals_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("globals()")

    def test_locals_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("locals()")

    def test_getattr_blocked(self):
        with pytest.raises(NameError):
            self._exec_restricted("getattr(int, '__bases__')")

    def test_range_bounded(self):
        limits = RestrictedLimits(max_range=50)
        with pytest.raises(ValueError, match="exceeding the limit"):
            self._exec_restricted("list(range(100))", limits=limits)

    def test_exception_handling_works(self):
        code = """\
try:
    x = 1 / 0
except ZeroDivisionError:
    result = 'caught'
"""
        g = self._exec_restricted(code)
        assert g["result"] == "caught"

    def test_class_definition_works(self):
        code = """\
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
p = Point(1, 2)
result = p.x + p.y
"""
        g = self._exec_restricted(code)
        assert g["result"] == 3

    def test_list_comprehension_works(self):
        g = self._exec_restricted("result = [x**2 for x in range(5)]")
        assert g["result"] == [0, 1, 4, 9, 16]

    def test_dict_comprehension_works(self):
        g = self._exec_restricted("result = {k: k*2 for k in range(3)}")
        assert g["result"] == {0: 0, 1: 2, 2: 4}

    def test_lambda_works(self):
        g = self._exec_restricted("fn = lambda x: x + 1; result = fn(5)")
        assert g["result"] == 6


class TestWrapCode:
    def test_produces_valid_python(self):
        wrapper = wrap_code_with_restricted_builtins("x = 1 + 1")
        compile(wrapper, "<test>", "exec")

    def test_escapes_quotes(self):
        wrapper = wrap_code_with_restricted_builtins("x = 'hello'")
        assert "\\'" in wrapper

    def test_custom_limits_embedded(self):
        limits = RestrictedLimits(max_range=500)
        wrapper = wrap_code_with_restricted_builtins("x = 1", limits=limits)
        assert "max_range=500" in wrapper


class TestRestrictedLimits:
    def test_defaults(self):
        limits = RestrictedLimits()
        assert limits.max_range == 10_000_000
        assert limits.max_string_length == 10_000_000
        assert limits.max_collection_size == 1_000_000
        assert limits.max_int_bits == 4096

    def test_custom_values(self):
        limits = RestrictedLimits(max_range=100, max_int_bits=256)
        assert limits.max_range == 100
        assert limits.max_int_bits == 256
