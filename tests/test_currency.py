"""PRD-003 decision 3: a currency is carried, and mixing two of them refuses.

The decision this file tests was not in the PRD. It came out of the
research gate: `grep -rn "currency" src/` returned two hits, both prose in
docstrings. USD was implicit everywhere because there had only ever been
one currency, and introducing a second would have made "discount the peso
leg on the dollar curve" a silent success.

Two claims, and they pull against each other, which is why both are here:

1. The mistake now raises. Not "is reported", not "is marked in the
   evidence" — raises, before a number exists.
2. Nothing that worked before changed. The rest of the suite is the real
   test of that; this file states the claim directly for the three types
   that gained a field.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.errors import CurrencyMismatchError, RatesEngineError
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.money import Currency, require_same_currency
from rates_engine.pricing import pv

AS_OF = date(2026, 9, 16)


def _curve(currency: Currency = Currency.USD, rate: float = 0.04) -> DiscountCurve:
    nodes = tuple(AS_OF + timedelta(days=365 * k) for k in (1, 2, 3))
    return DiscountCurve(
        AS_OF, nodes, tuple(math.exp(-rate * k) for k in (1, 2, 3)), currency=currency
    )


def _flow(currency: Currency, amount: float = 1_000_000.0) -> Cashflow:
    return Cashflow(
        payment_date=AS_OF + timedelta(days=365),
        amount=amount,
        leg="fixed",
        accrual_start=AS_OF,
        accrual_end=AS_OF + timedelta(days=365),
        year_fraction=1.0,
        currency=currency,
    )


class _OneFlow:
    """The smallest thing `pv` will price: one cashflow, no instrument."""

    def __init__(self, flow: Cashflow) -> None:
        self._flow = flow

    @property
    def span(self) -> tuple[date, date]:
        return self._flow.accrual_start, self._flow.payment_date

    def cashflows(self, curve_set: CurveSet) -> tuple[Cashflow, ...]:
        del curve_set
        return (self._flow,)

    def describe(self) -> dict[str, object]:
        return {"kind": "single_cashflow", "currency": self._flow.currency.value}


class TestTheDefaultKeepsEverythingWorking:
    """Decision 3 is additive, or it is not allowed."""

    def test_a_curve_built_the_v1_way_is_dollars(self):
        assert _curve().currency is Currency.USD

    def test_a_cashflow_built_the_v1_way_is_dollars(self):
        flow = Cashflow(
            payment_date=AS_OF + timedelta(days=365),
            amount=1.0,
            leg="fixed",
            accrual_start=AS_OF,
            accrual_end=AS_OF + timedelta(days=365),
            year_fraction=1.0,
        )
        assert flow.currency is Currency.USD

    def test_a_single_curve_set_takes_its_currency_from_its_curve(self):
        assert CurveSet(_curve(Currency.MXN)).currency is Currency.MXN

    def test_pricing_a_dollar_flow_on_a_dollar_curve_is_unchanged(self):
        result = pv(_OneFlow(_flow(Currency.USD)), CurveSet(_curve()))
        assert result.value == pytest.approx(1_000_000.0 * math.exp(-0.04))


class TestMixingRefuses:
    """The whole point: the mistake cannot be made quietly."""

    def test_discounting_a_peso_flow_on_a_dollar_curve_refuses(self):
        with pytest.raises(CurrencyMismatchError) as excinfo:
            pv(_OneFlow(_flow(Currency.MXN)), CurveSet(_curve(Currency.USD)))
        message = str(excinfo.value)
        assert "MXN" in message and "USD" in message

    def test_the_refusal_names_the_operation_and_the_flow(self):
        with pytest.raises(CurrencyMismatchError) as excinfo:
            pv(_OneFlow(_flow(Currency.MXN)), CurveSet(_curve(Currency.USD)))
        assert "discounting a fixed cashflow paid" in str(excinfo.value)

    def test_it_points_at_fx_rather_than_converting(self):
        with pytest.raises(CurrencyMismatchError) as excinfo:
            require_same_currency(Currency.USD, Currency.MXN, operation="adding two prices")
        message = str(excinfo.value)
        assert "no implicit conversion" in message
        assert "rates_engine.fx" in message

    def test_a_curve_set_of_two_currencies_refuses_at_construction(self):
        with pytest.raises(CurrencyMismatchError) as excinfo:
            CurveSet(_curve(Currency.USD), _curve(Currency.MXN))
        assert "a curve set" in str(excinfo.value)

    def test_a_dual_curve_set_in_one_currency_is_fine(self):
        assert CurveSet(_curve(Currency.MXN), _curve(Currency.MXN, 0.09)).is_dual

    def test_it_is_catchable_as_an_engine_error(self):
        with pytest.raises(RatesEngineError):
            require_same_currency(Currency.USD, Currency.MXN, operation="x")

    def test_the_calculation_is_impossible_not_the_input_unusable(self):
        """Exit code 2, not 1: each side is a valid input on its own."""
        with pytest.raises(CurrencyMismatchError) as excinfo:
            require_same_currency(Currency.USD, Currency.MXN, operation="x")
        assert excinfo.value.exit_code == 2

    def test_nothing_is_priced_before_the_refusal(self):
        """The check has to come before the arithmetic, or a partial sum for
        the matching flows would exist by the time it fires."""
        flows = (_flow(Currency.USD), _flow(Currency.MXN))

        class _Two(_OneFlow):
            def cashflows(self, curve_set):
                del curve_set
                return flows

        with pytest.raises(CurrencyMismatchError):
            pv(_Two(flows[0]), CurveSet(_curve()))


class TestItTravelsIntoThePayload:
    """A number whose currency is not in its payload is a number with a unit
    the reader has to guess."""

    def test_a_curve_serialises_its_currency(self):
        assert _curve(Currency.MXN).to_dict()["currency"] == "MXN"

    def test_a_cashflow_serialises_its_currency(self):
        assert _flow(Currency.MXN).to_dict()["currency"] == "MXN"

    def test_the_key_is_present_even_for_the_default(self):
        assert _curve().to_dict()["currency"] == "USD"
        assert _flow(Currency.USD).to_dict()["currency"] == "USD"

    def test_it_is_a_string_in_json_not_an_enum_repr(self):
        stored = json.dumps(_curve(Currency.MXN).to_dict())
        assert '"currency": "MXN"' in stored


class TestTheEnumeration:
    """Short on purpose."""

    def test_it_holds_exactly_the_two_currencies_v3_has_curves_for(self):
        assert {c.value for c in Currency} == {"USD", "MXN"}

    def test_it_compares_as_a_string(self):
        assert Currency.USD == "USD"

    def test_same_currency_returns_it(self):
        assert require_same_currency(Currency.MXN, Currency.MXN, operation="x") is Currency.MXN
