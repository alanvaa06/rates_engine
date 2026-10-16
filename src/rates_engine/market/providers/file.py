"""Load series and futures settlements from CSV, with provenance from the file.

The file provider is what every test uses and what makes the suite offline.
The CSVs it reads are the same shape FRED and CME publish, so a fixture and a
download are interchangeable.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.market.classify import classify_series
from rates_engine.market.snapshot import MarketSnapshot, Series

__all__ = [
    "MISSING_VALUE_MARKERS",
    "is_missing_value",
    "load_series_csv",
    "load_snapshot_csv",
    "FuturesSettlement",
    "load_settlements_csv",
]

MISSING_VALUE_MARKERS = frozenset({"", ".", "na", "n/a", "nan"})
"""How a CSV spells "no observation", compared stripped and case-insensitively.

``"."`` is FRED's, ``"nan"`` is what ``str(float("nan"))`` and numpy write,
``"NA"`` and ``"N/A"`` are R's and Excel's. The set used to be ``{"", ".",
"NA", "NaN"}``, case-sensitive, so a lower-case ``nan`` was read as a NaN
fixing and compounded into a NaN settlement. Anything else that parses as a
non-finite number is refused by :class:`~rates_engine.market.snapshot.Series`
rather than skipped: ``inf`` is not a way of saying a day is missing.
"""


def is_missing_value(raw: str) -> bool:
    """Whether a CSV cell says the day has no observation.

    Args:
        raw: The cell's text, as read.

    Returns:
        ``True`` for every spelling in :data:`MISSING_VALUE_MARKERS`, in any
        case and with surrounding whitespace.
    """
    return raw.strip().lower() in MISSING_VALUE_MARKERS


def _parse_date(text: str) -> date:
    return date.fromisoformat(text.strip())


def load_series_csv(
    path: str | Path,
    series_id: str,
    *,
    value_column: str = "value",
    date_column: str = "date",
    data_quality: DataQuality | None = None,
) -> Series:
    """Read one series from a two-column CSV.

    Args:
        path: CSV file with a date column and a value column. Rows whose value
            is a :data:`MISSING_VALUE_MARKERS` spelling -- empty, FRED's
            ``"."``, ``nan``, ``NA``, ``N/A`` in any case -- are skipped
            rather than read as zero or as NaN.
        series_id: Identifier to record on the series.
        value_column: Name of the value column.
        date_column: Name of the date column, ISO format.
        data_quality: Override the classification. Left ``None``, the quality
            comes from :func:`~rates_engine.market.classify.classify_series`,
            which marks ``DGS*`` as a proxy.

    Returns:
        The :class:`~rates_engine.market.snapshot.Series`, with provenance
        naming the file and the time it was read.

    Raises:
        FileNotFoundError: The path does not exist.
        ValueError: A row's date or value cannot be parsed, or a value is
            infinite; the message names the date.
    """
    path = Path(path)
    kind, quality, note = classify_series(series_id)
    rows: list[tuple[date, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get(value_column) or "").strip()
            if is_missing_value(raw):
                continue
            rows.append((_parse_date(row[date_column]), float(raw)))
    rows.sort(key=lambda item: item[0])
    return Series(
        series_id=series_id,
        dates=tuple(d for d, _ in rows),
        values=tuple(v for _, v in rows),
        provenance=Provenance(
            source="file",
            series_id=series_id,
            retrieved_at=datetime.now(UTC),
            instrument_kind=kind,
            data_quality=data_quality or quality,
            notes=note or f"read from {path.name}",
        ),
    )


def load_snapshot_csv(
    as_of: date,
    files: dict[str, str | Path],
    *,
    value_column: str = "value",
    date_column: str = "date",
) -> MarketSnapshot:
    """Build a snapshot from one CSV per series.

    Args:
        as_of: Valuation date the snapshot describes.
        files: Series identifier to CSV path.
        value_column: Value column name shared by the files.
        date_column: Date column name shared by the files.

    Returns:
        The :class:`~rates_engine.market.snapshot.MarketSnapshot`.
    """
    return MarketSnapshot(
        as_of=as_of,
        series={
            name: load_series_csv(
                path, name, value_column=value_column, date_column=date_column
            )
            for name, path in files.items()
        },
    )


@dataclass(frozen=True)
class FuturesSettlement:
    """One futures contract's settlement, as published.

    Attributes:
        symbol: Contract root, ``"SR1"`` or ``"SR3"``.
        contract_month: First day of the contract month, used as its label.
        settlement_price: Published price, ``100 - rate`` in percent.
        settlement_date: Date the price is as of.
        provenance: Where the row came from.
    """

    symbol: str
    contract_month: date
    settlement_price: float
    settlement_date: date
    provenance: Provenance

    @property
    def implied_rate(self) -> float:
        """The rate the price implies, as a decimal. ``(100 - price) / 100``."""
        return (100.0 - self.settlement_price) / 100.0


def load_settlements_csv(path: str | Path) -> tuple[FuturesSettlement, ...]:
    """Read futures settlements from a CSV in the CME public layout.

    Args:
        path: CSV with columns ``symbol``, ``contract_month``,
            ``settlement_price`` and ``settlement_date``.

    Returns:
        The settlements, ordered by contract month.

    Raises:
        FileNotFoundError: The path does not exist.
        KeyError: A required column is missing.
    """
    path = Path(path)
    retrieved = datetime.now(UTC)
    out: list[FuturesSettlement] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = row["symbol"].strip().upper()
            out.append(
                FuturesSettlement(
                    symbol=symbol,
                    contract_month=_parse_date(row["contract_month"]),
                    settlement_price=float(row["settlement_price"]),
                    settlement_date=_parse_date(row["settlement_date"]),
                    provenance=Provenance(
                        source="file",
                        series_id=f"{symbol}:{row['contract_month'].strip()}",
                        retrieved_at=retrieved,
                        instrument_kind="futures_settlement",
                        data_quality=DataQuality.OBSERVED,
                        notes=f"read from {path.name}",
                    ),
                )
            )
    out.sort(key=lambda s: (s.symbol, s.contract_month))
    return tuple(out)
