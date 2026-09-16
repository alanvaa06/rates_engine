"""Turning results and refusals into one JSON document each.

Two rules, both enforced by ``tests/test_payloads.py`` and
``tests/test_cli_json.py``. Every payload carries ``schema_version``, and an
absent value is ``null`` rather than a missing key — a consumer should never
have to tell "the engine did not compute this" apart from "the engine does not
have this field" by catching a ``KeyError``.
"""

from __future__ import annotations

import json
from typing import Any

from rates_engine.errors import RatesEngineError
from rates_engine.results import SCHEMA_VERSION, EngineResult

__all__ = ["result_payload", "error_payload", "dumps"]


def result_payload(result: EngineResult, **extra: Any) -> dict[str, Any]:
    """The JSON-ready payload of a result, plus any top-level extras.

    Args:
        result: The result to serialise.
        **extra: Additional top-level keys, e.g. the command that produced it.

    Returns:
        The payload mapping.
    """
    return {**result.to_dict(), **extra}


def error_payload(exc: BaseException) -> dict[str, Any]:
    """The payload for a failure that produced no result.

    Args:
        exc: The exception. A :class:`~rates_engine.errors.RatesEngineError`
            carries its own exit code; anything else is an unexpected failure
            and gets exit code 1.

    Returns:
        A mapping with ``schema_version``, ``error`` and ``exit_code``. The
        traceback is deliberately absent: it goes to stderr, so that stdout
        stays a single parseable document.
    """
    exit_code = exc.exit_code if isinstance(exc, RatesEngineError) else 1
    return {
        "schema_version": SCHEMA_VERSION,
        "result_type": None,
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
            "recoverable": exit_code == 1,
        },
        "exit_code": exit_code,
    }


def dumps(payload: dict[str, Any]) -> str:
    """Serialise a payload to a single-line JSON document.

    Args:
        payload: The mapping to serialise.

    Returns:
        The JSON text, with ``NaN`` and infinities rendered as ``null`` so the
        output parses under a strict JSON reader.
    """
    return json.dumps(_finite(payload), default=str, allow_nan=False)


def _finite(value: Any) -> Any:
    """Replace non-finite floats with ``None`` throughout a nested structure."""
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value
