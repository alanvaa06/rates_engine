"""PRD-003 AC-3.3 and PRD-003 AC-3.4: four conventions, four strikes, no default.

The criterion the research gate made real. It could not establish which
delta convention USD/MXN trades on — Banxico, ISDA and CME are all 403 from
the build environment — so a default here would let an unverified
convention silently set every strike in the smile. There is none, and
asking for a strike without stating one raises.

Everything below is checkable without a single MXN convention, because each
test *declares* the convention it is testing. That is the shape of the
whole phase: the machinery is knowable, the market default is not.
"""

from __future__ import annotations

import math

import pytest

from rates_engine.errors import DeltaConventionError, RatesEngineError
from rates_engine.fx import garman_kohlhagen as gk
from rates_engine.fx.delta import (
    DeltaBasis,
    DeltaConvention,
    PremiumAdjustment,
    delta,
    strike_from_delta,
)
from rates_engine.volatility.kinds import OptionKind

SPOT = 18.50
EXPIRY = 0.25
R_MXN = 0.0950
R_USD = 0.0420
VOL = 0.115

ALL_FOUR = [
    DeltaConvention(basis, adjustment)
    for basis in DeltaBasis
    for adjustment in PremiumAdjustment
]
UNADJUSTED = [c for c in ALL_FOUR if not c.is_premium_adjusted]
ADJUSTED = [c for c in ALL_FOUR if c.is_premium_adjusted]


def _strike(target, kind, convention):
    return strike_from_delta(SPOT, target, EXPIRY, R_MXN, R_USD, VOL, kind, convention)


def _delta(strike, kind, convention):
    return delta(SPOT, strike, EXPIRY, R_MXN, R_USD, VOL, kind, convention)


class TestTheConventionIsNeverAssumed:
    """PRD-003 AC-3.4."""

    def test_a_strike_from_a_delta_refuses_without_one(self):
        with pytest.raises(DeltaConventionError) as excinfo:
            strike_from_delta(SPOT, 0.25, EXPIRY, R_MXN, R_USD, VOL, OptionKind.CALL)
        assert "does not name a strike" in str(excinfo.value)

    def test_a_delta_refuses_without_one_too(self):
        with pytest.raises(DeltaConventionError):
            delta(SPOT, 19.0, EXPIRY, R_MXN, R_USD, VOL, OptionKind.CALL)

    def test_the_refusal_lists_all_four(self):
        with pytest.raises(DeltaConventionError) as excinfo:
            delta(SPOT, 19.0, EXPIRY, R_MXN, R_USD, VOL, OptionKind.CALL)
        message = str(excinfo.value)
        for name in ("spot unadjusted", "forward unadjusted",
                     "spot premium_adjusted", "forward premium_adjusted"):
            assert name in message

    def test_the_refusal_says_why_there_is_no_default(self):
        with pytest.raises(DeltaConventionError) as excinfo:
            delta(SPOT, 19.0, EXPIRY, R_MXN, R_USD, VOL, OptionKind.CALL)
        assert "research gate" in str(excinfo.value)

    def test_it_is_catchable_as_an_engine_error(self):
        with pytest.raises(RatesEngineError):
            delta(SPOT, 19.0, EXPIRY, R_MXN, R_USD, VOL, OptionKind.CALL)

    def test_the_convention_has_no_default_value_at_all(self):
        """Not `DeltaConvention()` with sensible fields — the dataclass
        requires both halves, because either one alone is ambiguous."""
        with pytest.raises(TypeError):
            DeltaConvention()  # type: ignore[call-arg]


class TestTheFourGiveFourStrikes:
    """PRD-003 AC-3.3: the same quoted delta, four different strikes."""

    def test_every_convention_round_trips(self):
        for convention in ALL_FOUR:
            for kind in OptionKind:
                strike = _strike(0.25, kind, convention)
                assert abs(_delta(strike, kind, convention)) == pytest.approx(0.25, abs=1e-9)

    def test_the_four_strikes_are_all_distinct(self):
        strikes = [_strike(0.25, OptionKind.CALL, c) for c in ALL_FOUR]
        assert len(set(round(k, 8) for k in strikes)) == 4

    def test_premium_adjustment_lowers_a_call_strike(self):
        """The premium is paid in the base currency, so it already carries
        base-currency exposure; netting it off means less strike is needed
        for the same delta."""
        for basis in DeltaBasis:
            plain = _strike(0.25, OptionKind.CALL,
                            DeltaConvention(basis, PremiumAdjustment.UNADJUSTED))
            adjusted = _strike(0.25, OptionKind.CALL,
                               DeltaConvention(basis, PremiumAdjustment.PREMIUM_ADJUSTED))
            assert adjusted < plain

    def test_the_adjustment_is_worth_hundreds_of_pips_not_a_rounding(self):
        """The 'trap' the criterion turns into a test: this is not a
        third-decimal difference, it is a different option."""
        plain = _strike(0.25, OptionKind.CALL,
                        DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED))
        adjusted = _strike(0.25, OptionKind.CALL,
                           DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.PREMIUM_ADJUSTED))
        pips = (plain - adjusted) / 1e-4
        assert pips > 100

    def test_the_gap_grows_with_volatility(self):
        def gap(vol):
            plain = strike_from_delta(
                SPOT, 0.25, EXPIRY, R_MXN, R_USD, vol, OptionKind.CALL,
                DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED),
            )
            adjusted = strike_from_delta(
                SPOT, 0.25, EXPIRY, R_MXN, R_USD, vol, OptionKind.CALL,
                DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.PREMIUM_ADJUSTED),
            )
            return plain - adjusted

        gaps = [gap(v) for v in (0.05, 0.10, 0.20, 0.35)]
        assert all(b > a for a, b in zip(gaps, gaps[1:], strict=False))

    def test_spot_and_forward_differ_by_the_foreign_discount(self):
        """For the unadjusted pair the relation is exact: the spot delta is
        the forward delta scaled by `e^{-r_f T}`."""
        strike = 19.5
        spot_delta = _delta(strike, OptionKind.CALL,
                            DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED))
        fwd_delta = _delta(strike, OptionKind.CALL,
                           DeltaConvention(DeltaBasis.FORWARD, PremiumAdjustment.UNADJUSTED))
        assert spot_delta == pytest.approx(fwd_delta * math.exp(-R_USD * EXPIRY), abs=1e-14)


class TestSignsAndMagnitudes:
    """A '25 delta put' is said with a positive number and has a negative delta."""

    def test_a_call_delta_is_positive_and_a_put_delta_negative(self):
        for convention in ALL_FOUR:
            assert _delta(19.5, OptionKind.CALL, convention) > 0.0
            assert _delta(17.5, OptionKind.PUT, convention) < 0.0

    def test_the_target_is_a_magnitude_for_both(self):
        for convention in ALL_FOUR:
            call = _strike(0.25, OptionKind.CALL, convention)
            put = _strike(0.25, OptionKind.PUT, convention)
            assert _delta(call, OptionKind.CALL, convention) == pytest.approx(0.25, abs=1e-9)
            assert _delta(put, OptionKind.PUT, convention) == pytest.approx(-0.25, abs=1e-9)

    def test_the_put_strike_is_below_the_call_strike(self):
        for convention in ALL_FOUR:
            assert _strike(0.25, OptionKind.PUT, convention) < _strike(
                0.25, OptionKind.CALL, convention
            )

    @pytest.mark.parametrize("bad", [0.0, 1.0, -0.25, 1.5])
    def test_a_delta_outside_zero_to_one_refuses(self, bad):
        with pytest.raises(ValueError, match="magnitude"):
            _strike(bad, OptionKind.CALL, ALL_FOUR[0])

    def test_a_higher_delta_means_a_lower_call_strike(self):
        for convention in ALL_FOUR:
            strikes = [_strike(d, OptionKind.CALL, convention) for d in (0.10, 0.25, 0.40)]
            assert all(b < a for a, b in zip(strikes, strikes[1:], strict=False))


class TestTheUnadjustedClosedForm:
    """The two conventions that invert analytically."""

    @pytest.mark.parametrize("convention", UNADJUSTED)
    def test_the_fifty_delta_forward_strike_is_near_the_forward(self, convention):
        """Not exactly the forward: the fifty-delta strike is the forward
        scaled by `exp(sigma^2 T / 2)`, which is the lognormal drift."""
        outright = gk.forward(SPOT, EXPIRY, R_MXN, R_USD)
        strike = _strike(0.5, OptionKind.CALL,
                         DeltaConvention(DeltaBasis.FORWARD, PremiumAdjustment.UNADJUSTED))
        assert strike == pytest.approx(outright * math.exp(0.5 * VOL * VOL * EXPIRY), rel=1e-12)
        del convention

    def test_a_spot_delta_above_the_foreign_discount_is_unattainable(self):
        """The spot delta of a call is capped at `e^{-r_f T}`, so asking for
        more than that has no strike — and says so rather than returning an
        extreme one."""
        cap = math.exp(-R_USD * EXPIRY)
        with pytest.raises(DeltaConventionError, match="not attainable"):
            _strike(cap + 1e-6, OptionKind.CALL,
                    DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED))

    def test_just_under_the_cap_is_attainable(self):
        cap = math.exp(-R_USD * EXPIRY)
        strike = _strike(cap - 1e-4, OptionKind.CALL,
                         DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED))
        assert strike > 0.0


class TestPremiumAdjustedIsNotMonotone:
    """The property that makes the numerical inversion non-trivial."""

    @pytest.mark.parametrize("convention", ADJUSTED)
    def test_the_delta_rises_then_falls_in_the_strike(self, convention):
        strikes = [10.0, 14.0, 17.0, 19.0, 22.0, 26.0, 32.0]
        deltas = [_delta(k, OptionKind.CALL, convention) for k in strikes]
        assert deltas[0] < max(deltas)
        assert deltas[-1] < max(deltas)
        peak = deltas.index(max(deltas))
        assert 0 < peak < len(deltas) - 1

    @pytest.mark.parametrize("convention", ADJUSTED)
    def test_it_returns_the_out_of_the_money_branch(self, convention):
        """Two strikes give a 25 delta; the market means the one past the
        peak, which for a call is above the forward."""
        outright = gk.forward(SPOT, EXPIRY, R_MXN, R_USD)
        assert _strike(0.25, OptionKind.CALL, convention) > outright

    @pytest.mark.parametrize("convention", ADJUSTED)
    def test_a_delta_above_the_peak_has_no_strike_and_says_so(self, convention):
        with pytest.raises(DeltaConventionError) as excinfo:
            _strike(0.99, OptionKind.CALL, convention)
        message = str(excinfo.value)
        assert "not monotone" in message
        assert "largest attainable" in message

    @pytest.mark.parametrize("convention", ADJUSTED)
    def test_a_long_dated_high_vol_smile_still_inverts(self, convention):
        """The bracket has to be wide enough for the peak to move a long way
        from the forward."""
        strike = strike_from_delta(
            SPOT, 0.25, 5.0, R_MXN, R_USD, 0.30, OptionKind.CALL, convention
        )
        back = delta(SPOT, strike, 5.0, R_MXN, R_USD, 0.30, OptionKind.CALL, convention)
        assert back == pytest.approx(0.25, abs=1e-8)


class TestSerialisation:
    """Either half alone is ambiguous, so both travel."""

    @pytest.mark.parametrize("convention", ALL_FOUR)
    def test_both_halves_are_in_the_payload(self, convention):
        stored = convention.to_dict()
        assert stored["delta_basis"] in {"spot", "forward"}
        assert stored["premium_adjustment"] in {"unadjusted", "premium_adjusted"}
        assert stored["delta_convention"] == convention.name

    def test_the_name_reads_as_the_market_says_it(self):
        assert DeltaConvention(
            DeltaBasis.SPOT, PremiumAdjustment.PREMIUM_ADJUSTED
        ).name == "spot premium_adjusted"

    def test_there_are_exactly_four(self):
        assert len(ALL_FOUR) == 4
        assert len({c.name for c in ALL_FOUR}) == 4


class TestDeterminism:
    """The premium-adjusted inversion is a numerical search, so it is pinned."""

    @pytest.mark.parametrize("convention", ALL_FOUR)
    def test_the_same_call_twice_is_bit_exact(self, convention):
        assert _strike(0.25, OptionKind.CALL, convention) == _strike(
            0.25, OptionKind.CALL, convention
        )
