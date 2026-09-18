"""PRD-002 AC-4.1 to PRD-002 AC-4.4: four parameters for a curve, and a step path for a strip.

Both models here are smoothings, and the tests are written to keep that
visible. A bootstrapped curve reproduces its quotes by construction; a
parametric curve does not, and the residual is the model's answer to "what
shape do you believe in". So every fit test reports the miss as well as
bounding it, and the swap test prices the same trade both ways rather than
presenting the parametric number on its own.

Fixtures: ``zero_curve.csv`` is a two-hump Svensson curve, which Nelson-Siegel
cannot reproduce; ``fomc_futures_strip.csv`` is a strip quoted off a known
step path and then rounded to the exchange tick, so neither fit is handed its
own answer.
"""

from __future__ import annotations

import ast
import csv
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

import rates_engine
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.curves.parametric import (
    TERM_RATE_CAVEAT,
    FOMCStepCurve,
    NelsonSiegel,
    fit_fomc_step_curve,
    fit_nelson_siegel,
)
from rates_engine.errors import CalibrationError, UnderdeterminedCurveError
from rates_engine.evidence import DataQuality
from rates_engine.instruments.swaps import OISSwap, Side
from rates_engine.pricing import ParametricComparison, price_on_parametric, pv

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = date(2026, 9, 16)


def _zero_curve() -> tuple[tuple[float, ...], tuple[float, ...]]:
    with (FIXTURES / "zero_curve.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return (
        tuple(float(r["time"]) for r in rows),
        tuple(float(r["zero_rate"]) for r in rows),
    )


@pytest.fixture(scope="module")
def zero_curve() -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Maturities in years and continuously compounded zero rates."""
    return _zero_curve()


@pytest.fixture(scope="module")
def ns_fit(zero_curve):
    """Nelson-Siegel fitted to the fixture curve."""
    times, rates = zero_curve
    return fit_nelson_siegel(times, rates)


@pytest.fixture(scope="module")
def strip() -> tuple[tuple[str, date, date, str, float], ...]:
    """The SR1 and SR3 settlements, as the fixture writes them."""
    with (FIXTURES / "fomc_futures_strip.csv").open(encoding="utf-8") as handle:
        return tuple(
            (
                r["label"],
                date.fromisoformat(r["reference_start"]),
                date.fromisoformat(r["reference_end"]),
                r["symbol"],
                float(r["price"]),
            )
            for r in csv.DictReader(handle)
        )


@pytest.fixture(scope="module")
def meetings() -> tuple[tuple[date, float], ...]:
    """Meeting effective dates and the path that generated the strip."""
    with (FIXTURES / "fomc_meetings.csv").open(encoding="utf-8") as handle:
        return tuple(
            (date.fromisoformat(r["effective_date"]), float(r["true_rate_after"]))
            for r in csv.DictReader(handle)
        )


@pytest.fixture(scope="module")
def step_fit(strip, meetings):
    """The step path fitted to the strip."""
    return fit_fomc_step_curve(AS_OF, tuple(d for d, _ in meetings), strip)


class TestNelsonSiegelTheFunction:
    """The model itself, before anything is fitted to it."""

    def test_the_short_rate_is_beta0_plus_beta1(self):
        model = NelsonSiegel(0.042, -0.005, 0.01, 2.0)
        assert model.zero_rate(1e-9) == pytest.approx(model.short_rate, abs=1e-8)
        assert model.short_rate == pytest.approx(0.037)

    def test_the_long_rate_is_beta0(self):
        model = NelsonSiegel(0.042, -0.005, 0.01, 2.0)
        assert model.zero_rate(500.0) == pytest.approx(0.042, abs=1e-4)

    def test_the_loadings_are_continuous_at_zero(self):
        near = NelsonSiegel.loadings(1e-10, 2.0)
        at = NelsonSiegel.loadings(0.0, 2.0)
        assert near == pytest.approx(at, abs=1e-8)

    def test_curvature_loading_peaks_inside_the_curve(self):
        """beta2 is a hump, not a level or a slope: its loading is zero at both
        ends and positive between them."""
        tau = 2.0
        interior = [NelsonSiegel.loadings(t, tau)[2] for t in (0.5, 1.0, 2.0, 4.0)]
        assert all(v > 0 for v in interior)
        assert NelsonSiegel.loadings(1e-9, tau)[2] == pytest.approx(0.0, abs=1e-8)
        assert NelsonSiegel.loadings(1e6, tau)[2] == pytest.approx(0.0, abs=1e-5)

    def test_a_non_positive_tau_refuses(self):
        with pytest.raises(ValueError):
            NelsonSiegel(0.04, 0.0, 0.0, 0.0)


class TestNelsonSiegelFit:
    """PRD-002 AC-4.1: how far four parameters miss a real-shaped curve, and by how much."""

    def test_rmse_is_under_three_basis_points(self, ns_fit):
        assert ns_fit.rmse_bp < 3.0, f"fitted to {ns_fit.rmse_bp:.4f} bp"

    def test_the_miss_is_not_zero(self, ns_fit):
        """The fixture is Svensson. If NS fitted it exactly, either the fixture
        stopped having a second hump or the fit stopped being Nelson-Siegel."""
        assert ns_fit.rmse_bp > 0.1

    def test_evidence_carries_all_four_parameters(self, ns_fit):
        parameters = ns_fit.evidence.fields["parameters"]
        assert set(parameters) >= {"beta0", "beta1", "beta2", "tau"}
        assert all(math.isfinite(parameters[k]) for k in ("beta0", "beta1", "beta2", "tau"))

    def test_evidence_reports_the_error_it_bounds(self, ns_fit):
        assert ns_fit.evidence.fields["rmse_bp"] == pytest.approx(ns_fit.rmse_bp)
        assert ns_fit.evidence.fields["max_error_bp"] == pytest.approx(ns_fit.max_error_bp)

    def test_the_fitted_values_are_the_model_evaluated(self, ns_fit):
        recomputed = tuple(ns_fit.model.zero_rate(t) for t in ns_fit.times)
        assert recomputed == pytest.approx(ns_fit.fitted)

    def test_tau_lands_inside_its_bounds(self, ns_fit):
        assert 0.05 < ns_fit.model.tau < 30.0

    def test_the_fit_is_deterministic(self, zero_curve):
        times, rates = zero_curve
        first = fit_nelson_siegel(times, rates)
        second = fit_nelson_siegel(times, rates)
        assert first.model == second.model
        assert first.rmse_bp == second.rmse_bp

    def test_it_recovers_a_curve_it_generated(self):
        """Against a curve that *is* Nelson-Siegel, the fit should be exact.
        This separates solver error from model error: the previous tests
        measure the model, this one measures the solver."""
        truth = NelsonSiegel(0.042, -0.006, 0.012, 2.5)
        times = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0)
        fit = fit_nelson_siegel(times, tuple(truth.zero_rate(t) for t in times))
        assert fit.rmse_bp < 1e-6
        assert fit.model.beta0 == pytest.approx(truth.beta0, abs=1e-8)
        assert fit.model.tau == pytest.approx(truth.tau, abs=1e-5)

    def test_fewer_points_than_parameters_refuses(self):
        with pytest.raises(CalibrationError) as excinfo:
            fit_nelson_siegel((1.0, 5.0, 10.0), (0.04, 0.042, 0.043))
        assert "interpolation wearing a model's name" in str(excinfo.value)

    def test_mismatched_lengths_refuse(self):
        with pytest.raises(CalibrationError):
            fit_nelson_siegel((1.0, 2.0, 3.0, 4.0), (0.04, 0.042, 0.043))

    def test_a_tolerance_is_enforced_when_given(self, zero_curve):
        times, rates = zero_curve
        with pytest.raises(CalibrationError) as excinfo:
            fit_nelson_siegel(times, rates, tolerance_bp=0.01)
        assert "above the 0.01 bp tolerance" in str(excinfo.value)

    def test_the_payload_marks_the_curve_kind(self, ns_fit):
        payload = ns_fit.payload_fields()
        assert payload["curve_kind"] == "parametric"
        assert payload["model"] == "nelson_siegel"
        json.dumps(ns_fit.to_dict())

    def test_sampling_onto_a_discount_curve_reproduces_the_model(self, ns_fit):
        nodes = tuple(AS_OF + timedelta(days=365 * k) for k in (1, 2, 5, 10, 30))
        curve = ns_fit.model.discount_curve(AS_OF, nodes)
        for node in nodes:
            time = year_fraction(AS_OF, node, DayCount.ACT_365F)
            assert curve.df(node) == pytest.approx(
                math.exp(-ns_fit.model.zero_rate(time) * time)
            )


class TestFOMCStepCurveTheFunction:
    """The step path itself: where it jumps and what it averages to."""

    def test_the_rate_is_flat_between_meetings(self):
        curve = FOMCStepCurve(AS_OF, (date(2026, 11, 5),), (0.043, 0.040))
        assert curve.rate_on(date(2026, 10, 1)) == 0.043
        assert curve.rate_on(date(2026, 11, 4)) == 0.043
        assert curve.rate_on(date(2026, 11, 5)) == 0.040
        assert curve.rate_on(date(2027, 6, 1)) == 0.040

    def test_the_average_of_a_flat_segment_is_the_segment_rate(self):
        curve = FOMCStepCurve(AS_OF, (date(2026, 11, 5),), (0.043, 0.040))
        assert curve.averaged(date(2026, 10, 1), date(2026, 11, 1)) == pytest.approx(0.043)

    def test_compounding_a_flat_segment_exceeds_averaging_it(self):
        """SR1 settles on the average and SR3 on the compounded rate, and the
        gap between them is not a rounding artefact: to first order it is
        ``r^2 T / 2``, which at 4.3% over a quarter is about 2.3 bp."""
        rate = 0.043
        curve = FOMCStepCurve(AS_OF, (), (rate,))
        start, end = date(2026, 12, 16), date(2027, 3, 17)
        gap = curve.compounded(start, end) - curve.averaged(start, end)
        expected = rate * rate * ((end - start).days / 360.0) / 2.0
        assert gap > 0.0
        assert gap == pytest.approx(expected, rel=0.05)

    def test_the_average_of_a_straddling_period_is_day_weighted(self):
        meeting = date(2026, 11, 5)
        curve = FOMCStepCurve(AS_OF, (meeting,), (0.043, 0.040))
        start, end = date(2026, 11, 1), date(2026, 12, 1)
        before = (meeting - start).days
        total = (end - start).days
        expected = (before * 0.043 + (total - before) * 0.040) / total
        assert curve.averaged(start, end) == pytest.approx(expected)

    def test_a_backwards_period_refuses(self):
        curve = FOMCStepCurve(AS_OF, (), (0.043,))
        with pytest.raises(ValueError):
            curve.compounded(date(2027, 1, 1), date(2026, 1, 1))
        with pytest.raises(ValueError):
            curve.averaged(date(2027, 1, 1), date(2027, 1, 1))

    def test_the_wrong_number_of_segments_refuses(self):
        with pytest.raises(ValueError) as excinfo:
            FOMCStepCurve(AS_OF, (date(2026, 11, 5), date(2026, 12, 17)), (0.043, 0.040))
        assert "need 3 segment rates" in str(excinfo.value)

    def test_unsorted_meetings_refuse(self):
        with pytest.raises(ValueError):
            FOMCStepCurve(AS_OF, (date(2026, 12, 17), date(2026, 11, 5)), (0.043, 0.04, 0.038))

    def test_the_discount_curve_compounds_the_path(self):
        curve = FOMCStepCurve(AS_OF, (), (0.043,))
        node = AS_OF + timedelta(days=90)
        sampled = curve.discount_curve((node,))
        expected = 1.0
        day = AS_OF
        while day < node:
            expected *= 1.0 + 0.043 / 360.0
            day += timedelta(days=1)
        assert sampled.df(node) == pytest.approx(1.0 / expected)


class TestFOMCStepFit:
    """PRD-002 AC-4.2: fitting the path, repricing the strip, and reading a term rate."""

    def test_the_strip_reprices_inside_a_basis_point(self, step_fit):
        assert step_fit.max_residual_bp < 1.0, (
            f"worst residual {step_fit.max_residual_bp:.4f} bp: "
            f"{ {k: round(v, 4) for k, v in step_fit.residuals_bp.items()} }"
        )

    def test_every_contract_is_accounted_for(self, step_fit, strip):
        assert set(step_fit.residuals_bp) == {c[0] for c in strip}

    def test_it_recovers_the_path_that_generated_the_strip(self, step_fit, meetings):
        """Stronger than repricing: the strip was quoted off a known path, and
        the fit should find that path and not merely something that fits."""
        recovered = step_fit.curve.segment_rates[1:]
        truth = tuple(rate for _, rate in meetings)
        for found, expected in zip(recovered, truth, strict=True):
            assert found == pytest.approx(expected, abs=1e-5)

    def test_the_implied_moves_are_the_differences_of_the_path(self, step_fit):
        rates = step_fit.curve.segment_rates
        for index, meeting in enumerate(step_fit.curve.meeting_dates):
            expected = (rates[index + 1] - rates[index]) * 1e4
            assert step_fit.implied_moves_bp[meeting.isoformat()] == pytest.approx(expected)

    def test_it_reads_an_easing_cycle(self, step_fit):
        assert all(move < 0 for move in step_fit.implied_moves_bp.values())

    def test_a_term_rate_comes_with_its_caveat(self, step_fit):
        rate = step_fit.curve.term_rate(date(2026, 10, 1), 3)
        assert 0.035 < rate < 0.045
        assert step_fit.payload_fields()["term_rate_caveat"] == TERM_RATE_CAVEAT
        assert "no convexity adjustment" in TERM_RATE_CAVEAT

    def test_the_caveat_is_a_warning_not_only_a_string(self, step_fit):
        codes = {w.code for w in step_fit.evidence.warnings}
        assert "term_rate_without_convexity" in codes
        assert step_fit.evidence.worst_quality is DataQuality.ASSUMED

    def test_the_evidence_says_no_convexity_was_applied(self, step_fit):
        assert step_fit.evidence.fields["convexity_adjustment_applied"] is False

    def test_a_six_month_term_rate_exceeds_the_three_month_one_in_an_easing_cycle(
        self, step_fit
    ):
        """Sanity, not a model claim: with rates falling, the compounded rate
        over six months averages lower cuts in, so it sits below the 3M."""
        three = step_fit.curve.term_rate(date(2026, 10, 1), 3)
        six = step_fit.curve.term_rate(date(2026, 10, 1), 6)
        assert six < three

    def test_too_few_contracts_refuse(self, meetings):
        with pytest.raises(UnderdeterminedCurveError) as excinfo:
            fit_fomc_step_curve(
                AS_OF,
                tuple(d for d, _ in meetings),
                (("SR1-Oct26", date(2026, 10, 1), date(2026, 11, 1), "SR1", 95.7),),
            )
        assert "cannot pin 5 segments" in str(excinfo.value)

    def test_an_unreachable_tolerance_refuses(self, strip, meetings):
        with pytest.raises(CalibrationError) as excinfo:
            fit_fomc_step_curve(
                AS_OF, tuple(d for d, _ in meetings), strip, tolerance_bp=1e-6
            )
        assert "Residuals:" in str(excinfo.value)

    def test_non_strict_reports_instead_of_refusing(self, strip, meetings):
        fit = fit_fomc_step_curve(
            AS_OF, tuple(d for d, _ in meetings), strip, tolerance_bp=1e-6, strict=False
        )
        assert fit.max_residual_bp > 1e-6

    def test_the_payload_marks_the_curve_kind(self, step_fit):
        payload = step_fit.payload_fields()
        assert payload["curve_kind"] == "parametric"
        assert payload["model"] == "fomc_step"
        json.dumps(step_fit.to_dict())

    def test_the_fit_is_deterministic(self, strip, meetings):
        dates = tuple(d for d, _ in meetings)
        first = fit_fomc_step_curve(AS_OF, dates, strip)
        second = fit_fomc_step_curve(AS_OF, dates, strip)
        assert first.curve.segment_rates == second.curve.segment_rates


class TestPricingOnAParametricCurve:
    """PRD-002 AC-4.3: the payload says which curve it used and what that cost."""

    @pytest.fixture
    def swap(self) -> OISSwap:
        return OISSwap(
            effective=AS_OF + timedelta(days=7),
            maturity=AS_OF + timedelta(days=365 * 5),
            fixed_rate=0.042,
            notional=100_000_000.0,
            side=Side.PAYER,
        )

    @pytest.fixture
    def both_curves(self, ns_fit, zero_curve):
        """The fitted curve and the curve it was fitted to, on the same nodes.

        The second is the fixture's own zero rates turned into discount
        factors with nothing in between — the quotes a bootstrap would
        reproduce exactly. Comparing against that isolates the smoothing,
        which is what PRD-002 AC-4.3 is about, rather than mixing it with a
        bootstrap's own interpolation choice.
        """
        times, rates = zero_curve
        nodes = tuple(AS_OF + timedelta(days=round(365.0 * t)) for t in times)
        exact = DiscountCurve(
            AS_OF,
            nodes,
            tuple(math.exp(-r * t) for t, r in zip(times, rates, strict=True)),
        )
        return CurveSet(ns_fit.model.discount_curve(AS_OF, nodes)), CurveSet(exact)

    def test_the_payload_marks_the_curve_kind(self, swap, both_curves, ns_fit):
        parametric, exact = both_curves
        result = price_on_parametric(swap, parametric, exact, fit=ns_fit)
        assert isinstance(result, ParametricComparison)
        assert result.payload_fields()["curve_kind"] == "parametric"
        assert result.model == "nelson_siegel"

    def test_it_reports_the_difference_against_the_other_curve(
        self, swap, both_curves, ns_fit
    ):
        parametric, exact = both_curves
        result = price_on_parametric(swap, parametric, exact, fit=ns_fit)
        assert result.difference == pytest.approx(result.parametric_pv - result.bootstrap_pv)
        assert result.parametric_pv == pytest.approx(pv(swap, parametric).value)
        assert result.bootstrap_pv == pytest.approx(pv(swap, exact).value)

    def test_the_difference_is_real_but_small(self, swap, both_curves, ns_fit):
        """A few basis points of curve error on a five-year swap is a few
        basis points of notional, not zero and not a blow-up."""
        parametric, exact = both_curves
        result = price_on_parametric(swap, parametric, exact, fit=ns_fit)
        assert 0.0 < abs(result.difference_bp_of_notional) < 20.0

    def test_the_difference_is_scaled_by_notional(self, both_curves, ns_fit):
        parametric, exact = both_curves
        small = OISSwap(
            effective=AS_OF + timedelta(days=7),
            maturity=AS_OF + timedelta(days=365 * 5),
            fixed_rate=0.042,
            notional=1_000_000.0,
            side=Side.PAYER,
        )
        big = OISSwap(
            effective=AS_OF + timedelta(days=7),
            maturity=AS_OF + timedelta(days=365 * 5),
            fixed_rate=0.042,
            notional=100_000_000.0,
            side=Side.PAYER,
        )
        a = price_on_parametric(small, parametric, exact, fit=ns_fit)
        b = price_on_parametric(big, parametric, exact, fit=ns_fit)
        assert a.difference_bp_of_notional == pytest.approx(b.difference_bp_of_notional)
        assert b.difference == pytest.approx(100.0 * a.difference)

    def test_the_fit_evidence_is_chained_in(self, swap, both_curves, ns_fit):
        parametric, exact = both_curves
        result = price_on_parametric(swap, parametric, exact, fit=ns_fit)
        assert ns_fit.evidence in result.evidence.sources

    def test_it_refuses_a_fit_that_is_not_parametric(self, swap, both_curves):
        parametric, exact = both_curves
        priced = pv(swap, exact)
        with pytest.raises(ValueError) as excinfo:
            price_on_parametric(swap, parametric, exact, fit=priced)
        assert "will not take a fit that is not one" in str(excinfo.value)

    def test_the_payload_is_serialisable(self, swap, both_curves, ns_fit):
        parametric, exact = both_curves
        json.dumps(price_on_parametric(swap, parametric, exact, fit=ns_fit).to_dict())


class TestNoDependencyOnTheOtherRepo:
    """PRD-002 AC-4.4: Nelson-Siegel is reimplemented here, not imported from elsewhere."""

    def test_nothing_imports_the_nelson_siegel_repo(self):
        root = Path(rates_engine.__file__).parent
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if "nelson" in name.lower() and not name.startswith("rates_engine"):
                        offenders.append(f"{path.name}: {name}")
        assert offenders == [], offenders

    def test_the_package_declares_no_such_dependency(self):
        text = (Path(rates_engine.__file__).parents[2] / "pyproject.toml").read_text()
        assert "Nelson_Siegel_Model" not in text
