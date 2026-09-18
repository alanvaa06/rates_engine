"""Fetch series from FRED over HTTPS, with no key and no import-time network.

The network call is inside the function, and the standard library import that
performs it is inside the function too. Importing ``rates_engine`` must not
open a socket, and the test that enforces that
(``tests/test_import_side_effects.py``) would not catch a module-level
``urllib`` import that only *later* dials out — but a reader would, which is
the other reason it is written this way.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

from rates_engine.evidence import Provenance
from rates_engine.market.classify import classify_series
from rates_engine.market.snapshot import MarketSnapshot, Series

__all__ = ["FRED_CSV_URL", "fetch_series", "fetch_snapshot"]

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
"""CSV endpoint. No API key, one series per request."""


def fetch_series(series_id: str, *, timeout: float = 30.0) -> Series:
    """Download one FRED series.

    Args:
        series_id: FRED identifier, e.g. ``"SOFR"`` or ``"DGS2"``.
        timeout: Socket timeout in seconds.

    Returns:
        The :class:`~rates_engine.market.snapshot.Series`. Percent-quoted
        series — every rate FRED publishes — are divided by 100 so that the
        values are decimals throughout this package.

    Raises:
        OSError: The request failed. Not wrapped in a package exception: a
            network failure is the environment's, not the engine's, and the
            original error says more than a rewrapped one would.
    """
    from urllib.request import urlopen

    url = FRED_CSV_URL.format(series_id=series_id)
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https host
        payload = response.read().decode("utf-8")
    retrieved = datetime.now(UTC)

    rows: list[tuple[date, float]] = []
    reader = csv.reader(io.StringIO(payload))
    header = next(reader)
    value_index = 1 if len(header) > 1 else 0
    for row in reader:
        raw = row[value_index].strip()
        if raw in {"", ".", "NA"}:
            continue
        rows.append((date.fromisoformat(row[0].strip()), float(raw) / 100.0))
    rows.sort(key=lambda item: item[0])

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
    """
    return MarketSnapshot(
        as_of=as_of,
        series={sid: fetch_series(sid, timeout=timeout) for sid in series_ids},
    )
