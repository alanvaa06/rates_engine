"""The shape every public result shares: a value, and the evidence under it.

One base class rather than a convention, so that ``schema_version`` and the
"absent fields are ``null``, never missing keys" rule are implemented once.
``tests/test_payloads.py`` holds every result type to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rates_engine.evidence import Evidence

__all__ = ["SCHEMA_VERSION", "EngineResult"]

SCHEMA_VERSION = "1.0"
"""Payload schema version. Bumped when a field changes meaning or disappears;
adding an optional field does not bump it."""


@dataclass(frozen=True)
class EngineResult:
    """Base for every result this package returns.

    Attributes:
        evidence: What the number rests on, including the chain of results
            this one consumed.
    """

    evidence: Evidence

    def payload_fields(self) -> dict[str, Any]:
        """The result-specific part of the payload. Subclasses override this."""
        return {}

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-ready mapping with ``schema_version`` and evidence."""
        return {
            "schema_version": SCHEMA_VERSION,
            "result_type": type(self).__name__,
            **self.payload_fields(),
            "evidence": self.evidence.to_dict(),
        }
