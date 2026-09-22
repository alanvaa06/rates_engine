"""Hot-path timings for the public API, so a refactor can prove it kept speed.

Imports only from the package root, which is the contract that survives any
internal reorganisation. Run it before and after a change and compare:

    python benchmarks/bench_core.py            # human table
    python benchmarks/bench_core.py --json     # machine-readable

Each case reports the best of ``--repeat`` runs of ``number`` calls, in
milliseconds per call. Best-of rather than mean: the minimum is the run with
the least interference from the rest of the machine, and it is the most
stable number to compare across two builds.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta

from rates_engine import (
    CurveSet,
    FuturesNode,
    OISSwap,
    RealizedStubNode,
    Side,
    bootstrap_discount_curve,
    dv01,
    imm_date,
    key_rate_dv01,
    next_imm_on_or_after,
    par_rate,
    pv,
    shock_table,
    strip_hedge,
)

AS_OF = date(2026, 1, 15)


def _strip(quarters: int, *, slope: float = 0.0) -> tuple:
    start = imm_date(2026, 3)
    instruments: list = [
        RealizedStubNode(end=start, accrual_factor=1.0 + 0.04 * ((start - AS_OF).days / 360.0))
    ]
    period = start
    for index in range(quarters):
        following = next_imm_on_or_after(period + timedelta(days=1))
        instruments.append(
            FuturesNode(
                start=period,
                end=following,
                forward_rate=0.04 + slope * index,
                label=f"SR3-{index + 1}",
            )
        )
        period = following
    return tuple(instruments)


def _span(instruments: tuple) -> tuple[date, date]:
    futures = [i for i in instruments if isinstance(i, FuturesNode)]
    return futures[0].start, futures[-1].end


def _par_swap(curve_set: CurveSet, start: date, end: date) -> OISSwap:
    draft = OISSwap(effective=start, maturity=end, fixed_rate=0.04, notional=100_000_000.0,
                    side=Side.PAYER)
    return OISSwap(effective=start, maturity=end, fixed_rate=par_rate(draft, curve_set).value,
                   notional=100_000_000.0, side=Side.PAYER)


def cases() -> dict[str, tuple[Callable[[], object], int]]:
    """Name -> (callable, calls per timing run)."""
    long_strip = _strip(40, slope=0.0002)
    short_strip = _strip(8)
    long_curve = bootstrap_discount_curve(AS_OF, long_strip).curve
    mc_curve = bootstrap_discount_curve(AS_OF, long_strip, interpolation="monotone_convex").curve
    long_set, mc_set = CurveSet(long_curve), CurveSet(mc_curve)
    start, end = _span(long_strip)
    ten_year = _par_swap(long_set, start, end)
    short_set = CurveSet(bootstrap_discount_curve(AS_OF, short_strip).curve)
    two_year = _par_swap(short_set, *_span(short_strip))
    hedge = strip_hedge(two_year, short_strip, as_of=AS_OF)
    tenors = (0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)
    return {
        "bootstrap_40q_loglinear": (lambda: bootstrap_discount_curve(AS_OF, long_strip), 5),
        "bootstrap_40q_monotone_convex": (
            lambda: bootstrap_discount_curve(AS_OF, long_strip, interpolation="monotone_convex"),
            2,
        ),
        "pv_10y_ois": (lambda: pv(ten_year, long_set), 50),
        "pv_10y_ois_monotone_convex": (lambda: pv(ten_year, mc_set), 10),
        "dv01_10y_ois": (lambda: dv01(ten_year, long_set), 20),
        "key_rate_dv01_10y_10tenors": (lambda: key_rate_dv01(ten_year, long_set, tenors), 3),
        "strip_hedge_2y_8q": (lambda: strip_hedge(two_year, short_strip, as_of=AS_OF), 2),
        "shock_table_8_shocks": (lambda: shock_table(hedge), 3),
    }


def run(repeat: int) -> dict[str, float]:
    """Milliseconds per call, best of ``repeat`` runs, for every case."""
    results: dict[str, float] = {}
    for name, (func, number) in cases().items():
        func()  # warm caches and imports outside the timing
        best = float("inf")
        for _ in range(repeat):
            began = time.perf_counter()
            for _ in range(number):
                func()
            best = min(best, (time.perf_counter() - began) / number)
        results[name] = best * 1e3
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time the public API's hot paths.")
    parser.add_argument("--repeat", type=int, default=5, help="timing runs per case")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args(argv)
    results = run(args.repeat)
    if args.json:
        print(json.dumps({k: round(v, 4) for k, v in results.items()}, indent=2))
        return 0
    width = max(len(k) for k in results)
    for name, ms in results.items():
        print(f"{name:<{width}}  {ms:10.3f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
