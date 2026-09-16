"""What a number rests on, carried with the number and composable across calls.

A curve is not a result until you can see what it rests on. The practical
problem that shapes this module is composition: ``hedge()`` consumes
``price()``, which consumes ``bootstrap()``. If each level flattened its
inputs' evidence into a dictionary, a degradation recorded at the bottom — a
Treasury par yield standing in for an OIS quote, a contract notional nobody
verified — would be gone by the time anyone read the hedge. So evidence nests:
``Evidence.sources`` holds the evidence of the inputs, unflattened, and
``qualities`` is the recursive union. A ``HedgeResult`` built on a proxied
curve reports the proxy without ``hedging`` knowing Treasuries exist.

``DataQuality`` is ranked, because "the worst thing in this chain" has to be a
single answer. The order runs from *I know exactly what this is* to *nobody
checked*:

``OBSERVED``
    Published by the source it claims. A SOFR fixing from FRED.
``SYNTHETIC``
    Constructed on purpose, and known to be constructed — the dual-curve
    solver's inputs. Deliberate and controlled, so it ranks above nothing.
``PROXY``
    Real data standing in for a different quantity, carrying a bias that is
    known to exist and not quantified. A Treasury par yield used as OIS par.
``ASSUMED``
    A number nobody verified against its source. The SR1 contract notional.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any

__all__ = ["DataQuality", "Provenance", "Degradation", "Evidence"]


class DataQuality(StrEnum):
    """How much a value is worth trusting, ranked worst-last.

    See the module docstring for what each level means and why the order runs
    this way. ``rank`` exists so that ``max`` over a chain has a single answer.
    """

    OBSERVED = "observed"
    SYNTHETIC = "synthetic"
    PROXY = "proxy"
    ASSUMED = "assumed"

    @property
    def rank(self) -> int:
        """Position in the trust order, 0 for ``OBSERVED`` and 3 for ``ASSUMED``."""
        return _QUALITY_RANK[self]


_QUALITY_RANK: dict[DataQuality, int] = {
    DataQuality.OBSERVED: 0,
    DataQuality.SYNTHETIC: 1,
    DataQuality.PROXY: 2,
    DataQuality.ASSUMED: 3,
}


@dataclass(frozen=True)
class Provenance:
    """Where one input came from, and what kind of thing it is.

    Attributes:
        source: Provider that supplied it — ``"fred"``, ``"file"``, ``"cme"``,
            ``"derived"``, ``"assumed"``, ``"synthetic"``.
        series_id: Identifier within that source, e.g. ``"SOFR"``. ``None`` when
            the input is not a named series.
        retrieved_at: When it was fetched. ``None`` for derived values.
        instrument_kind: What the numbers are, e.g. ``"treasury_par_yield"`` or
            ``"sofr_fixing"``. Read by the bootstrap to tell proxies apart from
            the real thing.
        data_quality: Trust level, see :class:`DataQuality`.
        notes: Free text for the one thing a reader would otherwise have to ask.
    """

    source: str
    series_id: str | None = None
    retrieved_at: datetime | date | None = None
    instrument_kind: str | None = None
    data_quality: DataQuality = DataQuality.OBSERVED
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise, with ``None`` for every absent field rather than a missing key."""
        return {
            "source": self.source,
            "series_id": self.series_id,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "instrument_kind": self.instrument_kind,
            "data_quality": self.data_quality.value,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Degradation:
    """Something that went wrong enough to report but not enough to refuse.

    The distinction from an exception is deliberate: a degradation means the
    number is computable and worth less than it looks. A Treasury proxy in the
    long end is one. Anything that makes the number *wrong* raises instead.

    Attributes:
        code: Stable machine-readable tag, e.g. ``"treasury_par_proxy"``.
        message: One sentence a human can act on.
        data_quality: The trust level this degradation imposes.
    """

    code: str
    message: str
    data_quality: DataQuality = DataQuality.PROXY

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-ready mapping."""
        return {
            "code": self.code,
            "message": self.message,
            "data_quality": self.data_quality.value,
        }


def _freeze(fields: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(fields or {}))


@dataclass(frozen=True)
class Evidence:
    """What one computation used, warned about, and was itself built on.

    Attributes:
        produced_by: Fully qualified-ish name of what produced the result, e.g.
            ``"curves.bootstrap_discount_curve"``.
        inputs: Provenance of each direct input.
        fields: Whatever this particular calculation has to declare — fit
            residuals in bp, interpolation method, convexity model and sigma,
            bump basis and shape. Module-specific by design; a fixed schema
            here would force every module through the widest one.
        warnings: Degradations reported rather than raised.
        sources: Evidence of the results this one consumed, unflattened. This
            is what makes the chain readable from the top.
    """

    produced_by: str
    inputs: tuple[Provenance, ...] = ()
    fields: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[Degradation, ...] = ()
    sources: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "fields", _freeze(self.fields))

    @property
    def qualities(self) -> frozenset[DataQuality]:
        """Every trust level anywhere in this evidence chain, including sources."""
        found = {p.data_quality for p in self.inputs}
        found |= {w.data_quality for w in self.warnings}
        for source in self.sources:
            found |= source.qualities
        return frozenset(found or {DataQuality.OBSERVED})

    @property
    def worst_quality(self) -> DataQuality:
        """The least trustworthy level anywhere in the chain, by :class:`DataQuality` rank."""
        return max(self.qualities, key=lambda q: q.rank)

    def with_sources(self, *sources: Evidence) -> Evidence:
        """Return a copy with ``sources`` appended. Evidence is never mutated."""
        return Evidence(
            produced_by=self.produced_by,
            inputs=self.inputs,
            fields=dict(self.fields),
            warnings=self.warnings,
            sources=self.sources + tuple(sources),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the whole chain recursively, ``None`` never a missing key."""
        return {
            "produced_by": self.produced_by,
            "inputs": [p.to_dict() for p in self.inputs],
            "fields": _jsonable(dict(self.fields)),
            "warnings": [w.to_dict() for w in self.warnings],
            "data_quality": self.worst_quality.value,
            "data_qualities": sorted(q.value for q in self.qualities),
            "sources": [s.to_dict() for s in self.sources],
        }


def _jsonable(value: Any) -> Any:
    """Coerce nested values into something ``json.dumps`` accepts, losslessly for our types."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (str, bytes)):
        return value.decode() if isinstance(value, bytes) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Iterable):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item") and hasattr(value, "dtype"):
        return value.item()
    return value
