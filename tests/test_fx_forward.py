"""PRD-003 AC-2.1 to AC-2.4: the forward, and the part parity does not explain.

Covered interest parity is an arbitrage relation, so every test of it here
is an identity that needs no market data — which is the whole reason US-2
was buildable when the research gate came back empty on MXN conventions.

The part that is not an identity is the cross-currency basis, and the
design decision under test is that it is never folded into the answer. A
result carries the parity forward, the basis contribution and the outright,
so a treasurer can see how much of a hedge's cost is arbitrage-free carry
and how much is funding.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from rates_engine.curves.discount import DiscountCurve
from rates_engine.errors import CurrencyMismatchError, ImplausibleInputError
from rates_engine.fx.forward import (
    CIP_BROKEN_NOTE,
    MAX_PLAUSIBLE_BASIS_BP,
    forward_from_curves,
    implied_basis,
)
from rates_engine.fx.quote import USDMXN, CurrencyPair
from rates_engine.money import Currency

AS_OF = date(2026, 9, 16)
SPOT = 18.50
R_USD = 0.0420
R_MXN = 0.0950


def _curve(rate: float, currency: Currency) -> DiscountCurve:
    nodes = tuple(AS_OF + timedelta(days=365 * k) for k in (1, 2, 3, 5))
    return DiscountCurve(
        AS_OF, nodes, tuple(math.exp(-rate * k) for k in (1, 2, 3, 5)), currency=currency
    )


@pytest.fixture
def usd() -> DiscountCurve:
    return _curve(R_USD, Currency.USD)


@pytest.fixture
def mxn() -> DiscountCurve:
    return _curve(R_MXN, Currency.MXN)


@pytest.fixture
def delivery() -> date:
    return AS_OF + timedelta(days=365)


class TestCoveredInterestParity:
    """AC-2.1: `F = S P_foreign / P_domestic`, an identity."""

    def test_the_parity_relation_holds_exactly(self, usd, mxn, delivery):
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        assert result.cip_forward * mxn.df(delivery) == pytest.approx(
            SPOT * usd.df(delivery), rel=1e-12
        )

    def test_with_no_basis_the_outright_is_the_parity_forward(self, usd, mxn, delivery):
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        assert result.outright == pytest.approx(result.cip_forward, rel=1e-15)
        assert result.basis_component == pytest.approx(0.0, abs=1e-15)

    def test_the_higher_rate_currency_is_forward_weak(self, usd, mxn, delivery):
        """MXN pays more than USD, so pesos buy fewer dollars forward: the
        USD/MXN forward is above the spot."""
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        assert result.outright > SPOT
        assert result.forward_points > 0.0

    def test_equal_rates_give_a_flat_forward(self, delivery):
        usd = _curve(0.06, Currency.USD)
        mxn = _curve(0.06, Currency.MXN)
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        assert result.outright == pytest.approx(SPOT, rel=1e-14)
        assert result.forward_points == pytest.approx(0.0, abs=1e-9)

    def test_the_points_are_reported_in_the_pairs_pips(self, usd, mxn, delivery):
        """AC-2.1 asks for pips under a declared quoting convention, so the
        pip travels with the pair rather than being assumed downstream."""
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        assert result.forward_points == pytest.approx((result.outright - SPOT) / USDMXN.pip)
        assert result.payload_fields()["pip"] == 1e-4

    def test_a_different_pip_rescales_the_points_and_nothing_else(self, usd, mxn, delivery):
        coarse = CurrencyPair(Currency.USD, Currency.MXN, pip=1e-2)
        fine = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        blunt = forward_from_curves(coarse, SPOT, delivery, mxn, usd)
        assert blunt.outright == pytest.approx(fine.outright)
        assert blunt.forward_points == pytest.approx(fine.forward_points / 100.0)

    @pytest.mark.parametrize("years", [1, 2, 3, 5])
    def test_it_holds_at_every_tenor(self, usd, mxn, years):
        day = AS_OF + timedelta(days=365 * years)
        result = forward_from_curves(USDMXN, SPOT, day, mxn, usd)
        assert result.cip_forward * mxn.df(day) == pytest.approx(SPOT * usd.df(day), rel=1e-12)


class TestTheBasisIsKeptSeparate:
    """AC-2.2: the payload decomposes rather than summing."""

    def test_the_outright_is_the_parity_forward_plus_the_basis_part(self, usd, mxn, delivery):
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=-25.0)
        assert result.outright == pytest.approx(
            result.cip_forward + result.basis_component, rel=1e-15
        )

    def test_the_points_decompose_the_same_way(self, usd, mxn, delivery):
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=-25.0)
        assert result.forward_points == pytest.approx(
            result.cip_points + result.basis_points_contribution, rel=1e-12
        )

    def test_a_negative_basis_raises_the_forward(self, usd, mxn, delivery):
        """The basis is a spread on the base currency leg: paying to borrow
        dollars against pesos makes dollars dearer forward."""
        without = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        with_basis = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=-25.0)
        assert with_basis.outright > without.outright

    def test_the_sign_reverses(self, usd, mxn, delivery):
        positive = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=25.0)
        negative = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=-25.0)
        assert positive.outright < negative.outright

    def test_the_basis_effect_grows_with_tenor(self, usd, mxn):
        def contribution(years):
            day = AS_OF + timedelta(days=365 * years)
            return abs(
                forward_from_curves(USDMXN, SPOT, day, mxn, usd, basis_bp=-25.0).basis_component
            )

        values = [contribution(y) for y in (1, 2, 3, 5)]
        assert all(b > a for a, b in zip(values, values[1:], strict=False))

    def test_the_payload_names_cip_as_broken(self, usd, mxn, delivery):
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        assert result.payload_fields()["cip_note"] == CIP_BROKEN_NOTE
        assert "2007" in CIP_BROKEN_NOTE

    def test_zero_basis_still_says_so_rather_than_implying_parity_holds(
        self, usd, mxn, delivery
    ):
        result = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd)
        assert result.payload_fields()["basis_bp"] == 0.0
        assert "cip_note" in result.payload_fields()


class TestInvertingAQuotedForward:
    """AC-2.3: whatever parity does not explain is the basis."""

    @pytest.mark.parametrize("basis", [-120.0, -25.0, 0.0, 40.0])
    def test_the_round_trip_is_exact(self, usd, mxn, delivery, basis):
        forward = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=basis)
        back = implied_basis(USDMXN, SPOT, forward.outright, delivery, mxn, usd)
        assert back.basis_bp == pytest.approx(basis, abs=1e-9)

    def test_the_inverted_result_carries_the_quote_as_its_outright(self, usd, mxn, delivery):
        back = implied_basis(USDMXN, SPOT, 19.60, delivery, mxn, usd)
        assert back.outright == 19.60

    def test_it_reports_which_curves_it_used(self, usd, mxn, delivery):
        back = implied_basis(USDMXN, SPOT, 19.60, delivery, mxn, usd)
        fields = back.evidence.fields
        assert fields["domestic_currency"] == "MXN"
        assert fields["foreign_currency"] == "USD"
        assert fields["cip_forward"] == pytest.approx(SPOT * usd.df(delivery) / mxn.df(delivery))

    def test_it_says_it_reads_the_basis_rather_than_fitting_a_curve(self, usd, mxn, delivery):
        back = implied_basis(USDMXN, SPOT, 19.60, delivery, mxn, usd)
        assert "hide the same number inside the curve" in back.evidence.fields["note"]

    def test_a_forward_below_parity_implies_a_positive_basis(self, usd, mxn, delivery):
        parity = forward_from_curves(USDMXN, SPOT, delivery, mxn, usd).cip_forward
        back = implied_basis(USDMXN, SPOT, parity * 0.99, delivery, mxn, usd)
        assert back.basis_bp > 0.0


class TestPlausibility:
    """AC-2.4: a number no market produces is refused, not used."""

    @pytest.mark.parametrize("basis", [600.0, -600.0, 5000.0])
    def test_a_basis_outside_the_band_refuses(self, usd, mxn, delivery, basis):
        with pytest.raises(ImplausibleInputError) as excinfo:
            forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=basis)
        assert "500 bp" in str(excinfo.value)

    def test_the_band_is_named_and_adjustable_rather_than_magic(self):
        assert MAX_PLAUSIBLE_BASIS_BP == 500.0

    def test_the_refusal_admits_the_band_is_not_a_measurement(self, usd, mxn, delivery):
        with pytest.raises(ImplausibleInputError) as excinfo:
            forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=600.0)
        message = str(excinfo.value)
        assert "plausibility check rather than a measurement" in message
        assert "deliberately" in message

    @pytest.mark.parametrize("basis", [499.9, -499.9])
    def test_just_inside_the_band_is_accepted(self, usd, mxn, delivery, basis):
        assert forward_from_curves(
            USDMXN, SPOT, delivery, mxn, usd, basis_bp=basis
        ).outright > 0.0

    def test_an_implied_basis_outside_the_band_refuses_too(self, usd, mxn, delivery):
        """A quote and two curves that disagree by that much means one of
        them is wrong, not that the market moved."""
        with pytest.raises(ImplausibleInputError):
            implied_basis(USDMXN, SPOT, 30.0, delivery, mxn, usd)

    def test_a_non_finite_basis_refuses(self, usd, mxn, delivery):
        with pytest.raises(ImplausibleInputError):
            forward_from_curves(USDMXN, SPOT, delivery, mxn, usd, basis_bp=float("nan"))


class TestTheCurvesMustBeTheRightWayRound:
    """The reversal that silently inverts every forward point."""

    def test_swapping_the_curves_refuses(self, usd, mxn, delivery):
        with pytest.raises(CurrencyMismatchError) as excinfo:
            forward_from_curves(USDMXN, SPOT, delivery, usd, mxn)
        assert "inverts the forward points" in str(excinfo.value)

    def test_a_curve_in_neither_currency_refuses(self, mxn, delivery):
        stray = _curve(0.03, Currency.USD)
        with pytest.raises(CurrencyMismatchError):
            forward_from_curves(USDMXN, SPOT, delivery, stray, stray)
        del mxn

    def test_the_inverse_checks_the_same_way(self, usd, mxn, delivery):
        with pytest.raises(CurrencyMismatchError):
            implied_basis(USDMXN, SPOT, 19.5, delivery, usd, mxn)

    def test_a_correctly_ordered_pair_is_accepted(self, usd, mxn, delivery):
        assert forward_from_curves(USDMXN, SPOT, delivery, mxn, usd).outright > 0.0


class TestArgumentRefusals:
    """Rates are positive and delivery is in the future."""

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_a_non_positive_spot_refuses(self, usd, mxn, delivery, bad):
        with pytest.raises(ValueError, match="positive"):
            forward_from_curves(USDMXN, bad, delivery, mxn, usd)

    def test_a_delivery_on_the_valuation_date_refuses(self, usd, mxn):
        with pytest.raises(ValueError, match="not after"):
            forward_from_curves(USDMXN, SPOT, AS_OF, mxn, usd)

    def test_a_non_positive_market_forward_refuses(self, usd, mxn, delivery):
        with pytest.raises(ValueError, match="positive rates"):
            implied_basis(USDMXN, SPOT, 0.0, delivery, mxn, usd)


class TestSerialisation:
    def test_the_payload_has_both_halves_and_both_units(self, usd, mxn, delivery):
        payload = forward_from_curves(
            USDMXN, SPOT, delivery, mxn, usd, basis_bp=-25.0
        ).payload_fields()
        for key in (
            "outright", "cip_forward", "basis_component", "basis_bp",
            "forward_points", "cip_points", "basis_points_contribution",
            "pair", "pip", "delivery",
        ):
            assert key in payload

    def test_it_is_json_serialisable(self, usd, mxn, delivery):
        json.dumps(forward_from_curves(USDMXN, SPOT, delivery, mxn, usd).to_dict())

    def test_curve_evidence_chains_in(self, usd, mxn, delivery):
        from rates_engine.evidence import Evidence

        source = Evidence(produced_by="test.curve")
        result = forward_from_curves(
            USDMXN, SPOT, delivery, mxn, usd, source_evidence=(source,)
        )
        assert source in result.evidence.sources
