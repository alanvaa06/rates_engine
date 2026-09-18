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
