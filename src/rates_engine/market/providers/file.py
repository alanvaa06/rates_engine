"""Load series and futures settlements from CSV, with provenance from the file.

The file provider is what every test uses and what makes the suite offline.
It reads two series layouts, and the units are part of the layout:

* this package's own, ``date,value``, in the decimals the rest of the package
  uses (5.31% is ``0.0531``) -- the fixtures are written this way;
* FRED's download, ``observation_date,<SERIES>``, in FRED's units, read by
  :func:`parse_series_csv` exactly as
  :func:`rates_engine.market.providers.fred.fetch_series` reads the same
  bytes off the wire. A file saved from FRED and a download are therefore
  the same series, which this docstring claimed before it was true: the
  file used to come back empty with the default columns, and a hundred
  times too large with FRED's columns named.

``percent=`` overrides either. A series FRED publishes in units this package
does not know is refused until they are stated, rather than divided by 100
on a guess.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from rates_engine.core.errors import InsufficientDataError
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.market.classify import classify_series, resolve_percent
from rates_engine.market.snapshot import MarketSnapshot, Series

__all__ = [
    "MISSING_VALUE_MARKERS",
    "FRED_DATE_COLUMNS",
    "is_missing_value",
    "parse_series_csv",
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


FRED_DATE_COLUMNS = ("observation_date", "DATE")
"""First-column headers of a FRED download: today's, and the one it used before.

A two-column file whose first header is one of these, and whose second is not
``value``, is read in FRED's layout: the second column is the series, named
after its identifier, in FRED's units.
"""


def _parse_date(text: str) -> date:
    return date.fromisoformat(text.strip())


def _is_fred_layout(names: list[str]) -> bool:
    return len(names) == 2 and names[0] in FRED_DATE_COLUMNS and names[1].lower() != "value"


def parse_series_csv(
    text: str,
    series_id: str,
    *,
    value_column: str = "value",
    date_column: str = "date",
    percent: bool | None = None,
    origin: str = "the CSV",
) -> tuple[tuple[date, float], ...]:
    """Observations of one series from CSV text, in date order.

    The one reader behind both :func:`load_series_csv` and
    :func:`rates_engine.market.providers.fred.fetch_series`, so that a file
    and a download of the same bytes cannot disagree.

    Args:
        text: The CSV, header first.
        series_id: Identifier the values belong to. In FRED's layout the
            value column must be named after it.
        value_column: Name of the value column. Left at ``"value"`` with no
            such column, FRED's layout is recognised from the header.
        date_column: Name of the date column, ISO format.
        percent: ``True`` divides the values by 100 into decimal rates,
            ``False`` keeps them as written. ``None``: this package's
            ``date,value`` layout is already in decimals, and FRED's layout is
            in the units FRED publishes the series in, from
            :func:`~rates_engine.market.classify.resolve_percent`.
        origin: What to call the text in error messages, e.g. a file name.

    Returns:
        ``(date, value)`` pairs, values as decimals unless ``percent=False``
        kept them as published. Rows marked missing are left out.

    Raises:
        ValueError: The header has neither the named columns nor FRED's
            layout, FRED's value column names another series, or a row's
            date or value cannot be parsed.
        InsufficientDataError: The text holds no observations at all -- it
            is empty, has only a header, or every value is marked missing.
            A file read as zero observations used to come back as an empty
            series, which is how a misnamed column went unnoticed.
        UnsupportedConventionError: FRED's layout, ``percent=None``, and a
            series whose units this package does not know.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        raise InsufficientDataError(
            f"{origin} is empty: no header and no observations of {series_id!r}"
        )
    names = [name.strip() for name in header]
    fred_layout = _is_fred_layout(names)
    if date_column in names and value_column in names:
        date_index, value_index = names.index(date_column), names.index(value_column)
    elif fred_layout and (date_column, value_column) == ("date", "value"):
        date_index, value_index = 0, 1
        if names[1].upper() != series_id.strip().upper():
            raise ValueError(
                f"{origin} is FRED's download of {names[1]!r}, not of {series_id!r}. Load it "
                f"under its own identifier, or name value_column={names[1]!r} if the label "
                "is deliberate"
            )
    else:
        raise ValueError(
            f"{origin} has columns {names}; expected {date_column!r} and {value_column!r}, "
            f"or FRED's download layout `observation_date,{series_id}`. Name the columns "
            "with date_column= and value_column=: reading a column that is not there gave "
            "an empty series rather than an error"
        )
    divide = resolve_percent(series_id, percent) if fred_layout else bool(percent)

    rows: list[tuple[date, float]] = []
    data_rows = 0
    for line, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        data_rows += 1
        raw = row[value_index].strip() if value_index < len(row) else ""
        if is_missing_value(raw):
            continue
        try:
            day = _parse_date(row[date_index])
            value = float(raw)
        except (IndexError, ValueError) as exc:
            raise ValueError(
                f"{origin} line {line}: cannot read {row!r} as an ISO date and a number "
                f"in columns {names[date_index]!r} and {names[value_index]!r}"
            ) from exc
        rows.append((day, value / 100.0 if divide else value))
    if not rows:
        raise InsufficientDataError(
            f"{origin} has {data_rows} data rows and not one observation of {series_id!r} "
            f"in column {names[value_index]!r}: every value is empty or marked missing. "
            "An empty series is not returned in its place"
        )
    rows.sort(key=lambda item: item[0])
    return tuple(rows)


def load_series_csv(
    path: str | Path,
    series_id: str,
    *,
    value_column: str = "value",
    date_column: str = "date",
    data_quality: DataQuality | None = None,
    percent: bool | None = None,
) -> Series:
    """Read one series from a two-column CSV.

    Args:
        path: CSV file with a date column and a value column: this package's
            ``date,value`` layout, or FRED's download as saved. Rows whose
            value is a :data:`MISSING_VALUE_MARKERS` spelling -- empty,
            FRED's ``"."``, ``nan``, ``NA``, ``N/A`` in any case -- are
            skipped rather than read as zero or as NaN.
        series_id: Identifier to record on the series.
        value_column: Name of the value column.
        date_column: Name of the date column, ISO format.
        data_quality: Override the classification. Left ``None``, the quality
            comes from :func:`~rates_engine.market.classify.classify_series`,
            which marks ``DGS*`` as a proxy.
        percent: Whether the values are in percent, to be divided by 100
            into decimals. ``None`` reads this package's layout as decimals
            and FRED's in FRED's units, as :func:`parse_series_csv` says.

    Returns:
        The :class:`~rates_engine.market.snapshot.Series`, with provenance
        naming the file and the time it was read. Values are decimal rates
        unless ``percent=False`` kept a FRED file as published.

    Raises:
        FileNotFoundError: The path does not exist.
        ValueError: The columns are not there, a row's date or value cannot
            be parsed, or a value is infinite; the message names the line or
            the date.
        InsufficientDataError: The file yields no observations.
        UnsupportedConventionError: A FRED file of a series whose units this
            package does not know, with ``percent`` left ``None``.
    """
    path = Path(path)
    kind, quality, note = classify_series(series_id)
    rows = parse_series_csv(
        path.read_text(encoding="utf-8-sig"),
        series_id,
        value_column=value_column,
        date_column=date_column,
        percent=percent,
        origin=path.name,
    )
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
    percent: bool | None = None,
) -> MarketSnapshot:
    """Build a snapshot from one CSV per series.

    Args:
        as_of: Valuation date the snapshot describes.
        files: Series identifier to CSV path.
        value_column: Value column name shared by the files.
        date_column: Date column name shared by the files.
        percent: Units shared by the files, as in :func:`load_series_csv`;
            ``None`` lets each file's layout say.

    Returns:
        The :class:`~rates_engine.market.snapshot.MarketSnapshot`.

    Raises:
        ValueError: A file's columns or rows cannot be read.
        InsufficientDataError: A file yields no observations.
        UnsupportedConventionError: A FRED file of unknown units, with
            ``percent`` left ``None``.
    """
    return MarketSnapshot(
        as_of=as_of,
        series={
            name: load_series_csv(
                path,
                name,
                value_column=value_column,
                date_column=date_column,
                percent=percent,
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
