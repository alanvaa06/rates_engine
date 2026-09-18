"""PRD-003 AC-1.1, PRD-003 AC-1.3 and PRD-003 AC-1.4: the peso curve, and what it admits.

This is the phase the research gate shaped. Banxico, ISDA and CME are all
403 from the build environment and QuantLib has no TIIE index, so the day
count, the coupon period and the distinction between the two benchmarks are
assumptions. The machinery is testable anyway — a bootstrap reprices what
it was built from regardless of whether the convention is right — and the
assumptions are testable *as assumptions*, which is what PRD-003 AC-1.4 asks for.

So the tests split three ways. The bootstrap arithmetic, which is exact.
The benchmark marking, which is bookkeeping and must not be skippable. And
the unresolved conventions, which must be present, must reach
`worst_quality`, and must be able to stop the calculation outright.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from rates_engine.conventions.calendar import BMV
from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.curves.bootstrap import ParSwapNode, RealizedStubNode
from rates_engine.curves.discount import CurveSet
from rates_engine.curves.mxn import (
    TIIE_DAY_COUNT,
    TIIE_PERIOD_DAYS,
    UNRESOLVED_MXN,
    MXNCurveResult,
    TIIEBenchmark,
    bootstrap_mxn_curve,
    compare_benchmarks,
    tiie_schedule,
)
from rates_engine.errors import RatesEngineError, UnresolvedConventionError
from rates_engine.evidence import DataQuality
from rates_engine.money import Currency

AS_OF = date(2026, 9, 16)
FLAT = 0.0950


def _instruments(rate: float = FLAT, periods: int = 13):
    """A stub plus a par swap on a flat peso curve, on the 28-day grid."""
    dates = tiie_schedule(AS_OF, periods)
    stub = RealizedStubNode(
        end=dates[0],
        accrual_factor=1.0 + rate * ((dates[0] - AS_OF).days / 360.0),
        label="tiie_stub",
    )
    starts = (AS_OF, *dates[:-1])
    fractions = tuple(
        year_fraction(s, e, TIIE_DAY_COUNT) for s, e in zip(starts, dates, strict=True)
    )
    swap = ParSwapNode(
        start=AS_OF,
        payment_dates=dates,
        year_fractions=fractions,
        quoted_rate=rate,
        label="tiie_1y",
    )
    return (stub, swap)


def _curve(rate: float = FLAT, benchmark=TIIEBenchmark.TIIE_FONDEO, **kw) -> MXNCurveResult:
    return bootstrap_mxn_curve(AS_OF, _instruments(rate), benchmark, **kw)


class TestTheSchedule:
    """Twenty-eight days, rolled onto BMV business days."""

    def test_the_unadjusted_grid_steps_by_the_period(self):
        dates = tiie_schedule(AS_OF, 4, roll=False)
        starts = (AS_OF, *dates[:-1])
        assert all(
            (b - a).days == TIIE_PERIOD_DAYS for a, b in zip(starts, dates, strict=True)
        )

    def test_the_rolled_grid_lands_on_business_days(self):
        for day in tiie_schedule(AS_OF, 13):
            assert BMV.is_business_day(day)

    def test_rolling_only_ever_moves_forward(self):
        plain = tiie_schedule(AS_OF, 13, roll=False)
        rolled = tiie_schedule(AS_OF, 13)
        assert all(r >= p for p, r in zip(plain, rolled, strict=True))

    def test_it_is_increasing(self):
        dates = tiie_schedule(AS_OF, 13)
        assert all(b > a for a, b in zip(dates, dates[1:], strict=False))

    def test_zero_periods_refuses(self):
        with pytest.raises(ValueError, match="at least one period"):
            tiie_schedule(AS_OF, 0)

    def test_the_period_and_basis_are_named_constants(self):
        assert TIIE_PERIOD_DAYS == 28
        assert TIIE_DAY_COUNT is DayCount.ACT_360


class TestTheBootstrapArithmetic:
    """PRD-003 AC-1.1: every instrument reprices, whatever the convention turns out to be."""

    def test_every_instrument_reprices_inside_a_hundredth_of_a_basis_point(self):
        result = _curve()
        assert result.max_residual_bp < 0.01, result.residuals_bp

    def test_each_instrument_is_accounted_for(self):
        result = _curve()
        assert set(result.residuals_bp) == {"tiie_stub", "tiie_1y"}

    def test_the_curve_is_in_pesos(self):
        assert _curve().curve.currency is Currency.MXN

    def test_it_discounts_at_roughly_the_quoted_rate(self):
        result = _curve()
        last = result.curve.nodes[-1]
        years = year_fraction(AS_OF, last, DayCount.ACT_365F)
        implied = -math.log(result.curve.df(last)) / years
        assert implied == pytest.approx(FLAT, rel=0.02)

    @pytest.mark.parametrize("rate", [0.05, 0.0950, 0.12])
    def test_it_reprices_at_any_level(self, rate):
        assert _curve(rate).max_residual_bp < 0.01

    def test_it_is_deterministic(self):
        assert _curve().curve.dfs == _curve().curve.dfs

    def test_a_peso_curve_will_not_discount_a_dollar_flow(self):
        """The currency carried in phase 0, doing its job on a real curve."""
        from rates_engine.errors import CurrencyMismatchError
        from rates_engine.instruments.cashflow import Cashflow
        from rates_engine.pricing import pv

        flow = Cashflow(
            payment_date=AS_OF + timedelta(days=28),
            amount=1.0,
            leg="fixed",
            accrual_start=AS_OF,
            accrual_end=AS_OF + timedelta(days=28),
            year_fraction=28 / 360,
            currency=Currency.USD,
        )

        class _One:
            span = (AS_OF, AS_OF + timedelta(days=28))

            def cashflows(self, curve_set):
                del curve_set
                return (flow,)

            def describe(self):
                return {"kind": "probe"}

        with pytest.raises(CurrencyMismatchError):
            pv(_One(), CurveSet(_curve().curve))


class TestTheBenchmarkIsAlwaysNamed:
    """PRD-003 AC-1.3: which rate this is, carried rather than inferable."""

    def test_the_benchmark_is_required(self):
        with pytest.raises(TypeError):
            bootstrap_mxn_curve(AS_OF, _instruments())  # type: ignore[call-arg]

    @pytest.mark.parametrize("benchmark", list(TIIEBenchmark))
    def test_it_reaches_the_payload(self, benchmark):
        payload = _curve(benchmark=benchmark).payload_fields()
        assert payload["benchmark"] == benchmark.value
        assert payload["currency"] == "MXN"

    @pytest.mark.parametrize("benchmark", list(TIIEBenchmark))
    def test_it_reaches_the_evidence(self, benchmark):
        assert _curve(benchmark=benchmark).evidence.fields["benchmark"] == benchmark.value

    def test_there_are_exactly_the_two_the_decision_kept(self):
        assert {b.value for b in TIIEBenchmark} == {"TIIE28", "TIIE_FONDEO"}


class TestComparingTheTwoBenchmarks:
    """PRD-003 AC-1.3's second half: the difference, in basis points."""

    @pytest.fixture
    def comparison(self):
        fondeo = _curve(0.0950, TIIEBenchmark.TIIE_FONDEO)
        twenty_eight = _curve(0.0985, TIIEBenchmark.TIIE_28)
        return compare_benchmarks(fondeo, twenty_eight)

    def test_it_reports_a_difference_in_basis_points(self, comparison):
        assert comparison.max_difference_bp > 0.0
        assert comparison.max_difference_bp == pytest.approx(35.0, abs=2.0)

    def test_the_sign_follows_the_quotes(self, comparison):
        """TIIE 28 was quoted higher, so its zero curve is above."""
        assert all(v > 0.0 for v in comparison.zero_difference_bp)

    def test_both_benchmarks_are_named_in_the_payload(self, comparison):
        payload = comparison.payload_fields()
        assert payload["first_benchmark"] == "TIIE_FONDEO"
        assert payload["second_benchmark"] == "TIIE28"

    def test_it_samples_the_shared_nodes(self, comparison):
        assert len(comparison.sample_dates) == len(comparison.zero_difference_bp)
        assert comparison.sample_dates

    def test_comparing_a_benchmark_with_itself_refuses(self):
        one = _curve(0.0950, TIIEBenchmark.TIIE_FONDEO)
        other = _curve(0.0985, TIIEBenchmark.TIIE_FONDEO)
        with pytest.raises(ValueError, match="comparing a benchmark with itself"):
            compare_benchmarks(one, other)

    def test_comparing_across_dates_refuses(self):
        later = bootstrap_mxn_curve(
            AS_OF + timedelta(days=1),
            _instruments(),
            TIIEBenchmark.TIIE_28,
        )
        with pytest.raises(ValueError, match="market move"):
            compare_benchmarks(_curve(), later)

    def test_both_curves_evidence_chains_in(self, comparison):
        assert len(comparison.evidence.sources) == 2

    def test_it_is_json_serialisable(self, comparison):
        json.dumps(comparison.to_dict())


class TestTheUnresolvedConventions:
    """PRD-003 AC-1.4, which is the whole reason this phase is honest."""

    def test_every_result_names_them(self):
        result = _curve()
        assert result.unresolved_conventions == tuple(n for n, _ in UNRESOLVED_MXN)
        assert result.unresolved_conventions

    def test_they_are_in_the_payload(self):
        payload = _curve().payload_fields()
        assert "tiie_day_count" in payload["unresolved_conventions"]
        assert "banxico_series_ids" in payload["unresolved_conventions"]

    def test_each_one_becomes_a_degradation(self):
        codes = {w.code for w in _curve().evidence.warnings}
        for name, _ in UNRESOLVED_MXN:
            assert f"unresolved_convention:{name}" in codes

    def test_they_push_the_quality_to_assumed(self):
        assert _curve().evidence.worst_quality is DataQuality.ASSUMED

    def test_anything_priced_on_the_curve_inherits_that(self):
        """The evidence chain is the mechanism, and this is the test that it
        is actually connected rather than decorative."""
        from rates_engine.instruments.swaps import OISSwap, Side
        from rates_engine.pricing import pv

        result = _curve()
        swap = OISSwap(
            effective=result.curve.nodes[0],
            maturity=result.curve.nodes[-1],
            fixed_rate=FLAT,
            notional=100_000_000.0,
            side=Side.PAYER,
        )
        # The flows are dollars by default, so state the peso currency by
        # pricing on a peso curve built for it.
        priced = pv(swap, CurveSet(result.curve.__class__(
            result.curve.as_of, result.curve.nodes, result.curve.dfs,
            result.curve.interpolation, currency=Currency.USD,
        )), source_evidence=(result.evidence,))
        assert priced.evidence.worst_quality is DataQuality.ASSUMED

    def test_the_messages_say_why_rather_than_just_what(self):
        for _, why in UNRESOLVED_MXN:
            assert len(why) > 40
        joined = " ".join(why for _, why in UNRESOLVED_MXN)
        assert "Banxico" in joined

    def test_the_calendar_gap_is_one_of_them(self):
        """The BMV-versus-Banxico distinction is a convention gap like the
        others, not a footnote in a docstring."""
        assert "mxn_calendar_is_bmv_not_banxico" in {n for n, _ in UNRESOLVED_MXN}

    def test_the_evidence_says_what_inherits(self):
        assert "inherits ASSUMED" in _curve().evidence.fields["note"]


class TestStrictConventionsRefuses:
    """PRD-003 AC-1.4's second half: a caller can demand verified conventions."""

    def test_it_refuses_while_anything_is_unresolved(self):
        with pytest.raises(UnresolvedConventionError) as excinfo:
            _curve(strict_conventions=True)
        assert "assumed rather than verified" in str(excinfo.value)

    def test_the_refusal_names_every_one(self):
        with pytest.raises(UnresolvedConventionError) as excinfo:
            _curve(strict_conventions=True)
        message = str(excinfo.value)
        for name, _ in UNRESOLVED_MXN:
            assert name in message

    def test_it_says_why_they_are_unresolved_and_how_to_proceed(self):
        with pytest.raises(UnresolvedConventionError) as excinfo:
            _curve(strict_conventions=True)
        message = str(excinfo.value)
        assert "403 at the egress proxy" in message
        assert "shorten UNRESOLVED_MXN" in message

    def test_it_refuses_before_computing_anything(self):
        """A refusal that arrives after the curve is built is a warning with
        extra steps."""
        with pytest.raises(UnresolvedConventionError):
            bootstrap_mxn_curve(
                AS_OF, (), TIIEBenchmark.TIIE_28, strict_conventions=True
            )

    def test_the_default_is_to_proceed_and_mark(self):
        assert _curve().max_residual_bp < 0.01

    def test_it_is_catchable_as_an_engine_error(self):
        with pytest.raises(RatesEngineError):
            _curve(strict_conventions=True)

    def test_resolving_them_would_turn_it_off_with_no_code_change(self, monkeypatch):
        """The gap is stored as data, so emptying the tuple is the whole fix.
        This simulates the half hour with a browser."""
        import rates_engine.curves.mxn as mxn

        monkeypatch.setattr(mxn, "UNRESOLVED_MXN", ())
        result = mxn.bootstrap_mxn_curve(
            AS_OF, _instruments(), TIIEBenchmark.TIIE_28, strict_conventions=True
        )
        assert result.unresolved_conventions == ()
        assert result.evidence.warnings == ()
        assert result.evidence.worst_quality is not DataQuality.ASSUMED


class TestSerialisation:
    def test_the_payload_carries_the_assumed_conventions_as_values(self):
        payload = _curve().payload_fields()
        assert payload["tiie_period_days"] == 28
        assert payload["tiie_day_count"] == "ACT/360"

    def test_it_is_json_serialisable(self):
        json.dumps(_curve().to_dict())

    def test_the_curve_serialises_its_currency(self):
        assert _curve().payload_fields()["curve"]["currency"] == "MXN"
