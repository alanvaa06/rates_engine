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
from rates_engine.conventions.side import Side
from rates_engine.core.errors import (
    CalibrationError,
    ImplausibleInputError,
    MissingFixingError,
    UnderdeterminedCurveError,
    UnsupportedConventionError,
)
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.curves.parametric import (
    TERM_RATE_CAVEAT,
    FOMCStepCurve,
    NelsonSiegel,
    fit_fomc_step_curve,
    fit_nelson_siegel,
)
from rates_engine.instruments.swaps import OISSwap
from rates_engine.market.snapshot import MarketSnapshot, Series
from rates_engine.pricing.linear import ParametricComparison, price_on_parametric, pv

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = date(2026, 9, 16)

SIFMA_2026_HOLIDAYS = frozenset(
    {date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25)}
)
"""SIFMA's full closes between September and December 2026, written out by hand
(Columbus Day, Veterans Day, Thanksgiving, Christmas), so the reference below
does not lean on the calendar it is checking."""


def _is_business(day: date, holidays: frozenset[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def _business_day_weights(
    start: date, end: date, holidays: frozenset[date]
) -> list[tuple[date, int]]:
    """CME's ``(business day, d_i)`` over ``[start, end)``, one calendar day at a time.

    Every calendar day takes the rate of the business day on or before it;
    ``d_i`` counts the calendar days each business day's rate covers. Written
    independently of the package so the tests check its arithmetic rather
    than repeat it.
    """
    out: list[tuple[date, int]] = []
    day = start
    while day < end:
        anchor = day
        while not _is_business(anchor, holidays):
            anchor -= timedelta(days=1)
        if out and out[-1][0] == anchor:
            out[-1] = (anchor, out[-1][1] + 1)
        else:
            out.append((anchor, 1))
        day += timedelta(days=1)
    return out


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

    def test_it_finds_the_global_minimum_of_the_tau_profile(self):
        """A Svensson curve whose profile has an interior minimum and a lower
        value nowhere else but a slope down to the upper bound.

        Bounded Brent alone settled on the bound: tau = 30, 7.16 bp RMSE. A
        dense grid over the same bounds (20,001 log-spaced points, loadings
        and least squares written out independently in a scratch script)
        finds tau = 2.0127 at 1.5988 bp. In the review, 24 of 300 random
        Svensson curves were left more than 5 bp worse than the grid.
        """
        b0, b1, b2, b3 = (
            0.046729229025487776,
            -0.001418807478039904,
            0.01112545124353296,
            -0.027950686078118084,
        )
        tau1, tau2 = 2.014123777370009, 13.41654368571956

        def svensson(t: float) -> float:
            x, y = t / tau1, t / tau2
            s1, s2 = (1.0 - math.exp(-x)) / x, (1.0 - math.exp(-y)) / y
            return b0 + b1 * s1 + b2 * (s1 - math.exp(-x)) + b3 * (s2 - math.exp(-y))

        times = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0)
        fit = fit_nelson_siegel(times, tuple(svensson(t) for t in times))
        assert fit.rmse_bp == pytest.approx(1.598783249179921, abs=1e-5)
        assert fit.model.tau == pytest.approx(2.0126537839608125, abs=1e-3)
        assert "nelson_siegel_tau_at_bound" not in {w.code for w in fit.evidence.warnings}

    def test_it_finds_a_minimum_on_the_bound_too(self):
        """The mirror case: an interior local minimum that is worse than the
        bound. Brent alone stopped at tau = 0.3006 inside ``(0.05, 1.0)``."""
        truth = NelsonSiegel(0.042, -0.006, 0.012, 2.5)
        times = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0)
        rates = tuple(truth.zero_rate(t) for t in times)
        fit = fit_nelson_siegel(times, rates, tau_bounds=(0.05, 1.0))
        assert fit.model.tau == pytest.approx(1.0, abs=1e-6)

    def test_a_tau_on_its_bound_is_flagged(self):
        """The optimum outside the bounds is not an error -- the caller chose
        the range -- but the decay was set by the range rather than the data,
        and the evidence has to say so."""
        truth = NelsonSiegel(0.042, -0.006, 0.012, 2.5)
        times = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0)
        fit = fit_nelson_siegel(
            times, tuple(truth.zero_rate(t) for t in times), tau_bounds=(4.0, 30.0)
        )
        assert fit.model.tau == pytest.approx(4.0, abs=1e-6)
        warnings = {w.code: w for w in fit.evidence.warnings}
        assert "nelson_siegel_tau_at_bound" in warnings
        assert "4.0" in warnings["nelson_siegel_tau_at_bound"].message
        assert fit.evidence.fields["tau_at_bound"] == "lower"

    def test_an_interior_tau_is_not_flagged(self, ns_fit):
        assert ns_fit.evidence.fields["tau_at_bound"] is None
        assert ns_fit.evidence.warnings == ()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_a_non_finite_rate_refuses(self, zero_curve, bad):
        """NaN used to pass straight through: ``rmse > tolerance`` is false
        for a NaN RMSE, so even an explicit tolerance never fired."""
        times, rates = zero_curve
        broken = (bad, *rates[1:])
        with pytest.raises(ImplausibleInputError, match="zero_rates"):
            fit_nelson_siegel(times, broken, tolerance_bp=1.0)

    def test_a_non_finite_time_refuses(self, zero_curve):
        times, rates = zero_curve
        with pytest.raises(ImplausibleInputError, match="times"):
            fit_nelson_siegel((*times[:-1], float("nan")), rates)

    def test_a_fit_that_is_not_a_number_refuses(self):
        """Finite inputs whose squares overflow: the RMSE is not a number, and
        that is a failed fit, not a fit to report."""
        times = (1.0, 2.0, 3.0, 5.0, 10.0)
        with pytest.raises(CalibrationError, match="not a finite"):
            fit_nelson_siegel(times, (1e300, -1e300, 1e300, -1e300, 1e300))

    def test_nonsense_bounds_refuse(self, zero_curve):
        times, rates = zero_curve
        with pytest.raises(ValueError, match="tau_bounds"):
            fit_nelson_siegel(times, rates, tau_bounds=(5.0, 1.0))
        with pytest.raises(ValueError, match="tau_bounds"):
            fit_nelson_siegel(times, rates, tau_bounds=(0.0, 30.0))

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
        """Business day by business day, each rate for the calendar days it
        covers, as the SR3 settlement does. It used to compound
        ``1 + r/360`` once per calendar day, which turns a weekend into
        ``(1 + r/360)^3`` rather than ``1 + 3r/360``."""
        curve = FOMCStepCurve(AS_OF, (), (0.043,))
        node = AS_OF + timedelta(days=90)
        sampled = curve.discount_curve((node,))
        expected = 1.0
        for _, days in _business_day_weights(AS_OF, node, SIFMA_2026_HOLIDAYS):
            expected *= 1.0 + 0.043 * days / 360.0
        assert sampled.df(node) == pytest.approx(1.0 / expected, rel=1e-15)

    def test_the_sampled_curve_and_compounded_agree_at_every_node(self):
        """One set of terms for both, so a later fix to one reaches the other.
        Nodes on weekdays, a weekend and across meetings."""
        curve = FOMCStepCurve(
            AS_OF, (date(2026, 11, 5), date(2026, 12, 17)), (0.043, 0.0405, 0.038)
        )
        nodes = (
            date(2026, 10, 3),   # a Saturday
            date(2026, 10, 30),
            date(2026, 11, 5),   # the meeting itself
            date(2026, 11, 29),  # a Sunday
            date(2027, 1, 20),
        )
        sampled = curve.discount_curve(nodes)
        for node in nodes:
            growth = 1.0 + curve.compounded(AS_OF, node) * (node - AS_OF).days / 360.0
            assert sampled.df(node) == pytest.approx(1.0 / growth, rel=1e-14)

    def test_compounding_follows_the_cme_and_new_york_fed_formula(self):
        """``prod(1 + r_i d_i / 360)`` over business days, ``d_i`` the calendar
        days rate ``r_i`` applies for -- three for a Friday. The window holds
        no SIFMA holiday (Columbus Day is the day before it, Veterans Day the
        day after), so the weights are plain weekday arithmetic."""
        rate = 0.043
        curve = FOMCStepCurve(AS_OF, (), (rate,))
        start, end = date(2026, 10, 13), date(2026, 11, 10)
        factor = 1.0
        for _, days in _business_day_weights(start, end, frozenset()):
            factor *= 1.0 + rate * days / 360.0
        expected = (factor - 1.0) * 360.0 / (end - start).days
        assert curve.compounded(start, end) == pytest.approx(expected, abs=1e-15)
        daily = (1.0 + rate / 360.0) ** (end - start).days
        old = (daily - 1.0) * 360.0 / (end - start).days
        assert abs(old - expected) * 1e4 > 0.02, "the window no longer separates the two"

    def test_a_period_starting_on_a_weekend_carries_the_friday_rate(self):
        """A step dated on a Saturday -- not a real effective date, chosen to
        separate the two readings -- and a period starting that Saturday. The
        weekend takes the rate published for Friday, the one before the step,
        not the one the path holds on the calendar day."""
        saturday = date(2026, 10, 17)
        curve = FOMCStepCurve(AS_OF, (saturday,), (0.043, 0.040))
        assert curve.compounded(saturday, saturday + timedelta(days=2)) == pytest.approx(
            0.043, abs=1e-12
        )
        assert curve.averaged(saturday, saturday + timedelta(days=2)) == pytest.approx(
            0.043, abs=1e-12
        )


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

    def test_a_non_finite_price_refuses_by_contract(self, strip, meetings):
        """It used to reach scipy, which raised a bare ``ValueError`` about
        residuals 'not finite in the initial point'."""
        broken = strip[:1] + ((strip[1][0], strip[1][1], strip[1][2], "SR1", float("nan")),)
        with pytest.raises(ImplausibleInputError, match=strip[1][0]):
            fit_fomc_step_curve(AS_OF, tuple(d for d, _ in meetings), broken + strip[2:])

    def test_an_unknown_symbol_refuses(self, strip, meetings):
        """Anything that was not ``SR1`` used to be compounded as an SR3."""
        relabelled = ((strip[0][0], strip[0][1], strip[0][2], "SR2", strip[0][4]), *strip[1:])
        with pytest.raises(UnsupportedConventionError, match="SR2"):
            fit_fomc_step_curve(AS_OF, tuple(d for d, _ in meetings), relabelled)


class TestFOMCStepFitInProgress:
    """A contract whose reference period started before ``as_of``.

    Its settlement is part history. The path starts at ``as_of``, so before
    this the days already fixed were read off the first segment as if the
    overnight rate had sat there all along -- through a meeting already
    held, if there was one. Now they come from the published fixings, passed
    as the same ``MarketSnapshot`` swaps and caps take, and a strip that
    needs them and does not have them is refused.
    """

    IN_PROGRESS_AS_OF = date(2026, 10, 15)
    MEETINGS = (date(2026, 11, 5), date(2026, 12, 17))
    PATH = (0.0430, 0.0405, 0.0380)
    FIXING = 0.0450
    """Every SOFR fixing before ``as_of``: deliberately not the first segment."""

    def _days(self, *, through_as_of: bool = False) -> list[date]:
        out: list[date] = []
        day = date(2026, 9, 14)
        while day < self.IN_PROGRESS_AS_OF or (through_as_of and day == self.IN_PROGRESS_AS_OF):
            if _is_business(day, SIFMA_2026_HOLIDAYS):
                out.append(day)
            day += timedelta(days=1)
        return out

    def _snapshot(self, days: list[date], values: list[float] | None = None) -> MarketSnapshot:
        series = Series(
            "SOFR",
            tuple(days),
            tuple(values if values is not None else [self.FIXING] * len(days)),
            Provenance(source="file", series_id="SOFR", instrument_kind="sofr_fixing"),
        )
        return MarketSnapshot(as_of=self.IN_PROGRESS_AS_OF, series={"SOFR": series})

    def _rate(self, business_day: date, *, as_of_fixed: bool = False) -> float:
        """The overnight rate published for a business day: a fixing before
        ``as_of`` (and on it, when it is fixed), the path after."""
        if business_day < self.IN_PROGRESS_AS_OF or (
            as_of_fixed and business_day == self.IN_PROGRESS_AS_OF
        ):
            return self.FIXING
        index = sum(1 for meeting in self.MEETINGS if business_day >= meeting)
        return self.PATH[index]

    def _sr1(self, start: date, end: date, **kw) -> float:
        """CME SR1: the calendar-day average, each day at its business day's rate."""
        weights = _business_day_weights(start, end, SIFMA_2026_HOLIDAYS)
        total = sum(self._rate(day, **kw) * days for day, days in weights)
        return 100.0 * (1.0 - total / (end - start).days)

    def _sr3(self, start: date, end: date, **kw) -> float:
        """CME SR3: ``prod(1 + r_i d_i / 360)`` over the business days."""
        factor = 1.0
        for day, days in _business_day_weights(start, end, SIFMA_2026_HOLIDAYS):
            factor *= 1.0 + self._rate(day, **kw) * days / 360.0
        return 100.0 * (1.0 - (factor - 1.0) * 360.0 / (end - start).days)

    def _strip(self, **kw):
        months = (
            ("SR1-Oct26", date(2026, 10, 1), date(2026, 11, 1)),
            ("SR1-Nov26", date(2026, 11, 1), date(2026, 12, 1)),
            ("SR1-Dec26", date(2026, 12, 1), date(2027, 1, 1)),
        )
        strip = [(label, s, e, "SR1", self._sr1(s, e, **kw)) for label, s, e in months]
        sep, dec = date(2026, 9, 16), date(2026, 12, 16)
        strip.append(("SR3-Sep26", sep, dec, "SR3", self._sr3(sep, dec, **kw)))
        return tuple(strip)

    def test_without_fixings_it_refuses_and_names_the_contracts(self):
        with pytest.raises(MissingFixingError) as excinfo:
            fit_fomc_step_curve(self.IN_PROGRESS_AS_OF, self.MEETINGS, self._strip())
        assert "SR1-Oct26" in str(excinfo.value)
        assert "SR3-Sep26" in str(excinfo.value)
        assert "MarketSnapshot" in str(excinfo.value)

    def test_with_fixings_it_recovers_the_path(self):
        fit = fit_fomc_step_curve(
            self.IN_PROGRESS_AS_OF,
            self.MEETINGS,
            self._strip(),
            fixings=self._snapshot(self._days()),
        )
        assert fit.curve.segment_rates == pytest.approx(self.PATH, abs=1e-10)
        assert fit.max_residual_bp < 1e-6

    def test_the_realised_days_are_in_the_evidence(self):
        fit = fit_fomc_step_curve(
            self.IN_PROGRESS_AS_OF,
            self.MEETINGS,
            self._strip(),
            fixings=self._snapshot(self._days()),
        )
        realised = fit.evidence.fields["realised_days"]
        assert realised["SR1-Oct26"] == 14
        assert realised["SR3-Sep26"] == 29
        assert realised["SR1-Nov26"] == 0
        assert fit.evidence.inputs[0].instrument_kind == "sofr_fixing"

    def test_the_as_of_fixing_is_forecast_when_absent_and_says_so(self):
        """The convention on as_of: a fixing dated as_of is read when it is
        there and forecast when it is not, and the forecast is a warning."""
        fit = fit_fomc_step_curve(
            self.IN_PROGRESS_AS_OF,
            self.MEETINGS,
            self._strip(),
            fixings=self._snapshot(self._days()),
        )
        warnings = {w.code: w for w in fit.evidence.warnings}
        assert "fixing_forecast_on_as_of" in warnings
        assert "SR1-Oct26" in warnings["fixing_forecast_on_as_of"].message
        assert fit.evidence.fields["forecast_days_on_as_of"]["SR1-Oct26"] == 1

    def test_the_as_of_fixing_is_read_when_present(self):
        days = self._days(through_as_of=True)
        fit = fit_fomc_step_curve(
            self.IN_PROGRESS_AS_OF,
            self.MEETINGS,
            self._strip(as_of_fixed=True),
            fixings=self._snapshot(days),
        )
        assert fit.curve.segment_rates == pytest.approx(self.PATH, abs=1e-10)
        assert fit.evidence.fields["realised_days"]["SR1-Oct26"] == 15
        assert "fixing_forecast_on_as_of" not in {w.code for w in fit.evidence.warnings}

    def test_a_missing_business_day_is_named(self):
        days = [d for d in self._days() if d != date(2026, 10, 7)]
        with pytest.raises(MissingFixingError, match="2026-10-07"):
            fit_fomc_step_curve(
                self.IN_PROGRESS_AS_OF, self.MEETINGS, self._strip(), fixings=self._snapshot(days)
            )

    def test_a_snapshot_without_sofr_is_refused(self):
        empty = MarketSnapshot(as_of=self.IN_PROGRESS_AS_OF, series={})
        with pytest.raises(MissingFixingError, match="SOFR"):
            fit_fomc_step_curve(self.IN_PROGRESS_AS_OF, self.MEETINGS, self._strip(), fixings=empty)

    def test_a_non_finite_fixing_refuses(self):
        """Refused by the fit on main's ``Series``; a ``Series`` that refuses
        NaN at construction (PR #11) stops it one step earlier, also naming
        the day."""
        days = self._days()
        values = [float("nan") if d == date(2026, 10, 7) else self.FIXING for d in days]
        with pytest.raises((ImplausibleInputError, ValueError), match="2026-10-07"):
            fit_fomc_step_curve(
                self.IN_PROGRESS_AS_OF,
                self.MEETINGS,
                self._strip(),
                fixings=self._snapshot(days, values),
            )

    def test_a_strip_that_starts_after_as_of_needs_none(self, strip, meetings):
        fit = fit_fomc_step_curve(AS_OF, tuple(d for d, _ in meetings), strip)
        assert set(fit.evidence.fields["realised_days"].values()) == {0}


class TestFOMCStepFitPinsEverySegment:
    """Alan Vazquez's PR #15 review: a segment no unfixed day touches is pinned by nothing.

    ``as_of`` 2026-01-30, meetings 2026-01-29 and 2026-03-19, fixings at
    3.65% before ``as_of`` and SR1 for January to March. The segment before
    the January meeting covers only days already fixed, so ``least_squares``
    left it at its starting point -- 100 minus the January price, exactly --
    and ``implied_moves_bp`` reported a move for a meeting already held.
    """

    AS_OF = date(2026, 1, 30)

    def _snapshot(self) -> MarketSnapshot:
        days, day = [], date(2025, 12, 20)
        while day < self.AS_OF:
            if day.weekday() < 5 and day not in (date(2025, 12, 25), date(2026, 1, 1),
                                                 date(2026, 1, 19)):
                days.append(day)
            day += timedelta(days=1)
        series = Series(
            "SOFR", tuple(days), (0.0365,) * len(days), Provenance(source="file")
        )
        return MarketSnapshot(as_of=self.AS_OF, series={"SOFR": series})

    STRIP = (
        ("SR1-Jan26", date(2026, 1, 1), date(2026, 2, 1), "SR1", 96.35),
        ("SR1-Feb26", date(2026, 2, 1), date(2026, 3, 1), "SR1", 96.40),
        ("SR1-Mar26", date(2026, 3, 1), date(2026, 4, 1), "SR1", 96.45),
    )

    def test_a_meeting_already_held_is_refused(self):
        with pytest.raises(UnderdeterminedCurveError, match="2026-01-29"):
            fit_fomc_step_curve(
                self.AS_OF,
                (date(2026, 1, 29), date(2026, 3, 19)),
                self.STRIP,
                fixings=self._snapshot(),
                strict=False,
            )

    def test_a_meeting_on_as_of_is_refused_too(self):
        with pytest.raises(UnderdeterminedCurveError, match="2026-01-30"):
            fit_fomc_step_curve(
                self.AS_OF, (self.AS_OF,), self.STRIP, fixings=self._snapshot(), strict=False
            )

    def test_without_it_the_same_strip_fits(self):
        fit = fit_fomc_step_curve(
            self.AS_OF, (date(2026, 3, 19),), self.STRIP, fixings=self._snapshot(), strict=False
        )
        assert len(fit.curve.segment_rates) == 2

    def test_a_meeting_the_strip_never_reaches_is_refused(self, strip, meetings):
        """Enough contracts to count, and none in the last segment: it came back
        at its starting point too."""
        dates = (*(d for d, _ in meetings), date(2027, 9, 16))
        with pytest.raises(UnderdeterminedCurveError, match="2027-09-16 onwards"):
            fit_fomc_step_curve(AS_OF, dates, strip, strict=False)

    def test_a_wholly_fixed_contract_does_not_count(self):
        """December was fixed in full by as_of: it prices nothing from the path."""
        strip = (("SR1-Dec25", date(2025, 12, 1), date(2026, 1, 1), "SR1", 96.35),) + self.STRIP[:1]
        days, day = [], date(2025, 11, 26)
        while day < self.AS_OF:
            if day.weekday() < 5 and day not in (
                date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19)
            ):
                days.append(day)
            day += timedelta(days=1)
        snapshot = MarketSnapshot(
            as_of=self.AS_OF,
            series={"SOFR": Series("SOFR", tuple(days), (0.0365,) * len(days),
                                   Provenance(source="file"))},
        )
        with pytest.raises(UnderdeterminedCurveError, match="wholly fixed"):
            fit_fomc_step_curve(
                self.AS_OF, (date(2026, 3, 19),), strip, fixings=snapshot, strict=False
            )


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
