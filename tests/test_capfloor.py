"""PRD-002 AC-2.1, 2.2 and 2.3: the strip, the parity that survives summing it, and the gap."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import AS_OF

from rates_engine.errors import MissingForwardError
from rates_engine.instruments import CapFloor, IRSwap, Side
from rates_engine.optionpricing import cap_floor_pv, caplet_pv
from rates_engine.pricing import pv
from rates_engine.risk import option_greeks
from rates_engine.volatility import Volatility
from rates_engine.volatility.kinds import OptionKind

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
            c.numeraire(option_curve_set) * c.forward_rate(option_curve_set)
            for c in cap.caplets
        )
        assert values == sorted(values)
        assert values[-1] == pytest.approx(floating, rel=1e-6)

    def test_a_single_caplet_at_a_zero_strike_is_its_discounted_forward(
        self, option_curve_set
    ):
        cap, _ = _pair(strike=0.0)
        caplet = cap.caplets[0]
        expected = caplet.numeraire(option_curve_set) * caplet.forward_rate(option_curve_set)
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
