"""AC-5.1, AC-5.2 and AC-8.5: settlement formulas and the derived contract DV01.

The comparison against CME's published final settlements is the test that
would catch a wrong *convention*, and it is skipped here because this
environment could not fetch the settlements — see
``tests/fixtures/cme_published/README.md``. What runs instead is an
independent re-implementation of each formula, written from the definition
rather than from the code under test. That catches a wrong formula. It cannot
catch the two of us being wrong the same way, which is exactly the gap the
skipped test fills, and the reason it is skipped rather than replaced.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import require_published

from rates_engine.conventions import SIFMA_US, imm_date, next_imm_on_or_after
from rates_engine.instruments import SOFRFuture1M, SOFRFuture3M
from rates_engine.instruments.futures import (
    SR1_CONTRACT_TENOR,
    SR1_NOTIONAL,
    SR3_CONTRACT_TENOR,
    SR3_NOTIONAL,
)
from rates_engine.market import load_series_csv
from rates_engine.market.snapshot import MarketSnapshot


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
    """AC-5.1: one-month contracts settle on the arithmetic average."""

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

    def test_contract_month_spans_the_calendar_month(self):
        contract = SOFRFuture1M(date(2026, 2, 14))
        assert contract.accrual_start == date(2026, 2, 1)
        assert contract.accrual_end == date(2026, 3, 1)

    def test_against_cme_final_settlements(self):
        """AC-5.1's third-party half: skipped until the settlements are supplied."""
        settlements, fixings = require_published("sr1_final_settlements.csv", "sofr_fixings.csv")
        from rates_engine.market import load_settlements_csv

        snapshot = MarketSnapshot(
            as_of=date.today(), series={"SOFR": load_series_csv(fixings, "SOFR")}
        )
        rows = [s for s in load_settlements_csv(settlements) if s.symbol == "SR1"]
        assert len(rows) >= 6, "AC-5.1 asks for at least six expired contracts"
        for row in rows:
            computed = SOFRFuture1M(row.contract_month).settlement_price(snapshot)
            assert computed == pytest.approx(row.settlement_price, abs=0.001)


class TestSR3Settlement:
    """AC-5.2: three-month contracts settle on the compounded rate."""

    def test_matches_an_independent_compounding(self, snapshot):
        period = imm_date(2025, 3)
        for _ in range(3):
            following = next_imm_on_or_after(period + timedelta(days=1))
            contract = SOFRFuture3M(period, following)
            rates = _daily_rates(snapshot, period, following)
            factor = 1.0
            for rate in rates:
                factor *= 1.0 + rate / 360.0
            expected = (factor - 1.0) / ((following - period).days / 360.0)
            assert contract.settlement_rate(snapshot).rate == pytest.approx(expected, rel=1e-14)
            period = following

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
        """AC-5.2's third-party half: skipped until the settlements are supplied."""
        settlements, fixings = require_published("sr3_final_settlements.csv", "sofr_fixings.csv")
        from rates_engine.market import load_settlements_csv

        snapshot = MarketSnapshot(
            as_of=date.today(), series={"SOFR": load_series_csv(fixings, "SOFR")}
        )
        rows = [s for s in load_settlements_csv(settlements) if s.symbol == "SR3"]
        assert len(rows) >= 6, "AC-5.2 asks for at least six expired contracts"
        for row in rows:
            start = next_imm_on_or_after(row.contract_month)
            contract = SOFRFuture3M(start, next_imm_on_or_after(start + timedelta(days=1)))
            assert contract.settlement_price(snapshot) == pytest.approx(
                row.settlement_price, abs=0.001
            )


class TestContractDV01:
    """AC-8.5: DV01 is derived from notional and nominal tenor, not quoted."""

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
