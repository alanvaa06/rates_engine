"""AC-2.2, 2.3, 2.4 and 2.6: loading data, refusing to invent it, and the proxy label."""

from __future__ import annotations

from datetime import date

import pytest

from rates_engine.conventions import SIFMA_US
from rates_engine.errors import MissingFixingError
from rates_engine.evidence import DataQuality, Provenance
from rates_engine.market import (
    MarketSnapshot,
    Series,
    load_series_csv,
    load_settlements_csv,
    load_snapshot_csv,
)
from rates_engine.market.classify import TREASURY_PAR_YIELD


class TestLoading:
    """AC-2.2: a row on disk becomes a value with a source attached."""

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


class TestRefusals:
    """AC-2.3: a fixing that is not there is never made up."""

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


class TestNonPublicationDays:
    """AC-2.4: a non-publication day repeats the last rate, and says how often."""

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
        # Three calendar days, all at Friday's rate.
        assert short.accrual_factor == pytest.approx((1 + friday / 360) ** 3, rel=1e-15)

    def test_holiday_repeats_too(self, snapshot):
        # 19 January 2026 is Martin Luther King Jr Day.
        assert not SIFMA_US.is_business_day(date(2026, 1, 19))
        result = snapshot.compounded("SOFR", date(2026, 1, 16), date(2026, 1, 20))
        assert result.repeated_days == 3
        assert result.business_days == 1

    def test_compounding_telescopes_the_way_the_curve_assumes(self, snapshot):
        # The property the futures bootstrap relies on: the product of daily
        # growth over two adjacent periods equals the growth over the whole.
        first = snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 12))
        second = snapshot.compounded("SOFR", date(2026, 1, 12), date(2026, 1, 20))
        whole = snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 20))
        assert first.accrual_factor * second.accrual_factor == pytest.approx(
            whole.accrual_factor, rel=1e-14
        )

    def test_average_and_compounded_differ(self, snapshot):
        averaged = snapshot.averaged("SOFR", date(2026, 1, 1), date(2026, 2, 1))
        compounded = snapshot.compounded("SOFR", date(2026, 1, 1), date(2026, 2, 1))
        assert compounded.rate > averaged.rate
        assert averaged.repeated_days == compounded.repeated_days

    def test_empty_period_is_the_identity(self, snapshot):
        result = snapshot.compounded("SOFR", date(2026, 1, 5), date(2026, 1, 5))
        assert result.accrual_factor == 1.0
        assert result.business_days == 0


class TestTreasuryClassification:
    """AC-2.6: a DGS series is a proxy from the moment it is loaded."""

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
