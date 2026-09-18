"""PRD-001 AC-3.3: bit-exact inside one interpreter, 1e-14 relative across the matrix.

Bit-exactness across Python or numpy versions is not promised, because it is
not deliverable: BLAS and libm differ between builds, and a suite that demands
it spends its life going red for reasons nobody can act on. What is promised
is that nothing here is random, and the two halves of that promise are tested
separately.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from rates_engine.curves import bootstrap_discount_curve
from rates_engine.hedging import shock_table, strip_hedge
from rates_engine.pricing import dv01, par_rate, pv
from rates_engine.risk import key_rate_dv01

CROSS_VERSION_RTOL = 1e-14


class TestIdempotence:
    """The same call twice in one interpreter gives the identical object."""

    def test_bootstrap_is_bit_exact(self, as_of, strip):
        first = bootstrap_discount_curve(as_of, strip)
        second = bootstrap_discount_curve(as_of, strip)
        assert first.curve.dfs == second.curve.dfs
        assert first.residuals_bp == second.residuals_bp

    def test_pricing_is_bit_exact(self, par_swap, curve_set):
        assert pv(par_swap, curve_set).value == pv(par_swap, curve_set).value
        assert par_rate(par_swap, curve_set).value == par_rate(par_swap, curve_set).value
        assert dv01(par_swap, curve_set).value == dv01(par_swap, curve_set).value

    def test_key_rates_are_bit_exact(self, par_swap, curve_set):
        tenors = (0.5, 1.0, 1.5, 2.0)
        assert (
            key_rate_dv01(par_swap, curve_set, tenors).values
            == key_rate_dv01(par_swap, curve_set, tenors).values
        )

    def test_hedging_is_bit_exact(self, par_swap, strip, as_of):
        first = strip_hedge(par_swap, strip, as_of=as_of)
        second = strip_hedge(par_swap, strip, as_of=as_of)
        assert first.contracts == second.contracts
        assert shock_table(first).table.equals(shock_table(second).table)


class TestNoRandomness:
    """Nothing in ``src/`` reaches for a random number generator."""

    def test_no_rng_in_the_package(self):
        import rates_engine

        root = Path(rates_engine.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in ("random.", "default_rng", "np.random", "RandomState", "shuffle("):
                if needle in text:
                    offenders.append(f"{path.relative_to(root)}: {needle}")
        assert offenders == [], offenders

    def test_no_set_iteration_feeds_a_numeric_path(self):
        # Sets iterate in hash order, which is stable within a run and not
        # something to build a curve on. The curve and risk modules build
        # their sequences from tuples and sorted lists only.
        import rates_engine

        root = Path(rates_engine.__file__).parent
        for name in ("curves/bootstrap.py", "curves/discount.py", "risk.py"):
            text = (root / name).read_text(encoding="utf-8")
            assert "for " not in text or "in set(" not in text, name


class TestCrossVersionContract:
    """The tolerance a second interpreter would be held to, exercised here."""

    def test_a_fresh_interpreter_reproduces_the_curve(self, as_of, strip):
        reference = bootstrap_discount_curve(as_of, strip).curve.dfs
        program = (
            "import sys, json\n"
            "sys.path.insert(0, 'tests')\n"
            "from datetime import date, timedelta\n"
            "from rates_engine.conventions import imm_date, next_imm_on_or_after\n"
            "from rates_engine.curves import FuturesNode, RealizedStubNode, "
            "bootstrap_discount_curve\n"
            "as_of = date(2026, 1, 15)\n"
            "start = imm_date(2026, 3)\n"
            "items = [RealizedStubNode(end=start, accrual_factor=1.0 + 0.04 * "
            "((start - as_of).days / 360.0))]\n"
            "period = start\n"
            "for index in range(9):\n"
            "    nxt = next_imm_on_or_after(period + timedelta(days=1))\n"
            "    items.append(FuturesNode(start=period, end=nxt, forward_rate=0.04, "
            "label=f'SR3-{index + 1}', convexity={'model': 'ho_lee', 'sigma': 0.01}))\n"
            "    period = nxt\n"
            "print(json.dumps(bootstrap_discount_curve(as_of, tuple(items)).curve.dfs))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, cwd=Path.cwd()
        )
        assert done.returncode == 0, done.stderr
        import json

        other = json.loads(done.stdout)
        assert len(other) == len(reference)
        for mine, theirs in zip(reference, other, strict=True):
            assert theirs == pytest.approx(mine, rel=CROSS_VERSION_RTOL, abs=0.0)


class TestCalibrationDeterminism:
    """The v2 additions run optimisers, which is where randomness would hide.

    Three of them: a bounded scalar search for Nelson-Siegel's decay, and
    least squares for SABR and for the FOMC path. None seeds anything, none
    takes a random start, and none depends on iteration order — but that is
    an argument, and these are the tests.
    """

    def test_sabr_calibration_is_bit_exact(self):
        from rates_engine.volatility.sabr import calibrate

        strikes = tuple(0.04 + d for d in (-0.01, -0.005, 0.0, 0.005, 0.01))
        vols = (0.0098, 0.0094, 0.0092, 0.0093, 0.0096)
        first = calibrate(0.04, 5.0, strikes, vols)
        second = calibrate(0.04, 5.0, strikes, vols)
        assert first.parameters == second.parameters
        assert first.fitted_vols == second.fitted_vols
        assert first.rmse_bp == second.rmse_bp

    def test_nelson_siegel_is_bit_exact(self):
        from rates_engine.curves.parametric import fit_nelson_siegel

        times = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0)
        rates = (0.0445, 0.0432, 0.0417, 0.0413, 0.0413, 0.0422, 0.0423)
        first = fit_nelson_siegel(times, rates)
        second = fit_nelson_siegel(times, rates)
        assert first.model == second.model
        assert first.fitted == second.fitted

    def test_the_fomc_path_is_bit_exact(self):
        from rates_engine.curves.parametric import fit_fomc_step_curve

        meetings = (date(2026, 11, 5), date(2026, 12, 17))
        contracts = tuple(
            (label, begin, end, "SR1", price)
            for label, begin, end, price in (
                ("oct", date(2026, 10, 1), date(2026, 11, 1), 95.70),
                ("nov", date(2026, 11, 1), date(2026, 12, 1), 95.92),
                ("dec", date(2026, 12, 1), date(2027, 1, 1), 96.07),
                ("jan", date(2027, 1, 1), date(2027, 2, 1), 96.10),
            )
        )
        # strict=False on purpose: these prices do not lie on any three-segment
        # path, and what is under test is that the solver lands in the same
        # place twice, not that it lands somewhere good.
        first = fit_fomc_step_curve(date(2026, 9, 16), meetings, contracts, strict=False)
        second = fit_fomc_step_curve(date(2026, 9, 16), meetings, contracts, strict=False)
        assert first.curve.segment_rates == second.curve.segment_rates
        assert first.residuals_bp == second.residuals_bp

    def test_option_pricing_is_bit_exact(self, atm_swaption, option_curve_set):
        from rates_engine.optionpricing import swaption_pv
        from rates_engine.risk import option_greeks
        from rates_engine.volatility.units import Volatility, VolUnits

        vol = Volatility(90.0, VolUnits.NORMAL_BP)
        assert (
            swaption_pv(atm_swaption, option_curve_set, vol).value
            == swaption_pv(atm_swaption, option_curve_set, vol).value
        )
        first = option_greeks(atm_swaption, option_curve_set, vol)
        second = option_greeks(atm_swaption, option_curve_set, vol)
        assert (first.delta, first.gamma, first.vega, first.theta) == (
            second.delta,
            second.gamma,
            second.vega,
            second.theta,
        )

    def test_the_monotone_convex_curve_is_bit_exact(self, as_of, strip):
        first = bootstrap_discount_curve(as_of, strip, interpolation="monotone_convex")
        second = bootstrap_discount_curve(as_of, strip, interpolation="monotone_convex")
        assert first.curve.dfs == second.curve.dfs
        probe = as_of + timedelta(days=200)
        assert first.curve.instantaneous_forward(probe) == second.curve.instantaneous_forward(
            probe
        )

    def test_a_fresh_interpreter_reproduces_the_sabr_fit(self):
        """A calibration carries a weaker cross-version promise than a curve.

        The parameters come out of an iterative solver, so a different scipy
        build can stop a few ulps elsewhere. What is held to
        ``CROSS_VERSION_RTOL`` here is a fresh process on *this* build, which
        is what catches module-level state, hash-order dependence and an
        uninitialised start. Across the CI matrix the guarantee the package
        makes is on the fit's *output* — the volatility it produces — not on
        the coordinates it found.
        """
        from rates_engine.volatility.sabr import calibrate

        strikes = tuple(0.04 + d for d in (-0.01, -0.005, 0.0, 0.005, 0.01))
        vols = (0.0098, 0.0094, 0.0092, 0.0093, 0.0096)
        reference = calibrate(0.04, 5.0, strikes, vols)
        program = (
            "import json\n"
            "from rates_engine.volatility.sabr import calibrate\n"
            f"strikes = {strikes!r}\n"
            f"vols = {vols!r}\n"
            "fit = calibrate(0.04, 5.0, strikes, vols)\n"
            "print(json.dumps([fit.parameters.alpha, fit.parameters.rho, "
            "fit.parameters.nu, fit.rmse_bp, list(fit.fitted_vols)]))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, cwd=Path.cwd()
        )
        assert done.returncode == 0, done.stderr
        import json

        alpha, rho, nu, rmse, fitted = json.loads(done.stdout)
        assert alpha == pytest.approx(reference.parameters.alpha, rel=CROSS_VERSION_RTOL)
        assert rho == pytest.approx(reference.parameters.rho, rel=CROSS_VERSION_RTOL)
        assert nu == pytest.approx(reference.parameters.nu, rel=CROSS_VERSION_RTOL)
        assert rmse == pytest.approx(reference.rmse_bp, rel=CROSS_VERSION_RTOL)
        for mine, theirs in zip(reference.fitted_vols, fitted, strict=True):
            assert theirs == pytest.approx(mine, rel=CROSS_VERSION_RTOL, abs=0.0)
