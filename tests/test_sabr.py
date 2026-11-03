"""PRD-002 AC-3.1, 3.2 and 3.3: Hagan's expansion, what it recovers, and where it fails.

Two kinds of test, deliberately separated. Recovering known parameters from a
smile the model itself generated proves the *calibrator* works and proves
nothing about the model. Fitting a smile the model did not generate measures
how good an approximation it is, and that number is reported rather than
asserted tight, because asserting it tight would be asserting that reality is
SABR.
"""

from __future__ import annotations

import math

import pytest

from rates_engine.core.errors import (
    CalibrationError,
    ExpansionBreakdownError,
    ShiftRequiredError,
    VolUnitsError,
)
from rates_engine.models import bachelier, black
from rates_engine.models.sabr import (
    SABRParameters,
    calibrate,
    density_diagnostics,
    expansion_is_valid,
    lognormal_vol,
    normal_vol,
)

FORWARD = 0.04
EXPIRY = 5.0
TRUTH = SABRParameters(alpha=0.0090, beta=0.5, rho=-0.30, nu=0.45)
STRIKES = tuple(FORWARD + d for d in (-0.02, -0.01, -0.005, 0.0, 0.005, 0.01, 0.02))


class TestClosedFormLimits:
    """The cases where the expansion has to collapse to something known."""

    def test_beta_zero_at_the_money_is_alpha_exactly(self):
        flat = SABRParameters(alpha=0.0080, beta=0.0, rho=0.0, nu=0.0)
        assert normal_vol(flat, FORWARD, FORWARD, EXPIRY) == pytest.approx(0.0080, abs=1e-18)

    def test_beta_one_at_the_money_is_alpha_exactly_in_lognormal_terms(self):
        flat = SABRParameters(alpha=0.25, beta=1.0, rho=0.0, nu=0.0)
        assert lognormal_vol(flat, FORWARD, FORWARD, EXPIRY) == pytest.approx(0.25, abs=1e-18)

    @pytest.mark.parametrize("strike", [0.02, 0.03, 0.05, 0.06])
    def test_no_vol_of_vol_leaves_the_backbone_and_its_correction(self, strike):
        # "No vol of vol" removes the z/x(z) factor, and nothing else: the
        # log-ratio terms and the O(T) correction survive. Writing the whole
        # expression out is the test — asserting only the leading backbone
        # would pass with the correction dropped entirely.
        alpha, beta, nu = 0.0090, 0.5, 0.0
        backbone = SABRParameters(alpha=alpha, beta=beta, rho=0.0, nu=nu)
        log_fk = math.log(FORWARD / strike)
        numerator = 1.0 + log_fk**2 / 24.0 + log_fk**4 / 1920.0
        denominator = (
            1.0 + (1.0 - beta) ** 2 * log_fk**2 / 24.0
            + (1.0 - beta) ** 4 * log_fk**4 / 1920.0
        )
        correction = 1.0 + (
            -beta * (2.0 - beta) * alpha**2 / (24.0 * (FORWARD * strike) ** (1.0 - beta))
        ) * EXPIRY
        expected = alpha * (FORWARD * strike) ** (beta / 2.0) * numerator / denominator * correction
        assert normal_vol(backbone, FORWARD, strike, EXPIRY) == pytest.approx(
            expected, rel=1e-14
        )

    def test_the_backbone_is_within_two_percent_of_the_leading_term(self):
        # And the correction is a correction: small, not structural.
        backbone = SABRParameters(alpha=0.0090, beta=0.5, rho=0.0, nu=0.0)
        for strike in (0.02, 0.03, 0.05, 0.06):
            leading = 0.0090 * (FORWARD * strike) ** 0.25
            assert normal_vol(backbone, FORWARD, strike, EXPIRY) == pytest.approx(
                leading, rel=0.02
            )

    def test_zero_expiry_removes_the_correction_term(self):
        assert normal_vol(TRUTH, FORWARD, FORWARD, 0.0) == pytest.approx(
            TRUTH.alpha * FORWARD**TRUTH.beta, rel=1e-15
        )

    def test_the_two_expansions_agree_at_the_money_to_leading_order(self):
        normal = normal_vol(TRUTH, FORWARD, FORWARD, EXPIRY)
        log = lognormal_vol(TRUTH, FORWARD, FORWARD, EXPIRY)
        assert normal == pytest.approx(log * FORWARD, rel=0.02)

    def test_the_ratio_is_continuous_across_the_money(self):
        # z / x(z) is 0/0 at the forward, and the implementation swaps to a
        # series below a threshold. A step at that swap would be invisible in
        # a price and fatal in a calibration, so the test is that the change
        # in volatility stays proportional to the change in strike — which is
        # continuity, rather than a tolerance picked to pass.
        base = normal_vol(TRUTH, FORWARD, FORWARD, EXPIRY)
        slope_bound = 0.2  # the smile's own slope here is about 0.06 per unit strike
        for offset in (1e-4, 1e-6, 1e-9, 1e-12, 1e-13):
            for signed in (-offset, offset):
                moved = normal_vol(TRUTH, FORWARD, FORWARD + signed, EXPIRY)
                assert abs(moved - base) <= slope_bound * abs(signed) + 1e-16

    @pytest.mark.parametrize("rho", [-0.8, -0.3, 0.0, 0.3, 0.8])
    def test_the_series_is_the_right_expansion(self, rho):
        """The series and the ratio must agree where *both* are accurate.

        Not at the threshold: there the ratio branch is the inaccurate one,
        because ``sqrt(1 - 2 rho z + z^2) + z - rho`` cancels to about one
        part in a billion at ``z = 1e-7``, which is the whole reason the
        threshold exists. Comparing them there measures that noise.

        At ``z = 1e-4`` the ratio is clean and the series is still good to
        ``O(z^2) ~ 1e-8``, so they agree to about that. A sign error in the
        series would put them ``rho * z ~ 1e-4`` apart — four orders of
        magnitude larger, and unmissable.
        """
        from rates_engine.models.sabr import _z_over_x

        z = 1e-4
        series = 1.0 - 0.5 * rho * z
        assert _z_over_x(z, rho) == pytest.approx(series, rel=1e-6)
        if rho != 0.0:
            wrong_sign = 1.0 + 0.5 * rho * z
            assert abs(_z_over_x(z, rho) - wrong_sign) > 1e-6

    @pytest.mark.parametrize("rho", [-0.8, -0.3, 0.0, 0.3, 0.8])
    def test_the_branches_join_within_the_ratio_s_own_precision(self, rho):
        from rates_engine.models.sabr import Z_SERIES_THRESHOLD, _z_over_x

        below = _z_over_x(Z_SERIES_THRESHOLD * 0.999, rho)
        above = _z_over_x(Z_SERIES_THRESHOLD * 1.001, rho)
        assert below == pytest.approx(above, rel=1e-7)


class TestCalibration:
    """PRD-002 AC-3.1 and 3.2: what the fit recovers, and how well it fits."""

    def test_it_recovers_parameters_it_generated(self):
        quotes = tuple(normal_vol(TRUTH, FORWARD, k, EXPIRY) for k in STRIKES)
        fit = calibrate(FORWARD, EXPIRY, STRIKES, quotes, beta=TRUTH.beta)
        assert fit.parameters.alpha == pytest.approx(TRUTH.alpha, rel=1e-6)
        assert fit.parameters.rho == pytest.approx(TRUTH.rho, abs=1e-6)
        assert fit.parameters.nu == pytest.approx(TRUTH.nu, rel=1e-6)
        assert fit.rmse_bp < 1e-6

    @pytest.mark.parametrize("expiry", [0.5, 2.0, 5.0, 10.0])
    def test_recovery_holds_across_expiries(self, expiry):
        quotes = tuple(normal_vol(TRUTH, FORWARD, k, expiry) for k in STRIKES)
        fit = calibrate(FORWARD, expiry, STRIKES, quotes, beta=TRUTH.beta)
        assert fit.rmse_bp < 0.5

    def test_it_fits_a_smile_it_did_not_generate_within_the_tolerance(self):
        # A quadratic smile, which SABR cannot reproduce exactly. PRD-002 AC-3.2's
        # 0.5 bp is the claim being tested; the achieved error is reported.
        quotes = tuple(
            0.0090 + 0.04 * (k - FORWARD) ** 2 - 0.05 * (k - FORWARD) for k in STRIKES
        )
        fit = calibrate(FORWARD, EXPIRY, STRIKES, quotes, beta=0.5)
        assert fit.rmse_bp < 0.5, f"achieved {fit.rmse_bp:.4f} bp"

    def test_the_fit_is_deterministic(self):
        quotes = tuple(normal_vol(TRUTH, FORWARD, k, EXPIRY) for k in STRIKES)
        first = calibrate(FORWARD, EXPIRY, STRIKES, quotes).parameters
        second = calibrate(FORWARD, EXPIRY, STRIKES, quotes).parameters
        assert first == second

    def test_beta_is_held_where_it_was_put(self):
        quotes = tuple(normal_vol(TRUTH, FORWARD, k, EXPIRY) for k in STRIKES)
        for beta in (0.0, 0.3, 0.5, 1.0):
            assert calibrate(FORWARD, EXPIRY, STRIKES, quotes, beta=beta).parameters.beta == beta

    def test_too_few_quotes_refuse_rather_than_interpolate(self):
        with pytest.raises(CalibrationError, match="at least three"):
            calibrate(FORWARD, EXPIRY, STRIKES[:2], (0.008, 0.009))

    def test_mismatched_inputs_refuse(self):
        with pytest.raises(CalibrationError, match="do not align"):
            calibrate(FORWARD, EXPIRY, STRIKES, (0.008, 0.009))

    def test_a_tolerance_that_is_missed_refuses_and_reports_the_error(self):
        quotes = tuple(0.0090 + 0.5 * (k - FORWARD) ** 2 * (1 if k > FORWARD else -8) for k in STRIKES)
        with pytest.raises(CalibrationError, match="RMSE"):
            calibrate(FORWARD, EXPIRY, STRIKES, quotes, tolerance_bp=1e-6)

    def test_the_evidence_names_the_equations_and_the_beta_decision(self):
        quotes = tuple(normal_vol(TRUTH, FORWARD, k, EXPIRY) for k in STRIKES)
        fields = calibrate(FORWARD, EXPIRY, STRIKES, quotes).evidence.fields
        assert fields["model"] == "sabr_hagan_2002"
        assert "A.59a" in fields["equation"]
        assert "unidentifiable" in fields["beta_note"]
        assert "asymptotic" in fields["approximation_note"]


class TestShiftAndRefusals:
    """PRD-002 AC-3.3: a forward at or below zero needs a shift, and says so."""

    def test_an_unshifted_negative_forward_refuses(self):
        with pytest.raises(ShiftRequiredError, match="shift"):
            normal_vol(TRUTH, -0.002, 0.01, EXPIRY)

    def test_a_shift_makes_it_work(self):
        shifted = SABRParameters(alpha=0.0090, beta=0.5, rho=-0.30, nu=0.45, shift=0.02)
        assert normal_vol(shifted, -0.002, 0.005, EXPIRY) > 0.0

    def test_calibration_refuses_a_negative_forward_without_a_shift(self):
        with pytest.raises(ShiftRequiredError, match="larger shift"):
            calibrate(-0.002, EXPIRY, STRIKES, tuple(0.008 for _ in STRIKES))

    def test_calibration_works_with_one(self):
        forward, shift = -0.002, 0.03
        truth = SABRParameters(alpha=0.0090, beta=0.5, rho=-0.2, nu=0.4, shift=shift)
        strikes = tuple(forward + d for d in (-0.01, -0.005, 0.0, 0.005, 0.01))
        quotes = tuple(normal_vol(truth, forward, k, EXPIRY) for k in strikes)
        fit = calibrate(forward, EXPIRY, strikes, quotes, beta=0.5, shift=shift)
        assert fit.rmse_bp < 0.01

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"alpha": 0.0},
            {"alpha": -0.01},
            {"beta": 1.5},
            {"rho": 1.0},
            {"rho": -1.0},
            {"nu": -0.1},
            {"shift": -0.01},
        ],
    )
    def test_out_of_range_parameters_refuse(self, kwargs):
        base = {"alpha": 0.009, "beta": 0.5, "rho": 0.0, "nu": 0.4}
        with pytest.raises(ValueError):
            SABRParameters(**{**base, **kwargs})

    @pytest.mark.parametrize("name", ["alpha", "beta", "rho", "nu", "shift"])
    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_a_parameter_that_is_not_a_finite_number_refuses(self, name, value):
        # ``alpha <= 0`` and ``nu < 0`` are both false for NaN, so a NaN alpha
        # or nu used to be accepted, the expansion returned NaN, and
        # ``expansion_is_valid`` said True because NaN is not ``<= 0``.
        base = {"alpha": 0.009, "beta": 0.5, "rho": 0.0, "nu": 0.4, "shift": 0.0}
        with pytest.raises(ValueError, match="finite"):
            SABRParameters(**{**base, name: value})

    def test_a_shifted_lognormal_vol_is_refused_unless_asked_for_by_name(self):
        # With a shift the expansion runs on F + s and K + s, so it returns the
        # sigma of Black(F + s, K + s) -- what QuantLib's shiftedSabrVolatility
        # returns -- and nothing in this package prices that: Volatility,
        # black.price and swaption_pv have no shift. Fed to plain Black it
        # priced 0.001611 where the model says 0.002588.
        shifted = SABRParameters(alpha=0.04, beta=0.5, rho=-0.2, nu=0.4, shift=0.01)
        with pytest.raises(VolUnitsError, match="shifted"):
            lognormal_vol(shifted, 0.03, 0.035, 2.0)

    def test_the_shifted_vol_prices_in_shifted_black_and_only_there(self):
        forward, strike, expiry, shift = 0.03, 0.035, 2.0, 0.01
        smile = SABRParameters(alpha=0.04, beta=0.5, rho=-0.2, nu=0.4, shift=shift)
        sigma = lognormal_vol(smile, forward, strike, expiry, shifted_black=True)
        # The model's own price, through the normal expansion, which a shift
        # does not disturb: Bachelier is unchanged by moving F and K together.
        reference = bachelier.price(
            forward, strike, expiry, normal_vol(smile, forward, strike, expiry)
        )
        shifted_black = black.price(forward + shift, strike + shift, expiry, sigma)
        plain_black = black.price(forward, strike, expiry, sigma)
        # Two expansions of one model agree closely; the unshifted use does not.
        assert shifted_black == pytest.approx(reference, rel=1e-3)
        assert abs(plain_black / reference - 1.0) > 0.3

    def test_an_unshifted_smile_needs_no_acknowledgement(self):
        assert lognormal_vol(TRUTH, FORWARD, 0.045, EXPIRY) == lognormal_vol(
            TRUTH, FORWARD, 0.045, EXPIRY, shifted_black=True
        )

    @pytest.mark.parametrize("forward,strike", [(float("nan"), 0.04), (0.04, float("nan"))])
    def test_a_forward_or_strike_that_is_not_a_number_refuses(self, forward, strike):
        # NaN passes ``f <= 0`` as false, and the expansion would hand back
        # NaN as a volatility.
        for function in (normal_vol, lognormal_vol):
            with pytest.raises(ValueError, match="finite"):
                function(TRUTH, forward, strike, EXPIRY)


def _z_over_x_60_digits(z: float, rho: float) -> float:
    """``z / x(z)`` from Hagan (2.17a) as written, evaluated in 60-digit decimals.

    The independent reference for the large-``|z|`` tests. The naive form
    cancels catastrophically in floating point when ``z`` is large and
    negative, but sixty digits leave forty to spare at ``|z| = 1e9``, so the
    formula exactly as the paper writes it is the truth here.
    """
    from decimal import Decimal, getcontext

    getcontext().prec = 60
    big_z, big_rho = Decimal(z), Decimal(rho)
    numerator = (1 - 2 * big_rho * big_z + big_z * big_z).sqrt() + big_z - big_rho
    return float(big_z / (numerator / (1 - big_rho)).ln())


def _hagan_lognormal_60_digits(parameters, forward, strike, expiry):
    """Hagan (2.17a) end to end in 60-digit decimals, for an unshifted smile."""
    from decimal import Decimal, getcontext

    getcontext().prec = 60
    f, k, alpha, beta, rho, nu, t = (
        Decimal(v)
        for v in (
            forward, strike, parameters.alpha, parameters.beta,
            parameters.rho, parameters.nu, expiry,
        )
    )
    omb = 1 - beta
    log_fk_product = (f * k).ln()
    fk_pow = (omb * log_fk_product).exp()
    correction = 1 + (
        (omb**2 / 24) * alpha * alpha / fk_pow
        + Decimal("0.25") * rho * beta * nu * alpha / fk_pow.sqrt()
        + ((2 - 3 * rho * rho) / 24) * nu * nu
    ) * t
    log_fk = (f / k).ln()
    half_pow = ((omb / 2) * log_fk_product).exp()
    denominator = half_pow * (1 + (omb**2 / 24) * log_fk**2 + (omb**4 / 1920) * log_fk**4)
    z = (nu / alpha) * half_pow * log_fk
    x = (((1 - 2 * rho * z + z * z).sqrt() + z - rho) / (1 - rho)).ln()
    return float(alpha / denominator * (z / x) * correction)


class TestLargeZ:
    """``x(z)`` without cancellation: far wings and tiny alpha make ``|z|`` huge.

    ``sqrt(1 - 2 rho z + z^2) + z - rho`` subtracts two numbers of size
    ``|z|`` when ``z`` is large and negative. At ``|z| = 1e7`` the result
    already carried a relative error of 4e-4; past about ``1e8`` it came out
    zero or negative, and ``math.log`` raised a bare ``ValueError: math domain
    error`` that neither ``expansion_is_valid`` nor the calibrator caught.
    QuantLib's ``sabrVolatility`` has the same cancellation and returns 0.0
    at the point below; the 60-digit evaluation says the formula is positive.
    """

    @pytest.mark.parametrize(
        "rho,z",
        [
            (0.5, -1e7), (0.5, -1e8), (0.0, -1e9), (-0.9, -1e8),
            (0.999, -1e7), (-0.999, -500.0), (0.9, -14.8), (0.5, -142772178.84092295),
        ],
    )
    def test_z_over_x_matches_sixty_digits_in_the_far_wing(self, rho, z):
        from rates_engine.models.sabr import _z_over_x

        assert _z_over_x(z, rho) == pytest.approx(_z_over_x_60_digits(z, rho), rel=1e-13, abs=0.0)

    @pytest.mark.parametrize("rho,z", [(0.0, 0.5), (0.4, -0.2), (-0.3, 3.0), (0.9, 1e-3)])
    def test_away_from_the_cancellation_nothing_moves(self, rho, z):
        # The stable branch is taken only where the naive sum has lost more
        # than two digits, so ordinary points keep the arithmetic QuantLib's
        # sabrVolatility uses -- the 3.7e-14 agreement measured against it.
        from rates_engine.models.sabr import _z_over_x

        naive = math.sqrt(1.0 - 2.0 * rho * z + z * z) + z - rho
        assert _z_over_x(z, rho) == z / math.log(naive / (1.0 - rho))

    def test_a_tiny_alpha_far_from_the_money_returns_a_volatility(self):
        # alpha = 1e-9 and K = 2F give z of about -1.4e8.
        tiny = SABRParameters(alpha=1e-9, beta=0.5, rho=0.5, nu=1.0)
        assert expansion_is_valid(tiny, 0.03, 0.06, 1.0)
        assert lognormal_vol(tiny, 0.03, 0.06, 1.0) == pytest.approx(
            _hagan_lognormal_60_digits(tiny, 0.03, 0.06, 1.0), rel=1e-12, abs=0.0
        )
        assert normal_vol(tiny, 0.03, 0.06, 1.0) > 0.0

    def test_the_lognormal_expansion_is_exact_where_quantlib_loses_digits(self):
        # The worst point of the 7,200-case comparison against QuantLib 1.44:
        # there QuantLib and the old code are both 7.0e-14 from the 60-digit
        # value, because the naive sum loses three digits; this is not.
        smile = SABRParameters(alpha=0.008, beta=0.0, rho=0.9, nu=1.2)
        forward, strike, expiry = 0.06, 0.1630969097075427, 5.0
        assert lognormal_vol(smile, forward, strike, expiry) == pytest.approx(
            _hagan_lognormal_60_digits(smile, forward, strike, expiry), rel=1e-15, abs=0.0
        )

    def test_the_calibrator_survives_a_tiny_alpha(self):
        # The lower bound on alpha in the fit is 1e-10; a residual there used
        # to raise math domain error straight out of least_squares.
        tiny = SABRParameters(alpha=1e-10, beta=0.5, rho=0.5, nu=1.0)
        for strike in (0.01, 0.06):
            assert expansion_is_valid(tiny, 0.03, strike, 1.0)


class TestValidityRegion:
    """The expansion's own boundary, measured rather than assumed."""

    def test_a_long_expiry_with_high_vol_of_vol_breaks_down(self):
        wild = SABRParameters(alpha=0.0090, beta=0.5, rho=-0.75, nu=0.90)
        with pytest.raises(ExpansionBreakdownError, match="validity region"):
            normal_vol(wild, FORWARD, 1e-6, 30.0)

    def test_the_breakdown_is_reported_not_propagated_by_the_diagnostic(self):
        wild = SABRParameters(alpha=0.0090, beta=0.5, rho=-0.75, nu=0.90)
        report = density_diagnostics(wild, FORWARD, 30.0)
        assert report["expansion_valid"] is False
        assert report["arbitrage_free"] is False
        assert report["breakdown_strike"] is not None

    def test_a_sensible_smile_is_arbitrage_free(self):
        report = density_diagnostics(TRUTH, FORWARD, EXPIRY)
        assert report["expansion_valid"] is True
        assert report["arbitrage_free"] is True
        assert report["method"] == "breeden_litzenberger_second_difference"

    def test_the_known_defect_shows_up_where_the_literature_says(self):
        # Hagan's expansion admits butterfly arbitrage in the low-strike wing
        # at long expiries. Detecting it is the diagnostic working.
        wild = SABRParameters(alpha=0.0090, beta=0.5, rho=-0.75, nu=0.90)
        verdicts = {T: density_diagnostics(wild, FORWARD, T) for T in (1.0, 5.0, 15.0)}
        assert verdicts[1.0]["arbitrage_free"] is True
        assert verdicts[15.0]["arbitrage_free"] is False

    def test_the_pointwise_guard_agrees_with_the_scan(self):
        wild = SABRParameters(alpha=0.0090, beta=0.5, rho=-0.75, nu=0.90)
        assert expansion_is_valid(TRUTH, FORWARD, 0.03, EXPIRY)
        assert not expansion_is_valid(wild, FORWARD, 1e-6, 30.0)

    def test_the_calibrator_survives_the_region_instead_of_dying_in_it(self):
        # Parameters near the boundary must not make the fit throw: the
        # optimiser has to be able to walk out again.
        quotes = tuple(normal_vol(TRUTH, FORWARD, k, 20.0) for k in STRIKES)
        fit = calibrate(FORWARD, 20.0, STRIKES, quotes, beta=0.5)
        assert fit.rmse_bp < 1.0


class TestPricesAreSane:
    """Whatever the smile says, the prices it implies must behave."""

    @pytest.mark.parametrize("strike", [0.02, 0.03, 0.04, 0.05, 0.06])
    def test_call_prices_fall_as_the_strike_rises(self, strike):
        prices = [
            bachelier.price(
                FORWARD, k, EXPIRY, normal_vol(TRUTH, FORWARD, k, EXPIRY), annuity=4.2
            )
            for k in (strike, strike + 0.0025)
        ]
        assert prices[0] > prices[1]

    def test_prices_stay_above_intrinsic(self):
        for strike in (0.02, 0.03, 0.04, 0.05, 0.06):
            price = bachelier.price(
                FORWARD, strike, EXPIRY, normal_vol(TRUTH, FORWARD, strike, EXPIRY), annuity=4.2
            )
            assert price >= 4.2 * max(FORWARD - strike, 0.0) - 1e-12

    def test_rho_controls_the_skew(self):
        # Not "the low wing is higher than the high wing": with beta below
        # one the backbone already slopes down, so that holds at almost any
        # rho and tests the backbone rather than the correlation. What
        # isolates rho is that raising it must lift the high wing relative to
        # the low one, monotonically.
        def skew(rho: float) -> float:
            params = SABRParameters(alpha=0.0090, beta=0.5, rho=rho, nu=0.45)
            return lognormal_vol(params, FORWARD, FORWARD + 0.015, EXPIRY) - lognormal_vol(
                params, FORWARD, FORWARD - 0.015, EXPIRY
            )

        skews = [skew(rho) for rho in (-0.6, -0.3, 0.0, 0.3, 0.6)]
        assert skews == sorted(skews)
        assert skews[0] < 0.0 < skews[-1]

    def test_the_backbone_slopes_the_way_beta_says(self):
        # And separately: with beta below one the lognormal backbone falls
        # with strike, which is the effect the skew test had to be purged of.
        flat = SABRParameters(alpha=0.0090, beta=0.5, rho=0.0, nu=0.0)
        assert lognormal_vol(flat, FORWARD, FORWARD - 0.015, EXPIRY) > lognormal_vol(
            flat, FORWARD, FORWARD + 0.015, EXPIRY
        )

    def test_more_vol_of_vol_makes_a_deeper_smile(self):
        def curvature(nu: float) -> float:
            params = SABRParameters(alpha=0.0090, beta=0.5, rho=0.0, nu=nu)
            wing = normal_vol(params, FORWARD, FORWARD + 0.015, EXPIRY)
            atm = normal_vol(params, FORWARD, FORWARD, EXPIRY)
            return wing - atm

        assert curvature(0.8) > curvature(0.4) > curvature(0.1)

    def test_the_backbone_scales_the_way_beta_says(self):
        # beta = 0 is a normal backbone: the at-the-money normal vol does not
        # move with the forward. beta = 1 is lognormal: it moves with it.
        normal_model = SABRParameters(alpha=0.0080, beta=0.0, rho=0.0, nu=0.0)
        lognormal_model = SABRParameters(alpha=0.20, beta=1.0, rho=0.0, nu=0.0)
        assert normal_vol(normal_model, 0.02, 0.02, EXPIRY) == pytest.approx(
            normal_vol(normal_model, 0.06, 0.06, EXPIRY), rel=1e-12
        )
        assert normal_vol(lognormal_model, 0.06, 0.06, EXPIRY) == pytest.approx(
            3.0 * normal_vol(lognormal_model, 0.02, 0.02, EXPIRY), rel=1e-3
        )


def test_hagan_matches_a_monte_carlo_of_the_model_it_approximates():
    """The only test here that checks the expansion against the *model*.

    Everything else checks the expansion against itself or against limits.
    This simulates the SABR SDE directly and compares the price, which is the
    one way to see whether the asymptotics are any good. The tolerance is
    loose because the comparison is Monte Carlo against an approximation, and
    both sides have error; a tight tolerance here would be a fiction.
    """
    import numpy as np

    params = SABRParameters(alpha=0.0090, beta=0.0, rho=0.0, nu=0.30)
    forward, expiry, strike = 0.04, 1.0, 0.04
    steps, paths = 400, 200_000
    dt = expiry / steps
    generator = np.random.default_rng(20260918)

    rate = np.full(paths, forward)
    vol = np.full(paths, params.alpha)
    root = math.sqrt(dt)
    for _ in range(steps):
        z1 = generator.standard_normal(paths)
        z2 = generator.standard_normal(paths)
        rate = rate + vol * root * z1
        vol = vol * np.exp(params.nu * root * z2 - 0.5 * params.nu**2 * dt)
    simulated = float(np.mean(np.maximum(rate - strike, 0.0)))

    expansion = bachelier.price(
        forward, strike, expiry, normal_vol(params, forward, strike, expiry)
    )
    standard_error = float(np.std(np.maximum(rate - strike, 0.0)) / math.sqrt(paths))
    assert abs(expansion - simulated) < max(4.0 * standard_error, 0.01 * simulated)
