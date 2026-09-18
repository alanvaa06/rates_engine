"""AC-5.3 to AC-5.7: the two models, their limit, and where sigma is allowed to come from."""

from __future__ import annotations

import csv

import pytest

from rates_engine.convexity import (
    MIN_REALIZED_OBSERVATIONS,
    ConvexityModel,
    convexity_adjustment,
    realized_sofr_sigma,
)
from rates_engine.errors import InsufficientDataError, UnsupportedConventionError
from rates_engine.evidence import DataQuality
from rates_engine.market import Series
from rates_engine.market.snapshot import MarketSnapshot


class TestHoLee:
    """AC-5.3: half sigma squared T1 T2, against the published table."""

    def test_matches_the_published_table(self, fixtures_dir):
        with (fixtures_dir / "hull_convexity_table.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 2
        for row in rows:
            result = convexity_adjustment(
                float(row["time_to_start"]),
                float(row["time_to_end"]),
                model=ConvexityModel.HO_LEE,
                sigma=float(row["sigma"]),
            )
            assert result.adjustment_bp == pytest.approx(float(row["adjustment_bp"]), abs=0.01)

    def test_the_closed_form_is_what_it_says(self):
        result = convexity_adjustment(2.0, 2.25, model=ConvexityModel.HO_LEE, sigma=0.01)
        assert result.adjustment_bp == pytest.approx(0.5 * 0.01**2 * 2.0 * 2.25 * 1e4, rel=1e-15)

    def test_zero_sigma_is_zero_adjustment(self):
        assert (
            convexity_adjustment(2.0, 2.25, model=ConvexityModel.HO_LEE, sigma=0.0).adjustment_bp
            == 0.0
        )

    def test_model_none_is_zero_whatever_sigma_is(self):
        assert (
            convexity_adjustment(5.0, 5.25, model=ConvexityModel.NONE, sigma=0.02).adjustment_bp
            == 0.0
        )

    def test_the_adjustment_is_a_decimal_too(self):
        result = convexity_adjustment(2.0, 2.25, model=ConvexityModel.HO_LEE, sigma=0.01)
        assert result.adjustment == pytest.approx(result.adjustment_bp * 1e-4)


class TestHullWhite:
    """AC-5.4: mean reversion, and the Ho-Lee limit it has to reach."""

    @pytest.mark.parametrize("kappa", [1e-6, 1e-7, 1e-8])
    def test_converges_to_ho_lee_as_kappa_vanishes(self, kappa):
        hull_white = convexity_adjustment(
            2.0, 2.25, model=ConvexityModel.HULL_WHITE, sigma=0.01, kappa=kappa
        )
        ho_lee = convexity_adjustment(2.0, 2.25, model=ConvexityModel.HO_LEE, sigma=0.01)
        assert hull_white.adjustment_bp == pytest.approx(ho_lee.adjustment_bp, abs=0.01)

    def test_mean_reversion_lowers_the_adjustment(self):
        adjustments = [
            convexity_adjustment(
                2.0, 2.25, model=ConvexityModel.HULL_WHITE, sigma=0.01, kappa=k
            ).adjustment_bp
            for k in (0.001, 0.01, 0.05, 0.2)
        ]
        assert adjustments == sorted(adjustments, reverse=True)

    def test_hull_white_without_kappa_refuses(self):
        with pytest.raises(UnsupportedConventionError, match="kappa"):
            convexity_adjustment(2.0, 2.25, model=ConvexityModel.HULL_WHITE, sigma=0.01)

    def test_the_limit_branch_is_recorded_in_the_evidence(self):
        result = convexity_adjustment(
            2.0, 2.25, model=ConvexityModel.HULL_WHITE, sigma=0.01, kappa=1e-12
        )
        assert result.evidence.fields["hull_white_evaluated_as_ho_lee_limit"] is True

    def test_a_real_kappa_does_not_take_the_limit_branch(self):
        result = convexity_adjustment(
            2.0, 2.25, model=ConvexityModel.HULL_WHITE, sigma=0.01, kappa=0.03
        )
        assert result.evidence.fields["hull_white_evaluated_as_ho_lee_limit"] is False


class TestShape:
    """AC-5.6: small at the front, growing with maturity."""

    def test_under_a_basis_point_inside_a_year(self):
        assert (
            convexity_adjustment(1.0, 1.25, model=ConvexityModel.HO_LEE, sigma=0.01).adjustment_bp
            < 1.0
        )

    def test_monotone_in_maturity(self):
        adjustments = [
            convexity_adjustment(
                t, t + 0.25, model=ConvexityModel.HO_LEE, sigma=0.01
            ).adjustment_bp
            for t in (0.25, 0.5, 1.0, 2.0, 3.0, 5.0)
        ]
        assert adjustments == sorted(adjustments)

    def test_material_only_past_two_years(self):
        # The qualitative claim from Skov-Skovmand: negligible at one year,
        # larger at five than at two.
        one = convexity_adjustment(1.0, 1.25, model=ConvexityModel.HO_LEE, sigma=0.01)
        two = convexity_adjustment(2.0, 2.25, model=ConvexityModel.HO_LEE, sigma=0.01)
        five = convexity_adjustment(5.0, 5.25, model=ConvexityModel.HO_LEE, sigma=0.01)
        assert one.adjustment_bp < 1.0 < two.adjustment_bp < five.adjustment_bp


class TestSigmaSource:
    """AC-5.5: realised sigma, its window, and the refusal when there is too little."""

    def test_realised_sigma_records_its_window(self, snapshot):
        estimate = realized_sofr_sigma(snapshot, window=252)
        assert estimate.observations == 252
        assert estimate.start < estimate.end <= snapshot.as_of
        assert estimate.sigma > 0.0
        fields = estimate.evidence.fields
        assert fields["window"] == 252
        assert fields["basis"] == "normal_absolute_rate"
        assert fields["annualisation"] == 252

    def test_too_short_a_window_refuses(self, snapshot):
        with pytest.raises(InsufficientDataError, match=str(MIN_REALIZED_OBSERVATIONS)):
            realized_sofr_sigma(snapshot, window=30)

    def test_a_thin_series_refuses_even_with_a_long_window(self, snapshot):
        series = snapshot.require("SOFR")
        thin = Series("SOFR", series.dates[:20], series.values[:20], series.provenance)
        with pytest.raises(InsufficientDataError):
            realized_sofr_sigma(MarketSnapshot(snapshot.as_of, {"SOFR": thin}), window=252)

    def test_explicit_sigma_is_marked_assumed(self):
        result = convexity_adjustment(1.0, 1.25, model=ConvexityModel.HO_LEE, sigma=0.01)
        assert result.evidence.inputs[0].data_quality is DataQuality.ASSUMED
        assert result.evidence.fields["sigma_source"] == "explicit"

    def test_realised_sigma_is_marked_observed_and_chains(self, snapshot):
        estimate = realized_sofr_sigma(snapshot, window=252)
        result = convexity_adjustment(
            1.0,
            1.25,
            model=ConvexityModel.HO_LEE,
            sigma=estimate.sigma,
            sigma_source="realized_sofr",
            sigma_evidence=estimate.evidence,
        )
        assert result.evidence.inputs[0].data_quality is DataQuality.OBSERVED
        assert result.evidence.sources[0].produced_by == "convexity.realized_sofr_sigma"

    def test_negative_sigma_refuses(self):
        with pytest.raises(ValueError, match="sigma"):
            convexity_adjustment(1.0, 1.25, model=ConvexityModel.HO_LEE, sigma=-0.01)

    def test_unknown_model_refuses(self):
        with pytest.raises(UnsupportedConventionError, match="sabr"):
            convexity_adjustment(1.0, 1.25, model="sabr", sigma=0.01)


class TestDiscountingDeclared:
    """AC-5.7: the evidence says the discounting is at the collateral rate."""

    def test_collateral_discounting_is_declared(self):
        fields = convexity_adjustment(
            1.0, 1.25, model=ConvexityModel.HO_LEE, sigma=0.01
        ).evidence.fields
        assert fields["discounting"] == "collateral_rate_ois_sofr"
        assert "Piterbarg" in fields["discounting_note"]

    def test_the_atm_calibration_caveat_travels_with_the_result(self):
        fields = convexity_adjustment(
            1.0, 1.25, model=ConvexityModel.HO_LEE, sigma=0.01
        ).evidence.fields
        assert "10-25%" in fields["atm_calibration_caveat"]
