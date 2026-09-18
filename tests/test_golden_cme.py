"""PRD-001 AC-6.6, PRD-001 AC-8.1 and PRD-001 AC-8.2: the numbers published by someone else.

These are the only tests in the suite whose value comes from outside this
package: 779 contracts, a 3.3304% IMM par coupon, +22,292 USD at minus a
hundred basis points and a DV01 moving from 19,480 to 19,921. Everything else
here proves the engine is self-consistent, which is necessary and is not the
same thing.

They are **skipped**, because the session that wrote this package could not
reach the CME whitepaper: egress was limited to package registries and
GitHub. That is the escalation
``docs/forge/plan/001-v1-plan-design.md`` task 0.4 exists to raise, and it is
being raised here rather than papered over. Decision A1 was explicit that if
the reconstruction cannot be done, the test becomes a documented skip and the
tolerance does not move. Supplying
``tests/fixtures/cme_published/whitepaper_2025_strip.csv`` turns them on with
no code change.

What runs unconditionally is the part that does not need the whitepaper: that
the machinery produces an answer of the right shape and magnitude, and that
PRD-001 AC-3.8 holds — no golden is validated against a curve carrying a proxy.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date, timedelta

import pytest
from conftest import require_published

from rates_engine.conventions import next_imm_on_or_after, year_fraction
from rates_engine.convexity import ConvexityModel, convexity_adjustment
from rates_engine.curves import (
    CurveSet,
    FuturesNode,
    RealizedStubNode,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import CURVE_TIME_BASIS
from rates_engine.evidence import DataQuality
from rates_engine.hedging import shock_table, strip_hedge
from rates_engine.instruments import OISSwap, Side
from rates_engine.pricing import dv01, par_rate

WHITEPAPER_CONTRACTS = 779
WHITEPAPER_CONTRACT_TOLERANCE = 2
WHITEPAPER_IMM_PAR_RATE = 0.033304
WHITEPAPER_PAR_TOLERANCE_BP = 0.5
WHITEPAPER_NET_PNL = 22_292.0
WHITEPAPER_DV01_BASE = 19_480.0
WHITEPAPER_DV01_SHOCKED = 19_921.0
WHITEPAPER_RELATIVE_TOLERANCE = 0.03


def assert_no_proxy(result) -> None:
    """PRD-001 AC-3.8: a published number is never validated against a proxied curve."""
    assert DataQuality.PROXY not in result.node_quality, (
        "this golden rests on a Treasury proxy; PRD-001 AC-3.8 forbids it, because the "
        "swap spread would be inside the number being compared"
    )
    assert result.long_end_source is None
    assert result.evidence.worst_quality is not DataQuality.PROXY


def _load_whitepaper_strip(ac: str):
    """Build the calibration set from the whitepaper's published SR3 prices.

    Args:
        ac: The acceptance criterion the caller serves, so the skip message
            names it.

    Returns:
        The valuation date and the calibration instruments.
    """
    path = require_published("whitepaper_2025_strip.csv", ac=ac)[0]
    import json

    sidecar = path.with_suffix(path.suffix + ".provenance.json")
    assumptions = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    as_of = date.fromisoformat(assumptions["as_of"])
    sigma = float(assumptions.get("sigma", 0.0))
    model = assumptions.get("convexity_model", ConvexityModel.HO_LEE)

    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, "the whitepaper strip fixture is empty"

    first_start = date.fromisoformat(rows[0]["start"])
    instruments: list[object] = [
        RealizedStubNode(
            end=first_start,
            accrual_factor=float(assumptions["stub_accrual_factor"]),
        )
    ]
    for row in rows:
        start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
        adjustment = convexity_adjustment(
            year_fraction(as_of, start, CURVE_TIME_BASIS),
            year_fraction(as_of, end, CURVE_TIME_BASIS),
            model=model,
            sigma=sigma,
        )
        instruments.append(
            FuturesNode(
                start=start,
                end=end,
                forward_rate=(100.0 - float(row["price"])) / 100.0 - adjustment.adjustment,
                label=row["label"],
                convexity={"model": model, "sigma": sigma},
            )
        )
    return as_of, tuple(instruments)


class TestWhitepaperGoldens:
    """Third-party numbers. Skipped until the strip is supplied; tolerances fixed."""

    def test_two_year_imm_par_coupon(self):
        """PRD-001 AC-6.6: 3.3304% within half a basis point."""
        as_of, instruments = _load_whitepaper_strip(ac="6.6")
        result = bootstrap_discount_curve(as_of, instruments)
        assert_no_proxy(result)
        futures = [i for i in instruments if isinstance(i, FuturesNode)]
        swap = OISSwap(
            effective=futures[0].start,
            maturity=futures[-1].end,
            fixed_rate=WHITEPAPER_IMM_PAR_RATE,
            notional=100_000_000.0,
            side=Side.PAYER,
        )
        computed = par_rate(swap, CurveSet(result.curve)).value
        assert computed * 1e4 == pytest.approx(
            WHITEPAPER_IMM_PAR_RATE * 1e4, abs=WHITEPAPER_PAR_TOLERANCE_BP
        )

    def test_contract_count(self):
        """PRD-001 AC-8.1: 779 contracts, plus or minus two, summing over the periods."""
        as_of, instruments = _load_whitepaper_strip(ac="8.1")
        result = bootstrap_discount_curve(as_of, instruments)
        assert_no_proxy(result)
        futures = [i for i in instruments if isinstance(i, FuturesNode)]
        provisional = OISSwap(
            effective=futures[0].start,
            maturity=futures[-1].end,
            fixed_rate=WHITEPAPER_IMM_PAR_RATE,
            notional=100_000_000.0,
            side=Side.PAYER,
        )
        swap = replace(
            provisional, fixed_rate=par_rate(provisional, CurveSet(result.curve)).value
        )
        hedge = strip_hedge(swap, instruments, as_of=as_of)
        assert hedge.total_contracts == pytest.approx(
            WHITEPAPER_CONTRACTS, abs=WHITEPAPER_CONTRACT_TOLERANCE
        )
        assert sum(hedge.contracts.values()) == pytest.approx(hedge.total_contracts)

    def test_minus_one_hundred_basis_points(self):
        """PRD-001 AC-8.2: +22,292 USD net, and the DV01 moving 19,480 -> 19,921."""
        as_of, instruments = _load_whitepaper_strip(ac="8.2")
        result = bootstrap_discount_curve(as_of, instruments)
        assert_no_proxy(result)
        futures = [i for i in instruments if isinstance(i, FuturesNode)]
        curve_set = CurveSet(result.curve)
        provisional = OISSwap(
            effective=futures[0].start,
            maturity=futures[-1].end,
            fixed_rate=WHITEPAPER_IMM_PAR_RATE,
            notional=100_000_000.0,
            side=Side.PAYER,
        )
        swap = replace(provisional, fixed_rate=par_rate(provisional, curve_set).value)
        assert abs(dv01(swap, curve_set).value) == pytest.approx(
            WHITEPAPER_DV01_BASE, rel=WHITEPAPER_RELATIVE_TOLERANCE
        )
        table = shock_table(strip_hedge(swap, instruments, as_of=as_of), (-100.0,)).table
        row = table.iloc[0]
        assert row.net_pnl == pytest.approx(
            WHITEPAPER_NET_PNL, rel=WHITEPAPER_RELATIVE_TOLERANCE
        )
        assert abs(row.swap_dv01_post_shock) == pytest.approx(
            WHITEPAPER_DV01_SHOCKED, rel=WHITEPAPER_RELATIVE_TOLERANCE
        )


class TestShapeWithoutTheWhitepaper:
    """What can be checked without the published strip: the answer's shape."""

    def test_the_goldens_run_on_a_proxy_free_curve(self, flat_curve):
        assert_no_proxy(flat_curve)

    def test_a_two_year_hundred_million_swap_needs_roughly_eight_hundred_contracts(
        self, par_swap, strip, as_of
    ):
        # The whitepaper's 779 is on its own curve at its own level. What is
        # checkable here is the order of magnitude and the mechanism: a
        # two-year DV01 near 20,000 divided by a 25-dollar tick.
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert 700 < hedge.total_contracts < 900
        assert 18_000 < abs(hedge.swap_dv01) < 24_000

    def test_the_dv01_grows_as_rates_fall(self, par_swap, strip, as_of):
        # The direction behind the whitepaper's 19,480 -> 19,921.
        table = shock_table(strip_hedge(par_swap, strip, as_of=as_of), (-100.0, 100.0)).table
        down, up = table.iloc[0], table.iloc[1]
        assert abs(down.swap_dv01_post_shock) > abs(up.swap_dv01_post_shock)

    def test_the_net_residual_is_a_meaningful_fraction_of_a_basis_point(
        self, par_swap, strip, as_of
    ):
        table = shock_table(strip_hedge(par_swap, strip, as_of=as_of), (-100.0,)).table
        assert 0.1 < abs(table.iloc[0].net_per_dv01_bp) < 10.0

    def test_the_strip_covers_two_years_of_imm_quarters(self, strip):
        futures = [i for i in strip if isinstance(i, FuturesNode)]
        assert len(futures) == 9
        for contract in futures:
            assert contract.start.weekday() == 2
            assert next_imm_on_or_after(contract.start + timedelta(days=1)) == contract.end
