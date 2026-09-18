"""PRD-001 AC-9.1: every public result serialises the same way, with no missing keys.

The rule that matters is the second one. A consumer should never have to tell
"the engine did not compute this" apart from "the engine has no such field" by
catching a ``KeyError``, so an absent value is ``null`` and the key stays.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from rates_engine.convexity import convexity_adjustment, realized_sofr_sigma
from rates_engine.curves import all_views
from rates_engine.curves.comparison import compare_interpolations
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.curves.dual import BasisSwapNode, TenorParSwapNode, solve_dual_curve
from rates_engine.curves.parametric import fit_fomc_step_curve, fit_nelson_siegel
from rates_engine.hedging import shock_table, strip_hedge
from rates_engine.optionpricing import swaption_pv
from rates_engine.pricing import annuity, dv01, par_rate, price_on_parametric, pv
from rates_engine.reporting import dumps, error_payload, result_payload
from rates_engine.results import SCHEMA_VERSION, EngineResult
from rates_engine.risk import key_rate_dv01, money_convexity, option_greeks, pvbp
from rates_engine.volatility.cube import CubePoint, VolCube
from rates_engine.volatility.units import Volatility, VolUnits

NS_TIMES = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0)
NS_RATES = (0.0445, 0.0432, 0.0417, 0.0413, 0.0413, 0.0422)


def _parametric(as_of, par_swap):
    """A Nelson-Siegel fit, and the same swap priced on it and on the exact curve."""
    fit = fit_nelson_siegel(NS_TIMES, NS_RATES)
    nodes = tuple(as_of + timedelta(days=round(365.0 * t)) for t in NS_TIMES)
    exact = DiscountCurve(
        as_of, nodes, tuple(math.exp(-r * t) for t, r in zip(NS_TIMES, NS_RATES, strict=True))
    )
    parametric = CurveSet(fit.model.discount_curve(as_of, nodes))
    return fit, price_on_parametric(par_swap, parametric, CurveSet(exact), fit=fit)


def _fx_smile():
    """A vanna-volga reading, as `test_fx_smile.py` builds the smile."""
    from rates_engine.fx.delta import DeltaBasis, DeltaConvention, PremiumAdjustment
    from rates_engine.fx.vannavolga import ATMConvention, SmileQuotes, VannaVolgaSmile

    smile = VannaVolgaSmile(
        spot=18.50,
        expiry=0.25,
        r_domestic=0.0950,
        r_foreign=0.0420,
        quotes=SmileQuotes(atm=0.1150, risk_reversal_25=0.0180, butterfly_25=0.0035),
        delta_convention=DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED),
        atm_convention=ATMConvention.DELTA_NEUTRAL_STRADDLE,
    )
    return smile.volatility_at(19.0)


def _volatility(option_curve_set, atm_swaption, forward_swap_rate):
    """A cube read, the fit behind it, a swaption price and its greeks."""
    vol = Volatility(90.0, VolUnits.NORMAL_BP)
    strikes = tuple(forward_swap_rate + d for d in (-0.01, -0.005, 0.0, 0.005, 0.01))
    cube = VolCube(
        as_of=option_curve_set.discount.as_of,
        points=tuple(
            CubePoint(5.0, 10.0, k, Volatility(90.0 + 300.0 * (k - forward_swap_rate) ** 2 * 1e4,
                                               VolUnits.NORMAL_BP))
            for k in strikes
        ),
    )
    quote = cube.volatility_at(5.0, 10.0, forward_swap_rate + 0.0025, forward_swap_rate)
    return [
        quote,
        quote.calibration,
        swaption_pv(atm_swaption, option_curve_set, vol),
        option_greeks(atm_swaption, option_curve_set, vol),
    ]


def _dual_curve(as_of):
    """A dual-curve solve on constructed inputs, as `TestDualCurvePayload` builds it."""
    from rates_engine.conventions import DayCount, year_fraction
    from rates_engine.curves import ParSwapNode, RealizedStubNode

    maturities = (date(2027, 1, 15), date(2028, 1, 17))
    ois = (
        RealizedStubNode(
            end=maturities[0],
            accrual_factor=1.0 + 0.04 * ((maturities[0] - as_of).days / 360.0),
        ),
        ParSwapNode(
            start=as_of,
            payment_dates=maturities,
            year_fractions=tuple(
                year_fraction(
                    as_of if k == 0 else maturities[k - 1], maturities[k], DayCount.ACT_360
                )
                for k in range(2)
            ),
            quoted_rate=0.04,
            label="ois_2y",
        ),
    )
    periods = ((as_of, maturities[0]), (maturities[0], maturities[1]))
    tenor = (
        TenorParSwapNode(
            start=as_of,
            payment_dates=(maturities[0],),
            year_fractions=(year_fraction(as_of, maturities[0], DayCount.ACT_360),),
            float_periods=(periods[0],),
            quoted_rate=0.0405,
            label="term_1y",
        ),
    )
    basis = (BasisSwapNode(float_periods=periods, quoted_spread=0.0005, label="basis_2y"),)
    return solve_dual_curve(as_of, ois, tenor, basis)


def _fomc_fit():
    """The step-curve fit, read from the same fixture `test_parametric.py` uses."""
    import csv

    fixtures = Path(__file__).parent / "fixtures"
    with (fixtures / "fomc_futures_strip.csv").open(encoding="utf-8") as handle:
        contracts = tuple(
            (
                row["label"],
                date.fromisoformat(row["reference_start"]),
                date.fromisoformat(row["reference_end"]),
                row["symbol"],
                float(row["price"]),
            )
            for row in csv.DictReader(handle)
        )
    with (fixtures / "fomc_meetings.csv").open(encoding="utf-8") as handle:
        meetings = tuple(date.fromisoformat(r["effective_date"]) for r in csv.DictReader(handle))
    return fit_fomc_step_curve(date(2026, 9, 16), meetings, contracts)


def _all_results(par_swap, curve_set, flat_curve, strip, as_of,
                 option_curve_set, atm_swaption, forward_swap_rate, snapshot):
    hedge = strip_hedge(par_swap, strip, as_of=as_of)
    fit, comparison = _parametric(as_of, par_swap)
    return [
        flat_curve,
        all_views(flat_curve.curve, source_evidence=flat_curve.evidence),
        pv(par_swap, curve_set),
        par_rate(par_swap, curve_set),
        annuity(par_swap, curve_set),
        dv01(par_swap, curve_set),
        pvbp(par_swap, curve_set),
        money_convexity(par_swap, curve_set),
        key_rate_dv01(par_swap, curve_set, (0.5, 1.0, 2.0)),
        hedge,
        shock_table(hedge, (-10.0, 10.0)),
        convexity_adjustment(1.0, 1.25, model="ho_lee", sigma=0.01),
        fit,
        comparison,
        compare_interpolations(as_of, strip, step_days=30),
        _dual_curve(as_of),
        _fomc_fit(),
        realized_sofr_sigma(snapshot),
        _fx_smile(),
        *_volatility(option_curve_set, atm_swaption, forward_swap_rate),
    ]


def _every_result_type() -> set[str]:
    """Every concrete :class:`EngineResult` subclass the package defines.

    Imports the whole package first, because a subclass is only discoverable
    once its module has been imported.
    """
    import importlib
    import pkgutil

    import rates_engine

    root = Path(rates_engine.__file__).parent
    for module in pkgutil.walk_packages([str(root)], prefix="rates_engine."):
        importlib.import_module(module.name)

    def descendants(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from descendants(sub)

    return {cls.__name__ for cls in descendants(EngineResult)}


def test_the_list_below_is_every_result_type_the_package_defines(
    par_swap, curve_set, flat_curve, strip, as_of,
    option_curve_set, atm_swaption, forward_swap_rate, snapshot,
):
    """The class below says the contract holds for all of them. This is what
    makes that true: a new result type that nobody added to ``_all_results``
    fails here rather than quietly going untested."""
    covered = {
        type(r).__name__
        for r in _all_results(par_swap, curve_set, flat_curve, strip, as_of,
                              option_curve_set, atm_swaption, forward_swap_rate, snapshot)
    }
    missing = sorted(_every_result_type() - covered)
    assert missing == [], f"result types with no payload test: {missing}"


class TestEveryResult:
    """The contract holds for all of them, not for the ones that were tested."""

    @pytest.fixture
    def results(self, par_swap, curve_set, flat_curve, strip, as_of,
                option_curve_set, atm_swaption, forward_swap_rate, snapshot):
        return _all_results(par_swap, curve_set, flat_curve, strip, as_of,
                            option_curve_set, atm_swaption, forward_swap_rate, snapshot)

    def test_every_payload_has_a_schema_version(self, results):
        for result in results:
            assert result.to_dict()["schema_version"] == SCHEMA_VERSION

    def test_every_payload_names_its_type(self, results):
        for result in results:
            assert result.to_dict()["result_type"] == type(result).__name__

    def test_every_payload_carries_evidence(self, results):
        for result in results:
            evidence = result.to_dict()["evidence"]
            assert evidence["produced_by"]
            assert "data_quality" in evidence
            assert isinstance(evidence["sources"], list)

    def test_every_payload_is_json_serialisable(self, results):
        for result in results:
            assert json.loads(dumps(result.to_dict()))

    def test_no_payload_hides_an_absent_value_by_dropping_the_key(self, results):
        # Walk the whole document: a value may be null, but the key it would
        # have lived under must be there.
        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    assert key == key.strip(), f"whitespace in key at {path}"
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        for result in results:
            walk(result.to_dict())

    def test_nulls_are_present_where_a_value_is_absent(self, par_swap, curve_set):
        # A PV carries cashflows; a DV01 does not, and says so with a null.
        assert pv(par_swap, curve_set).to_dict()["cashflows"] is not None
        assert dv01(par_swap, curve_set).to_dict()["cashflows"] is None


class TestErrorPayloads:
    """PRD-001 AC-9.3's shape: a failure is a document too."""

    def test_an_engine_error_carries_its_exit_code(self):
        from rates_engine.errors import MissingFixingError

        payload = error_payload(MissingFixingError("no fixing for 2026-01-14"))
        assert payload["exit_code"] == 1
        assert payload["error"]["type"] == "MissingFixingError"
        assert payload["error"]["recoverable"] is True
        assert payload["result_type"] is None

    def test_an_impossible_calculation_is_exit_code_two(self):
        from rates_engine.errors import CurveArbitrageError

        payload = error_payload(CurveArbitrageError("rising discount factors"))
        assert payload["exit_code"] == 2
        assert payload["error"]["recoverable"] is False

    def test_an_unexpected_error_is_exit_code_one(self):
        payload = error_payload(RuntimeError("something else"))
        assert payload["exit_code"] == 1
        assert payload["error"]["type"] == "RuntimeError"

    def test_the_error_payload_has_a_schema_version_too(self):
        assert error_payload(RuntimeError("x"))["schema_version"] == SCHEMA_VERSION

    def test_no_traceback_leaks_into_the_document(self):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            payload = error_payload(exc)
        assert "Traceback" not in json.dumps(payload)


class TestSerialisation:
    """The JSON writer does not emit anything a strict reader rejects."""

    def test_non_finite_floats_become_null(self):
        assert json.loads(dumps({"a": float("nan"), "b": float("inf")})) == {
            "a": None,
            "b": None,
        }

    def test_nested_non_finite_floats_become_null(self):
        parsed = json.loads(dumps({"rows": [{"x": float("nan")}], "t": (1.0, float("-inf"))}))
        assert parsed["rows"][0]["x"] is None
        assert parsed["t"] == [1.0, None]

    def test_extras_reach_the_top_level(self, flat_curve):
        payload = result_payload(flat_curve, command="bootstrap")
        assert payload["command"] == "bootstrap"
        assert payload["schema_version"] == SCHEMA_VERSION

    def test_dates_serialise_as_iso_strings(self, flat_curve):
        payload = json.loads(dumps(flat_curve.to_dict()))
        assert payload["curve"]["nodes"][0].count("-") == 2


class TestDualCurvePayload:
    """The dual-curve result honours the same contract as the rest."""

    def test_it_serialises_with_both_curves(self, as_of):
        from datetime import date

        from rates_engine.conventions import DayCount, year_fraction
        from rates_engine.curves import ParSwapNode, RealizedStubNode

        maturities = (date(2027, 1, 15), date(2028, 1, 17))
        ois = (
            RealizedStubNode(
                end=maturities[0],
                accrual_factor=1.0 + 0.04 * ((maturities[0] - as_of).days / 360.0),
            ),
            ParSwapNode(
                start=as_of,
                payment_dates=maturities,
                year_fractions=tuple(
                    year_fraction(
                        as_of if k == 0 else maturities[k - 1], maturities[k], DayCount.ACT_360
                    )
                    for k in range(2)
                ),
                quoted_rate=0.04,
                label="ois_2y",
            ),
        )
        periods = ((as_of, maturities[0]), (maturities[0], maturities[1]))
        tenor = (
            TenorParSwapNode(
                start=as_of,
                payment_dates=(maturities[0],),
                year_fractions=(year_fraction(as_of, maturities[0], DayCount.ACT_360),),
                float_periods=(periods[0],),
                quoted_rate=0.0405,
                label="term_1y",
            ),
        )
        basis = (BasisSwapNode(float_periods=periods, quoted_spread=0.0005, label="basis_2y"),)
        payload = solve_dual_curve(as_of, ois, tenor, basis).to_dict()
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["inputs_origin"] == "synthetic"
        assert json.loads(dumps(payload))


class TestImmutability:
    """Results are frozen, so a payload cannot drift from the thing it describes."""

    def test_a_result_cannot_be_mutated(self, par_swap, curve_set):
        from dataclasses import FrozenInstanceError

        result = pv(par_swap, curve_set)
        with pytest.raises(FrozenInstanceError):
            result.value = 0.0  # type: ignore[misc]

    def test_replace_makes_a_copy(self, par_swap, curve_set):
        result = pv(par_swap, curve_set)
        other = replace(result, value=1.0)
        assert result.value != other.value
