"""AC-10.8 to AC-10.13: the duration conventions, and the two that do not exist."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rates_engine.errors import UndefinedDurationError
from rates_engine.pricing import dv01, pv
from rates_engine.risk import (
    ZERO_PRICE_TOLERANCE,
    effective_convexity,
    effective_duration,
    macaulay_duration,
    modified_duration,
    money_convexity,
    money_duration,
    pvbp,
)


@pytest.fixture
def off_market(par_swap):
    """A swap struck a hundred basis points away from par, so it has a price."""
    return replace(par_swap, fixed_rate=par_swap.fixed_rate + 0.01)


class TestEquivalences:
    """AC-10.8: pvbp is dv01, money duration is dv01 times ten thousand."""

    def test_pvbp_is_dv01(self, par_swap, curve_set):
        assert pvbp(par_swap, curve_set).value == dv01(par_swap, curve_set).value

    def test_pvbp_is_computed_by_calling_dv01_not_reimplementing_it(self, par_swap, curve_set):
        # Two implementations of one number is two chances to be wrong.
        assert pvbp(par_swap, curve_set).evidence.fields["equivalent_to"] == "pricing.dv01"
        assert pvbp(par_swap, curve_set).evidence.sources[0].produced_by == "pricing.dv01"

    def test_money_duration_is_ten_thousand_dv01(self, par_swap, curve_set):
        assert money_duration(par_swap, curve_set).value == pytest.approx(
            dv01(par_swap, curve_set).value * 1e4, rel=1e-15
        )

    def test_the_units_distinguish_them(self, par_swap, curve_set):
        assert pvbp(par_swap, curve_set).unit == "USD_per_bp"
        assert money_duration(par_swap, curve_set).unit == "USD_per_unit_yield"

    def test_every_risk_result_declares_its_bump(self, par_swap, curve_set):
        for result in (
            pvbp(par_swap, curve_set),
            money_duration(par_swap, curve_set),
            money_convexity(par_swap, curve_set),
        ):
            assert result.bump_bp == 1.0
            assert result.to_dict()["unit"]


class TestMoneyConvexity:
    """AC-10.9: defined even when the price is zero, which is the point of it."""

    def test_it_works_on_a_par_swap(self, par_swap, curve_set):
        assert abs(pv(par_swap, curve_set).value) < 1e-6
        assert money_convexity(par_swap, curve_set).value != 0.0

    def test_the_unit_is_per_yield_squared(self, par_swap, curve_set):
        result = money_convexity(par_swap, curve_set)
        assert result.unit == "USD_per_yield_squared"
        assert result.evidence.fields["defined_at_zero_price"] is True

    def test_it_is_the_central_second_difference(self, par_swap, curve_set):
        shift = 1e-4
        base = pv(par_swap, curve_set).value
        up = pv(par_swap, curve_set.shifted(shift)).value
        down = pv(par_swap, curve_set.shifted(-shift)).value
        expected = (up + down - 2.0 * base) / shift**2
        assert money_convexity(par_swap, curve_set).value == pytest.approx(expected, rel=1e-12)

    def test_a_payer_and_a_receiver_have_opposite_convexity(self, par_swap, curve_set):
        from rates_engine.instruments import Side

        payer = money_convexity(replace(par_swap, side=Side.PAYER), curve_set).value
        receiver = money_convexity(replace(par_swap, side=Side.RECEIVER), curve_set).value
        assert payer == pytest.approx(-receiver, rel=1e-9)

    def test_a_non_positive_bump_refuses(self, par_swap, curve_set):
        with pytest.raises(ValueError, match="bump_bp"):
            money_convexity(par_swap, curve_set, bump_bp=-1.0)


class TestEffectiveMeasures:
    """AC-10.11: curve-based, full reprice, and named as effective."""

    def test_effective_duration_is_the_normalised_first_difference(
        self, off_market, curve_set
    ):
        shift = 1e-4
        price = pv(off_market, curve_set).value
        up = pv(off_market, curve_set.shifted(shift)).value
        down = pv(off_market, curve_set.shifted(-shift)).value
        expected = (down - up) / (2.0 * price * shift)
        assert effective_duration(off_market, curve_set).value == pytest.approx(
            expected, rel=1e-12
        )

    def test_effective_convexity_is_money_convexity_over_price(self, off_market, curve_set):
        price = pv(off_market, curve_set).value
        assert effective_convexity(off_market, curve_set).value == pytest.approx(
            money_convexity(off_market, curve_set).value / price, rel=1e-12
        )

    def test_they_declare_that_they_are_effective(self, off_market, curve_set):
        fields = effective_duration(off_market, curve_set).evidence.fields
        assert fields["kind"] == "effective"
        assert "not from a single" in fields["kind_note"]
        assert fields["bump_basis"] == "zero_curve_parallel"

    def test_the_units_are_years_and_years_squared(self, off_market, curve_set):
        assert effective_duration(off_market, curve_set).unit == "years"
        assert effective_convexity(off_market, curve_set).unit == "years_squared"

    def test_a_non_positive_bump_refuses(self, off_market, curve_set):
        with pytest.raises(ValueError, match="bump_bp"):
            effective_duration(off_market, curve_set, bump_bp=0.0)


class TestZeroPriceRefusals:
    """AC-10.12: normalised measures refuse at a zero price; monetary ones answer."""

    def test_effective_duration_refuses_on_a_par_swap(self, par_swap, curve_set):
        with pytest.raises(UndefinedDurationError, match="effective_duration"):
            effective_duration(par_swap, curve_set)

    def test_effective_convexity_refuses_on_a_par_swap(self, par_swap, curve_set):
        with pytest.raises(UndefinedDurationError, match="effective_convexity"):
            effective_convexity(par_swap, curve_set)

    def test_the_refusal_names_the_measures_that_do_work(self, par_swap, curve_set):
        with pytest.raises(UndefinedDurationError) as caught:
            effective_duration(par_swap, curve_set)
        message = str(caught.value)
        assert "dv01" in message
        assert "money_convexity" in message
        assert "par swap" in message

    def test_the_same_swap_answers_the_monetary_questions(self, par_swap, curve_set):
        assert dv01(par_swap, curve_set).value != 0.0
        assert money_convexity(par_swap, curve_set).value != 0.0
        assert money_duration(par_swap, curve_set).value != 0.0

    def test_the_tolerance_is_relative_to_notional(self, par_swap, curve_set):
        # So that it means the same thing on a one million and a one billion
        # trade. A swap priced just inside the band still refuses.
        tiny = replace(par_swap, notional=1_000.0)
        assert abs(pv(tiny, curve_set).value) < ZERO_PRICE_TOLERANCE * tiny.notional
        with pytest.raises(UndefinedDurationError):
            effective_duration(tiny, curve_set)

    def test_just_outside_the_band_it_computes(self, par_swap, curve_set):
        nearly = replace(par_swap, fixed_rate=par_swap.fixed_rate + 1e-5)
        assert effective_duration(nearly, curve_set).value != 0.0


class TestYieldBasedStubs:
    """AC-10.13: the two that need a bond say so, and are never faked."""

    @pytest.mark.parametrize("function", [macaulay_duration, modified_duration])
    def test_they_raise_not_implemented(self, function):
        with pytest.raises(NotImplementedError):
            function()

    @pytest.mark.parametrize("function", [macaulay_duration, modified_duration])
    def test_the_message_explains_why(self, function):
        with pytest.raises(NotImplementedError) as caught:
            function()
        message = str(caught.value)
        assert "single yield" in message
        assert "FixedRateBond" in message
        assert "v1.1" in message

    @pytest.mark.parametrize("function", [macaulay_duration, modified_duration])
    def test_the_message_refuses_the_substitution(self, function):
        with pytest.raises(NotImplementedError) as caught:
            function()
        assert "NOT the same quantity" in str(caught.value)

    def test_they_exist_as_names_so_an_agent_gets_the_reason(self):
        # An AttributeError would say nothing. These say what to use instead.
        import rates_engine

        assert hasattr(rates_engine, "macaulay_duration")
        assert hasattr(rates_engine, "modified_duration")

    def test_they_accept_any_arguments_and_still_refuse(self, par_swap, curve_set):
        with pytest.raises(NotImplementedError):
            macaulay_duration(par_swap, curve_set, bump_bp=1.0)

    def test_effective_duration_is_not_reachable_under_the_stub_names(self, off_market, curve_set):
        assert effective_duration(off_market, curve_set).measure == "effective_duration"
        with pytest.raises(NotImplementedError):
            modified_duration(off_market, curve_set)
