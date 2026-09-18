"""PRD-003 AC-3.1 and AC-3.5: Garman-Kohlhagen and its greeks.

Garman-Kohlhagen is Black-Scholes with the dividend yield replaced by the
foreign interest rate, so almost everything here is a closed-form identity
that needs no market data to check — which is the point: the research gate
could not reach a single MXN convention, and none of these tests wants one.

The one thing that does need care is which rate is which. `r_domestic`
belongs to the quote currency and `r_foreign` to the base; for USD/MXN that
is MXN and USD, several hundred basis points apart. Reversing them gives a
well-formed price that is wrong, so the asymmetry is tested directly.
"""

from __future__ import annotations

import math

import pytest

from rates_engine.fx import garman_kohlhagen as gk
from rates_engine.fx.quote import USDMXN
from rates_engine.volatility.kinds import OptionKind

SPOT = 18.50
EXPIRY = 0.25
R_MXN = 0.0950   # domestic: the quote currency
R_USD = 0.0420   # foreign: the base currency
VOL = 0.115
STRIKES = (16.0, 17.5, 18.5, 18.7468, 19.5, 21.0)


def _price(strike: float, kind: OptionKind = OptionKind.CALL, **kw) -> float:
    args = {"expiry": EXPIRY, "r_domestic": R_MXN, "r_foreign": R_USD, "volatility": VOL}
    args.update(kw)
    return gk.price(SPOT, strike, kind=kind, **args)


class TestPutCallParity:
    """AC-3.1: `C - P = S e^{-r_f T} - K e^{-r_d T}` to 1e-10."""

    @pytest.mark.parametrize("strike", STRIKES)
    def test_it_holds_at_every_strike(self, strike):
        left = _price(strike, OptionKind.CALL) - _price(strike, OptionKind.PUT)
        right = SPOT * math.exp(-R_USD * EXPIRY) - strike * math.exp(-R_MXN * EXPIRY)
        assert left == pytest.approx(right, abs=1e-10)

    @pytest.mark.parametrize("vol", [0.02, 0.115, 0.40, 1.20])
    def test_it_holds_at_every_volatility(self, vol):
        left = _price(19.0, OptionKind.CALL, volatility=vol) - _price(
            19.0, OptionKind.PUT, volatility=vol
        )
        right = SPOT * math.exp(-R_USD * EXPIRY) - 19.0 * math.exp(-R_MXN * EXPIRY)
        assert left == pytest.approx(right, abs=1e-10)

    @pytest.mark.parametrize("expiry", [0.01, 0.25, 2.0, 10.0])
    def test_it_holds_at_every_expiry(self, expiry):
        left = _price(19.0, OptionKind.CALL, expiry=expiry) - _price(
            19.0, OptionKind.PUT, expiry=expiry
        )
        right = SPOT * math.exp(-R_USD * expiry) - 19.0 * math.exp(-R_MXN * expiry)
        assert left == pytest.approx(right, abs=1e-10)

    def test_it_holds_when_the_rates_are_equal(self):
        """With no interest differential the forward is the spot, and parity
        collapses to the discounted moneyness."""
        left = _price(19.0, OptionKind.CALL, r_domestic=0.05, r_foreign=0.05) - _price(
            19.0, OptionKind.PUT, r_domestic=0.05, r_foreign=0.05
        )
        assert left == pytest.approx((SPOT - 19.0) * math.exp(-0.05 * EXPIRY), abs=1e-12)


class TestTheClosedFormLimits:
    """Where the formula has to collapse to something already known."""

    def test_zero_expiry_is_intrinsic(self):
        assert _price(17.0, OptionKind.CALL, expiry=0.0) == pytest.approx(1.5)
        assert _price(20.0, OptionKind.CALL, expiry=0.0) == 0.0
        assert _price(20.0, OptionKind.PUT, expiry=0.0) == pytest.approx(1.5)

    def test_zero_volatility_is_the_discounted_forward_intrinsic(self):
        outright = gk.forward(SPOT, EXPIRY, R_MXN, R_USD)
        value = _price(18.0, OptionKind.CALL, volatility=0.0)
        assert value == pytest.approx(math.exp(-R_MXN * EXPIRY) * (outright - 18.0))

    def test_a_vanishing_volatility_approaches_that_limit(self):
        exact = _price(18.0, OptionKind.CALL, volatility=0.0)
        assert _price(18.0, OptionKind.CALL, volatility=1e-8) == pytest.approx(exact, abs=1e-9)

    def test_deep_out_of_the_money_is_positive_but_tiny(self):
        """Through erfc, so the tail does not collapse to a hard zero the way
        an erf-based cdf would."""
        value = _price(80.0, OptionKind.CALL)
        assert 0.0 < value < 1e-12

    def test_a_call_is_bounded_by_the_discounted_spot(self):
        for strike in STRIKES:
            assert _price(strike, OptionKind.CALL) < SPOT * math.exp(-R_USD * EXPIRY)


class TestMonotonicity:
    """Directions the price must move, which no formula error survives."""

    def test_a_call_falls_with_the_strike(self):
        values = [_price(k, OptionKind.CALL) for k in STRIKES]
        assert all(b < a for a, b in zip(values, values[1:], strict=False))

    def test_a_put_rises_with_the_strike(self):
        values = [_price(k, OptionKind.PUT) for k in STRIKES]
        assert all(b > a for a, b in zip(values, values[1:], strict=False))

    def test_both_rise_with_volatility(self):
        for kind in OptionKind:
            values = [_price(19.0, kind, volatility=v) for v in (0.05, 0.10, 0.20, 0.40)]
            assert all(b > a for a, b in zip(values, values[1:], strict=False))


class TestTheTwoRatesAreNotInterchangeable:
    """The reversal that produces a well-formed wrong answer."""

    def test_swapping_them_changes_the_price(self):
        right = _price(19.0, OptionKind.CALL)
        wrong = _price(19.0, OptionKind.CALL, r_domestic=R_USD, r_foreign=R_MXN)
        assert abs(right - wrong) > 0.05

    def test_the_forward_carries_the_differential_in_the_right_direction(self):
        """MXN rates above USD rates means the peso is forward-weak: the
        forward is above the spot."""
        assert gk.forward(SPOT, EXPIRY, R_MXN, R_USD) > SPOT
        assert gk.forward(SPOT, EXPIRY, R_USD, R_MXN) < SPOT

    def test_the_forward_is_the_spot_when_the_rates_match(self):
        assert gk.forward(SPOT, EXPIRY, 0.05, 0.05) == pytest.approx(SPOT)


class TestArgumentRefusals:
    """Lognormal means positive, and d1 has no value where it has none."""

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_a_non_positive_spot_or_strike_refuses(self, bad):
        with pytest.raises(ValueError, match="lognormal"):
            gk.d1_d2(bad, 18.0, EXPIRY, R_MXN, R_USD, VOL)
        with pytest.raises(ValueError, match="lognormal"):
            gk.d1_d2(SPOT, bad, EXPIRY, R_MXN, R_USD, VOL)

    def test_zero_expiry_or_volatility_refuses_in_d1_but_not_in_price(self):
        with pytest.raises(ValueError, match="undefined"):
            gk.d1_d2(SPOT, 18.0, 0.0, R_MXN, R_USD, VOL)
        assert gk.price(SPOT, 18.0, 0.0, R_MXN, R_USD, VOL) == pytest.approx(0.5)


class TestGreeks:
    """AC-3.5. Each analytic greek is checked against a numerical bump."""

    STRIKE = 19.0
    BUMP = 1e-5

    def _bumped(self, **kw):
        base = {"expiry": EXPIRY, "r_domestic": R_MXN, "r_foreign": R_USD, "volatility": VOL}
        base.update(kw)
        spot = base.pop("spot", SPOT)
        kind = base.pop("kind", OptionKind.CALL)
        return gk.price(spot, self.STRIKE, kind=kind, **base)

    def test_delta_matches_a_spot_bump(self):
        up = self._bumped(spot=SPOT + self.BUMP)
        down = self._bumped(spot=SPOT - self.BUMP)
        numeric = (up - down) / (2.0 * self.BUMP)
        analytic = gk.delta_spot(SPOT, self.STRIKE, EXPIRY, R_MXN, R_USD, VOL)
        assert analytic == pytest.approx(numeric, rel=1e-6)

    def test_gamma_matches_a_second_spot_bump(self):
        up = self._bumped(spot=SPOT + 1e-3)
        mid = self._bumped()
        down = self._bumped(spot=SPOT - 1e-3)
        numeric = (up + down - 2.0 * mid) / 1e-6
        analytic = gk.gamma(SPOT, self.STRIKE, EXPIRY, R_MXN, R_USD, VOL)
        assert analytic == pytest.approx(numeric, rel=1e-4)

    def test_vega_matches_a_volatility_bump(self):
        up = self._bumped(volatility=VOL + self.BUMP)
        down = self._bumped(volatility=VOL - self.BUMP)
        numeric = (up - down) / (2.0 * self.BUMP)
        analytic = gk.vega(SPOT, self.STRIKE, EXPIRY, R_MXN, R_USD, VOL)
        assert analytic == pytest.approx(numeric, rel=1e-6)

    def test_vanna_matches_a_cross_bump(self):
        step = 1e-4

        def at(spot, vol):
            return gk.price(spot, self.STRIKE, EXPIRY, R_MXN, R_USD, vol)

        numeric = (
            at(SPOT + step, VOL + step)
            - at(SPOT + step, VOL - step)
            - at(SPOT - step, VOL + step)
            + at(SPOT - step, VOL - step)
        ) / (4.0 * step * step)
        analytic = gk.vanna(SPOT, self.STRIKE, EXPIRY, R_MXN, R_USD, VOL)
        assert analytic == pytest.approx(numeric, rel=1e-4)

    def test_volga_matches_a_second_volatility_bump(self):
        step = 1e-3
        up = self._bumped(volatility=VOL + step)
        mid = self._bumped()
        down = self._bumped(volatility=VOL - step)
        numeric = (up + down - 2.0 * mid) / (step * step)
        analytic = gk.volga(SPOT, self.STRIKE, EXPIRY, R_MXN, R_USD, VOL)
        assert analytic == pytest.approx(numeric, rel=1e-4)

    def test_theta_matches_a_time_bump(self):
        step = 1e-6
        later = self._bumped(expiry=EXPIRY - step)
        earlier = self._bumped(expiry=EXPIRY + step)
        numeric = (later - earlier) / (2.0 * step)
        analytic = gk.theta(SPOT, self.STRIKE, EXPIRY, R_MXN, R_USD, VOL)
        assert analytic == pytest.approx(numeric, rel=1e-4)

    def test_vega_gamma_vanna_and_volga_do_not_depend_on_the_kind(self):
        """A call and a put differ by a forward, which has no volatility or
        second-order spot sensitivity. That is what makes a risk reversal a
        vanna trade and a butterfly a volga trade."""
        args = (SPOT, self.STRIKE, EXPIRY, R_MXN, R_USD, VOL)
        step = 1e-5
        for fn in (gk.vega, gk.gamma, gk.vanna, gk.volga):
            assert fn(*args) == fn(*args)  # no kind argument at all
        call_delta = gk.delta_spot(*args, kind=OptionKind.CALL)
        put_delta = gk.delta_spot(*args, kind=OptionKind.PUT)
        assert call_delta - put_delta == pytest.approx(math.exp(-R_USD * EXPIRY), abs=1e-12)
        del step

    def test_vega_is_positive_and_peaks_near_the_forward(self):
        outright = gk.forward(SPOT, EXPIRY, R_MXN, R_USD)
        at_forward = gk.vega(SPOT, outright, EXPIRY, R_MXN, R_USD, VOL)
        assert at_forward > 0.0
        for strike in (outright * 0.8, outright * 1.25):
            assert gk.vega(SPOT, strike, EXPIRY, R_MXN, R_USD, VOL) < at_forward

    def test_every_greek_is_zero_at_expiry(self):
        args = (SPOT, self.STRIKE, 0.0, R_MXN, R_USD, VOL)
        assert gk.vega(*args) == 0.0
        assert gk.gamma(*args) == 0.0
        assert gk.vanna(*args) == 0.0
        assert gk.volga(*args) == 0.0
        assert gk.theta(*args) == 0.0


class TestTheCurrencyPair:
    """Forward points need the pip the pair is quoted in."""

    def test_the_pair_names_itself_base_first(self):
        assert USDMXN.name == "USD/MXN"

    def test_forward_points_come_out_in_pips(self):
        outright = gk.forward(SPOT, EXPIRY, R_MXN, R_USD)
        points = USDMXN.pips(outright - SPOT)
        assert points == pytest.approx((outright - SPOT) / 1e-4)
        assert 2000 < points < 3000

    def test_a_pair_of_one_currency_refuses(self):
        from rates_engine.fx.quote import CurrencyPair
        from rates_engine.money import Currency

        with pytest.raises(ValueError, match="two currencies"):
            CurrencyPair(Currency.USD, Currency.USD, pip=1e-4)

    def test_a_non_positive_pip_refuses(self):
        from rates_engine.fx.quote import CurrencyPair
        from rates_engine.money import Currency

        with pytest.raises(ValueError, match="pip size"):
            CurrencyPair(Currency.USD, Currency.MXN, pip=0.0)

    def test_the_pip_travels_in_the_payload(self):
        assert USDMXN.to_dict()["pip"] == 1e-4
