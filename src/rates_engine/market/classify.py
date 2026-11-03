"""What a series identifier says about the kind of number inside it.

Kept in one place because two providers load Treasury yields and both have to
reach the same conclusion about them. A ``DGS*`` series is a constant-maturity
Treasury par yield; used as a stand-in for OIS par it carries a swap spread,
so it is classified ``PROXY`` at the point it enters a snapshot and not later,
when someone might forget.

Units live here for the same reason. A FRED download and the same bytes read
from disk have to come out in the same units, so the file provider and the
FRED provider both ask :func:`resolve_percent` rather than each deciding.
"""

from __future__ import annotations

from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.evidence import DataQuality

__all__ = [
    "classify_series",
    "quoted_in_percent",
    "resolve_percent",
    "TREASURY_PAR_YIELD",
    "OVERNIGHT_FIXING",
    "TREASURY_PROXY_NOTE",
]

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
        A triple of instrument kind, :class:`~rates_engine.core.evidence.DataQuality`
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


_INDEX_LEVELS = frozenset({"SOFRINDEX"})


def quoted_in_percent(series_id: str) -> bool | None:
    """Whether FRED publishes a series in percent, as far as this package knows.

    Args:
        series_id: FRED identifier, e.g. ``"SOFR"`` or ``"DGS2"``.

    Returns:
        ``True`` for the overnight rates, the SOFR averages and the ``DGS*``
        Treasury yields, which FRED quotes in percent; ``False`` for
        ``SOFRINDEX``, an index level (2 April 2018 = 1) that was divided by
        100 along with the rates until this existed; ``None`` for any other
        identifier. ``None`` means "not known here", and the readers refuse
        on it rather than guess: dividing every FRED series by 100 turned
        a peso-per-dollar rate of 17.5 into 0.175, marked observed.
    """
    upper = series_id.strip().upper()
    if upper in _INDEX_LEVELS:
        return False
    if upper in _OVERNIGHT or upper in _SOFR_DERIVED or upper.startswith("DGS"):
        return True
    return None


def resolve_percent(series_id: str, percent: bool | None) -> bool:
    """The units to read a FRED-format series in: the caller's, else the known ones.

    Args:
        series_id: FRED identifier the values belong to.
        percent: ``True`` if the values are in percent and should be divided
            by 100 into decimals, ``False`` if they are already in the units
            wanted, ``None`` to use :func:`quoted_in_percent`.

    Returns:
        Whether to divide by 100.

    Raises:
        UnsupportedConventionError: ``percent`` is ``None`` and the
            identifier is not one whose units this package knows. Stating
            them is one argument; guessing them is a hundredfold error that
            looks like data.
    """
    if percent is not None:
        return percent
    known = quoted_in_percent(series_id)
    if known is None:
        raise UnsupportedConventionError(
            f"the units of FRED series {series_id!r} are not known to this package. "
            "Pass percent=True if FRED quotes it in percent (it is then divided by 100 "
            "into a decimal), or percent=False to keep the values as published; "
            "dividing it by 100 on a guess is the error this refuses"
        )
    return known
