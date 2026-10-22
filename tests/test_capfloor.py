"""PRD-002 AC-2.1, 2.2 and 2.3: the strip, the parity that survives summing it, and the gap."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
from conftest import AS_OF

from rates_engine.conventions.option_kind import OptionKind
from rates_engine.conventions.side import Side
from rates_engine.core.errors import MissingFixingError, MissingForwardError
from rates_engine.core.evidence import Provenance
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.capfloor import TERM_RATE_FIXING_LAG_DAYS, CapFloor
from rates_engine.instruments.swaps import IRSwap
from rates_engine.market.snapshot import MarketSnapshot, Series
from rates_engine.models import bachelier, black
from rates_engine.pricing.linear import pv
from rates_engine.pricing.options import (
    PeriodStatus,
    cap_floor_periods,
    cap_floor_pv,
    caplet_forward_rate,
    caplet_numeraire,
    caplet_pv,
)
from rates_engine.risk.greeks import option_greeks
from rates_engine.volatility.units import Volatility, VolUnits

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

    @pytest.mark.parametrize("lag", [1.5, 2.0, True, "2"])
    def test_a_lag_that_is_not_a_whole_number_of_days_refuses_at_construction(self, lag):
        # 1.5 used to be accepted; add_business_days' countdown then never
        # reached zero, walked back for seconds and died on "year 0 is out
        # of range". A bool is an int to Python and not a number of days.
        with pytest.raises(ValueError, match="whole number"):
            self._five_year("cap", lag=lag)
        caplet = self._five_year("cap").caplets[0]
        with pytest.raises(ValueError, match="whole number"):
            replace(caplet, fixing_lag_days=lag)


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


SEASONED_QL = {
    # QuantLib 1.44, set up exactly as QL_FIVE_YEAR (fixingDays = 2), on the
    # strip from 2025-01-15 to 2028-01-18 struck at 3%, notional 10M, with
    # index.addFixing(d, 0.043) for every fixing date d on or before
    # 2026-01-15. QuantLib's caps drop a period paid on or before the
    # evaluation date and value one already fixed at its intrinsic value on
    # the historic fixing -- the rule pricing.options now follows.
    ("floor", "normal"): 11200.150898214792,
    ("floor", "lognormal"): 9891.506487209324,
    ("cap", "normal"): 239215.22873756554,
    ("cap", "lognormal"): 237906.58432656006,
}
SEASONED_CAP_QL = {"normal": 206098.42405991862, "lognormal": 204921.42004308637}
"""QuantLib 1.44 as above, cap at 3% from 2025-12-15 to 2027-12-15, 2025-12-11 fixed at 4.30%."""

SEASONED_WITHOUT_TODAY_QL = {
    # QuantLib 1.44 as SEASONED_QL, but with only the fixings dated *before*
    # 2026-01-15 in the history. With enforcesTodaysHistoricFixings = False,
    # its default, QuantLib forecasts the period fixing that day from the
    # curve and values it with no time left. Script: work-fix-options-vol/
    # ql_today.py in the review scratchpad.
    ("cap", "normal"): 230920.174553038,
    ("cap", "lognormal"): 229611.5301420325,
    ("floor", "normal"): 11200.150898214792,
    ("floor", "lognormal"): 9891.506487209324,
}
QL_TODAYS_FORECAST = 0.03964725409627512
"""QuantLib's forecast of the 2026-01-15 fixing for the period 2026-01-20 to 2026-04-20."""

FIRST_FIXING_TODAY_QL = {
    # QuantLib 1.44, cap at 4.2% on 10M from 2026-01-20 to 2027-01-20, whose
    # first period fixes on the evaluation date 2026-01-15: no history, and
    # with a 4.50% fixing for that day added. Same script.
    ("normal", None): 11018.001739678133,
    ("lognormal", None): 13288.573871299286,
    ("normal", 0.045): 18440.324596276183,
    ("lognormal", 0.045): 20710.896727897336,
}


def _fixings(*days: date, rate: float = 0.043) -> MarketSnapshot:
    """Term SOFR 3M fixings, all at ``rate``, under the index's name."""
    return MarketSnapshot(
        AS_OF,
        {
            "TERM_SOFR_3M": Series(
                "TERM_SOFR_3M",
                tuple(days),
                tuple(rate for _ in days),
                Provenance(
                    source="test", series_id="TERM_SOFR_3M", instrument_kind="term_sofr_fixing"
                ),
            )
        },
    )


def _seasoned(product: str) -> CapFloor:
    """Started 2025-01-15, valued 2026-01-15: four periods paid, two fixed, the rest live."""
    return CapFloor(
        effective=date(2025, 1, 15), maturity=date(2028, 1, 18), strike=0.03,
        notional=10_000_000.0, product=product, frequency_months=3,
    )


SEASONED_FIXINGS = (date(2025, 10, 16), date(2026, 1, 15))
"""The fixings of the two periods fixed but unpaid on 2026-01-15; the second is that day's."""


class TestASeasonedCapOrFloor:
    """A strip that has started is valued on what has already happened.

    The floor below used to be worth 314,181.72, of which 231,666.67 was four
    periods paid in 2025 and counted again at a discount factor of one; its
    running period was priced off a 2.61% "forward" read across the
    valuation date on a 4% curve. Now a paid period is gone, a fixed one is
    its intrinsic value on the published fixing, and only the rest is an
    option -- QuantLib's rule, and its numbers.
    """

    @pytest.mark.parametrize("product", ["floor", "cap"])
    @pytest.mark.parametrize(
        "units,vol",
        [("normal", Volatility.normal_bp(90.0)), ("lognormal", Volatility.lognormal_percent(25.0))],
    )
    def test_it_matches_quantlib_with_the_fixings(self, option_curve_set, product, units, vol):
        result = cap_floor_pv(
            _seasoned(product), option_curve_set, vol, fixings=_fixings(*SEASONED_FIXINGS)
        )
        assert result.value == pytest.approx(SEASONED_QL[(product, units)], rel=1e-13, abs=0.0)

    def test_paid_periods_are_not_valued(self, option_curve_set):
        floor = _seasoned("floor")
        result = cap_floor_pv(floor, option_curve_set, VOL, fixings=_fixings(*SEASONED_FIXINGS))
        fields = result.evidence.fields
        assert fields["periods_paid"] == 4
        assert fields["periods"] == len(floor.caplets) - 4
        assert all(p["payment"] > AS_OF.isoformat() for p in fields["per_period"])

    @pytest.mark.parametrize(
        "units,vol",
        [("normal", Volatility.normal_bp(90.0)), ("lognormal", Volatility.lognormal_percent(25.0))],
    )
    def test_a_running_period_is_its_intrinsic_value_on_the_fixing(
        self, option_curve_set, units, vol
    ):
        cap = CapFloor(
            effective=date(2025, 12, 15), maturity=date(2027, 12, 15), strike=0.03,
            notional=10_000_000.0, product="cap", frequency_months=3,
        )
        result = cap_floor_pv(cap, option_curve_set, vol, fixings=_fixings(date(2025, 12, 11)))
        first = result.evidence.fields["per_period"][0]
        # Closed form: (4.30% - 3%) on 91/360 of 10M, paid 2026-03-16, sixty
        # days out on a flat 4% continuous ACT/365F curve.
        intrinsic = 0.013 * (91 / 360) * 10_000_000.0 * math.exp(-0.04 * 60 / 365)
        assert first["status"] == "fixed"
        assert first["fixing"] == 0.043
        assert first["value"] == pytest.approx(intrinsic, rel=1e-14, abs=0.0)
        assert result.value == pytest.approx(SEASONED_CAP_QL[units], rel=1e-13, abs=0.0)

    def test_without_fixings_it_refuses_and_says_what_to_pass(self, option_curve_set):
        with pytest.raises(MissingFixingError) as caught:
            cap_floor_pv(_seasoned("floor"), option_curve_set, VOL)
        message = str(caught.value)
        assert "fixings=" in message
        assert "'TERM_SOFR_3M'" in message
        assert "2025-10-16" in message

    @pytest.mark.parametrize("product", ["cap", "floor"])
    @pytest.mark.parametrize(
        "units,vol",
        [("normal", Volatility.normal_bp(90.0)), ("lognormal", Volatility.lognormal_percent(25.0))],
    )
    def test_a_fixing_on_the_valuation_date_comes_from_the_curve_when_absent(
        self, option_curve_set, product, units, vol
    ):
        # 2026-01-15 is both the valuation date and the fixing of the period
        # from 2026-01-20. Without that day's fixing the period is valued on
        # the curve's forward with no time left -- QuantLib's default -- and
        # the evidence says so; the fixing before it is still required.
        result = cap_floor_pv(
            _seasoned(product), option_curve_set, vol, fixings=_fixings(date(2025, 10, 16))
        )
        assert result.value == pytest.approx(
            SEASONED_WITHOUT_TODAY_QL[(product, units)], rel=1e-13, abs=0.0
        )
        today = next(
            p for p in result.evidence.fields["per_period"] if p["fixing_date"] == "2026-01-15"
        )
        assert today["status"] == "fixes_today"
        assert today["fixing_source"] == "curve_forward_on_fixing_day"
        assert today["fixing"] is None
        assert today["expiry_years"] == 0.0
        assert today["forward"] == pytest.approx(QL_TODAYS_FORECAST, rel=1e-13, abs=0.0)

    def test_a_fixing_on_the_valuation_date_is_used_when_supplied(self, option_curve_set):
        result = cap_floor_pv(
            _seasoned("cap"), option_curve_set, VOL, fixings=_fixings(*SEASONED_FIXINGS)
        )
        today = next(
            p for p in result.evidence.fields["per_period"] if p["fixing_date"] == "2026-01-15"
        )
        assert today["status"] == "fixed"
        assert today["fixing_source"] == "published"
        assert today["fixing"] == 0.043

    @pytest.mark.parametrize("rate", [float("nan"), float("inf")])
    def test_a_fixing_that_is_not_a_number_is_never_valued(self, option_curve_set, rate):
        # What matters is that it never reaches a price, not which layer
        # stops it: market.snapshot may refuse it when the series is built
        # (ValueError), and the pricer refuses it when it reads it
        # (MissingFixingError). Both messages print the value.
        with pytest.raises((ValueError, MissingFixingError), match=r"\b(nan|inf)\b"):
            cap_floor_pv(
                _seasoned("floor"), option_curve_set, VOL,
                fixings=_fixings(*SEASONED_FIXINGS, rate=rate),
            )

    def test_the_pricer_refuses_a_nan_that_got_past_the_series(self, option_curve_set):
        # The pricer's own guard, whatever Series validates: a NaN put into a
        # series without its constructor -- as a subclass or a later edit of
        # market.snapshot could -- is still not priced.
        unvalidated = object.__new__(Series)
        for name, value in (
            ("series_id", "TERM_SOFR_3M"),
            ("dates", SEASONED_FIXINGS),
            ("values", (float("nan"), float("nan"))),
            ("provenance", Provenance(source="test", series_id="TERM_SOFR_3M")),
        ):
            object.__setattr__(unvalidated, name, value)
        snapshot = MarketSnapshot(AS_OF, {"TERM_SOFR_3M": unvalidated})
        with pytest.raises(MissingFixingError, match="not a finite number"):
            cap_floor_pv(_seasoned("floor"), option_curve_set, VOL, fixings=snapshot)

    def test_the_fixings_reach_the_evidence(self, option_curve_set):
        snapshot = _fixings(*SEASONED_FIXINGS)
        result = cap_floor_pv(_seasoned("floor"), option_curve_set, VOL, fixings=snapshot)
        assert snapshot.series["TERM_SOFR_3M"].provenance in result.evidence.inputs
        fixed = [p for p in result.evidence.fields["per_period"] if p["status"] == "fixed"]
        assert [p["fixing_date"] for p in fixed] == ["2025-10-16", "2026-01-15"]
        assert all(p["forward"] is None and p["expiry_years"] == 0.0 for p in fixed)

    def test_a_forward_starting_cap_is_untouched_by_fixings(self, option_curve_set):
        cap, _ = _pair()
        plain = cap_floor_pv(cap, option_curve_set, VOL)
        with_fixings = cap_floor_pv(
            cap, option_curve_set, VOL, fixings=_fixings(*SEASONED_FIXINGS)
        )
        assert with_fixings.value == plain.value
        assert with_fixings.evidence.inputs == ()
        assert plain.evidence.fields["periods_paid"] == 0

    def test_a_strip_with_every_period_paid_refuses(self, option_curve_set):
        old = CapFloor(
            effective=date(2024, 1, 16), maturity=date(2025, 1, 16), strike=0.03,
            notional=10_000_000.0,
        )
        with pytest.raises(ValueError, match="nothing left"):
            cap_floor_pv(old, option_curve_set, VOL)

    def test_a_period_that_began_before_the_curve_has_no_forward(self, option_curve_set):
        # The curve's discount factor is one on and before its date, so the
        # "forward" of the period from 2025-12-15 came out 2.61% on a 4%
        # curve: the rate from 2026-01-15 to the end, spread over the whole
        # accrual. Its rate is a fixing, and asking for a forward says so.
        running = CapFloor(
            effective=date(2025, 12, 15), maturity=date(2026, 12, 15), strike=0.03,
            notional=10_000_000.0,
        ).caplets[0]
        with pytest.raises(MissingFixingError, match="fixing"):
            caplet_forward_rate(running, option_curve_set)

    def test_one_caplet_follows_the_same_rule(self, option_curve_set):
        caplets = _seasoned("floor").caplets
        assert caplet_pv(caplets[0], option_curve_set, VOL) == 0.0  # paid in January 2025
        with pytest.raises(MissingFixingError):
            caplet_pv(caplets[4], option_curve_set, VOL)  # fixed 2025-10-16, paid 2026-01-20
        assert caplet_pv(
            caplets[4], option_curve_set, VOL, fixings=_fixings(*SEASONED_FIXINGS)
        ) == 0.0  # a 3% floor on a 4.30% fixing

    def test_the_periods_say_where_they_stand(self, option_curve_set):
        periods = cap_floor_periods(
            _seasoned("floor"), option_curve_set, VOL, fixings=_fixings(*SEASONED_FIXINGS)
        )
        statuses = [p.status for p in periods]
        assert statuses[:2] == [PeriodStatus.FIXED, PeriodStatus.FIXED]
        assert set(statuses[2:]) == {PeriodStatus.LIVE}
        without_today = cap_floor_periods(
            _seasoned("floor"), option_curve_set, VOL, fixings=_fixings(date(2025, 10, 16))
        )
        assert [p.status for p in without_today[:2]] == [
            PeriodStatus.FIXED, PeriodStatus.FIXES_TODAY,
        ]

    def test_a_seasoned_cap_has_greeks(self, option_curve_set):
        cap = CapFloor(
            effective=date(2025, 12, 15), maturity=date(2027, 12, 15), strike=0.03,
            notional=10_000_000.0, product="cap", frequency_months=3,
        )
        snapshot = _fixings(date(2025, 12, 11))
        greeks = option_greeks(cap, option_curve_set, VOL, fixings=snapshot)
        assert greeks.value == cap_floor_pv(cap, option_curve_set, VOL, fixings=snapshot).value
        assert greeks.vega > 0.0
        with pytest.raises(MissingFixingError):
            option_greeks(cap, option_curve_set, VOL)

    def test_theta_does_not_trip_over_tomorrows_fixing(self, option_curve_set):
        # The first period fixes on 2026-01-16, the day after the valuation
        # date. Theta shortens every expiry by a day and re-classifies
        # nothing, so it neither asks for tomorrow's fixing nor drops a period.
        cap = CapFloor(
            effective=date(2026, 1, 21), maturity=date(2027, 1, 21), strike=0.042,
            notional=10_000_000.0,
        )
        assert cap.caplets[0].fixing_date == date(2026, 1, 16)
        greeks = option_greeks(cap, option_curve_set, VOL)
        assert greeks.theta < 0.0


class TestALognormalCapHasTheVegaOfItsCaplets:
    """Vega per basis point of normal vol, converted caplet by caplet.

    A flat lognormal quote is a different normal volatility on every caplet,
    because each has its own forward. The bump used to convert through the
    last caplet's forward for the whole strip, and with the at-the-money
    leading term: 3,820.56 per bp on the cap below, against 5,959.07 summed
    caplet by caplet with the exact conversion (on the same expiries).
    """

    @staticmethod
    def _sloping_curve() -> CurveSet:
        # Zero rates from 2% at one year to 5% at fifteen, continuous.
        tenors = range(1, 16)
        zeros = {k: 0.02 + 0.03 * (k - 1) / 14 for k in tenors}
        nodes = tuple(AS_OF + timedelta(days=365 * k) for k in tenors)
        return CurveSet(
            DiscountCurve(AS_OF, nodes, tuple(math.exp(-zeros[k] * k) for k in tenors))
        )

    def test_it_is_the_vega_of_each_caplet_at_its_own_normal_vol(self):
        curves = self._sloping_curve()
        cap = CapFloor(date(2026, 4, 15), date(2036, 4, 15), 0.04, 10_000_000.0, "cap", 3)
        quoted = Volatility(0.25, VolUnits.LOGNORMAL_DECIMAL)
        # The reference: the same strip, each caplet quoted at the normal
        # volatility that reproduces its own Black price, bumped one basis
        # point in Bachelier -- the definition of a normal vega, written out.
        reference = 0.0
        closed_form = 0.0
        for caplet in cap.caplets:
            forward = caplet_forward_rate(caplet, curves)
            expiry = caplet.time_to_expiry(AS_OF)
            annuity = caplet_numeraire(caplet, curves)
            unit = black.price(forward, caplet.strike, expiry, 0.25)
            normal = bachelier.implied_normal_vol(unit, forward, caplet.strike, expiry)
            reference += annuity * (
                bachelier.price(forward, caplet.strike, expiry, normal + 1e-4) - unit
            )
            closed_form += bachelier.vega(
                forward, caplet.strike, expiry, normal, annuity=annuity
            ) * 1e-4
        vega = option_greeks(cap, curves, quoted).vega
        assert vega == pytest.approx(reference, rel=1e-8, abs=0.0)
        # A forward difference of one basis point against the derivative:
        # close, not equal, and nowhere near the 30% the last-forward
        # conversion was off by.
        assert vega == pytest.approx(closed_form, rel=0.01)

    def test_a_normal_quote_is_bumped_as_it_was(self, option_curve_set):
        cap, _ = _pair()
        bumped = cap_floor_pv(cap, option_curve_set, Volatility.normal_bp(81.0)).value
        base = cap_floor_pv(cap, option_curve_set, VOL).value
        assert option_greeks(cap, option_curve_set, VOL).vega == bumped - base

    def test_a_fixed_caplet_has_no_vega(self, option_curve_set):
        cap = CapFloor(
            effective=date(2025, 12, 15), maturity=date(2026, 6, 15), strike=0.03,
            notional=10_000_000.0, product="cap", frequency_months=3,
        )
        snapshot = _fixings(date(2025, 12, 11))
        whole = option_greeks(
            cap, option_curve_set, Volatility.lognormal_percent(25.0), fixings=snapshot
        ).vega
        live_only = option_greeks(
            replace(cap, include_first_period=False), option_curve_set,
            Volatility.lognormal_percent(25.0),
        ).vega
        assert whole == pytest.approx(live_only, rel=1e-12, abs=0.0)


class TestACapWhoseFirstFixingIsToday:
    """A spot-starting strip valued on its first fixing day.

    The cap from 2026-01-20 fixes its first period on 2026-01-15, the
    valuation date. It needs no fixings to be priced -- the curve's forward
    stands in for that day's rate -- and uses the fixing when given one.
    """

    @staticmethod
    def _cap() -> CapFloor:
        return CapFloor(
            effective=date(2026, 1, 20), maturity=date(2027, 1, 20), strike=0.042,
            notional=10_000_000.0,
        )

    @pytest.mark.parametrize(
        "units,vol",
        [("normal", Volatility.normal_bp(90.0)), ("lognormal", Volatility.lognormal_percent(25.0))],
    )
    def test_without_the_fixing_it_matches_quantlibs_forecast(self, option_curve_set, units, vol):
        cap = self._cap()
        assert cap.caplets[0].fixing_date == AS_OF
        value = cap_floor_pv(cap, option_curve_set, vol).value
        assert value == pytest.approx(FIRST_FIXING_TODAY_QL[(units, None)], rel=1e-13, abs=0.0)

    @pytest.mark.parametrize(
        "units,vol",
        [("normal", Volatility.normal_bp(90.0)), ("lognormal", Volatility.lognormal_percent(25.0))],
    )
    def test_with_the_fixing_it_is_the_intrinsic_value_on_it(self, option_curve_set, units, vol):
        result = cap_floor_pv(
            self._cap(), option_curve_set, vol, fixings=_fixings(AS_OF, rate=0.045)
        )
        assert result.value == pytest.approx(
            FIRST_FIXING_TODAY_QL[(units, 0.045)], rel=1e-13, abs=0.0
        )
        # Closed form: (4.50% - 4.2%) on 90/360 of 10M, paid 2026-04-20,
        # 95 days out on a flat 4% continuous ACT/365F curve.
        first = result.evidence.fields["per_period"][0]
        intrinsic = 0.003 * (90 / 360) * 10_000_000.0 * math.exp(-0.04 * 95 / 365)
        assert first["status"] == "fixed"
        assert first["value"] == pytest.approx(intrinsic, rel=1e-13, abs=0.0)

    def test_the_period_fixing_today_has_no_theta_and_no_vega(self, option_curve_set):
        # Its value is intrinsic whatever the volatility and whatever the day,
        # so the strip's theta and vega are those of the periods after it.
        vol = Volatility.lognormal_percent(25.0)
        whole = option_greeks(self._cap(), option_curve_set, vol)
        rest = option_greeks(
            replace(self._cap(), include_first_period=False), option_curve_set, vol
        )
        assert whole.theta == pytest.approx(rest.theta, rel=1e-12, abs=0.0)
        assert whole.vega == pytest.approx(rest.vega, rel=1e-12, abs=0.0)
        assert whole.theta < 0.0

    def test_theta_still_asks_for_no_fixing_tomorrow(self, option_curve_set):
        # Seen from tomorrow, today's fixing would be in the past and
        # required; theta holds every period's status at the valuation date.
        greeks = option_greeks(self._cap(), option_curve_set, VOL)
        assert math.isfinite(greeks.theta)


QL_HIGH_VOL_CAP = {
    # QuantLib 1.44, independent of this package: a quarterly Term SOFR 3M
    # cap from 2026-04-15 (fixingDays = 2, GovernmentBond calendar), notional
    # 10M, struck at the curve's level, on FlatForward(rate, continuous,
    # ACT/365F) as of 2026-01-15, BlackCapFloorEngine at the lognormal vol.
    # The vega is summed over QuantLib's own caplets: per caplet, the
    # Bachelier volatility that reproduces its Black price
    # (bachelierBlackFormulaImpliedVol), and its numeraire times the
    # Bachelier price one basis point higher less the price at it. Value is
    # the cap's NPV. Script: work-fix-options-vol/ql_vega_high.py.
    # (rate, years, sigma): (vega per bp, NPV)
    (0.01, 30, 0.90): (37248.772707535376, 2143926.6451400053),
    (0.01, 20, 1.10): (21546.88293612977, 1509667.6731859013),
    (0.04, 20, 1.30): (15199.640771535185, 4668990.826207118),
}


class TestVegaAtHighVolatilityAndLongExpiry:
    """A lognormal vega at high vol and long expiry is a number.

    The bumped Bachelier price used to be inverted back to a Black
    volatility, and on long-dated caps it passed Black's ceiling well inside
    the accepted band: a 30-year cap at 90% raised ValueError and took delta,
    gamma and theta down with it. The bumped value is Bachelier's in closed
    form; no inversion back is needed.
    """

    @staticmethod
    def _flat(rate: float) -> CurveSet:
        nodes = tuple(AS_OF + timedelta(days=365 * k) for k in range(1, 46))
        return CurveSet(
            DiscountCurve(AS_OF, nodes, tuple(math.exp(-rate * k) for k in range(1, 46)))
        )

    @pytest.mark.parametrize("rate,years,sigma", list(QL_HIGH_VOL_CAP))
    def test_it_matches_quantlib_and_keeps_the_other_greeks(self, rate, years, sigma):
        curves = self._flat(rate)
        cap = CapFloor(date(2026, 4, 15), date(2026 + years, 4, 15), rate, 10_000_000.0, "cap", 3)
        quoted = Volatility(sigma, VolUnits.LOGNORMAL_DECIMAL)
        vega, npv = QL_HIGH_VOL_CAP[(rate, years, sigma)]
        assert cap_floor_pv(cap, curves, quoted).value == pytest.approx(npv, rel=1e-13, abs=0.0)
        greeks = option_greeks(cap, curves, quoted)
        assert greeks.vega == pytest.approx(vega, rel=1e-9, abs=0.0)
        assert all(math.isfinite(g) for g in (greeks.delta, greeks.gamma, greeks.theta))

    def test_a_deep_out_of_the_money_caplet_has_no_negative_vega(self, option_curve_set):
        # Its Black price is below the inversion's tolerance, so its normal
        # vol inverts to zero; the vega is a difference of two Bachelier
        # prices, monotone in the volatility, and cannot come out negative.
        cap = CapFloor(
            effective=date(2026, 2, 17), maturity=date(2026, 5, 18), strike=0.08,
            notional=10_000_000.0,
        )
        vega = option_greeks(cap, option_curve_set, Volatility.lognormal_percent(20.0)).vega
        assert 0.0 <= vega < 1e-20
