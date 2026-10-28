"""PRD-001 AC-5.1, PRD-001 AC-5.2 and PRD-001 AC-8.5: settlement formulas and the derived contract DV01.

The comparison against CME's published final settlements is the test that
would catch a wrong *convention*, and it is skipped here because this
environment could not fetch the settlements — see
``tests/fixtures/cme_published/README.md``. What runs instead is an
independent re-implementation of each formula, written from the definition
rather than from the code under test. That catches a wrong formula. It cannot
catch the two of us being wrong the same way, which is exactly the gap the
skipped test fills, and the reason it is skipped rather than replaced.

That gap is not hypothetical. Until this file pinned a QuantLib value, the
SR3 re-implementation here compounded ``1 + r/360`` once per calendar day,
exactly as the code did, so a weekend entered as ``(1 + r/360)**3`` rather
than CME's ``1 + 3r/360`` and both sides agreed on a rate 0.02-0.04 bp too
high. The constant from QuantLib below is the third party that breaks the
tie until CME's own settlements are supplied.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import require_published

from rates_engine.conventions.calendar import SIFMA_US
from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
from rates_engine.core.errors import MissingFixingError
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.instruments.futures import (
    SR1_CONTRACT_TENOR,
    SR1_NOTIONAL,
    SR3_CONTRACT_TENOR,
    SR3_NOTIONAL,
    SOFRFuture1M,
    SOFRFuture3M,
)
from rates_engine.market.providers.file import load_series_csv
from rates_engine.market.snapshot import MarketSnapshot, Series


def _cme_sr3_rate(snapshot, start: date, end: date) -> float:
    """SR3's settlement rate, written from CME's rule rather than from the code.

    ``R = [prod_i (1 + r_i * d_i / 360) - 1] * 360 / D``: the product runs
    over the publication days ``i`` of the reference quarter, ``r_i`` is the
    SOFR published for day ``i`` and ``d_i`` the number of calendar days it
    applies to. A Friday's rate covers Friday, Saturday and Sunday, so it
    enters once with ``d_i = 3`` -- not three times with ``d_i = 1``, which is
    the error this function exists to catch. Days before the quarter's first
    publication day carry the rate of the publication day before it.
    """
    series = snapshot.require("SOFR")
    calendar_days = [start + timedelta(days=k) for k in range((end - start).days)]
    publication = [d for d in calendar_days if SIFMA_US.is_business_day(d)]
    weighted: list[tuple[float, int]] = []
    if publication[0] != start:
        carried = series.get(SIFMA_US.previous_business_day(start))
        assert carried is not None, "fixture is missing the day before the quarter"
        weighted.append((carried, (publication[0] - start).days))
    for k, day in enumerate(publication):
        following = publication[k + 1] if k + 1 < len(publication) else end
        rate = series.get(day)
        assert rate is not None, f"fixture is missing {day}"
        weighted.append((rate, (following - day).days))
    assert sum(days for _, days in weighted) == (end - start).days
    factor = 1.0
    for rate, days in weighted:
        factor *= 1.0 + rate * days / 360.0
    return (factor - 1.0) * 360.0 / (end - start).days


def _synthetic_rate(day: date) -> float:
    """4.00% plus 0 to 12 bp by the day's ordinal: varied, and rebuildable anywhere."""
    return 0.0400 + 0.0001 * (day.toordinal() % 13)


#: The window the synthetic fixings cover, and the only holidays in it that
#: fall on a weekday. QuantLib's ``UnitedStates(SOFR)`` calendar and
#: ``SIFMA_US`` agree on every day from 2026-08-01 to 2027-01-31, which is
#: why the window was chosen; the holidays are pinned so that a calendar
#: change shows up as itself rather than as a mysterious rate mismatch.
SYNTHETIC_WINDOW = (date(2026, 8, 1), date(2027, 1, 31))
SYNTHETIC_SR3_HOLIDAYS = [date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26)]

#: QuantLib 1.44: ``ql.OvernightIndexedCoupon(payment=end, nominal=1.0,
#: start=2026-09-16, end=2026-12-16, ql.Sofr())`` with ``.rate()`` read
#: after adding :func:`_synthetic_rate` as the fixing of every
#: ``ql.UnitedStates(ql.UnitedStates.SOFR)`` business day in
#: :data:`SYNTHETIC_WINDOW`. Computed in a scratch environment and pasted
#: here; QuantLib is not a dependency of the package or of this suite. The
#: per-calendar-day product the package used before gives
#: 0.04080118534023062 on the same fixings, 0.0224 bp higher.
QUANTLIB_SR3_SEP_2026 = 0.040798949343134126


@pytest.fixture(scope="module")
def synthetic_snapshot() -> MarketSnapshot:
    first, last = SYNTHETIC_WINDOW
    days = [
        first + timedelta(days=k)
        for k in range((last - first).days + 1)
        if SIFMA_US.is_business_day(first + timedelta(days=k))
    ]
    series = Series(
        "SOFR",
        tuple(days),
        tuple(_synthetic_rate(d) for d in days),
        Provenance(source="test", series_id="SOFR", data_quality=DataQuality.SYNTHETIC),
    )
    return MarketSnapshot(as_of=last, series={"SOFR": series})


def _daily_rates(snapshot, start: date, end: date) -> list[float]:
    """Per-calendar-day rates, written from the rule rather than from the code."""
    series = snapshot.require("SOFR")
    rates: list[float] = []
    # A period can open on a holiday — 1 January, for the January contract —
    # in which case the rate carried in is the last one published before it.
    previous = series.last_on_or_before(start - timedelta(days=1))
    carried: float | None = previous[1] if previous else None
    day = start
    while day < end:
        if SIFMA_US.is_business_day(day):
            carried = series.get(day)
            assert carried is not None, f"fixture is missing {day}"
        assert carried is not None
        rates.append(carried)
        day += timedelta(days=1)
    return rates


class TestSR1Settlement:
    """PRD-001 AC-5.1: one-month contracts settle on the arithmetic average."""

    def test_matches_an_independent_arithmetic_average(self, snapshot):
        for month in range(1, 13):
            contract = SOFRFuture1M(date(2025, month, 1))
            rates = _daily_rates(snapshot, contract.accrual_start, contract.accrual_end)
            expected = sum(rates) / len(rates)
            assert contract.settlement_rate(snapshot).rate == pytest.approx(expected, abs=1e-15)

    def test_price_is_a_hundred_minus_the_rate_in_percent(self, snapshot):
        contract = SOFRFuture1M(date(2025, 6, 1))
        rate = contract.settlement_rate(snapshot).rate
        assert contract.settlement_price(snapshot) == pytest.approx(100.0 - 100.0 * rate)

    def test_it_is_not_the_compounded_rate(self, snapshot):
        contract = SOFRFuture1M(date(2025, 6, 1))
        averaged = contract.settlement_rate(snapshot).rate
        compounded = snapshot.compounded(
            "SOFR", contract.accrual_start, contract.accrual_end
        ).rate
        # Using the wrong one of the two is a real error, and at these levels
        # it is larger than the 0.1 bp tolerance the CME comparison holds.
        assert compounded > averaged

    def test_a_month_opening_on_a_weekend_refuses_a_stale_carried_fixing(self, fixtures_dir):
        # August 2026 opens on a Saturday, so its first two days carry the
        # rate of Friday 31 July. With July missing, the last fixing on file
        # is 30 June's, and the code carried it in without a word -- 1.46 bp
        # on the contract in the review's example.
        full = load_series_csv(fixtures_dir / "sofr_fixings.csv", "SOFR")
        kept = [
            (d, v)
            for d, v in zip(full.dates, full.values, strict=True)
            if not date(2026, 7, 1) <= d <= date(2026, 7, 31)
        ]
        snapshot = MarketSnapshot(
            as_of=kept[-1][0],
            series={
                "SOFR": Series(
                    "SOFR", tuple(d for d, _ in kept), tuple(v for _, v in kept), full.provenance
                )
            },
        )
        assert date(2026, 8, 1).weekday() == 5
        with pytest.raises(MissingFixingError, match="2026-07-31"):
            SOFRFuture1M(date(2026, 8, 1)).settlement_rate(snapshot)

    @pytest.mark.parametrize(
        "named",
        [{}, {"date_column": "observation_date", "value_column": "SOFR"}],
        ids=["default-columns", "named-columns"],
    )
    def test_a_2021_fred_download_settles_in_basis_points(self, tmp_path, named):
        # Alan's second review: a 2020-22 backtest that named FRED's columns
        # read every fixing as a decimal, so every settlement was about 100x
        # off. SOFR flat at 5 bp (FRED's 0.05) settles June 2021's SR1 at
        # exactly 0.0005, a price of 99.95. Read as decimals it was 0.05 and
        # 95.00, observed.
        first, last = date(2021, 5, 28), date(2021, 7, 2)
        days = (first + timedelta(days=k) for k in range((last - first).days + 1))
        rows = "".join(f"{d.isoformat()},0.05\n" for d in days if SIFMA_US.is_business_day(d))
        path = tmp_path / "SOFR.csv"
        path.write_text(f"observation_date,SOFR\n{rows}", encoding="utf-8")
        snapshot = MarketSnapshot(
            as_of=last, series={"SOFR": load_series_csv(path, "SOFR", **named)}
        )
        contract = SOFRFuture1M(date(2021, 6, 1))
        assert contract.settlement_rate(snapshot).rate == pytest.approx(0.0005, abs=1e-15)
        assert contract.settlement_price(snapshot) == pytest.approx(99.95, abs=1e-12)

    def test_contract_month_spans_the_calendar_month(self):
        contract = SOFRFuture1M(date(2026, 2, 14))
        assert contract.accrual_start == date(2026, 2, 1)
        assert contract.accrual_end == date(2026, 3, 1)

    def test_against_cme_final_settlements(self):
        """PRD-001 AC-5.1's third-party half: skipped until the settlements are supplied."""
        settlements, fixings = require_published(
            "sr1_final_settlements.csv", "sofr_fixings.csv", ac="5.1"
        )
        from rates_engine.market.providers.file import load_settlements_csv

        snapshot = MarketSnapshot(
            as_of=date.today(), series={"SOFR": load_series_csv(fixings, "SOFR")}
        )
        rows = [s for s in load_settlements_csv(settlements) if s.symbol == "SR1"]
        assert len(rows) >= 6, "PRD-001 AC-5.1 asks for at least six expired contracts"
        for row in rows:
            computed = SOFRFuture1M(row.contract_month).settlement_price(snapshot)
            assert computed == pytest.approx(row.settlement_price, abs=0.001)


class TestSR3Settlement:
    """PRD-001 AC-5.2: three-month contracts settle on the compounded rate."""

    def test_matches_an_independent_compounding(self, snapshot):
        period = imm_date(2025, 3)
        for _ in range(3):
            following = next_imm_on_or_after(period + timedelta(days=1))
            contract = SOFRFuture3M(period, following)
            expected = _cme_sr3_rate(snapshot, period, following)
            assert contract.settlement_rate(snapshot).rate == pytest.approx(expected, rel=1e-14)
            period = following

    def test_matches_quantlib_overnight_indexed_coupon(self, synthetic_snapshot):
        contract = SOFRFuture3M.from_contract_month(2026, 9)
        assert (contract.imm_start, contract.imm_end) == (date(2026, 9, 16), date(2026, 12, 16))
        weekday_holidays = [
            contract.imm_start + timedelta(days=k)
            for k in range((contract.imm_end - contract.imm_start).days)
            if (contract.imm_start + timedelta(days=k)).weekday() < 5
            and not SIFMA_US.is_business_day(contract.imm_start + timedelta(days=k))
        ]
        assert weekday_holidays == SYNTHETIC_SR3_HOLIDAYS, (
            "the QuantLib constant was computed on these holidays; recompute it if they change"
        )
        rate = contract.settlement_rate(synthetic_snapshot).rate
        # Rounding in a product of some sixty factors, evaluated in a
        # different order, is a few times 1e-14; the error this guards
        # against is 2.2e-6.
        assert rate == pytest.approx(QUANTLIB_SR3_SEP_2026, abs=1e-13)
        assert rate == pytest.approx(
            _cme_sr3_rate(synthetic_snapshot, contract.imm_start, contract.imm_end), rel=1e-14
        )

    def test_a_long_weekend_is_one_simple_period_at_friday_s_rate(self, synthetic_snapshot):
        # Friday 9 October 2026 to Tuesday 13 October, Columbus Day between:
        # one rate for four days is 1 + 4r/360, so the period rate is r
        # itself. Compounding the four days separately gives r + 0.068 bp.
        # QuantLib's OvernightIndexedCoupon over the same dates returns
        # 0.04030000000000422; the 4e-15 is the rounding of 1 + x - 1.
        friday = date(2026, 10, 9)
        assert _synthetic_rate(friday) == pytest.approx(0.0403, abs=1e-15)
        result = synthetic_snapshot.compounded("SOFR", friday, date(2026, 10, 13))
        assert result.rate == pytest.approx(_synthetic_rate(friday), abs=1e-14)
        assert result.business_days == 1
        assert result.repeated_days == 3

    def test_reference_period_runs_between_third_wednesdays(self):
        contract = SOFRFuture3M.from_contract_month(2026, 3)
        assert contract.imm_start == date(2026, 3, 18)
        assert contract.imm_end == date(2026, 6, 17)
        assert contract.imm_start.weekday() == contract.imm_end.weekday() == 2

    def test_non_imm_month_refuses(self):
        with pytest.raises(ValueError, match="not an IMM month"):
            SOFRFuture3M.from_contract_month(2026, 5)

    def test_implied_forward_subtracts_the_adjustment(self):
        contract = SOFRFuture3M.from_contract_month(2026, 3)
        assert contract.implied_forward_rate(96.0) == pytest.approx(0.04)
        assert contract.implied_forward_rate(96.0, 2.25e-4) == pytest.approx(0.04 - 2.25e-4)

    def test_against_cme_final_settlements(self):
        """PRD-001 AC-5.2's third-party half: skipped until the settlements are supplied."""
        settlements, fixings = require_published(
            "sr3_final_settlements.csv", "sofr_fixings.csv", ac="5.2"
        )
        from rates_engine.market.providers.file import load_settlements_csv

        snapshot = MarketSnapshot(
            as_of=date.today(), series={"SOFR": load_series_csv(fixings, "SOFR")}
        )
        rows = [s for s in load_settlements_csv(settlements) if s.symbol == "SR3"]
        assert len(rows) >= 6, "PRD-001 AC-5.2 asks for at least six expired contracts"
        for row in rows:
            start = next_imm_on_or_after(row.contract_month)
            contract = SOFRFuture3M(start, next_imm_on_or_after(start + timedelta(days=1)))
            assert contract.settlement_price(snapshot) == pytest.approx(
                row.settlement_price, abs=0.001
            )


class TestContractDV01:
    """PRD-001 AC-8.5: DV01 is derived from notional and nominal tenor, not quoted."""

    def test_sr3_is_twenty_five_dollars(self):
        contract = SOFRFuture3M.from_contract_month(2026, 3)
        assert contract.dv01 == pytest.approx(SR3_NOTIONAL * SR3_CONTRACT_TENOR * 1e-4)
        assert contract.dv01 == pytest.approx(25.00, abs=1e-9)

    def test_sr1_is_forty_one_sixty_seven(self):
        contract = SOFRFuture1M(date(2026, 3, 1))
        assert contract.dv01 == pytest.approx(SR1_NOTIONAL * SR1_CONTRACT_TENOR * 1e-4)
        assert contract.dv01 == pytest.approx(41.67, abs=0.005)

    @pytest.mark.parametrize("month", range(1, 13))
    def test_sr1_dv01_does_not_move_with_the_length_of_the_month(self, month):
        # The nominal 30/360 tenor, not the realised accrual: February and
        # March are worth the same per basis point.
        assert SOFRFuture1M(date(2026, month, 1)).dv01 == pytest.approx(41.666666, abs=1e-5)

    @pytest.mark.parametrize("month", [3, 6, 9, 12])
    def test_sr3_dv01_does_not_move_with_the_length_of_the_quarter(self, month):
        contract = SOFRFuture3M.from_contract_month(2026, month)
        assert contract.year_fraction != pytest.approx(0.25, abs=1e-6)
        assert contract.dv01 == pytest.approx(25.0, abs=1e-9)

    def test_the_notional_provenance_is_honest(self):
        assert SOFRFuture1M(date(2026, 3, 1)).notional_source == "assumed"
        assert SOFRFuture3M.from_contract_month(2026, 3).notional_source == "derived"
