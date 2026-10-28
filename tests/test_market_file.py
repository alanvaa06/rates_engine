"""PRD-001 AC-2.2, 2.3, 2.4 and 2.6: loading data, refusing to invent it, and the proxy label."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rates_engine.conventions.calendar import SIFMA_US
from rates_engine.core.errors import (
    ImplausibleInputError,
    InsufficientDataError,
    MissingFixingError,
    UnsupportedConventionError,
)
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.market.classify import TREASURY_PAR_YIELD
from rates_engine.market.providers.file import (
    load_series_csv,
    load_settlements_csv,
    load_snapshot_csv,
)
from rates_engine.market.snapshot import MarketSnapshot, Series


@pytest.fixture(scope="module")
def late_snapshot(fixtures_dir) -> MarketSnapshot:
    """The fixture fixings as of their own last date, for periods past the suite's.

    The shared ``snapshot`` is as of 2026-01-15, and a snapshot serves nothing
    dated after its valuation date. A test about weekends and holidays in late
    January needs one dated after them, not one that leaks the future.
    """
    series = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
    return MarketSnapshot(as_of=series.dates[-1], series={"SOFR": series})


class TestLoading:
    """PRD-001 AC-2.2: a row on disk becomes a value with a source attached."""

    def test_series_loads_with_provenance(self, fixtures_dir):
        series = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
        assert len(series) > 1000
        assert series.provenance.source == "file"
        assert series.provenance.series_id == "SOFR"
        assert series.provenance.retrieved_at is not None
        assert series.provenance.instrument_kind == "overnight_fixing"

    def test_dates_are_strictly_increasing(self, fixtures_dir):
        series = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
        assert all(b > a for a, b in zip(series.dates, series.dates[1:], strict=False))

    def test_snapshot_exposes_provenance_per_series(self, fixtures_dir):
        snap = load_snapshot_csv(date(2026, 1, 15), {"SOFR": fixtures_dir / "sofr_fixings.csv"})
        assert set(snap.provenance) == {"SOFR"}

    def test_settlements_load_with_contract_month_and_date(self, tmp_path):
        path = tmp_path / "settlements.csv"
        path.write_text(
            "symbol,contract_month,settlement_price,settlement_date\n"
            "SR3,2025-03-01,95.8500,2025-06-18\n"
            "SR1,2025-04-01,95.6700,2025-04-30\n",
            encoding="utf-8",
        )
        settlements = load_settlements_csv(path)
        assert [s.symbol for s in settlements] == ["SR1", "SR3"]
        assert settlements[1].contract_month == date(2025, 3, 1)
        assert settlements[1].settlement_date == date(2025, 6, 18)
        assert settlements[1].implied_rate == pytest.approx(0.0415)
        assert all(s.provenance.source == "file" for s in settlements)

    def test_missing_values_are_skipped_not_read_as_zero(self, tmp_path):
        path = tmp_path / "gappy.csv"
        path.write_text(
            "date,value\n2026-01-05,0.0431\n2026-01-06,.\n2026-01-07,0.0432\n", encoding="utf-8"
        )
        series = load_series_csv(path, "SOFR")
        assert len(series) == 2
        assert series.get(date(2026, 1, 6)) is None

    @pytest.mark.parametrize(
        "marker", ["", ".", " . ", "nan", "NaN", "NAN", "na", "NA", "n/a", "N/A"]
    )
    def test_every_spelling_of_missing_is_skipped(self, tmp_path, marker):
        # "nan" is what str(float("nan")) and numpy write; "." is FRED's;
        # "NA" and "N/A" are R's and Excel's. Lower-case "nan" used to load
        # as a NaN fixing and compound into a NaN settlement with no refusal.
        path = tmp_path / "gappy.csv"
        path.write_text(
            f"date,value\n2026-01-05,0.0431\n2026-01-06,{marker}\n2026-01-07,0.0432\n",
            encoding="utf-8",
        )
        series = load_series_csv(path, "SOFR")
        assert series.dates == (date(2026, 1, 5), date(2026, 1, 7))

    @pytest.mark.parametrize("value", ["inf", "-inf", "Infinity"])
    def test_an_infinite_value_refuses_and_names_the_date(self, tmp_path, value):
        path = tmp_path / "infinite.csv"
        path.write_text(f"date,value\n2026-01-05,0.0431\n2026-01-06,{value}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="2026-01-06"):
            load_series_csv(path, "SOFR")


#: The review's FRED download, as fredgraph.csv writes it: in percent.
FRED_CSV = "observation_date,SOFR\n2026-01-12,3.64\n2026-01-13,.\n2026-01-14,3.65\n"

#: Two days of SOFR at a few basis points, the level of much of 2020-22, as
#: FRED writes them: in percent (Alan's second review used these values).
#: Read as decimals they are a believable 1% and 5%, so the 30% band cannot
#: tell the two readings apart; only the layout, or ``percent=``, can.
LOW_RATE_ROWS = "2021-06-01,0.01\n2021-06-02,0.05\n"
LOW_RATES_AS_DECIMALS = (pytest.approx(0.0001), pytest.approx(0.0005))
LOW_RATES_AS_WRITTEN = (0.01, 0.05)

#: The whole units rule, one row per layout: the header over LOW_RATE_ROWS,
#: the columns named (None for the defaults), and what ``percent=None``
#: does. ``percent=True`` divides and ``percent=False`` keeps, in every row.
UNITS_RULE = [
    ("observation_date,SOFR", None, "divide"),
    ("observation_date,SOFR", ("observation_date", "SOFR"), "divide"),
    ("OBSERVATION_DATE,sofr", None, "divide"),
    ("DATE,SOFR", None, "refuse"),
    ("DATE,SOFR", ("DATE", "SOFR"), "refuse"),
    ("date,SOFR", None, "refuse"),
    ("date,SOFR", ("date", "SOFR"), "refuse"),
    ("observation_date,EFFR", ("observation_date", "EFFR"), "refuse"),
    ("day,rate", ("day", "rate"), "refuse"),
    ("date,value", None, "keep"),
    ("date,value", ("date", "value"), "keep"),
]


class TestUnits:
    """The units a file is read in are stated or known, never assumed by accident."""

    @pytest.mark.parametrize(
        ("header", "columns", "unstated"),
        UNITS_RULE,
        ids=[f"{h}-{'named' if c else 'default'}-{u}" for h, c, u in UNITS_RULE],
    )
    def test_the_units_rule_at_2021_s_rates(self, tmp_path, header, columns, unstated):
        # Two rounds turned this rule around, each time in a case the tests
        # did not hold at a low rate. Every layout is held here at 1 and 5
        # bp, where a percent file and a decimal one both look plausible.
        path = tmp_path / "rates.csv"
        path.write_text(f"{header}\n{LOW_RATE_ROWS}", encoding="utf-8")
        named = {} if columns is None else {"date_column": columns[0], "value_column": columns[1]}

        def load(**units):
            return load_series_csv(path, "SOFR", **named, **units).values

        assert load(percent=True) == LOW_RATES_AS_DECIMALS
        assert load(percent=False) == LOW_RATES_AS_WRITTEN
        if unstated == "refuse":
            with pytest.raises(UnsupportedConventionError, match="percent=True"):
                load()
        else:
            expected = LOW_RATES_AS_DECIMALS if unstated == "divide" else LOW_RATES_AS_WRITTEN
            assert load() == expected

    def test_a_fred_download_loads_in_decimals_with_the_default_columns(self, tmp_path):
        # Used to load zero observations, without an error.
        path = tmp_path / "SOFR.csv"
        path.write_text(FRED_CSV, encoding="utf-8")
        series = load_series_csv(path, "SOFR")
        assert series.dates == (date(2026, 1, 12), date(2026, 1, 14))
        assert series.values == (pytest.approx(0.0364), pytest.approx(0.0365))

    @pytest.mark.parametrize(
        "header", ["\ufeffobservation_date,SOFR", "OBSERVATION_DATE,SOFR", "Observation_Date,sofr"]
    )
    def test_fred_s_header_is_recognised_in_any_case_and_after_a_bom(self, tmp_path, header):
        path = tmp_path / "SOFR.csv"
        path.write_text(f"{header}\n2026-01-12,3.64\n", encoding="utf-8")
        assert load_series_csv(path, "SOFR").values == (pytest.approx(0.0364),)

    @pytest.mark.parametrize(
        "named",
        [{}, {"date_column": "observation_date", "value_column": "SOFR"}],
        ids=["default-columns", "named-columns"],
    )
    def test_fred_s_layout_is_read_in_fred_s_units_named_or_not(self, tmp_path, named):
        # Alan's second review: round two read FRED's download with its
        # columns named as decimals, so 2021's 1 and 5 bp came back as a 1%
        # and a 5% SOFR, observed, below anything the 30% band can see. The
        # header is FRED's, so FRED's units apply however the columns were
        # found: (0.0001, 0.0005), as fred.fetch_series reads the same bytes.
        path = tmp_path / "SOFR.csv"
        path.write_text(f"observation_date,SOFR\n{LOW_RATE_ROWS}", encoding="utf-8")
        series = load_series_csv(path, "SOFR", **named)
        assert series.values == LOW_RATES_AS_DECIMALS
        assert series.provenance.data_quality is DataQuality.OBSERVED
        path.write_text(FRED_CSV, encoding="utf-8")
        assert load_series_csv(path, "SOFR", **named).values == (
            pytest.approx(0.0364),
            pytest.approx(0.0365),
        )

    def test_a_decimal_file_under_fred_s_header_states_percent_false(self, tmp_path):
        # The other side of the rule above: observation_date,<ID> means
        # FRED's units, so a file of decimals that borrows FRED's header is
        # divided by 100 unless it says otherwise. This is the price of
        # reading FRED's download right; the header is FRED's own spelling.
        path = tmp_path / "decimal.csv"
        path.write_text("observation_date,SOFR\n2026-01-02,0.0387\n", encoding="utf-8")
        assert load_series_csv(path, "SOFR").values == (pytest.approx(0.000387),)
        assert load_series_csv(path, "SOFR", percent=False).values == (0.0387,)

    @pytest.mark.parametrize("date_header", ["DATE", "date", "Date"])
    @pytest.mark.parametrize("named", [False, True], ids=["default-columns", "named-columns"])
    def test_the_older_date_header_asks_for_the_units(self, tmp_path, date_header, named):
        # Alan's second review, finding 2: a FRED download saved before FRED
        # renamed its first column (DATE,SOFR, in percent) stopped loading,
        # and naming its columns read it as decimals: 0.01 and 0.05, a
        # hundred times too large. DATE,<ID> is also the obvious header for
        # a file of decimals, and at 2021's rates the values cannot say which
        # it is, so with or without named columns the units are asked for.
        path = tmp_path / "SOFR.csv"
        path.write_text(f"{date_header},SOFR\n{LOW_RATE_ROWS}", encoding="utf-8")
        columns = {"date_column": date_header, "value_column": "SOFR"} if named else {}
        with pytest.raises(UnsupportedConventionError) as excinfo:
            load_series_csv(path, "SOFR", **columns)
        message = str(excinfo.value)
        assert "percent=True" in message and "percent=False" in message
        assert "observation_date" in message
        decimals = load_series_csv(path, "SOFR", percent=True, **columns).values
        assert decimals == LOW_RATES_AS_DECIMALS
        written = load_series_csv(path, "SOFR", percent=False, **columns).values
        assert written == LOW_RATES_AS_WRITTEN

    @pytest.mark.parametrize(
        ("text", "series_id", "date_column", "value_column"),
        [
            (f"day,rate\n{LOW_RATE_ROWS}", "SOFR", "day", "rate"),
            (f"day,SOFR\n{LOW_RATE_ROWS}", "MY_LABEL", "day", "SOFR"),
            (
                "date,DGS1MO,DGS10\n2021-06-01,0.01,1.60\n2021-06-02,0.05,1.59\n",
                "DGS1MO",
                "date",
                "DGS1MO",
            ),
            (
                "observation_date,SOFR,EFFR\n2021-06-01,0.01,0.06\n2021-06-02,0.05,0.08\n",
                "SOFR",
                "observation_date",
                "SOFR",
            ),
        ],
        ids=["own-names", "rate-column-under-a-label", "treasury-table", "fred-multi-series"],
    )
    def test_named_columns_of_a_rate_in_another_layout_ask_for_the_units(
        self, tmp_path, text, series_id, date_column, value_column
    ):
        # Alan's alternative fix: a series known to be a rate, read from
        # columns whose layout does not say the units, is refused until
        # percent= states them. FRED's own two-column download is the one
        # layout that says (the test above); a table of several series is
        # not it, even when it is FRED's.
        path = tmp_path / "rates.csv"
        path.write_text(text, encoding="utf-8")
        columns = {"date_column": date_column, "value_column": value_column}
        with pytest.raises(UnsupportedConventionError) as excinfo:
            load_series_csv(path, series_id, **columns)
        message = str(excinfo.value)
        assert "percent=True" in message and "percent=False" in message
        decimals = load_series_csv(path, series_id, percent=True, **columns).values
        assert decimals == LOW_RATES_AS_DECIMALS
        written = load_series_csv(path, series_id, percent=False, **columns).values
        assert written == LOW_RATES_AS_WRITTEN

    def test_named_columns_of_a_series_not_known_to_be_a_rate_are_read_as_written(self, tmp_path):
        # Only a known rate has a percent reading to confuse with a decimal
        # one. An index level, or a series this package does not classify,
        # is kept as written, as it always was.
        path = tmp_path / "level.csv"
        path.write_text("day,level\n2021-06-01,1.04\n2021-06-02,1.05\n", encoding="utf-8")
        columns = {"date_column": "day", "value_column": "level"}
        assert load_series_csv(path, "SOFRINDEX", **columns).values == (1.04, 1.05)
        assert load_series_csv(path, "MY_SPREAD", **columns).values == (1.04, 1.05)

    def test_a_rate_in_percent_read_as_decimals_refuses(self, tmp_path):
        # 3.64 as a decimal SOFR is 364%. The band is checked only when the
        # units were not stated, and only for series known to be rates.
        path = tmp_path / "own.csv"
        path.write_text("date,value\n2026-01-12,3.64\n", encoding="utf-8")
        with pytest.raises(ImplausibleInputError) as excinfo:
            load_series_csv(path, "SOFR")
        message = str(excinfo.value)
        assert "2026-01-12" in message and "percent=True" in message
        assert load_series_csv(path, "SOFR", percent=False).values == (3.64,)
        assert load_series_csv(path, "SOMETHING_NEW").values == (3.64,)

    @pytest.mark.parametrize(
        "named", [{}, {"date_column": "date", "value_column": "value"}], ids=["default", "named"]
    )
    def test_the_package_s_own_layout_is_decimals(self, tmp_path, named):
        # date,value is this package's layout and is decimals by contract,
        # at 2021's rates as at 2026's; percent=True is how a percent file
        # in this layout says so.
        path = tmp_path / "sofr.csv"
        path.write_text(
            "date,value\n2021-06-01,0.0001\n2021-06-02,0.0005\n2026-01-12,0.0364\n",
            encoding="utf-8",
        )
        assert load_series_csv(path, "SOFR", **named).values == (0.0001, 0.0005, 0.0364)
        path.write_text(f"date,value\n{LOW_RATE_ROWS}", encoding="utf-8")
        decimals = load_series_csv(path, "SOFR", percent=True, **named).values
        assert decimals == LOW_RATES_AS_DECIMALS

    def test_stated_units_win_in_either_layout(self, tmp_path):
        fred_file = tmp_path / "SOFR.csv"
        fred_file.write_text(FRED_CSV, encoding="utf-8")
        own = tmp_path / "own.csv"
        own.write_text("date,value\n2026-01-12,3.64\n", encoding="utf-8")
        assert load_series_csv(fred_file, "SOFR", percent=False).values == (3.64, 3.65)
        assert load_series_csv(own, "SOFR", percent=True).values == (pytest.approx(0.0364),)

    def test_a_fred_series_of_unknown_units_refuses_until_they_are_stated(self, tmp_path):
        path = tmp_path / "DEXMXUS.csv"
        path.write_text("observation_date,DEXMXUS\n2026-01-12,17.5\n", encoding="utf-8")
        with pytest.raises(UnsupportedConventionError, match="percent="):
            load_series_csv(path, "DEXMXUS")
        assert load_series_csv(path, "DEXMXUS", percent=False).values == (17.5,)

    @pytest.mark.parametrize("date_header", ["observation_date", "DATE"])
    def test_a_fred_file_holding_another_series_refuses_and_the_remedy_works(
        self, tmp_path, date_header
    ):
        # The first version told the caller to name value_column=, and doing
        # exactly that raised a second ValueError. The message now names
        # both columns and the units, and following it loads the file. It
        # holds for FRED's older header too, with the units stated or not.
        path = tmp_path / "DGS2.csv"
        path.write_text(f"{date_header},DGS2\n2026-01-12,4.05\n", encoding="utf-8")
        for units in ({}, {"percent": True}):
            with pytest.raises(ValueError) as excinfo:
                load_series_csv(path, "SOFR", **units)
            message = str(excinfo.value)
            for part in (f"date_column={date_header!r}", "value_column='DGS2'", "percent="):
                assert part in message
        series = load_series_csv(
            path, "SOFR", date_column=date_header, value_column="DGS2", percent=True
        )
        assert series.values == (pytest.approx(0.0405),)

    def test_an_index_level_is_never_divided_because_of_a_label(self, tmp_path):
        # The first version took the units from the label: SOFRINDEX at
        # 1.23456789 read as "SOFR" came back 0.0123456789, an index level
        # divided by 100. A FRED column read under another name is refused
        # until percent= says whose units apply; percent=False keeps it.
        path = tmp_path / "SOFRINDEX.csv"
        path.write_text("observation_date,SOFRINDEX\n2026-01-12,1.23456789\n", encoding="utf-8")
        named = {"value_column": "SOFRINDEX", "date_column": "observation_date"}
        with pytest.raises(UnsupportedConventionError) as excinfo:
            load_series_csv(path, "SOFR", **named)
        assert "'SOFRINDEX'" in str(excinfo.value) and "percent=" in str(excinfo.value)
        assert load_series_csv(path, "SOFR", percent=False, **named).values == (1.23456789,)
        assert load_series_csv(path, "SOFRINDEX").values == (1.23456789,)

    def test_a_low_rate_fred_column_under_another_name_asks_for_the_units(self, tmp_path):
        # EFFR at 6 and 8 bp, named and read as "SOFR": round two loaded
        # (0.06, 0.08), a 6% and an 8% SOFR, observed. Relabelling is
        # allowed, but the units of a relabelled column are the caller's.
        path = tmp_path / "EFFR.csv"
        path.write_text(
            "observation_date,EFFR\n2021-06-01,0.06\n2021-06-02,0.08\n", encoding="utf-8"
        )
        named = {"value_column": "EFFR", "date_column": "observation_date"}
        with pytest.raises(UnsupportedConventionError, match="percent=True"):
            load_series_csv(path, "SOFR", **named)
        assert load_series_csv(path, "SOFR", percent=True, **named).values == (
            pytest.approx(0.0006),
            pytest.approx(0.0008),
        )

    def test_columns_that_are_not_there_refuse_rather_than_reading_nothing(self, tmp_path):
        path = tmp_path / "other.csv"
        path.write_text("day,rate\n2026-01-12,0.0364\n", encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            load_series_csv(path, "SOFR")
        message = str(excinfo.value)
        assert "day" in message and "rate" in message
        assert "date_column=" in message

    @pytest.mark.parametrize(
        "text", ["", "date,value\n", "date,value\n2026-01-12,.\n2026-01-13,nan\n"]
    )
    def test_a_file_that_yields_no_observations_refuses(self, tmp_path, text):
        path = tmp_path / "empty.csv"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(InsufficientDataError, match="empty.csv"):
            load_series_csv(path, "SOFR")


class TestRefusals:
    """PRD-001 AC-2.3: a fixing that is not there is never made up."""

    def test_missing_fixing_names_the_date(self, snapshot):
        # A business day before the series begins: in range for the question,
        # absent from the data. The message has to say which day.
        before_the_start = date(2021, 12, 30)
        assert SIFMA_US.is_business_day(before_the_start)
        with pytest.raises(MissingFixingError, match="2021-12-30"):
            snapshot.fixing("SOFR", before_the_start)

    def test_present_fixing_comes_back(self, snapshot):
        assert snapshot.fixing("SOFR", date(2026, 1, 14)) > 0.0

    def test_missing_series_names_what_is_available(self, snapshot):
        with pytest.raises(MissingFixingError, match="SOFR"):
            snapshot.require("TIIE")

    def test_compounding_across_a_hole_refuses_and_names_the_day(self, fixtures_dir):
        full = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
        hole = date(2026, 1, 8)
        assert SIFMA_US.is_business_day(hole)
        pruned = Series(
            "SOFR",
            tuple(d for d in full.dates if d != hole),
            tuple(v for d, v in zip(full.dates, full.values, strict=True) if d != hole),
            full.provenance,
        )
        snap = MarketSnapshot(date(2026, 1, 15), {"SOFR": pruned})
        with pytest.raises(MissingFixingError, match=str(hole)):
            snap.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 12))

    def test_reversed_period_refuses(self, snapshot):
        with pytest.raises(ValueError, match="precedes"):
            snapshot.compounded("SOFR", date(2026, 1, 12), date(2026, 1, 5))

    def test_series_with_mismatched_columns_refuses(self):
        with pytest.raises(ValueError, match="2 dates"):
            Series("X", (date(2026, 1, 1), date(2026, 1, 2)), (0.01,), Provenance("file"))

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_value_refuses_and_names_the_date(self, value):
        # A NaN fixing passed every check and came out of compounding as a
        # NaN settlement price. A missing observation is left out of a
        # series, which is what the refusals above are built on.
        with pytest.raises(ValueError, match="2026-01-02"):
            Series(
                "SOFR",
                (date(2026, 1, 1), date(2026, 1, 2)),
                (0.0431, value),
                Provenance("file"),
            )


class TestNonPublicationDays:
    """PRD-001 AC-2.4: a non-publication day repeats the last rate, and says how often."""

    def test_weekend_days_repeat_and_are_counted(self, snapshot):
        # Mon 5 Jan to Mon 12 Jan 2026: five business days, two weekend days.
        result = snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 12))
        assert result.business_days == 5
        assert result.repeated_days == 2
        assert result.first_fixing_date == date(2026, 1, 5)
        assert result.last_fixing_date == date(2026, 1, 9)

    def test_the_repeated_rate_is_friday_s(self, snapshot):
        friday = snapshot.fixing("SOFR", date(2026, 1, 9))
        short = snapshot.compounded("SOFR", date(2026, 1, 9), date(2026, 1, 12))
        # Three calendar days at Friday's rate, as one simple-interest period:
        # CME's SR3 rule and the New York Fed's compounded averages weight
        # each published rate by the days it applies to, 1 + r * 3/360. The
        # cube is what compounding the weekend as three separate days gives,
        # and it is not the convention.
        assert short.accrual_factor == pytest.approx(1 + friday * 3 / 360, rel=1e-15)
        assert short.accrual_factor != pytest.approx((1 + friday / 360) ** 3, rel=1e-12)

    def test_a_period_ending_on_a_weekend_takes_only_its_own_days(self, snapshot):
        # Friday to Sunday: two of the three days Friday's rate covers fall in
        # the period, and only those two accrue in it.
        friday = snapshot.fixing("SOFR", date(2026, 1, 9))
        short = snapshot.compounded("SOFR", date(2026, 1, 9), date(2026, 1, 11))
        assert short.accrual_factor == pytest.approx(1 + friday * 2 / 360, rel=1e-15)

    def test_holiday_repeats_too(self, late_snapshot):
        # 19 January 2026 is Martin Luther King Jr Day.
        assert not SIFMA_US.is_business_day(date(2026, 1, 19))
        result = late_snapshot.compounded("SOFR", date(2026, 1, 16), date(2026, 1, 20))
        assert result.repeated_days == 3
        assert result.business_days == 1

    def test_compounding_telescopes_across_a_publication_day(self, late_snapshot):
        # Two adjacent periods multiply to the whole when the boundary is a
        # publication day (Monday 12 January), because every published rate
        # and the days it covers then sit inside one of the two periods.
        first = late_snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 12))
        second = late_snapshot.compounded("SOFR", date(2026, 1, 12), date(2026, 1, 20))
        whole = late_snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 20))
        assert first.accrual_factor * second.accrual_factor == pytest.approx(
            whole.accrual_factor, rel=1e-14
        )

    def test_a_boundary_on_a_weekend_splits_the_carried_rate(self, snapshot):
        # Saturday 10 January as the boundary: Friday's rate covers one day
        # of the first period and two of the second, and each period accrues
        # its own share as simple interest. The product differs from the
        # whole by Friday's rate squared times 1 * 2 / 360**2 -- the second
        # order term, exactly.
        friday = snapshot.fixing("SOFR", date(2026, 1, 9))
        first = snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 10))
        second = snapshot.compounded("SOFR", date(2026, 1, 10), date(2026, 1, 13))
        whole = snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 13))
        split = first.accrual_factor * second.accrual_factor
        assert split - whole.accrual_factor == pytest.approx(
            whole.accrual_factor / (1 + friday * 3 / 360) * (friday**2 * 2 / 360**2), rel=1e-6
        )

    def test_a_period_opening_on_a_weekend_carries_friday_s_rate(self, snapshot):
        # Saturday 10 to Tuesday 13 January: Saturday and Sunday take the rate
        # published for Friday 9 January, the business day before the period.
        friday = snapshot.fixing("SOFR", date(2026, 1, 9))
        monday = snapshot.fixing("SOFR", date(2026, 1, 12))
        result = snapshot.compounded("SOFR", date(2026, 1, 10), date(2026, 1, 13))
        assert result.accrual_factor == pytest.approx(
            (1 + friday * 2 / 360) * (1 + monday / 360), rel=1e-15
        )
        assert result.repeated_days == 2
        assert result.first_fixing_date == date(2026, 1, 12)

    def test_only_the_business_day_before_the_period_may_be_carried_in(self, fixtures_dir):
        # Remove Friday 9 January: the last fixing before Saturday is now
        # Thursday's, which is not the rate a Saturday takes. Carrying it would
        # be a guess, and an unbounded one -- with July missing, the review
        # carried 30 June's fixing into an August SR1 settlement.
        full = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
        friday = date(2026, 1, 9)
        pruned = Series(
            "SOFR",
            tuple(d for d in full.dates if d != friday),
            tuple(v for d, v in zip(full.dates, full.values, strict=True) if d != friday),
            full.provenance,
        )
        snap = MarketSnapshot(date(2026, 1, 15), {"SOFR": pruned})
        for method in (snap.compounded, snap.averaged):
            with pytest.raises(MissingFixingError) as excinfo:
                method("SOFR", date(2026, 1, 10), date(2026, 1, 13))
            message = str(excinfo.value)
            assert "2026-01-09" in message
            assert "2026-01-08" in message  # the fixing it declined to carry

    def test_a_period_opening_on_a_holiday_carries_the_day_before(self, snapshot):
        # Thursday 1 January 2026 is New Year's Day; the rate it takes is
        # Wednesday 31 December's.
        assert not SIFMA_US.is_business_day(date(2026, 1, 1))
        new_year_s_eve = snapshot.fixing("SOFR", date(2025, 12, 31))
        result = snapshot.averaged("SOFR", date(2026, 1, 1), date(2026, 1, 2))
        assert result.rate == pytest.approx(new_year_s_eve, rel=1e-15)

    def test_average_and_compounded_differ(self, late_snapshot):
        averaged = late_snapshot.averaged("SOFR", date(2026, 1, 1), date(2026, 2, 1))
        compounded = late_snapshot.compounded("SOFR", date(2026, 1, 1), date(2026, 2, 1))
        assert compounded.rate > averaged.rate
        assert averaged.repeated_days == compounded.repeated_days

    def test_empty_period_is_the_identity(self, snapshot):
        result = snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 5))
        assert result.accrual_factor == 1.0
        assert result.business_days == 0


def _review_lookahead_snapshot() -> MarketSnapshot:
    """The review's look-ahead case: 4% up to the valuation date, 3% after it."""
    as_of = date(2026, 1, 15)
    days = [
        date(2025, 12, 1) + timedelta(days=k)
        for k in range(89)
        if SIFMA_US.is_business_day(date(2025, 12, 1) + timedelta(days=k))
    ]
    values = tuple(0.0400 if d <= as_of else 0.0300 for d in days)
    return MarketSnapshot(
        as_of=as_of,
        series={"SOFR": Series("SOFR", tuple(days), values, Provenance("fred", "SOFR"))},
    )


class TestTheValuationDate:
    """A snapshot as of a date serves nothing dated after it.

    ``as_of`` was a label: a snapshot as of 15 January served 10 February's
    fixing and compounded through 30 January, which in a backtest is the
    future leaking into the past with nothing in the result to show it.
    """

    def test_a_fixing_after_the_valuation_date_refuses(self):
        snapshot = _review_lookahead_snapshot()
        with pytest.raises(MissingFixingError) as excinfo:
            snapshot.fixing("SOFR", date(2026, 2, 10))
        message = str(excinfo.value)
        assert "2026-02-10" in message and "2026-01-15" in message

    def test_the_valuation_date_s_own_fixing_is_served(self):
        snapshot = _review_lookahead_snapshot()
        assert snapshot.fixing("SOFR", date(2026, 1, 15)) == 0.0400

    @pytest.mark.parametrize("method", ["compounded", "averaged"])
    def test_accruing_past_the_valuation_date_refuses(self, method):
        snapshot = _review_lookahead_snapshot()
        with pytest.raises(MissingFixingError) as excinfo:
            getattr(snapshot, method)("SOFR", date(2026, 1, 2), date(2026, 2, 2))
        message = str(excinfo.value)
        assert "2026-01-16" in message and "2026-01-15" in message

    def test_accruing_up_to_the_valuation_date_is_served(self):
        snapshot = _review_lookahead_snapshot()
        result = snapshot.compounded("SOFR", date(2026, 1, 2), date(2026, 1, 16))
        assert result.last_fixing_date == date(2026, 1, 15)
        assert result.rate == pytest.approx(0.04, abs=1e-4)  # none of the 3% after it

    def test_a_weekend_after_a_friday_valuation_date_takes_friday_s_rate(self, fixtures_dir):
        # What a Saturday accrues is fixed by Friday's rate the moment it is
        # published, so a snapshot as of Friday can serve it. Monday's rate
        # it cannot.
        series = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
        snap = MarketSnapshot(date(2026, 1, 9), {"SOFR": series})
        result = snap.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 12))
        assert result.last_fixing_date == date(2026, 1, 9)
        with pytest.raises(MissingFixingError, match="2026-01-12"):
            snap.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 13))

    def test_the_series_it_serves_stop_at_the_valuation_date(self, snapshot):
        loaded = snapshot.series["SOFR"]
        served = snapshot.require("SOFR")
        kept = [(d, v) for d, v in zip(loaded.dates, loaded.values, strict=True)]
        kept = [(d, v) for d, v in kept if d <= snapshot.as_of]
        assert served.dates == tuple(d for d, _ in kept)
        assert served.values == tuple(v for _, v in kept)
        assert served.provenance is loaded.provenance
        # What was loaded is kept as loaded; only what is served is cut.
        assert loaded.dates[-1] > snapshot.as_of

    def test_lookups_do_not_rebuild_the_series(self, snapshot, monkeypatch):
        # Alan's review: require() copied and re-validated the whole series on
        # every fixing() once the history ran past as_of -- 0.23 s for 1,000
        # lookups on 2,000 points, against 0.0003 s with nothing to cut. No
        # lookup constructs a Series any more, which this pins without a
        # timing test.
        def rebuilt(self):
            raise AssertionError("a lookup rebuilt the series")

        monkeypatch.setattr(Series, "__post_init__", rebuilt)
        assert snapshot.fixing("SOFR", date(2026, 1, 14)) > 0.0
        assert snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 12)).rate > 0.0
        assert snapshot.averaged("SOFR", date(2026, 1, 5), date(2026, 1, 12)).rate > 0.0
        assert snapshot.require("SOFR").dates[-1] <= snapshot.as_of

    def test_realised_sigma_cannot_be_measured_at_a_later_date(self, snapshot):
        from rates_engine.market.estimators import realized_sofr_sigma

        with pytest.raises(MissingFixingError, match=str(snapshot.as_of)):
            realized_sofr_sigma(snapshot, as_of=date(2026, 6, 1))
        earlier = realized_sofr_sigma(snapshot, as_of=date(2025, 12, 31))
        assert earlier.end <= date(2025, 12, 31)


class TestTreasuryClassification:
    """PRD-001 AC-2.6: a DGS series is a proxy from the moment it is loaded."""

    def test_dgs_is_a_proxy_with_the_right_instrument_kind(self, fixtures_dir, tmp_path):
        path = tmp_path / "dgs2.csv"
        path.write_text("date,value\n2026-01-15,0.0405\n", encoding="utf-8")
        series = load_series_csv(path, "DGS2")
        assert series.provenance.data_quality is DataQuality.PROXY
        assert series.provenance.instrument_kind == TREASURY_PAR_YIELD
        assert "swap spread" in (series.provenance.notes or "")

    def test_sofr_is_observed(self, fixtures_dir):
        series = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
        assert series.provenance.data_quality is DataQuality.OBSERVED

    def test_snapshot_worst_quality_reflects_the_proxy(self, fixtures_dir, tmp_path):
        path = tmp_path / "dgs2.csv"
        path.write_text("date,value\n2026-01-15,0.0405\n", encoding="utf-8")
        snap = MarketSnapshot(
            date(2026, 1, 15),
            {
                "SOFR": load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR"),
                "DGS2": load_series_csv(path, "DGS2"),
            },
        )
        assert snap.worst_quality() is DataQuality.PROXY

    def test_unknown_series_is_not_guessed_at(self, tmp_path):
        path = tmp_path / "x.csv"
        path.write_text("date,value\n2026-01-15,0.01\n", encoding="utf-8")
        series = load_series_csv(path, "SOMETHING_NEW")
        assert series.provenance.instrument_kind == "unclassified"
        assert series.provenance.data_quality is DataQuality.OBSERVED
