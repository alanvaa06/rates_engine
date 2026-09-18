"""PRD-002 AC-1.1, 1.2, 1.3 and 1.5: the two pricers, their identities, and the greeks.

The identities are the point. A closed-form option price can be wrong in ways
that survive inspection and even survive a fixture, because the fixture and
the code can be wrong together. What they cannot survive is put-call parity,
the two degenerate limits, monotonicity in volatility, and the second-order
agreement between two models that approximate the same thing — each of those
pins a different part of the formula, and no plausible transcription error
passes all of them.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from scipy.stats import norm

from rates_engine.errors import ShiftRequiredError
from rates_engine.instruments import OISSwap, Side, Swaption
from rates_engine.optionpricing import (
    forward_swap_rate,
    model_for,
    swaption_annuity,
    swaption_pv,
)
from rates_engine.pricing import dv01
from rates_engine.risk import money_convexity, option_greeks
from rates_engine.volatility import bachelier, black
from rates_engine.volatility.kinds import OptionKind
from rates_engine.volatility.units import Volatility, VolUnits

# Twenty cases: five strikes across two expiries across two volatilities.
CASES = [
    (0.04, strike, expiry, sigma)
    for strike in (0.02, 0.03, 0.04, 0.05, 0.06)
    for expiry, sigma in ((1.0, 0.20), (5.0, 0.35), (1.0, 0.60), (10.0, 0.15))
]


def independent_black(forward, strike, expiry, sigma, *, call=True, annuity=1.0):
    """Black written from the definition, through scipy rather than ``erfc``.

    A second implementation catches a transcription error in the first. It
    cannot catch both being wrong the same way, which is what the identities
    below are for.
    """
    if sigma * math.sqrt(expiry) == 0.0:
        return annuity * max((forward - strike) if call else (strike - forward), 0.0)
    total = sigma * math.sqrt(expiry)
    d1 = (math.log(forward / strike) + 0.5 * total**2) / total
    d2 = d1 - total
    if call:
        return annuity * (forward * norm.cdf(d1) - strike * norm.cdf(d2))
    return annuity * (strike * norm.cdf(-d2) - forward * norm.cdf(-d1))


class TestBachelier:
    """PRD-002 AC-1.1: the formula, and the parity that follows from it identically."""

    @pytest.mark.parametrize("strike", [0.0, 0.02, 0.04, 0.06, -0.01])
    def test_parity_holds_to_machine_precision(self, strike):
        forward, expiry, sigma, annuity = 0.04, 5.0, 0.008, 4.2
        call = bachelier.price(
            forward, strike, expiry, sigma, kind=OptionKind.CALL, annuity=annuity
        )
        put = bachelier.price(
            forward, strike, expiry, sigma, kind=OptionKind.PUT, annuity=annuity
        )
        assert call - put == pytest.approx(annuity * (forward - strike), abs=1e-12)

    def test_the_formula_is_what_the_ac_writes(self):
        forward, strike, expiry, sigma, annuity = 0.04, 0.045, 5.0, 0.008, 4.2
        d = (forward - strike) / (sigma * math.sqrt(expiry))
        expected = annuity * (
            (forward - strike) * norm.cdf(d) + sigma * math.sqrt(expiry) * norm.pdf(d)
        )
        assert bachelier.price(
            forward, strike, expiry, sigma, annuity=annuity
        ) == pytest.approx(expected, rel=1e-13)

    def test_at_the_money_it_is_exactly_the_closed_form(self):
        # d = 0, so the price collapses to A sigma sqrt(T / 2 pi).
        forward, expiry, sigma, annuity = 0.04, 5.0, 0.008, 4.2
        expected = annuity * sigma * math.sqrt(expiry / (2.0 * math.pi))
        assert bachelier.price(
            forward, forward, expiry, sigma, annuity=annuity
        ) == pytest.approx(expected, rel=1e-14)

    def test_it_handles_a_negative_forward_without_complaint(self):
        assert bachelier.price(-0.002, 0.005, 2.0, 0.006) > 0.0

    @pytest.mark.parametrize("expiry,sigma", [(0.0, 0.008), (5.0, 0.0)])
    def test_the_degenerate_cases_are_the_intrinsic_value(self, expiry, sigma):
        assert bachelier.price(0.05, 0.04, expiry, sigma) == pytest.approx(0.01)
        assert bachelier.price(0.03, 0.04, expiry, sigma) == 0.0

    def test_it_increases_with_volatility(self):
        values = [bachelier.price(0.04, 0.045, 5.0, s) for s in (0.002, 0.004, 0.008, 0.016)]
        assert values == sorted(values)

    def test_negative_inputs_refuse(self):
        with pytest.raises(ValueError, match="sigma"):
            bachelier.price(0.04, 0.04, 1.0, -0.001)
        with pytest.raises(ValueError, match="expiry"):
            bachelier.price(0.04, 0.04, -1.0, 0.008)


class TestBlack:
    """PRD-002 AC-1.2: an independent implementation, plus the identities."""

    @pytest.mark.parametrize("forward,strike,expiry,sigma", CASES)
    def test_it_matches_an_independent_implementation(self, forward, strike, expiry, sigma):
        for kind, call in ((OptionKind.CALL, True), (OptionKind.PUT, False)):
            assert black.price(
                forward, strike, expiry, sigma, kind=kind, annuity=4.2
            ) == pytest.approx(
                independent_black(forward, strike, expiry, sigma, call=call, annuity=4.2),
                rel=1e-12,
                abs=1e-15,
            )

    def test_there_are_at_least_twenty_cases(self):
        assert len(CASES) >= 20

    @pytest.mark.parametrize("forward,strike,expiry,sigma", CASES)
    def test_parity_holds_in_every_case(self, forward, strike, expiry, sigma):
        call = black.price(forward, strike, expiry, sigma, kind=OptionKind.CALL, annuity=4.2)
        put = black.price(forward, strike, expiry, sigma, kind=OptionKind.PUT, annuity=4.2)
        assert call - put == pytest.approx(4.2 * (forward - strike), rel=1e-12, abs=1e-14)

    @pytest.mark.parametrize("forward,strike,expiry,sigma", CASES)
    def test_it_increases_with_volatility_in_every_case(self, forward, strike, expiry, sigma):
        low = black.price(forward, strike, expiry, sigma * 0.5, annuity=4.2)
        high = black.price(forward, strike, expiry, sigma * 2.0, annuity=4.2)
        assert high >= low

    @pytest.mark.parametrize("expiry,sigma", [(0.0, 0.2), (5.0, 0.0)])
    def test_the_degenerate_cases_are_the_intrinsic_value(self, expiry, sigma):
        assert black.price(0.05, 0.04, expiry, sigma) == pytest.approx(0.01)
        assert black.price(0.03, 0.04, expiry, sigma) == 0.0

    def test_it_converges_to_bachelier_at_second_order(self):
        # Not "they are close": the gap must fall like sigma^2 T, so halving
        # the volatility must quarter it. A wrong power anywhere in either
        # formula breaks this and survives a tolerance test.
        forward, expiry, annuity = 0.04, 5.0, 4.2
        gaps = []
        for sigma in (0.40, 0.20, 0.10, 0.05):
            relative = abs(
                black.price(forward, forward, expiry, sigma, annuity=annuity)
                - bachelier.price(forward, forward, expiry, sigma * forward, annuity=annuity)
            ) / black.price(forward, forward, expiry, sigma, annuity=annuity)
            gaps.append(relative)
        for coarse, fine in zip(gaps, gaps[1:], strict=False):
            assert coarse / fine == pytest.approx(4.0, rel=0.02)

    def test_a_non_positive_forward_refuses_and_names_the_alternative(self):
        with pytest.raises(ShiftRequiredError, match="Bachelier"):
            black.price(0.0, 0.04, 5.0, 0.2)
        with pytest.raises(ShiftRequiredError):
            black.price(-0.001, 0.04, 5.0, 0.2)

    def test_a_negative_strike_refuses(self):
        with pytest.raises(ShiftRequiredError):
            black.price(0.04, -0.001, 5.0, 0.2)

    def test_a_zero_strike_is_the_limit_not_a_refusal(self):
        # PRD-002 AC-2.2's limit: a call certain to be exercised is worth the forward.
        assert black.price(0.04, 0.0, 5.0, 0.2, annuity=4.2) == pytest.approx(4.2 * 0.04)
        assert black.price(0.04, 0.0, 5.0, 0.2, kind=OptionKind.PUT) == 0.0

    def test_the_limit_is_approached_continuously(self):
        annuity, forward = 4.2, 0.04
        approach = [
            black.price(forward, k, 5.0, 0.2, annuity=annuity) for k in (1e-3, 1e-6, 1e-9)
        ]
        assert approach == sorted(approach)
        assert approach[-1] == pytest.approx(annuity * forward, rel=1e-6)


class TestImpliedVol:
    """PRD-002 AC-1.3: both inversions reproduce the price, and agree at the money."""

    @pytest.mark.parametrize("sigma", [0.002, 0.004, 0.008, 0.020, 0.050])
    def test_normal_inversion_round_trips(self, sigma):
        forward, strike, expiry, annuity = 0.04, 0.05, 5.0, 4.2
        target = bachelier.price(forward, strike, expiry, sigma, annuity=annuity)
        recovered = bachelier.implied_normal_vol(
            target, forward, strike, expiry, annuity=annuity
        )
        assert bachelier.price(
            forward, strike, expiry, recovered, annuity=annuity
        ) == pytest.approx(target, abs=1e-8 * annuity)
        assert recovered == pytest.approx(sigma, rel=1e-9)

    @pytest.mark.parametrize("sigma", [0.05, 0.10, 0.30, 0.80, 1.50])
    def test_lognormal_inversion_round_trips(self, sigma):
        forward, strike, expiry, annuity = 0.04, 0.05, 5.0, 4.2
        target = black.price(forward, strike, expiry, sigma, annuity=annuity)
        recovered = black.implied_lognormal_vol(
            target, forward, strike, expiry, annuity=annuity
        )
        assert black.price(
            forward, strike, expiry, recovered, annuity=annuity
        ) == pytest.approx(target, abs=1e-8 * annuity)
        assert recovered == pytest.approx(sigma, rel=1e-9)

    def test_both_inversions_of_one_price_meet_the_atm_rule(self):
        # One price, inverted under each model. The at-the-money rule says
        # the two answers differ by a factor of the forward, and it is a
        # leading-order rule, so the agreement is close rather than exact.
        forward, expiry, annuity = 0.04, 2.0, 4.2
        target = bachelier.price(forward, forward, expiry, 0.008, annuity=annuity)
        normal = bachelier.implied_normal_vol(target, forward, forward, expiry, annuity=annuity)
        lognormal = black.implied_lognormal_vol(
            target, forward, forward, expiry, annuity=annuity
        )
        assert normal == pytest.approx(lognormal * forward, rel=0.01)
        quoted = Volatility(normal, VolUnits.NORMAL_DECIMAL)
        assert quoted.atm_equivalent_lognormal(forward).as_lognormal_decimal() == pytest.approx(
            lognormal, rel=0.01
        )

    def test_a_target_below_intrinsic_refuses(self):
        with pytest.raises(ValueError, match="intrinsic"):
            bachelier.implied_normal_vol(0.001, 0.05, 0.04, 5.0, annuity=4.2)

    def test_the_intrinsic_value_itself_inverts_to_zero(self):
        assert bachelier.implied_normal_vol(4.2 * 0.01, 0.05, 0.04, 5.0, annuity=4.2) == 0.0

    def test_inversion_needs_a_positive_expiry(self):
        with pytest.raises(ValueError, match="expiry"):
            bachelier.implied_normal_vol(0.1, 0.04, 0.04, 0.0)


class TestSwaptionPricing:
    """The contract, the numeraire, and the model that follows from the units."""

    def test_the_model_follows_the_units(self, atm_swaption, option_curve_set):
        normal = swaption_pv(atm_swaption, option_curve_set, Volatility.normal_bp(80.0))
        lognormal = swaption_pv(
            atm_swaption, option_curve_set, Volatility.lognormal_percent(20.0)
        )
        assert normal.model == "bachelier"
        assert lognormal.model == "black"
        assert model_for(Volatility.normal_bp(80.0)) == "bachelier"

    def test_an_atm_payer_and_receiver_are_worth_the_same(self, atm_swaption, option_curve_set):
        receiver = replace(atm_swaption, side=Side.RECEIVER)
        vol = Volatility.normal_bp(80.0)
        assert swaption_pv(atm_swaption, option_curve_set, vol).value == pytest.approx(
            swaption_pv(receiver, option_curve_set, vol).value, rel=1e-12
        )

    @pytest.mark.parametrize("strike", [0.025, 0.03, 0.045, 0.05, 0.06])
    def test_parity_holds_against_the_swap_struck_there(
        self, underlying_swap, option_curve_set, strike
    ):
        from datetime import timedelta

        from conftest import AS_OF

        expiry = AS_OF + timedelta(days=365 * 5)
        vol = Volatility.normal_bp(80.0)
        payer = Swaption(
            expiry=expiry,
            underlying=underlying_swap,
            strike=strike,
            side=Side.PAYER,
            notional=1e8,
        )
        receiver = replace(payer, side=Side.RECEIVER)
        difference = (
            swaption_pv(payer, option_curve_set, vol).value
            - swaption_pv(receiver, option_curve_set, vol).value
        )
        annuity = swaption_annuity(payer, option_curve_set)
        forward = forward_swap_rate(payer, option_curve_set)
        assert difference == pytest.approx(annuity * (forward - strike), rel=1e-10)

    def test_the_annuity_is_the_numeraire_and_says_so(self, atm_swaption, option_curve_set):
        result = swaption_pv(atm_swaption, option_curve_set, Volatility.normal_bp(80.0))
        assert result.numeraire == pytest.approx(swaption_annuity(atm_swaption, option_curve_set))
        assert "martingale" in result.evidence.fields["numeraire_note"]

    def test_a_swaption_expiring_after_its_underlying_starts_refuses(self, underlying_swap):
        from datetime import timedelta

        from conftest import AS_OF

        with pytest.raises(ValueError, match="already begun"):
            Swaption(
                expiry=AS_OF + timedelta(days=365 * 6),
                underlying=underlying_swap,
                strike=0.04,
            )

    def test_an_unknown_side_refuses(self, underlying_swap):
        from datetime import timedelta

        from conftest import AS_OF

        with pytest.raises(ValueError, match="payer"):
            Swaption(
                expiry=AS_OF + timedelta(days=365 * 5),
                underlying=underlying_swap,
                strike=0.04,
                side="long",
            )

    def test_the_payload_carries_every_model_input(self, atm_swaption, option_curve_set):
        payload = swaption_pv(
            atm_swaption, option_curve_set, Volatility.normal_bp(80.0)
        ).to_dict()
        for key in ("model", "volatility", "forward", "strike", "expiry", "numeraire"):
            assert key in payload
        assert payload["volatility"]["units"] == "normal_bp"
        assert payload["schema_version"] == "1.0"


class TestGreeks:
    """PRD-002 AC-1.5: bumped, reported with units, and satisfying differentiated parity."""

    @pytest.mark.parametrize("strike", [0.025, 0.03, 0.05, 0.06])
    def test_delta_and_gamma_satisfy_differentiated_parity(
        self, underlying_swap, option_curve_set, strike
    ):
        # Put-call parity is V_p - V_r = A(F - K), and both A and F move with
        # the curve. So its first derivative is the DV01 of the swap struck at
        # K and its second is that swap's convexity — not zero. Getting this
        # identity right at every strike is a much tighter constraint than
        # checking the greeks look plausible.
        from datetime import timedelta

        from conftest import AS_OF

        vol = Volatility.normal_bp(80.0)
        payer = Swaption(
            expiry=AS_OF + timedelta(days=365 * 5),
            underlying=underlying_swap,
            strike=strike,
            side=Side.PAYER,
            notional=1e8,
        )
        receiver = replace(payer, side=Side.RECEIVER)
        reference = OISSwap(
            effective=underlying_swap.effective,
            maturity=underlying_swap.maturity,
            fixed_rate=strike,
            notional=1e8,
            side=Side.PAYER,
        )
        gp = option_greeks(payer, option_curve_set, vol)
        gr = option_greeks(receiver, option_curve_set, vol)
        assert gp.delta - gr.delta == pytest.approx(
            dv01(reference, option_curve_set).value, abs=1e-6
        )
        assert gp.gamma - gr.gamma == pytest.approx(
            money_convexity(reference, option_curve_set).value * 1e-8, abs=1e-6
        )

    def test_vega_is_the_same_for_a_payer_and_a_receiver(
        self, atm_swaption, option_curve_set
    ):
        # Parity has no volatility in it, so its vega is zero identically.
        vol = Volatility.normal_bp(80.0)
        receiver = replace(atm_swaption, side=Side.RECEIVER)
        assert option_greeks(atm_swaption, option_curve_set, vol).vega == pytest.approx(
            option_greeks(receiver, option_curve_set, vol).vega, abs=1e-6
        )

    def test_a_long_option_has_positive_gamma_and_negative_theta(
        self, atm_swaption, option_curve_set
    ):
        greeks = option_greeks(atm_swaption, option_curve_set, Volatility.normal_bp(80.0))
        assert greeks.gamma > 0.0
        assert greeks.theta < 0.0
        assert greeks.vega > 0.0

    def test_every_greek_declares_its_unit(self, atm_swaption, option_curve_set):
        payload = option_greeks(
            atm_swaption, option_curve_set, Volatility.normal_bp(80.0)
        ).to_dict()
        assert payload["delta_unit"] == "USD_per_bp"
        assert payload["gamma_unit"] == "USD_per_bp_squared"
        assert payload["vega_unit"] == "USD_per_bp_normal_vol"
        assert payload["theta_unit"] == "USD_per_day"
        assert payload["method"] == "bump_and_reprice"

    def test_the_bumps_are_recorded(self, atm_swaption, option_curve_set):
        greeks = option_greeks(
            atm_swaption, option_curve_set, Volatility.normal_bp(80.0),
            rate_bump_bp=2.0, vol_bump_bp=5.0,
        )
        assert greeks.rate_bump_bp == 2.0
        assert greeks.vol_bump_bp == 5.0
        assert greeks.evidence.fields["vol_bump_basis"] == "normal_volatility"

    def test_vega_is_on_the_normal_basis_whichever_model_priced_it(
        self, atm_swaption, option_curve_set, forward_swap_rate
    ):
        # Two desks quoting different conventions must be able to compare
        # vegas, so the bump is always a basis point of normal volatility.
        normal = Volatility.normal_bp(80.0)
        equivalent = normal.atm_equivalent_lognormal(forward_swap_rate)
        under_black = option_greeks(atm_swaption, option_curve_set, equivalent).vega
        under_bachelier = option_greeks(atm_swaption, option_curve_set, normal).vega
        assert under_black == pytest.approx(under_bachelier, rel=0.05)

    def test_a_non_positive_bump_refuses(self, atm_swaption, option_curve_set):
        with pytest.raises(ValueError, match="bumps must be positive"):
            option_greeks(
                atm_swaption, option_curve_set, Volatility.normal_bp(80.0), vol_bump_bp=0.0
            )

    def test_greeks_of_something_that_is_not_an_option_refuse(
        self, underlying_swap, option_curve_set
    ):
        with pytest.raises(ValueError, match="Swaption and CapFloor"):
            option_greeks(underlying_swap, option_curve_set, Volatility.normal_bp(80.0))
