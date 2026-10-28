"""Load series and futures settlements from CSV, with provenance from the file.

The file provider is what every test uses and what makes the suite offline.
Where the units of a file come from is the part that has to be exact, because
a wrong answer is a hundredfold error that looks like data:

* **Columns named by the caller** -- including this package's own
  ``date,value`` -- are read in the units ``percent=`` states, decimals when
  it is left ``None`` (5.31% is ``0.0531``). Nothing is inferred from the
  header: the first version took a ``DATE,SOFR`` decimal file for FRED's
  layout and divided it by 100, while ``date,SOFR`` came back right.
* **FRED's download as saved**, recognised only when the default columns are
  absent and the header is ``observation_date,<SERIES>`` (any case, BOM or
  not), is read in FRED's units for the series that column names, exactly
  as :func:`rates_engine.market.providers.fred.fetch_series` reads the same
  bytes off the wire. A series whose FRED units this package does not know
  is refused until ``percent=`` states them.

Whenever the units were not stated, a series known to be a rate is checked
against :data:`MAX_PLAUSIBLE_DECIMAL_RATE`, so a percent file read as
decimals -- the first review's FRED download with its columns named came back
at 3.64 instead of 0.0364, marked observed -- is refused with the argument
that fixes it.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from rates_engine.core.errors import ImplausibleInputError, InsufficientDataError
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.market.classify import classify_series, quoted_in_percent, resolve_percent
from rates_engine.market.snapshot import MarketSnapshot, Series

__all__ = [
    "MISSING_VALUE_MARKERS",
    "FRED_DATE_COLUMN",
    "MAX_PLAUSIBLE_DECIMAL_RATE",
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
non-finite number is refused, naming the line and the date, rather than
skipped: ``inf`` is not a way of saying a day is missing.
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


FRED_DATE_COLUMN = "observation_date"
"""First-column header of a FRED download, compared case-insensitively.

A two-column file whose first header is this, and whose second is not
``value``, is FRED's download: the second column is the series, named after
its identifier, in FRED's units. FRED's older ``DATE`` header is not
recognised on purpose: ``DATE,<ID>`` is as likely to be anyone's decimal
file, and recognising it is how the first version divided one by 100.
"""

MAX_PLAUSIBLE_DECIMAL_RATE = 0.30
"""Largest decimal value a rate series is read at when its units were not stated.

30%, well above any US overnight rate or Treasury yield on record (the
effective fed funds rate peaked near 20% in 1980-81). A known rate
series -- one :func:`~rates_engine.market.classify.quoted_in_percent` says
FRED quotes in percent -- with a value above it was almost certainly read in
percent as decimals, and is refused; ``percent=`` states the units and turns
the check off. It is a net, not a measurement: a percent file whose rates
all sit below 0.30% (SOFR in 2020-21) reads as plausible decimals and is not
caught, which is why the units are an argument.
"""


def _parse_date(text: str) -> date:
    return date.fromisoformat(text.strip())


def _is_fred_layout(names: list[str]) -> bool:
    return (
        len(names) == 2
        and names[0].lower() == FRED_DATE_COLUMN
        and names[1].lower() != "value"
    )


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
        text: The CSV, header first. A leading byte-order mark is dropped,
            so the same bytes read the same whether they were decoded with
            ``utf-8`` off the wire or ``utf-8-sig`` off disk.
        series_id: Identifier the values belong to. Read as FRED's download,
            the value column must be named after it.
        value_column: Name of the value column.
        date_column: Name of the date column, ISO format. With both columns
            left at their defaults and absent from the header, a header of
            ``observation_date,<SERIES>`` is read as FRED's download.
        percent: ``True`` divides the values by 100 into decimal rates,
            ``False`` keeps them as written. ``None``: columns that are in
            the header are read as decimals, and FRED's download in the
            units FRED publishes the series in, from
            :func:`~rates_engine.market.classify.resolve_percent` applied to
            the column's identifier.
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
        UnsupportedConventionError: FRED's download, ``percent=None``, and a
            series whose units this package does not know.
        ImplausibleInputError: The units were not stated, the series is a
            rate FRED quotes in percent, and a value read as a decimal is
            above :data:`MAX_PLAUSIBLE_DECIMAL_RATE`: it was in percent.
    """
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff")))
    header = next(reader, None)
    if header is None:
        raise InsufficientDataError(
            f"{origin} is empty: no header and no observations of {series_id!r}"
        )
    names = [name.strip() for name in header]
    if date_column in names and value_column in names:
        # The caller described the file, or it is this package's own
        # layout: the units are percent= and nothing is read from the
        # header's spelling.
        date_index, value_index = names.index(date_column), names.index(value_column)
        identifier = series_id
        divide = bool(percent)
    elif _is_fred_layout(names) and (date_column, value_column) == ("date", "value"):
        date_index, value_index = 0, 1
        identifier = names[1]
        if identifier.upper() != series_id.strip().upper():
            raise ValueError(
                f"{origin} is FRED's download of {identifier!r}, not of {series_id!r}. Load "
                f"it as {identifier!r}; or, if the label is deliberate, name both columns "
                f"and state the units: date_column={names[0]!r}, "
                f"value_column={identifier!r}, and percent=True if FRED quotes it in "
                "percent, percent=False if not"
            )
        divide = resolve_percent(identifier, percent)
    else:
        raise ValueError(
            f"{origin} has columns {names}; expected {date_column!r} and {value_column!r}, "
            f"or FRED's download layout `{FRED_DATE_COLUMN},{series_id}`. Name both columns "
            "with date_column= and value_column=, and state the units with percent=: "
            "reading a column that is not there gave an empty series rather than an error"
        )

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
        if not math.isfinite(value):
            raise ValueError(
                f"{origin} line {line}: {raw!r} on {day} is not a finite number. A missing "
                f"observation is written as one of {sorted(MISSING_VALUE_MARKERS)}"
            )
        rows.append((day, value / 100.0 if divide else value))
    if not rows:
        raise InsufficientDataError(
            f"{origin} has {data_rows} data rows and not one observation of {series_id!r} "
            f"in column {names[value_index]!r}: every value is empty or marked missing. "
            "An empty series is not returned in its place"
        )
    if percent is None and quoted_in_percent(identifier):
        day, value = max(rows, key=lambda item: abs(item[1]))
        if abs(value) > MAX_PLAUSIBLE_DECIMAL_RATE:
            raise ImplausibleInputError(
                f"{origin} gives {series_id!r} {value!r} on {day}, which as a decimal rate is "
                f"{value:.0%}: above the {MAX_PLAUSIBLE_DECIMAL_RATE:.0%} band, so the file is "
                "almost certainly in percent. Pass percent=True to divide it by 100, or "
                "percent=False if these really are the values you mean"
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
            into decimals. ``None`` reads named columns (and this package's
            ``date,value``) as decimals and FRED's download in FRED's units,
            as :func:`parse_series_csv` says.

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
        ImplausibleInputError: ``percent`` left ``None`` and a rate series
            with a value above :data:`MAX_PLAUSIBLE_DECIMAL_RATE` as a
            decimal: the file is in percent.
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
        ImplausibleInputError: ``percent`` left ``None`` and a rate file
            that is evidently in percent.
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
