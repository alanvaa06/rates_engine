"""What a valuation says about the curve it discounted on.

Every pricer records the discounting in its evidence. Until v0.4 each wrote
the same literal -- ``"collateral_rate_ois_sofr"`` -- whatever the curve's
currency, so a peso swap priced on a peso curve reported an OIS-SOFR
discount. The fields now come from
:func:`rates_engine.conventions.indices.collateral_index`, and a currency
whose collateral convention is assumed carries that assumption as a
degradation, so the price's ``worst_quality`` says so.

For USD the fields are byte-for-byte what they always were.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rates_engine.conventions.indices import UNRESOLVED_MXN, collateral_index
from rates_engine.core.evidence import DataQuality, Degradation
from rates_engine.core.money import Currency

__all__ = ["discounting_fields", "collateral_warnings", "merge_warnings"]

_WHY = dict(UNRESOLVED_MXN)


def discounting_fields(currency: Currency) -> dict[str, Any]:
    """The ``discounting`` tag and its note, for flows in ``currency``.

    Args:
        currency: The currency of the discount curve.

    Returns:
        ``{"discounting": "collateral_rate_ois_<index>", "discounting_note": ...}``.

    Raises:
        UnsupportedConventionError: ``currency`` has no collateral index.
    """
    index = collateral_index(currency)
    return {
        "discounting": f"collateral_rate_ois_{index.slug}",
        "discounting_note": (
            f"Discounted on the OIS-{index.label} curve because collateral is remunerated at "
            f"{index.label} (Fujii-Shimada-Takahashi; Piterbarg), not on a separate funding curve."
        ),
    }


def collateral_warnings(currency: Currency) -> tuple[Degradation, ...]:
    """Degradations for a collateral index whose conventions were assumed.

    Only the collateral assumption itself is reported here. The index's
    other unresolved conventions belong to whatever built the curve, which
    carries them in its own provenance.

    Args:
        currency: The currency of the discount curve.

    Returns:
        One ``ASSUMED`` degradation when the collateral rate is unverified,
        otherwise nothing.
    """
    index = collateral_index(currency)
    name = f"{currency.value.lower()}_collateral_rate"
    if name not in index.unresolved:
        return ()
    return (
        Degradation(
            code=f"unresolved_convention:{name}",
            message=_WHY[name],
            data_quality=DataQuality.ASSUMED,
        ),
    )


def merge_warnings(*groups: Iterable[Degradation]) -> tuple[Degradation, ...]:
    """Concatenate degradations, keeping the first of each code.

    A peso curve from the MXN bootstrap already carries the collateral
    assumption in its provenance; a hand-built peso curve does not. Merging
    by code reports it exactly once either way.

    Args:
        *groups: Degradations, in order of precedence.

    Returns:
        The merged tuple, first occurrence of each code kept.
    """
    seen: dict[str, Degradation] = {}
    for group in groups:
        for item in group:
            seen.setdefault(item.code, item)
    return tuple(seen.values())
