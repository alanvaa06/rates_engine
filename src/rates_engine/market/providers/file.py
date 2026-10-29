"""Load series and futures settlements from CSV, with provenance from the file.

The file provider is what every test uses and what makes the suite offline.
Where the units of a file come from is the part that has to be exact, because
a wrong answer is a hundredfold error that looks like data, and at the rates
of 2020-22 no plausibility check can see it: SOFR at 0.05% read as a decimal
is a believable 5%.

``percent=`` decides whenever it is given, in every layout. Left ``None``,
the header decides, and only where it can:

* **FRED's download**, ``observation_date,<SERIES>`` in any case, BOM or
  not, is read in FRED's units for the series its value column names,
  whether the columns were named or found by default -- exactly as
  :func:`rates_engine.market.providers.fred.fetch_series` reads the same
  bytes off the wire. A series whose FRED units this package does not know,
  or a FRED column read under another series' name, is refused until
  ``percent=`` states the units.
* **This package's own layout**, ``date,value``, is decimals (5.31% is
  ``0.0531``).
* **FRED's older header**, ``DATE,<SERIES>`` in any case, is refused. It is
  FRED's download from before the column was renamed, in percent, and also
  the obvious header for a file of decimals, and the values cannot say which.
* **Any other named columns** are refused for a series known to be a rate,
  for the same reason, and read as written for anything else.

The first two versions each read the units off the header in a way the
next review turned around: a ``DATE,SOFR`` file of decimals divided by 100,
then FRED's own file with its columns named read a hundred times too large.
Refusing where the header cannot say is what closes both. When the units
were not stated, a series known to be a rate is still checked against
:data:`MAX_PLAUSIBLE_DECIMAL_RATE`, which catches a percent file in the
package's own layout at any rate above 0.30%.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum, auto
from pathlib import Path

from rates_engine.core.errors import (
    ImplausibleInputError,
    InsufficientDataError,
    UnsupportedConventionError,
)
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
its identifier, and is read in FRED's units unless ``percent=`` says
otherwise -- with the columns named or not. A file of decimals that borrows
the header says ``percent=False``.
"""

_OLD_FRED_DATE_COLUMN = "date"
"""First header of FRED's older downloads (``DATE``), compared case-insensitively.

Not taken for FRED's: ``DATE,<ID>`` is just as likely to head a file of
decimals, and at low rates the values cannot tell the two apart, so a file
with this header is refused until ``percent=`` states its units. The first
version took it for FRED's and divided a file of decimals by 100; the second
stopped recognising it, and FRED's old downloads stopped loading.
"""

_OWN_COLUMNS = ("date", "value")
"""This package's layout, and the default ``(date_column, value_column)``."""


class _Layout(Enum):
    """What a header says about the units of its values when ``percent=`` does not."""

    OWN = auto()
    """This package's ``date,value``: decimals."""

    FRED = auto()
    """FRED's download, ``observation_date,<ID>``: FRED's units for ``<ID>``."""

    OLD_FRED = auto()
    """``DATE,<ID>``: FRED's older download or a file of decimals, and no way to tell."""

    NAMED = auto()
    """Any other columns the caller named: nothing about the units."""


MAX_PLAUSIBLE_DECIMAL_RATE = 0.30
"""Largest decimal value a rate series is read at when its units were not stated.

30%, well above any US overnight rate or Treasury yield on record (the
effective fed funds rate peaked near 20% in 1980-81). A known rate
series -- one :func:`~rates_engine.market.classify.quoted_in_percent` says
FRED quotes in percent -- with a value above it was almost certainly read in
percent as decimals, and is refused; ``percent=`` states the units and turns
the check off. It is a net, not a measurement: a percent file whose rates
all sit below 0.30% (SOFR through much of 2020-22) reads as plausible
decimals and is not caught, which is why a rate is read without
``percent=`` only from a layout that says its units.
"""


def _parse_date(text: str) -> date:
    return date.fromisoformat(text.strip())


def _two_column_layout(names: list[str]) -> _Layout | None:
    """FRED's download or its older header, read off the header alone."""
    if len(names) != 2 or names[1].lower() == "value":
        return None
    first = names[0].lower()
    if first == FRED_DATE_COLUMN:
        return _Layout.FRED
    if first == _OLD_FRED_DATE_COLUMN:
        return _Layout.OLD_FRED
    return None


def _locate(
    names: list[str], *, date_column: str, value_column: str, series_id: str, origin: str
) -> tuple[int, int, _Layout]:
    """Where the date and value columns are, and the layout they sit in.

    Named columns that are FRED's two columns, in FRED's order, are FRED's
    layout (or its older one): naming them describes the same file, so it
    cannot change the units. That it did is the second review's finding.
    """
    header = _two_column_layout(names)
    defaults = (date_column, value_column) == _OWN_COLUMNS
    if date_column in names and value_column in names:
        date_index, value_index = names.index(date_column), names.index(value_column)
        if header is not None and (date_index, value_index) == (0, 1):
            return date_index, value_index, header
        return date_index, value_index, _Layout.OWN if defaults else _Layout.NAMED
    if header is not None and defaults:
        return 0, 1, header
    raise ValueError(
        f"{origin} has columns {names}; expected {date_column!r} and {value_column!r}, "
        f"or FRED's download layout `{FRED_DATE_COLUMN},{series_id}`. Name both columns "
        "with date_column= and value_column=, and state the units with percent=: "
        "reading a column that is not there gave an empty series rather than an error"
    )


def _units_from_layout(
    layout: _Layout, *, column: str, series_id: str, names: list[str], origin: str
) -> bool:
    """Whether to divide by 100 when ``percent=`` was left ``None``.

    Raises:
        UnsupportedConventionError: The layout cannot say the units of this
            series; the message names ``percent=True`` and ``percent=False``.
    """
    if layout is _Layout.OWN:
        return False
    if layout is _Layout.FRED:
        if column.upper() != series_id.strip().upper():
            raise UnsupportedConventionError(
                f"{origin} is FRED's download of {column!r}, read here as {series_id!r}. "
                f"FRED's units for {column!r} are not necessarily those of {series_id!r}, "
                "so state them: percent=True to divide the values by 100, percent=False "
                "to keep them as written"
            )
        return resolve_percent(column, None)
    if layout is _Layout.OLD_FRED:
        raise UnsupportedConventionError(
            f"{origin} has the header {','.join(names)!r}, which is both FRED's older "
            "download, in percent (0.05 for 0.05%), and a common layout for a file of "
            "decimals (0.0005), and the values cannot tell them apart: at the rates of "
            "2020-22, a rate in percent read as a decimal is a believable 1-5%. State the "
            "units: percent=True for a FRED download, percent=False for decimals. FRED's "
            f"current download, headed {FRED_DATE_COLUMN}, is read in FRED's units "
            "without it"
        )
    rate = next((name for name in (series_id, column) if quoted_in_percent(name)), None)
    if rate is not None:
        raise UnsupportedConventionError(
            f"{origin}: column {column!r}, read as {series_id!r}, holds a rate ({rate!r}, "
            f"which FRED quotes in percent), and its header {names} does not say the "
            "units. State them: percent=True if the values are in percent (5.31 for "
            "5.31%), percent=False if they are decimals (0.0531). The values cannot "
            "decide it, since a rate below 0.30% in percent reads as a believable "
            f"decimal; only FRED's two-column `{FRED_DATE_COLUMN},<ID>` and this "
            "package's `date,value` are read without percent="
        )
    return False


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
        series_id: Identifier the values belong to. With the default
            columns, a ``observation_date,<ID>`` or ``DATE,<ID>`` file's
            value column must be named after it.
        value_column: Name of the value column.
        date_column: Name of the date column, ISO format. With both columns
            left at their defaults and absent from the header, a two-column
            header of ``observation_date,<ID>`` or ``DATE,<ID>`` is found
            by position. Naming FRED's two columns finds the same file and
            reads it the same way.
        percent: ``True`` divides the values by 100 into decimal rates,
            ``False`` keeps them as written, in any layout. ``None`` lets
            the layout say, and refuses where it cannot: FRED's download
            (``observation_date,<ID>``, named or not) is read in FRED's
            units for ``<ID>``, from
            :func:`~rates_engine.market.classify.resolve_percent`; this
            package's ``date,value`` as decimals; ``DATE,<ID>`` is refused;
            other named columns are refused for a series known to be a rate
            and read as written otherwise.
        origin: What to call the text in error messages, e.g. a file name.

    Returns:
        ``(date, value)`` pairs, values as decimals unless ``percent=False``
        kept them as published. Rows marked missing are left out.

    Raises:
        ValueError: The header has neither the named columns nor a
            two-column FRED-shaped layout; such a layout, read with the
            default columns, holds another series; or a row's date or value
            cannot be parsed.
        InsufficientDataError: The text holds no observations at all -- it
            is empty, has only a header, or every value is marked missing.
            A file read as zero observations used to come back as an empty
            series, which is how a misnamed column went unnoticed.
        UnsupportedConventionError: ``percent=None`` and the layout cannot
            say the units: a FRED series whose units this package does not
            know, a FRED column read under another series' name, the
            ``DATE,<ID>`` header, or other named columns holding a rate.
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
    date_index, value_index, layout = _locate(
        names,
        date_column=date_column,
        value_column=value_column,
        series_id=series_id,
        origin=origin,
    )
    column = names[value_index]
    found_by_position = (date_column, value_column) == _OWN_COLUMNS
    if (
        layout in (_Layout.FRED, _Layout.OLD_FRED)
        and found_by_position
        and column.upper() != series_id.strip().upper()
    ):
        # Checked before the units, stated or not: a column found by
        # position is only the series asked for if it says so. Naming it
        # is how a deliberate relabelling is spelt.
        raise ValueError(
            f"{origin} holds {column!r} (header {names}), not {series_id!r}. Load it as "
            f"{column!r}; or, if the label is deliberate, name both columns and state "
            f"the units: date_column={names[0]!r}, value_column={column!r}, and "
            "percent=True if the values are in percent, percent=False if not"
        )
    if percent is None:
        divide = _units_from_layout(
            layout, column=column, series_id=series_id, names=names, origin=origin
        )
    else:
        divide = percent

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
    if percent is None and quoted_in_percent(series_id):
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
            into decimals; stated, it wins in every layout. ``None`` reads
            FRED's download (``observation_date,<ID>``, columns named or not)
            in FRED's units and this package's ``date,value`` as decimals,
            and refuses ``DATE,<ID>`` and any other named columns holding a
            rate, as :func:`parse_series_csv` says.

    Returns:
        The :class:`~rates_engine.market.snapshot.Series`, with provenance
        naming the file and the time it was read. Values are decimal rates
        unless ``percent=False`` kept a FRED file as published.

    Raises:
        FileNotFoundError: The path does not exist.
        ValueError: The columns are not there, a column found by position
            holds another series, a row's date or value cannot be parsed,
            or a value is infinite; the message names the line or the date.
        InsufficientDataError: The file yields no observations.
        UnsupportedConventionError: ``percent`` left ``None`` and a layout
            that cannot say the units: a FRED series whose units this
            package does not know, a FRED column read under another name,
            ``DATE,<ID>``, or other named columns holding a rate.
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
            ``None`` lets each file's layout say, and refuses a file whose
            layout cannot.

    Returns:
        The :class:`~rates_engine.market.snapshot.MarketSnapshot`.

    Raises:
        ValueError: A file's columns or rows cannot be read.
        InsufficientDataError: A file yields no observations.
        UnsupportedConventionError: ``percent`` left ``None`` and a file
            whose layout cannot say its units, as in :func:`load_series_csv`.
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
