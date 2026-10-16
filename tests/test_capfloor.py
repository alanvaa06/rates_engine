"""PRD-002 AC-2.1, 2.2 and 2.3: the strip, the parity that survives summing it, and the gap."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from conftest import AS_OF

from rates_engine.conventions.option_kind import OptionKind
from rates_engine.conventions.side import Side
from rates_engine.core.errors import MissingForwardError
from rates_engine.instruments.capfloor import TERM_RATE_FIXING_LAG_DAYS, CapFloor
from rates_engine.instruments.swaps import IRSwap
from rates_engine.pricing.linear import pv
from rates_engine.pricing.options import (
    cap_floor_pv,
    caplet_forward_rate,
    caplet_numeraire,
    caplet_pv,
)
from rates_engine.risk.greeks import option_greeks
from rates_engine.volatility.units import Volatility

STRIKE = 0.042
VOL = Volatility.normal_bp(80.0)


def _pair(frequency: int = 3, strike: float = STRIKE):
    start, end = AS_OF + timedelta(days=365), AS_OF + timedelta(days=365 * 6)
    cap = CapFloor(
        effective=start, maturity=end, strike=strike, notional=1e8,
        product="cap", frequency_months=frequency,
    )
    return cap, replace(cap, product="floor")


class TestParity:
    """PRD-002 AC-2.1: cap minus floor is the payer swap struck there."""

    @pytest.mark.parametrize("strike", [0.02, 0.035, 0.042, 0.05, 0.07])
    def test_at_every_strike(self, option_curve_set, strike):
        cap, floor = _pair(strike=strike)
        swap = IRSwap(
            effective=cap.effective, maturity=cap.maturity, fixed_rate=strike,
            notional=1e8, side=Side.PAYER,
            fixed_frequency_months=3, float_frequency_months=3,
        )
        difference = (
            cap_floor_pv(cap, option_curve_set, VOL).value
            - cap_floor_pv(floor, option_curve_set, VOL).value
        )
        assert difference == pytest.approx(pv(swap, option_curve_set).value, rel=1e-10)

    @pytest.mark.parametrize("frequency", [1, 3, 6, 12])
    def test_at_every_frequency(self, option_curve_set, frequency):
        cap, floor = _pair(frequency=frequency)
        swap = IRSwap(
            effective=cap.effective, maturity=cap.maturity, fixed_rate=STRIKE,
            notional=1e8, side=Side.PAYER,
            fixed_frequency_months=frequency, float_frequency_months=frequency,
        )
        difference = (
            cap_floor_pv(cap, option_curve_set, VOL).value
            - cap_floor_pv(floor, option_curve_set, VOL).value
        )
        assert difference == pytest.approx(pv(swap, option_curve_set).value, rel=1e-10)

    def test_it_holds_under_black_too(self, option_curve_set):
        cap, floor = _pair()
        swap = IRSwap(
            effective=cap.effective, maturity=cap.maturity, fixed_rate=STRIKE,
            notional=1e8, side=Side.PAYER,
            fixed_frequency_months=3, float_frequency_months=3,
        )
        vol = Volatility.lognormal_percent(20.0)
        difference = (
            cap_floor_pv(cap, option_curve_set, vol).value
            - cap_floor_pv(floor, option_curve_set, vol).value
        )
        assert difference == pytest.approx(pv(swap, option_curve_set).value, rel=1e-10)

    def test_it_does_not_depend_on_the_volatility(self, option_curve_set):
        cap, floor = _pair()
        gaps = [
            cap_floor_pv(cap, option_curve_set, Volatility.normal_bp(v)).value
            - cap_floor_pv(floor, option_curve_set, Volatility.normal_bp(v)).value
            for v in (20.0, 80.0, 300.0)
        ]
        assert gaps[0] == pytest.approx(gaps[1], rel=1e-10)
        assert gaps[1] == pytest.approx(gaps[2], rel=1e-10)


class TestZeroStrikeLimit:
    """PRD-002 AC-2.2: a caplet struck at nothing is the floating flow itself."""

    def test_the_cap_tends_to_the_floating_leg(self, option_curve_set):
        cap, _ = _pair()
        values = [
            cap_floor_pv(replace(cap, strike=k), option_curve_set,
                         Volatility.lognormal_percent(20.0)).value
            for k in (1e-2, 1e-4, 1e-6, 1e-9)
        ]
        floating = sum(
            caplet_numeraire(c, option_curve_set) * caplet_forward_rate(c, option_curve_set)
            for c in cap.caplets
        )
        assert values == sorted(values)
        assert values[-1] == pytest.approx(floating, rel=1e-6)

    def test_a_single_caplet_at_a_zero_strike_is_its_discounted_forward(
        self, option_curve_set
    ):
        cap, _ = _pair(strike=0.0)
        caplet = cap.caplets[0]
        expected = caplet_numeraire(caplet, option_curve_set) * caplet_forward_rate(caplet, option_curve_set)
        assert caplet_pv(
            caplet, option_curve_set, Volatility.lognormal_percent(20.0)
        ) == pytest.approx(expected, rel=1e-12)

    def test_the_matching_floorlet_is_worthless(self, option_curve_set):
        _, floor = _pair(strike=0.0)
        assert caplet_pv(
            floor.caplets[0], option_curve_set, Volatility.lognormal_percent(20.0)
        ) == 0.0


class TestMissingForward:
    """PRD-002 AC-2.3: a period the curve does not reach is refused, never extrapolated."""

    def test_a_cap_running_past_the_curve_refuses(self, option_curve_set):
        far = CapFloor(
            effective=AS_OF + timedelta(days=365),
            maturity=AS_OF + timedelta(days=365 * 25),
            strike=STRIKE, notional=1e8, product="cap",
        )
        with pytest.raises(MissingForwardError, match="past the"):
            cap_floor_pv(far, option_curve_set, VOL)

    def test_the_refusal_names_the_period_and_the_last_node(self, option_curve_set):
        far = CapFloor(
            effective=AS_OF + timedelta(days=365),
            maturity=AS_OF + timedelta(days=365 * 25),
            strike=STRIKE, notional=1e8, product="cap",
        )
        with pytest.raises(MissingForwardError) as caught:
            cap_floor_pv(far, option_curve_set, VOL)
        assert option_curve_set.projection.nodes[-1].isoformat() in str(caught.value)
        assert "never quoted" in str(caught.value)

    def test_a_cap_inside_the_curve_is_fine(self, option_curve_set):
        cap, _ = _pair()
        assert cap_floor_pv(cap, option_curve_set, VOL).value > 0.0


QL_FIVE_YEAR = {
    # QuantLib 1.44, independent of this package: ql.Cap / ql.Floor on an
    # IborLeg of Term SOFR 3M -- UnitedStates(GovernmentBond), Modified
    # Following, backward schedule, ACT/360 -- from 2026-04-15 to 2031-04-15,
    # notional 10M, strike 4.1%, priced on FlatForward(4%, continuous,
    # ACT/365F) with evaluation date 2026-01-15; BachelierCapFloorEngine at
    # 90 bp and BlackCapFloorEngine at 25%. The key's first entry is the
    # IborIndex fixingDays. Generated by work-fix-options-vol/ql_caps.py in
    # the review scratchpad, 2026-10-15.
    (0, "cap", "normal"): 216763.10546190687,
    (0, "floor", "normal"): 277899.59052539297,
    (0, "cap", "lognormal"): 243993.08471698035,
    (0, "floor", "lognormal"): 305129.5697804665,
    (2, "cap", "normal"): 216249.07217321548,
    (2, "floor", "normal"): 277385.5572367016,
    (2, "cap", "lognormal"): 243426.63505453937,
    (2, "floor", "lognormal"): 304563.1201180256,
}
QL_FIRST_FIXINGS = ("2026-04-13", "2026-07-13", "2026-10-13", "2027-01-13", "2027-04-13", "2027-07-13")
"""QuantLib's fixing dates for the first six periods of that cap, fixingDays = 2."""


class TestTheOptionExpiresAtTheFixing:
    """A caplet on Term SOFR is an option until its rate is set, not until it accrues.

    Term SOFR is set two US Government Securities business days before the
    period starts (the ARRC convention), so that is where the optionality
    ends. Measuring expiry to the accrual start gave every caplet two days of
    time value it does not have: +0.24% on a five-year cap against QuantLib
    with fixingDays = 2.
    """

    @staticmethod
    def _five_year(product: str, lag: int | None = None) -> CapFloor:
        terms = {
            "effective": date(2026, 4, 15), "maturity": date(2031, 4, 15), "strike": 0.041,
            "notional": 10_000_000.0, "product": product, "frequency_months": 3,
        }
        if lag is not None:
            terms["fixing_lag_days"] = lag
        return CapFloor(**terms)

    @pytest.mark.parametrize("product", ["cap", "floor"])
    @pytest.mark.parametrize(
        "units,vol",
        [("normal", Volatility.normal_bp(90.0)), ("lognormal", Volatility.lognormal_percent(25.0))],
    )
    def test_the_default_matches_quantlib_with_two_fixing_days(
        self, option_curve_set, product, units, vol
    ):
        value = cap_floor_pv(self._five_year(product), option_curve_set, vol).value
        assert value == pytest.approx(QL_FIVE_YEAR[(2, product, units)], rel=1e-13, abs=0.0)

    @pytest.mark.parametrize("product", ["cap", "floor"])
    @pytest.mark.parametrize(
        "units,vol",
        [("normal", Volatility.normal_bp(90.0)), ("lognormal", Volatility.lognormal_percent(25.0))],
    )
    def test_a_zero_lag_still_reproduces_quantlib_without_one(
        self, option_curve_set, product, units, vol
    ):
        # The old behaviour, kept reachable and still verified.
        value = cap_floor_pv(self._five_year(product, lag=0), option_curve_set, vol).value
        assert value == pytest.approx(QL_FIVE_YEAR[(0, product, units)], rel=1e-13, abs=0.0)

    def test_the_fixing_dates_are_quantlibs(self):
        cap = self._five_year("cap")
        assert tuple(c.fixing_date.isoformat() for c in cap.caplets[:6]) == QL_FIRST_FIXINGS
        assert cap.fixing_lag_days == TERM_RATE_FIXING_LAG_DAYS == 2

    def test_the_fixing_rolls_back_over_a_holiday(self):
        # The period from 2027-01-19 starts the day after Martin Luther King
        # Day, so two business days back is Thursday 14 January, not Sunday 17.
        caplet = replace(self._five_year("cap").caplets[0], accrual_start=date(2027, 1, 19))
        assert caplet.fixing_date == date(2027, 1, 14)

    def test_the_evidence_carries_the_fixing_and_its_expiry(self, option_curve_set):
        result = cap_floor_pv(self._five_year("cap"), option_curve_set, VOL)
        first = result.evidence.fields["per_period"][0]
        assert first["fixing_date"] == "2026-04-13"
        assert first["expiry_years"] == pytest.approx((date(2026, 4, 13) - AS_OF).days / 365.0)
        assert result.evidence.fields["fixing_lag_days"] == 2

    def test_a_negative_lag_refuses(self):
        with pytest.raises(ValueError, match="fixing_lag_days"):
            self._five_year("cap", lag=-1)


class TestStructure:
    """The strip itself: periods, kinds and what the payload carries."""

    def test_the_periods_come_from_the_shared_schedule_generator(self, option_curve_set):
        cap, _ = _pair()
        assert len(cap.caplets) == 20
        assert cap.caplets[0].accrual_start == cap.effective
        assert cap.caplets[-1].accrual_end == cap.maturity

    def test_a_cap_is_calls_and_a_floor_is_puts(self):
        cap, floor = _pair()
        assert all(c.kind is OptionKind.CALL for c in cap.caplets)
        assert all(c.kind is OptionKind.PUT for c in floor.caplets)

    def test_the_first_period_can_be_dropped(self, option_curve_set):
        cap, _ = _pair()
        without = replace(cap, include_first_period=False)
        assert len(without.caplets) == len(cap.caplets) - 1
        assert cap_floor_pv(without, option_curve_set, VOL).value < cap_floor_pv(
            cap, option_curve_set, VOL
        ).value

    def test_an_unknown_product_refuses(self):
        collar = CapFloor(
            effective=AS_OF, maturity=AS_OF + timedelta(days=365),
            strike=STRIKE, product="collar",
        )
        with pytest.raises(ValueError, match="cap"):
            _ = collar.kind

    def test_the_payload_details_every_period(self, option_curve_set):
        cap, _ = _pair()
        payload = cap_floor_pv(cap, option_curve_set, VOL).to_dict()
        per_period = payload["evidence"]["fields"]["per_period"]
        assert len(per_period) == len(cap.caplets)
        assert all({"forward", "expiry_years", "numeraire", "value"} <= set(p) for p in per_period)
        assert payload["evidence"]["fields"]["flat_volatility"] is True

    def test_a_cap_has_greeks_too(self, option_curve_set):
        cap, _ = _pair()
        greeks = option_greeks(cap, option_curve_set, VOL)
        assert greeks.vega > 0.0
        assert greeks.theta < 0.0
        assert greeks.value == pytest.approx(cap_floor_pv(cap, option_curve_set, VOL).value)
