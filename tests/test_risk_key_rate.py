"""PRD-001 AC-10.1 to PRD-001 AC-10.7: tent shocks, the sum that follows from them, and two bases.

The load-bearing test here is :meth:`TestTentShocks.test_the_shocks_sum_to_a_parallel_shift`.
Everything else about the key-rate profile — that it adds up to the parallel
DV01, that it is comparable across instruments — is downstream of the shocks
being a partition of unity, so that property is asserted on the shocks
themselves rather than inferred from a total that happens to come out right.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rates_engine.errors import KeyTenorOutOfRangeError, UndefinedDurationError
from rates_engine.hedging import strip_hedge
from rates_engine.pricing import dv01
from rates_engine.risk import (
    INTERPOLATION_CAVEAT,
    key_rate_duration,
    key_rate_dv01,
    tent_weights,
)

TENORS = (0.5, 1.0, 1.5, 2.0, 2.4)
SUM_RELATIVE_TOLERANCE = 1e-6


class TestTentShocks:
    """PRD-001 AC-10.1: the shocks are a partition of unity, checked directly."""

    @pytest.mark.parametrize(
        "time", [0.0, 0.1, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.4, 3.0, 10.0]
    )
    def test_the_shocks_sum_to_a_parallel_shift(self, time):
        assert sum(tent_weights(time, TENORS)) == pytest.approx(1.0, abs=1e-15)

    @pytest.mark.parametrize("time", [0.0, 0.7, 1.3, 2.2, 5.0])
    def test_weights_are_never_negative(self, time):
        assert all(weight >= 0.0 for weight in tent_weights(time, TENORS))

    def test_each_tenor_peaks_at_itself(self):
        for index, tenor in enumerate(TENORS):
            weights = tent_weights(tenor, TENORS)
            assert weights[index] == pytest.approx(1.0)
            assert sum(weights) == pytest.approx(1.0)

    def test_the_first_and_last_shocks_are_flat_outside_the_grid(self):
        assert tent_weights(0.0, TENORS)[0] == 1.0
        assert tent_weights(99.0, TENORS)[-1] == 1.0

    def test_halfway_between_two_tenors_splits_evenly(self):
        weights = tent_weights(0.75, TENORS)
        assert weights[0] == pytest.approx(0.5)
        assert weights[1] == pytest.approx(0.5)
        assert sum(weights[2:]) == 0.0

    def test_a_single_tenor_carries_everything(self):
        assert tent_weights(3.0, (1.0,)) == (1.0,)

    def test_unordered_tenors_refuse(self):
        with pytest.raises(ValueError, match="strictly increasing"):
            tent_weights(1.0, (2.0, 1.0))

    def test_no_tenors_refuse(self):
        with pytest.raises(ValueError, match="must not be empty"):
            tent_weights(1.0, ())


class TestKeyRateDV01:
    """PRD-001 AC-10.2: the profile sums to the parallel DV01."""

    def test_the_total_matches_the_parallel_dv01(self, par_swap, curve_set):
        profile = key_rate_dv01(par_swap, curve_set, TENORS)
        assert profile.total == pytest.approx(
            dv01(par_swap, curve_set).value, rel=SUM_RELATIVE_TOLERANCE
        )

    def test_it_is_far_tighter_than_the_tolerance(self, par_swap, curve_set):
        # The tolerance is relative, and it has to be: the residual is the
        # third-order term of a symmetric difference, so it is tiny but it is
        # not zero, and an absolute 1e-6 on a twenty-thousand-dollar number
        # would be asking for bit-exactness under a different name.
        profile = key_rate_dv01(par_swap, curve_set, TENORS)
        parallel = dv01(par_swap, curve_set).value
        assert abs(profile.total - parallel) / abs(parallel) < 1e-8

    def test_one_value_per_tenor(self, par_swap, curve_set):
        profile = key_rate_dv01(par_swap, curve_set, TENORS)
        assert tuple(profile.values) == TENORS
        assert profile.key_tenors == TENORS

    def test_the_risk_sits_where_the_swap_matures(self, par_swap, curve_set):
        profile = key_rate_dv01(par_swap, curve_set, TENORS)
        heaviest = max(profile.values, key=lambda t: abs(profile.values[t]))
        assert heaviest == max(TENORS)

    def test_the_unit_is_declared(self, par_swap, curve_set):
        assert key_rate_dv01(par_swap, curve_set, TENORS).unit == "USD_per_bp"

    def test_a_larger_bump_gives_nearly_the_same_profile(self, par_swap, curve_set):
        small = key_rate_dv01(par_swap, curve_set, TENORS, bump_bp=1.0)
        large = key_rate_dv01(par_swap, curve_set, TENORS, bump_bp=5.0)
        for tenor in TENORS:
            assert large.values[tenor] == pytest.approx(small.values[tenor], rel=1e-4)

    def test_a_non_positive_bump_refuses(self, par_swap, curve_set):
        with pytest.raises(ValueError, match="bump_bp"):
            key_rate_dv01(par_swap, curve_set, TENORS, bump_bp=0.0)

    def test_dates_work_as_well_as_years(self, par_swap, curve_set, as_of):
        by_date = key_rate_dv01(par_swap, curve_set, (as_of + timedelta(days=730),))
        assert len(by_date.values) == 1
        assert list(by_date.values)[0] == pytest.approx(2.0, abs=0.01)


class TestOutOfRange:
    """PRD-001 AC-10.4: a tenor beyond the curve is refused, not extrapolated."""

    def test_a_tenor_past_the_last_node_refuses(self, par_swap, curve_set):
        with pytest.raises(KeyTenorOutOfRangeError, match="not extrapolated"):
            key_rate_dv01(par_swap, curve_set, (0.5, 10.0))

    def test_the_refusal_names_the_tenor_and_the_span(self, par_swap, curve_set):
        with pytest.raises(KeyTenorOutOfRangeError) as caught:
            key_rate_dv01(par_swap, curve_set, (10.0,))
        assert "10.0" in str(caught.value)
        assert "years" in str(caught.value)

    def test_a_non_positive_tenor_refuses(self, par_swap, curve_set):
        with pytest.raises(KeyTenorOutOfRangeError):
            key_rate_dv01(par_swap, curve_set, (0.0,))


class TestEvidence:
    """PRD-001 AC-10.3: the basis, the shape, the nodes and the caveat are all recorded."""

    def test_the_basis_and_shape_are_named(self, par_swap, curve_set):
        fields = key_rate_dv01(par_swap, curve_set, TENORS).evidence.fields
        assert fields["bump_basis"] == "zero_curve_node"
        assert fields["bump_shape"] == "tent"
        assert fields["partition_of_unity"] is True
        assert fields["difference"] == "central"

    def test_the_curve_nodes_are_recorded(self, par_swap, curve_set):
        fields = key_rate_dv01(par_swap, curve_set, TENORS).evidence.fields
        assert len(fields["curve_nodes"]) == len(curve_set.discount.nodes)
        assert fields["interpolation"] == "log_linear_df"

    def test_the_interpolation_caveat_is_a_field_not_a_docstring(self, par_swap, curve_set):
        fields = key_rate_dv01(par_swap, curve_set, TENORS).evidence.fields
        assert fields["interpolation_caveat"] == INTERPOLATION_CAVEAT
        assert "node" in INTERPOLATION_CAVEAT

    def test_the_payload_repeats_the_basis(self, par_swap, curve_set):
        payload = key_rate_dv01(par_swap, curve_set, TENORS).to_dict()
        assert payload["bump_basis"] == "zero_curve_node"
        assert payload["bump_shape"] == "tent"
        assert payload["total"] == pytest.approx(
            sum(float(v) for v in payload["values"].values())
        )


class TestNormalisedRefusal:
    """PRD-001 AC-10.5: at a zero price the normalised profile does not exist."""

    def test_a_par_swap_refuses(self, par_swap, curve_set):
        with pytest.raises(UndefinedDurationError, match="key_rate_duration"):
            key_rate_duration(par_swap, curve_set, TENORS)

    def test_the_refusal_points_at_the_measure_that_works(self, par_swap, curve_set):
        with pytest.raises(UndefinedDurationError, match="key_rate_dv01"):
            key_rate_duration(par_swap, curve_set, TENORS)

    def test_an_off_market_swap_gets_a_number(self, par_swap, curve_set):
        from dataclasses import replace

        rich = replace(par_swap, fixed_rate=par_swap.fixed_rate + 0.01)
        profile = key_rate_duration(rich, curve_set, TENORS)
        assert profile.unit == "per_bp"
        assert profile.measure == "key_rate_duration"
        assert profile.total != 0.0

    def test_it_is_the_dv01_profile_over_the_price(self, par_swap, curve_set):
        from dataclasses import replace

        from rates_engine.pricing import pv

        rich = replace(par_swap, fixed_rate=par_swap.fixed_rate + 0.01)
        price = pv(rich, curve_set).value
        raw = key_rate_dv01(rich, curve_set, TENORS)
        scaled = key_rate_duration(rich, curve_set, TENORS)
        for tenor in TENORS:
            assert scaled.values[tenor] == pytest.approx(raw.values[tenor] / price, rel=1e-12)


class TestTwoBases:
    """PRD-001 AC-10.6 and PRD-001 AC-10.7: node basis and quote basis, named and reconciled."""

    def test_the_hedge_reports_a_bucketed_delta_by_instrument(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert hedge.bucketed_delta_by_instrument
        assert hedge.to_dict()["bump_basis"] == "instrument_quote"

    def test_each_basis_sums_to_its_own_parallel(self, par_swap, strip, curve_set, as_of):
        # The identity that has to hold in each basis separately. They are
        # different derivatives, so they are not compared to each other here.
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert sum(hedge.bucketed_delta_by_instrument.values()) == pytest.approx(
            hedge.quote_parallel_dv01, rel=SUM_RELATIVE_TOLERANCE
        )
        assert key_rate_dv01(par_swap, curve_set, TENORS).total == pytest.approx(
            dv01(par_swap, curve_set).value, rel=SUM_RELATIVE_TOLERANCE
        )

    def test_the_difference_between_the_bases_is_reported(self, par_swap, strip, as_of):
        payload = strip_hedge(par_swap, strip, as_of=as_of).to_dict()
        assert payload["node_vs_instrument_difference"] == pytest.approx(
            payload["swap_dv01"] - payload["quote_parallel_dv01"]
        )
        assert payload["node_vs_instrument_difference"] != 0.0

    def test_neither_basis_is_declared_the_correct_one(self, par_swap, strip, as_of):
        note = strip_hedge(par_swap, strip, as_of=as_of).to_dict()["node_vs_instrument_note"]
        assert "Neither is" in note
        assert "different questions" in note

    def test_the_two_agree_to_within_the_basis_factor(self, par_swap, strip, as_of):
        # A basis point on an ACT/360 simple forward against one on a
        # continuously compounded zero rate: roughly 365/360, less the part of
        # the swap covered by the realised stub, which is not a quote.
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        ratio = hedge.quote_parallel_dv01 / hedge.swap_dv01
        assert 1.0 < ratio < 365.0 / 360.0
