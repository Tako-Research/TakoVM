"""
Safe builtins module for restricted Python execution.

Provides a curated __builtins__ dictionary that exposes only safe
operations and blocks access to the filesystem, network, process
control, and Python runtime internals.

Used by the restricted_python execution mode (see issues #10, #11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class RestrictedLimits:
    """Configurable limits for restricted execution primitives."""

    max_range: int = 10_000_000
    max_string_length: int = 10_000_000
    max_collection_size: int = 1_000_000
    max_int_bits: int = 4096


_DEFAULT_LIMITS = RestrictedLimits()


def _make_safe_range(limits: RestrictedLimits):
    """Create a range wrapper that enforces a maximum iteration count."""

    _builtin_range = range

    def safe_range(*args: int) -> range:
        if len(args) == 1:
            start, stop, step = 0, args[0], 1
        elif len(args) == 2:
            start, stop, step = args[0], args[1], 1
        elif len(args) == 3:
            start, stop, step = args[0], args[1], args[2]
        else:
            raise TypeError(
                f"range expected at most 3 arguments, got {len(args)}"
            )

        if step == 0:
            raise ValueError("range() arg 3 must not be zero")

        if step > 0:
            length = max(0, (stop - start + step - 1) // step)
        else:
            length = max(0, (start - stop - step - 1) // (-step))

        if length > limits.max_range:
            raise ValueError(
                f"range() would produce {length} items, "
                f"exceeding the limit of {limits.max_range}"
            )

        return _builtin_range(*args)

    return safe_range


# Builtins that are always safe: pure computation, no I/O or introspection.
_SAFE_BUILTIN_NAMES = frozenset(
    {
        # Aggregation / ordering
        "len",
        "sum",
        "min",
        "max",
        "sorted",
        "reversed",
        "enumerate",
        "zip",
        "map",
        "filter",
        # Type constructors / conversions
        "int",
        "float",
        "str",
        "bool",
        "list",
        "tuple",
        "dict",
        "set",
        "frozenset",
        "bytes",
        "bytearray",
        "complex",
        # Numeric helpers
        "abs",
        "round",
        "pow",
        "divmod",
        # String / representation
        "repr",
        "format",
        "chr",
        "ord",
        "bin",
        "oct",
        "hex",
        "ascii",
        # Predicates
        "isinstance",
        "issubclass",
        "callable",
        "hasattr",
        "all",
        "any",
        # Iteration
        "iter",
        "next",
        "slice",
        # Misc safe
        "id",
        "hash",
        "type",
        "object",
        "super",
        "property",
        "staticmethod",
        "classmethod",
        # Exceptions (needed so user code can raise/catch)
        "Exception",
        "BaseException",
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "BlockingIOError",
        "BrokenPipeError",
        "BufferError",
        "BytesWarning",
        "ChildProcessError",
        "ConnectionAbortedError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "DeprecationWarning",
        "EOFError",
        "EnvironmentError",
        "FileExistsError",
        "FileNotFoundError",
        "FloatingPointError",
        "FutureWarning",
        "GeneratorExit",
        "IOError",
        "ImportError",
        "ImportWarning",
        "IndentationError",
        "IndexError",
        "InterruptedError",
        "IsADirectoryError",
        "KeyError",
        "KeyboardInterrupt",
        "LookupError",
        "MemoryError",
        "ModuleNotFoundError",
        "NameError",
        "NotADirectoryError",
        "NotImplementedError",
        "OSError",
        "OverflowError",
        "PendingDeprecationWarning",
        "PermissionError",
        "ProcessLookupError",
        "RecursionError",
        "ReferenceError",
        "ResourceWarning",
        "RuntimeError",
        "RuntimeWarning",
        "StopAsyncIteration",
        "StopIteration",
        "SyntaxError",
        "SyntaxWarning",
        "SystemError",
        "SystemExit",
        "TabError",
        "TimeoutError",
        "TypeError",
        "UnboundLocalError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "UnicodeError",
        "UnicodeTranslationError",
        "UnicodeWarning",
        "UserWarning",
        "ValueError",
        "Warning",
        "ZeroDivisionError",
        # Constants
        "True",
        "False",
        "None",
        "Ellipsis",
        "NotImplemented",
        # Print is allowed (captured by sandbox stdout)
        "print",
        # Required by Python internals for class definitions
        "__build_class__",
    }
)

# Builtins explicitly denied — any of these in __builtins__ would allow
# sandbox escape or environment leakage.
_DENIED_BUILTIN_NAMES = frozenset(
    {
        "__import__",
        "open",
        "input",
        "help",
        "dir",
        "vars",
        "globals",
        "locals",
        "eval",
        "exec",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "exit",
        "quit",
        "license",
        "credits",
        "copyright",
        "memoryview",
    }
)


def build_safe_builtins(
    limits: Optional[RestrictedLimits] = None,
) -> Dict[str, Any]:
    """
    Build a __builtins__ dict containing only safe operations.

    The returned dict can be passed as the ``__builtins__`` key inside
    a globals dict used with ``exec()`` to sandbox user code.

    Args:
        limits: Optional execution limits. Uses defaults if None.

    Returns:
        Dictionary suitable for use as ``__builtins__``.
    """
    if limits is None:
        limits = _DEFAULT_LIMITS

    import builtins as _builtins

    safe: Dict[str, Any] = {}

    for name in _SAFE_BUILTIN_NAMES:
        obj = getattr(_builtins, name, None)
        if obj is not None:
            safe[name] = obj

    # Replace range with the bounded version
    safe["range"] = _make_safe_range(limits)

    return safe


def wrap_code_with_restricted_builtins(
    code: str,
    limits: Optional[RestrictedLimits] = None,
) -> str:
    """
    Generate a wrapper script that executes *code* under restricted builtins.

    The wrapper imports ``restricted_builtins``, builds the safe dict,
    compiles the user code, and ``exec()``s it with the restricted
    ``__builtins__``.  This is the entrypoint injected by the execution
    layer when ``execution_mode == "restricted_python"``.

    Args:
        code: Raw user Python source.
        limits: Optional execution limits.

    Returns:
        Python source string for the wrapper script.
    """
    if limits is None:
        limits = _DEFAULT_LIMITS

    # Escape the user code for embedding as a raw string literal.
    escaped = code.replace("\\", "\\\\").replace("'", "\\'")

    return f"""\
import tako_vm.restricted_builtins as _rb

_limits = _rb.RestrictedLimits(
    max_range={limits.max_range!r},
    max_string_length={limits.max_string_length!r},
    max_collection_size={limits.max_collection_size!r},
    max_int_bits={limits.max_int_bits!r},
)
_safe = _rb.build_safe_builtins(_limits)
_code = '''{escaped}'''
_compiled = compile(_code, "<user>", "exec")
exec(_compiled, {{"__builtins__": _safe, "__name__": "__restricted__"}})
"""
