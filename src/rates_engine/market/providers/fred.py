"""Fetch series from FRED over HTTPS, with no key and no import-time network.

The network call is inside the function, and the standard library import that
performs it is inside the function too. Importing ``rates_engine`` must not
open a socket, and the test that enforces that
(``tests/test_import_side_effects.py``) would not catch a module-level
``urllib`` import that only *later* dials out — but a reader would, which is
the other reason it is written this way.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from rates_engine.core.evidence import Provenance
from rates_engine.market.classify import classify_series
from rates_engine.market.providers.file import parse_series_csv
from rates_engine.market.snapshot import MarketSnapshot, Series

__all__ = ["FRED_CSV_URL", "fetch_series", "fetch_snapshot"]

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
"""CSV endpoint. No API key, one series per request."""


def fetch_series(series_id: str, *, timeout: float = 30.0, percent: bool | None = None) -> Series:
    """Download one FRED series.

    The response is read by :func:`~rates_engine.market.providers.file.parse_series_csv`,
    the same reader that loads a FRED CSV saved to disk, so the two come out
    identical.

    Args:
        series_id: FRED identifier, e.g. ``"SOFR"`` or ``"DGS2"``.
        timeout: Socket timeout in seconds.
        percent: Whether FRED publishes the series in percent, to be divided
            by 100 into decimals. ``None`` uses
            :func:`~rates_engine.market.classify.quoted_in_percent`: the
            overnight rates, the SOFR averages and the ``DGS*`` yields are in
            percent, ``SOFRINDEX`` is an index level and is not divided, and
            any other series has to say.

    Returns:
        The :class:`~rates_engine.market.snapshot.Series`, rates as decimals
        throughout this package.

    Raises:
        OSError: The request failed. Not wrapped in a package exception: a
            network failure is the environment's, not the engine's, and the
            original error says more than a rewrapped one would.
        UnsupportedConventionError: ``percent`` is ``None`` and the series is
            not one whose units this package knows. Every series used to be
            divided by 100, which is right for a rate and a hundredfold
            error for an index or an exchange rate.
        InsufficientDataError: The response holds no observations.
        ValueError: The response is not FRED's CSV of this series.
    """
    from urllib.request import urlopen

    url = FRED_CSV_URL.format(series_id=series_id)
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https host
        payload = response.read().decode("utf-8")
    retrieved = datetime.now(UTC)
    rows = parse_series_csv(
        payload, series_id, percent=percent, origin=f"FRED's response for {series_id!r}"
    )

    kind, quality, note = classify_series(series_id)
    return Series(
        series_id=series_id,
        dates=tuple(d for d, _ in rows),
        values=tuple(v for _, v in rows),
        provenance=Provenance(
            source="fred",
            series_id=series_id,
            retrieved_at=retrieved,
            instrument_kind=kind,
            data_quality=quality,
            notes=note,
        ),
    )


def fetch_snapshot(
    as_of: date, series_ids: tuple[str, ...], *, timeout: float = 30.0
) -> MarketSnapshot:
    """Download several series into one snapshot.

    Args:
        as_of: Valuation date the snapshot describes.
        series_ids: FRED identifiers to fetch.
        timeout: Per-request socket timeout in seconds.

    Returns:
        The snapshot, each series carrying ``source="fred"`` provenance.

    Raises:
        OSError: Any request failed.
        UnsupportedConventionError: A series whose units this package does
            not know. Fetch it with :func:`fetch_series` and ``percent=``,
            and build the :class:`MarketSnapshot` from the series.
        InsufficientDataError: A response held no observations.
    """
    return MarketSnapshot(
        as_of=as_of,
        series={sid: fetch_series(sid, timeout=timeout) for sid in series_ids},
    )
