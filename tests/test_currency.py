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
from dataclasses import replace
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


def _futures_strip():
    """A quarterly SR3 strip on a curve that is not flat, so the two
    interpolations actually disagree and the comparison has work to do."""
    from rates_engine.curves import FuturesNode, RealizedStubNode

    start = AS_OF + timedelta(days=14)
    nodes: list[object] = [
        RealizedStubNode(end=start, accrual_factor=1.0 + 0.043 * (14 / 360.0))
    ]
    period = start
    for index, forward in enumerate((0.0430, 0.0405, 0.0380, 0.0360, 0.0355, 0.0365)):
        following = period + timedelta(days=91)
        nodes.append(
            FuturesNode(
                start=period, end=following, forward_rate=forward, label=f"SR3-{index + 1}"
            )
        )
        period = following
    return tuple(nodes)


def _futures_hedge():
    """A real sized strip hedge, which is what `shock_table` consumes."""
    from rates_engine.curves import FuturesNode
    from rates_engine.hedging import strip_hedge
    from rates_engine.instruments.swaps import OISSwap, Side

    instruments = _futures_strip()
    futures = [i for i in instruments if isinstance(i, FuturesNode)]
    swap = OISSwap(
        effective=futures[0].start,
        maturity=futures[-1].end,
        fixed_rate=0.039,
        notional=100_000_000.0,
        side=Side.PAYER,
    )
    return strip_hedge(swap, instruments, as_of=AS_OF)


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


class TestItSurvivesEveryTransformation:
    """The bug that made the whole check ornamental.

    A currency that a bump or a bootstrap step silently reverts to USD is
    worse than no currency at all: the refusal stops firing exactly where
    the curve has been through the most machinery. Every method that
    reconstructs a `DiscountCurve` is pinned here.
    """

    def test_a_shift_keeps_it(self):
        assert _curve(Currency.MXN).shifted(1e-4).currency is Currency.MXN

    def test_appending_a_node_keeps_it(self):
        curve = _curve(Currency.MXN)
        extended = curve.with_node(AS_OF + timedelta(days=365 * 4), 0.70)
        assert extended.currency is Currency.MXN

    def test_replacing_the_last_node_keeps_it(self):
        curve = _curve(Currency.MXN)
        replaced = curve.with_node(curve.nodes[-1], 0.80)
        assert replaced.currency is Currency.MXN

    def test_the_bootstrap_produces_the_currency_it_was_asked_for(self):
        from rates_engine.curves.bootstrap import RealizedStubNode, bootstrap_discount_curve

        end = AS_OF + timedelta(days=28)
        result = bootstrap_discount_curve(
            AS_OF,
            (RealizedStubNode(end=end, accrual_factor=1.0 + 0.095 * 28 / 360),),
            currency=Currency.MXN,
        )
        assert result.curve.currency is Currency.MXN

    def test_the_bootstrap_still_defaults_to_dollars(self):
        from rates_engine.curves.bootstrap import RealizedStubNode, bootstrap_discount_curve

        end = AS_OF + timedelta(days=28)
        result = bootstrap_discount_curve(
            AS_OF, (RealizedStubNode(end=end, accrual_factor=1.0 + 0.04 * 28 / 360),)
        )
        assert result.curve.currency is Currency.USD

    def test_a_parametric_curve_can_be_sampled_in_a_currency(self):
        from rates_engine.curves.parametric import NelsonSiegel

        model = NelsonSiegel(0.09, -0.01, 0.005, 2.0)
        nodes = (AS_OF + timedelta(days=365),)
        assert model.discount_curve(AS_OF, nodes, currency=Currency.MXN).currency is Currency.MXN
        assert model.discount_curve(AS_OF, nodes).currency is Currency.USD

    def test_a_shifted_curve_set_keeps_both_currencies(self):
        curves = CurveSet(_curve(Currency.MXN), _curve(Currency.MXN, 0.10))
        shifted = curves.shifted(1e-4)
        assert shifted.currency is Currency.MXN
        assert shifted.tenor is not None and shifted.tenor.currency is Currency.MXN

    def test_a_bumped_peso_curve_still_refuses_a_dollar_flow(self):
        """The end-to-end version: the refusal has to survive the machinery,
        not just the constructor."""
        curves = CurveSet(_curve(Currency.MXN)).shifted(1e-4)
        with pytest.raises(CurrencyMismatchError):
            pv(_OneFlow(_flow(Currency.USD)), curves)


class TestARealInstrumentNotJustAProbe:
    """Everything above prices a hand-built flow through a shim. The guard
    has to hold for something with real cashflow-generation logic, and for
    a long time it did not: no instrument set a currency at all, so the
    check had no true positives and fired on every peso valuation."""

    @staticmethod
    def _swap(notional: float = 1_000_000.0) -> object:
        from rates_engine.instruments.swaps import OISSwap, Side

        return OISSwap(
            effective=AS_OF + timedelta(days=30),
            maturity=AS_OF + timedelta(days=365 * 2),
            fixed_rate=0.05,
            notional=notional,
            side=Side.PAYER,
        )

    def test_a_real_swap_takes_its_currency_from_the_curve(self):
        curves = CurveSet(_curve(Currency.MXN, 0.09))
        assert {c.currency for c in self._swap().cashflows(curves)} == {Currency.MXN}

    def test_and_prices_on_it_rather_than_refusing_its_own_output(self):
        """The bug this closes: `bootstrap_mxn_curve` produced a curve that
        `pv` then refused, because the flows were hardcoded USD."""
        curves = CurveSet(_curve(Currency.MXN, 0.09))
        assert pv(self._swap(), curves).value != 0.0

    def test_the_price_is_labelled_in_the_curves_currency(self):
        curves = CurveSet(_curve(Currency.MXN, 0.09))
        assert pv(self._swap(), curves).unit == "MXN"
        assert pv(self._swap(), curves).to_dict()["unit"] == "MXN"

    def test_a_dollar_swap_on_a_dollar_curve_is_unchanged(self):
        curves = CurveSet(_curve(Currency.USD))
        assert pv(self._swap(), curves).unit == "USD"

    def test_hand_assembling_a_mixed_portfolio_still_refuses(self):
        """Where the guard has real work: flows the caller built, not ones
        an instrument generated from a curve."""
        curves = CurveSet(_curve(Currency.USD))
        with pytest.raises(CurrencyMismatchError):
            pv(_OneFlow(_flow(Currency.MXN)), curves)


class TestAddingTwoPresentValues:
    """`docs/ERRORS.md` and `money.py` both name "summing present values in
    different currencies" as a thing this package refuses. Until `__add__`
    existed the claim had no code behind it: `a.value + b.value` is two
    floats and floats add.
    """

    @staticmethod
    def _priced(currency: Currency, rate: float = 0.04):
        swap = TestARealInstrumentNotJustAProbe._swap()
        return pv(swap, CurveSet(_curve(currency, rate)))

    def test_two_dollar_present_values_add(self):
        one = self._priced(Currency.USD)
        assert (one + one).value == pytest.approx(2 * one.value)

    def test_the_sum_keeps_the_measure_and_the_unit(self):
        one = self._priced(Currency.USD)
        total = one + one
        assert (total.measure, total.unit) == ("pv", "USD")

    def test_a_peso_present_value_plus_a_dollar_one_refuses(self):
        with pytest.raises(CurrencyMismatchError) as caught:
            _ = self._priced(Currency.MXN, 0.09) + self._priced(Currency.USD)
        assert "MXN" in str(caught.value) and "USD" in str(caught.value)

    def test_the_refusal_names_where_conversion_lives(self):
        with pytest.raises(CurrencyMismatchError) as caught:
            _ = self._priced(Currency.MXN, 0.09) + self._priced(Currency.USD)
        assert "rates_engine.fx" in str(caught.value)

    def test_adding_a_rate_to_a_present_value_is_a_type_error_not_a_refusal(self):
        """A measure mismatch is a bug in the caller, not a data problem, so
        it is not in the RatesEngineError taxonomy."""
        from rates_engine.pricing import par_rate

        priced = self._priced(Currency.USD)
        rate = par_rate(TestARealInstrumentNotJustAProbe._swap(), CurveSet(_curve()))
        with pytest.raises(TypeError, match="same measure"):
            _ = priced + rate
        with pytest.raises(TypeError):
            _ = priced + 1.0

    def test_the_sum_carries_both_evidence_chains(self):
        one = self._priced(Currency.USD)
        total = one + one
        assert len(total.evidence.sources) == 2
        assert total.evidence.produced_by == "pricing.PriceResult.__add__"

    def test_it_concatenates_the_cashflows_rather_than_dropping_them(self):
        one = self._priced(Currency.USD)
        assert len((one + one).cashflows or ()) == 2 * len(one.cashflows or ())

    def test_an_assumed_leg_degrades_the_total(self):
        """The reason the sum composes evidence instead of picking one side:
        adding a marked result to a clean one must not launder the mark."""
        from rates_engine.evidence import DataQuality, Degradation

        clean = self._priced(Currency.USD)
        marked = replace(
            clean,
            evidence=clean.evidence.__class__(
                produced_by="test",
                warnings=(
                    Degradation(
                        code="unverified_convention",
                        message="a convention this build assumes rather than knows",
                        data_quality=DataQuality.ASSUMED,
                    ),
                ),
            ),
        )
        assert clean.evidence.worst_quality is DataQuality.OBSERVED
        assert (clean + marked).evidence.worst_quality is DataQuality.ASSUMED


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


class TestTheCurrencySurvivesTheDerivedCalculations:
    """Deep review, D4-D8. Four places built a new curve, or a new export,
    from one that had a currency and did not carry it. None of them was
    reachable with a peso curve on the day they were written, which is
    exactly why they were wrong: a default that is correct only because
    nothing exercises it is a latent bug, and the currency would have been
    silently reset the first time a peso path reached them.
    """

    def test_the_dual_solver_gives_both_curves_the_currency_it_was_given(self):
        """`solve_dual_curve` built its OIS curve with no currency and then
        constructed the tenor curve seven more times without one."""
        import tests.test_dual_curve as dual_tests
        from rates_engine.curves.dual import solve_dual_curve

        tenor, basis = dual_tests._dual_instruments(0.0005)
        result = solve_dual_curve(
            dual_tests.AS_OF,
            dual_tests._ois_instruments(),
            tenor,
            basis,
            currency=Currency.MXN,
        )
        assert result.ois.currency is Currency.MXN
        assert result.tenor.currency is Currency.MXN

    def test_both_dual_modes_carry_it(self):
        """Simultaneous and sequential build the curves through different
        helpers, and only one of them was fixed by fixing the other."""
        import tests.test_dual_curve as dual_tests
        from rates_engine.curves.dual import solve_dual_curve

        tenor, basis = dual_tests._dual_instruments(0.0005)
        for mode in ("simultaneous", "sequential"):
            result = solve_dual_curve(
                dual_tests.AS_OF,
                dual_tests._ois_instruments(),
                tenor,
                basis,
                mode=mode,
                currency=Currency.MXN,
            )
            assert (result.ois.currency, result.tenor.currency) == (
                Currency.MXN,
                Currency.MXN,
            ), mode

    def test_the_interpolation_comparison_compares_two_curves_in_one_currency(self):
        from rates_engine.curves.comparison import compare_interpolations

        comparison = compare_interpolations(
            AS_OF, _futures_strip(), step_days=7, currency=Currency.MXN
        )
        assert comparison.log_linear.currency is Currency.MXN
        assert comparison.monotone_convex.currency is Currency.MXN

    def test_the_exported_views_say_which_currency_the_rates_are(self):
        """A zero rate is dimensionless. Nine percent on a peso curve and
        nine percent on a dollar curve are the same float, so the export is
        the one place the currency can be lost on the way out."""
        from rates_engine.curves.views import all_views

        views = all_views(_curve(Currency.MXN, 0.09))
        for kind in ("discount", "zero", "par", "forward"):
            view = getattr(views, kind)
            assert view.currency is Currency.MXN, kind
            assert view.to_dict()["currency"] == "MXN", kind

    def test_a_dollar_curve_still_exports_as_usd(self):
        from rates_engine.curves.views import all_views

        assert all_views(_curve()).zero.to_dict()["currency"] == "USD"

    def test_the_shock_table_refuses_a_curve_set_it_cannot_net(self):
        """`shock_table` subtracts a strip P&L built from SR3_DV01 — a dollar
        constant — from the swap's P&L. On a peso curve set that sum is two
        currencies reported as one number, so it refuses instead."""
        from rates_engine.hedging import shock_table

        hedge = _futures_hedge()
        assert not shock_table(hedge, (-100.0,)).table.empty

        pesos = replace(hedge, curve_set=CurveSet(_curve(Currency.MXN, 0.09)))
        with pytest.raises(CurrencyMismatchError, match="futures strip"):
            shock_table(pesos, (-100.0,))

    def test_the_shock_table_says_what_currency_its_columns_are(self):
        from rates_engine.hedging import shock_table

        assert shock_table(_futures_hedge(), (-100.0,)).to_dict()["currency"] == "USD"
