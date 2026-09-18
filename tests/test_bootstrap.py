"""AC-3.1, 3.2, 3.4 and 3.5: everything reprices, nothing is dropped in silence."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from rates_engine.conventions import DayCount, imm_date, next_imm_on_or_after
from rates_engine.curves import (
    DiscountCurve,
    FuturesNode,
    ParSwapNode,
    RealizedStubNode,
    bootstrap_discount_curve,
)
from rates_engine.errors import (
    BootstrapResidualError,
    CurveArbitrageError,
    UnderdeterminedCurveError,
)


class TestRoundTrip:
    """AC-3.1: every input instrument reprices to under a hundredth of a bp."""

    def test_every_residual_is_inside_tolerance(self, flat_curve):
        assert flat_curve.residuals_bp
        assert max(abs(r) for r in flat_curve.residuals_bp.values()) < 0.01

    def test_residuals_are_far_better_than_the_tolerance(self, flat_curve):
        # Solved rather than fitted: each node is a root, so the residual is
        # machine noise, not an approximation inside the tolerance.
        assert max(abs(r) for r in flat_curve.residuals_bp.values()) < 1e-8

    def test_forwards_come_back_at_the_quoted_rate(self, strip, flat_curve):
        for instrument in strip:
            if isinstance(instrument, FuturesNode):
                implied = flat_curve.curve.forward(
                    instrument.start, instrument.end, day_count=DayCount.ACT_360
                )
                assert implied == pytest.approx(instrument.forward_rate, abs=1e-12)

    def test_the_stub_reprices(self, strip, flat_curve):
        stub = next(i for i in strip if isinstance(i, RealizedStubNode))
        growth = flat_curve.curve.df(flat_curve.curve.as_of) / flat_curve.curve.df(stub.end)
        assert growth == pytest.approx(stub.accrual_factor, rel=1e-14)

    def test_par_swap_nodes_reprice(self, as_of):
        payments = tuple(date(2027 + k, 1, 15) for k in range(3))
        node = ParSwapNode(
            start=as_of,
            payment_dates=payments,
            year_fractions=(1.0138, 1.0139, 1.0139),
            quoted_rate=0.041,
            label="ois_3y",
        )
        result = bootstrap_discount_curve(as_of, (node,))
        assert node.par_rate(result.curve) == pytest.approx(0.041, abs=1e-12)

    def test_instrument_order_does_not_change_the_answer(self, strip, as_of):
        forward = bootstrap_discount_curve(as_of, strip)
        backward = bootstrap_discount_curve(as_of, tuple(reversed(strip)))
        assert forward.curve.dfs == backward.curve.dfs


class TestCurveShape:
    """AC-3.2: positive, non-increasing, or a refusal naming the segment."""

    def test_discount_factors_are_positive_and_non_increasing(self, flat_curve):
        dfs = flat_curve.curve.dfs
        assert all(df > 0 for df in dfs)
        assert all(b <= a for a, b in zip(dfs, dfs[1:], strict=False))

    def test_interpolated_points_are_monotone_too(self, flat_curve):
        curve = flat_curve.curve
        day = curve.as_of
        previous = 1.0
        while day <= curve.nodes[-1]:
            current = curve.df(day)
            assert current <= previous + 1e-15
            previous = current
            day += timedelta(days=7)

    def test_rising_discount_factor_is_refused_where_it_matters(self, as_of):
        # Constructing such a curve is allowed — a risk bump can produce one,
        # and negative rates are not a data error. Asserting it came out of a
        # calibration is what raises, and the message names the segment.
        nodes = (as_of + timedelta(days=365), as_of + timedelta(days=730))
        curve = DiscountCurve(as_of, nodes, (0.96, 0.97))
        assert curve.rising_segments == ((nodes[0], nodes[1]),)
        with pytest.raises(CurveArbitrageError, match="2027-01-15 to 2028-01-15"):
            curve.require_monotone()

    def test_an_ordinary_curve_has_no_rising_segment(self, flat_curve):
        assert flat_curve.curve.rising_segments == ()
        flat_curve.curve.require_monotone()

    def test_a_bump_through_zero_is_allowed_rather_than_refused(self, as_of):
        # A one basis point forward shifted down by one basis point. The
        # symmetric difference behind every DV01 in this package needs this
        # to work, and a currency that has had negative rates needs it too.
        nodes = (as_of + timedelta(days=365), as_of + timedelta(days=730))
        curve = DiscountCurve(as_of, nodes, (0.9999, 0.99985))  # ~0.5 bp forward
        bumped = curve.shifted(-1e-4)
        assert bumped.rising_segments
        with pytest.raises(CurveArbitrageError):
            bumped.require_monotone()

    def test_non_positive_discount_factor_refuses(self, as_of):
        with pytest.raises(CurveArbitrageError, match="not a curve"):
            DiscountCurve(as_of, (as_of + timedelta(days=365),), (0.0,))

    def test_a_quote_implying_a_rising_curve_refuses_at_bootstrap(self, as_of):
        start = imm_date(2026, 3)
        following = next_imm_on_or_after(start + timedelta(days=1))
        instruments = (
            RealizedStubNode(end=start, accrual_factor=1.007),
            FuturesNode(start=start, end=following, forward_rate=-0.05, label="negative"),
        )
        with pytest.raises(CurveArbitrageError, match="negative"):
            bootstrap_discount_curve(as_of, instruments)


class TestRefusalsAndDrops:
    """AC-3.4: nothing is discarded quietly, under either strictness."""

    def test_no_instruments_refuses(self, as_of):
        with pytest.raises(UnderdeterminedCurveError, match="undefined"):
            bootstrap_discount_curve(as_of, ())

    def test_two_instruments_on_one_node_refuses_and_names_both(self, as_of, strip):
        futures = [i for i in strip if isinstance(i, FuturesNode)]
        duplicate = replace(futures[0], label="duplicate")
        with pytest.raises(UnderdeterminedCurveError, match="duplicate"):
            bootstrap_discount_curve(as_of, strip + (duplicate,))

    def test_nothing_is_dropped_on_a_clean_fit(self, flat_curve):
        assert flat_curve.dropped_instruments == ()

    def test_an_unsupported_interpolation_refuses(self, as_of, strip):
        from rates_engine.errors import UnsupportedConventionError

        with pytest.raises(UnsupportedConventionError, match="monotone_convex"):
            bootstrap_discount_curve(as_of, strip, interpolation="monotone_convex")

    def test_strict_raises_where_lenient_records(self, as_of, monkeypatch):
        # An instrument the solver cannot zero: its residual does not depend on
        # the node it claims to pin, so no discount factor solves it.
        start = imm_date(2026, 3)

        class Stubborn:
            label = "stubborn"
            provenance = RealizedStubNode(end=start, accrual_factor=1.0).provenance

            @property
            def node_date(self):
                return start

            def residual_bp(self, curve):
                del curve
                return 5.0

            def describe(self):
                return {"kind": "stubborn"}

        with pytest.raises(CurveArbitrageError, match="stubborn"):
            bootstrap_discount_curve(as_of, (Stubborn(),))

    def test_drops_are_recorded_in_the_evidence_not_only_on_the_result(self, as_of, strip):
        lenient = bootstrap_discount_curve(as_of, strip, strict=False, tolerance_bp=1e-18)
        recorded = lenient.evidence.fields["dropped_instruments"]
        assert recorded is not None
        assert len(recorded) == len(lenient.dropped_instruments)
        assert all("reason" in entry and "residual_bp" in entry for entry in recorded)

    def test_a_clean_fit_records_none_rather_than_omitting_the_key(self, flat_curve):
        assert "dropped_instruments" in flat_curve.evidence.fields
        assert flat_curve.evidence.fields["dropped_instruments"] is None

    def test_lenient_mode_records_the_reason(self, as_of, strip):
        # Tolerance tightened below machine noise so a real fit trips it.
        with pytest.raises(BootstrapResidualError, match="residual"):
            bootstrap_discount_curve(as_of, strip, strict=True, tolerance_bp=1e-18)
        lenient = bootstrap_discount_curve(as_of, strip, strict=False, tolerance_bp=1e-18)
        assert lenient.dropped_instruments
        assert all("residual" in d["reason"] for d in lenient.dropped_instruments)
        assert all("label" in d for d in lenient.dropped_instruments)


class TestEvidence:
    """AC-3.5: the evidence carries everything needed to audit the fit."""

    def test_the_required_fields_are_all_present(self, flat_curve):
        fields = flat_curve.evidence.fields
        for key in (
            "interpolation",
            "fit_residuals_bp",
            "max_abs_residual_bp",
            "nodes",
            "instruments_used",
            "convexity_models",
            "tolerance_bp",
            "long_end_source",
        ):
            assert key in fields, key

    def test_every_instrument_appears_with_its_residual(self, strip, flat_curve):
        used = flat_curve.evidence.fields["instruments_used"]
        assert len(used) == len(strip)
        assert all("residual_bp" in entry and "kind" in entry for entry in used)

    def test_provenance_of_every_input_is_carried(self, strip, flat_curve):
        assert len(flat_curve.evidence.inputs) == len(strip)

    def test_the_convexity_model_is_recorded(self, flat_curve):
        assert flat_curve.evidence.fields["convexity_models"] == ["ho_lee"]

    def test_the_interpolation_is_recorded_on_the_curve_too(self, flat_curve):
        assert flat_curve.curve.to_dict()["interpolation"] == "log_linear_df"
        assert flat_curve.curve.to_dict()["time_basis"] == "ACT/365F"
