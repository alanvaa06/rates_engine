"""What a series identifier says about the kind of number inside it.

Kept in one place because two providers load Treasury yields and both have to
reach the same conclusion about them. A ``DGS*`` series is a constant-maturity
Treasury par yield; used as a stand-in for OIS par it carries a swap spread,
so it is classified ``PROXY`` at the point it enters a snapshot and not later,
when someone might forget.
"""

from __future__ import annotations

from rates_engine.evidence import DataQuality

__all__ = ["classify_series", "TREASURY_PAR_YIELD", "OVERNIGHT_FIXING", "TREASURY_PROXY_NOTE"]

TREASURY_PAR_YIELD = "treasury_par_yield"
"""Instrument kind for constant-maturity Treasury par yields (``DGS*``)."""

OVERNIGHT_FIXING = "overnight_fixing"
"""Instrument kind for published overnight rates (SOFR, EFFR)."""

TREASURY_PROXY_NOTE = (
    "Constant-maturity Treasury par yield. Standing in for an OIS par rate it "
    "carries the swap spread, which is negative and variable; the bias is known "
    "to exist and is not quantified here."
)

_OVERNIGHT = frozenset({"SOFR", "EFFR", "OBFR"})
_SOFR_DERIVED = frozenset({"SOFR30DAYAVG", "SOFR90DAYAVG", "SOFR180DAYAVG", "SOFRINDEX"})


def classify_series(series_id: str) -> tuple[str, DataQuality, str | None]:
    """Instrument kind, data quality and note implied by a series identifier.

    Args:
        series_id: Provider identifier, e.g. ``"SOFR"`` or ``"DGS2"``.

    Returns:
        A triple of instrument kind, :class:`~rates_engine.evidence.DataQuality`
        and an optional note. Unrecognised identifiers come back as
        ``("unclassified", OBSERVED, None)`` rather than raising: an unknown
        series is not yet a problem, and becomes one only if a curve tries to
        use it.
    """
    upper = series_id.strip().upper()
    if upper in _OVERNIGHT:
        return OVERNIGHT_FIXING, DataQuality.OBSERVED, None
    if upper in _SOFR_DERIVED:
        return "sofr_derived", DataQuality.OBSERVED, None
    if upper.startswith("DGS"):
        return TREASURY_PAR_YIELD, DataQuality.PROXY, TREASURY_PROXY_NOTE
    return "unclassified", DataQuality.OBSERVED, None
