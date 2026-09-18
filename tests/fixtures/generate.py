"""Regenerate the synthetic fixtures. Run once; the CSVs are what the suite reads.

Everything this writes is *constructed*, and every file gets a provenance
sibling that says so. The suite is offline by design, and a fixture without a
source record is an invented number with extra steps.

The one thing it cannot write is ``cme_published/``: those are third-party
observations, not something a generator is allowed to have an opinion about.
See ``cme_published/README.md``.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rates_engine.conventions.calendar import BMV, SIFMA_US  # noqa: E402
from rates_engine.conventions.schedule import add_months  # noqa: E402
from rates_engine.curves.parametric import FOMCStepCurve  # noqa: E402
from rates_engine.instruments.futures import imm_date, next_imm_on_or_after  # noqa: E402

HERE = Path(__file__).parent
START = date(2022, 1, 3)
END = date(2026, 9, 16)
SEED = 20260916


def _provenance(path: Path, **fields: object) -> None:
    path.with_suffix(path.suffix + ".provenance.json").write_text(
        json.dumps(
            {
                "file": path.name,
                "generated_by": "tests/fixtures/generate.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "seed": SEED,
                **fields,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def business_days() -> list[date]:
    days, day = [], START
    while day <= END:
        if SIFMA_US.is_business_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def write_sofr() -> None:
    """A plausible overnight path: mean reverting, quantised the way SOFR is published."""
    days = business_days()
    rng = np.random.default_rng(SEED)
    level, target = 0.0008, 0.0430
    values = []
    for index in range(len(days)):
        if index < 120:
            target = 0.0008 + 0.045 * index / 120
        elif index > len(days) - 300:
            target = 0.0395
        level += 0.06 * (target - level) + rng.normal(0.0, 0.00012)
        # SOFR is published to two decimals in percent, so the fixture is too.
        values.append(round(max(level, 0.0), 4))
    path = HERE / "sofr_fixings.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "value"])
        writer.writerows([[d.isoformat(), f"{v:.4f}"] for d, v in zip(days, values, strict=True)])
    _provenance(
        path,
        series_id="SOFR",
        data_quality="synthetic",
        units="decimal rate, e.g. 0.0431 for 4.31%",
        rows=len(days),
        note=(
            "Constructed, NOT the published SOFR series. This environment's egress "
            "policy blocks fred.stlouisfed.org, so no observed fixings could be "
            "frozen. Every test that reads this file asserts a property of the "
            "engine's arithmetic, never a published number."
        ),
    )


def write_treasuries() -> None:
    """Constant-maturity par yields, used only to exercise the proxy gate."""
    days = business_days()
    rng = np.random.default_rng(SEED + 1)
    path = HERE / "dgs_treasury.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "DGS2", "DGS3", "DGS5", "DGS10"])
        base = np.array([0.0405, 0.0410, 0.0420, 0.0440])
        for day in days:
            base = base + rng.normal(0.0, 0.00008, size=4)
            writer.writerow([day.isoformat(), *[f"{v:.4f}" for v in base]])
    _provenance(
        path,
        series_ids=["DGS2", "DGS3", "DGS5", "DGS10"],
        data_quality="proxy",
        units="decimal rate",
        note=(
            "Constructed. Classified as a proxy on load regardless of source, because "
            "a Treasury par yield standing in for OIS par carries the swap spread."
        ),
    )


def write_holidays() -> None:
    """The SIFMA calendar this package implements, dumped for review."""
    path = HERE / "sifma_holidays.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "year"])
        for year in range(2022, 2031):
            for day in sorted(SIFMA_US.holidays(year)):
                writer.writerow([day.isoformat(), year])
    _provenance(
        path,
        data_quality="assumed",
        note=(
            "Generated FROM rates_engine.conventions.calendar, so it cannot catch a "
            "wrong rule — it exists to be diffed against SIFMA's published calendar "
            "by a human. PRD-001 AC-1.2 marks that check [manual-check]; it has NOT "
            "been done, because this environment cannot reach sifma.org. The "
            "Saturday-observed-on-Friday rule is the specific line to confirm."
        ),
    )


def write_hull_table() -> None:
    """The two published Ho-Lee adjustments the convexity test checks against."""
    path = HERE / "hull_convexity_table.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_to_start", "time_to_end", "sigma", "adjustment_bp"])
        writer.writerow([1.0, 1.25, 0.01, 0.62])
        writer.writerow([2.0, 2.25, 0.01, 2.25])
    _provenance(
        path,
        data_quality="observed",
        source="Hull, via the vault note 'Forward vs Futures and the Eurodollar Convexity Bias'",
        note=(
            "Transcribed from the wiki, which cites Hull. Not read in Hull directly, "
            "which is why PRD-001 records it as verified in a secondary source."
        ),
    )


# The swaption cube fixture. Levels and shape are typical of the USD normal-vol
# grid; the numbers themselves are constructed, which is why the provenance
# sibling marks them ``synthetic``. The smile is quadratic in moneyness, which
# SABR is *not* — that is the point. A fixture generated by SABR would make
# AC-3.2's residual a statement about the solver rather than about how well
# Hagan's expansion approximates a market-shaped smile.
CUBE_SLICES: dict[tuple[float, float], tuple[float, float]] = {
    (1.0, 5.0): (0.0425, 95.0),
    (5.0, 5.0): (0.0405, 105.0),
    (5.0, 10.0): (0.0398, 100.0),
    (10.0, 10.0): (0.0390, 85.0),
}
CUBE_OFFSETS = (-0.010, -0.005, -0.0025, 0.0, 0.0025, 0.005, 0.010)
CUBE_SKEW = -0.030
CUBE_CURVATURE = 0.020
# The hole AC-3.1 reads through: this slice does not quote fifty basis points
# below the money, so asking for it has to go through SABR.
CUBE_HOLE = ((5.0, 10.0), -0.005)


def write_swaption_cube() -> None:
    """A four-slice swaption volatility cube with one strike deliberately absent."""
    path = HERE / "swaption_vol_cube.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["expiry", "tenor", "forward", "strike_offset", "strike", "normal_vol_bp"]
        )
        for (expiry, tenor), (forward, atm_bp) in CUBE_SLICES.items():
            for offset in CUBE_OFFSETS:
                if ((expiry, tenor), offset) == CUBE_HOLE:
                    continue
                moneyness = offset / 0.01
                vol_bp = atm_bp * (
                    1.0 + CUBE_SKEW * moneyness + CUBE_CURVATURE * moneyness * moneyness
                )
                writer.writerow(
                    [
                        expiry,
                        tenor,
                        forward,
                        offset,
                        round(forward + offset, 10),
                        round(vol_bp, 10),
                    ]
                )
    _provenance(
        path,
        data_quality="synthetic",
        source="Constructed by this generator; no market quote was copied.",
        note=(
            "Normal volatility in basis points, strikes absolute. ATM levels and the "
            "sign of the skew are typical of the USD swaption grid, but every number "
            f"is generated from vol(m) = atm * (1 + {CUBE_SKEW} m + {CUBE_CURVATURE} m^2) with m the "
            "strike offset in units of 100 bp. The shape is quadratic in moneyness, "
            "which SABR is not, so the residual in PRD-002 AC-3.2 measures the "
            "expansion's fit to a smile it did not produce. The 5y10y slice omits "
            "the -50 bp strike on purpose: that is the gap AC-3.1 fills."
        ),
    )


# A zero curve with two humps, so that fitting Nelson-Siegel to it is a
# measurement rather than a tautology. The generator is Svensson, which is
# Nelson-Siegel plus a second decay term; NS cannot reproduce it, and the
# residual PRD-002 AC-4.1 bounds is how much of a real curve's shape four
# parameters miss.
ZERO_TIMES = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0)
SVENSSON = dict(b0=0.0415, b1=0.0050, b2=-0.0120, b3=0.0060, tau1=1.6, tau2=6.0)


def _svensson(time: float) -> float:
    x, y = time / SVENSSON["tau1"], time / SVENSSON["tau2"]
    s1 = -np.expm1(-x) / x
    s2 = -np.expm1(-y) / y
    return float(
        SVENSSON["b0"]
        + SVENSSON["b1"] * s1
        + SVENSSON["b2"] * (s1 - np.exp(-x))
        + SVENSSON["b3"] * (s2 - np.exp(-y))
    )


def write_zero_curve() -> None:
    """Eleven zero rates on a two-hump curve, for the Nelson-Siegel fit."""
    path = HERE / "zero_curve.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "zero_rate"])
        for time in ZERO_TIMES:
            writer.writerow([time, round(_svensson(time), 10)])
    _provenance(
        path,
        data_quality="synthetic",
        units="continuously compounded decimal rate",
        source="Constructed by this generator from a Svensson curve.",
        note=(
            "Levels and the inverted front end are typical of USD, but every number "
            f"comes from Svensson with {SVENSSON}. Svensson is Nelson-Siegel plus a "
            "second hump, so a Nelson-Siegel fit to this cannot be exact and the "
            "residual in PRD-002 AC-4.1 measures the model rather than the solver."
        ),
    )


# The step path the futures strip below is quoted off. Meeting dates are
# plausible FOMC effective dates; the path is a constructed easing cycle.
FOMC_AS_OF = date(2026, 9, 16)
FOMC_MEETINGS = (date(2026, 11, 5), date(2026, 12, 17), date(2027, 1, 28), date(2027, 3, 18))
FOMC_PATH = (0.0430, 0.0405, 0.0380, 0.0365, 0.0355)
FUTURES_TICK = 0.0025
"""Quarter of a basis point of price. Rounding to it is what keeps the fit
from being handed its own answer: the strip below is not exactly repriceable
by any step path, so AC-4.2's sub-basis-point residual is earned."""


def write_fomc_strip() -> None:
    """Seven SR1 and two SR3 settlements quoted off a known step path."""
    truth = FOMCStepCurve(FOMC_AS_OF, FOMC_MEETINGS, FOMC_PATH)
    rows: list[tuple[str, date, date, str, float]] = []
    for offset in range(7):
        begin = add_months(date(2026, 10, 1), offset)
        end = add_months(begin, 1)
        rows.append(
            (f"SR1-{begin:%b%y}", begin, end, "SR1", truth.averaged(begin, end))
        )
    for start in (imm_date(2026, 12), imm_date(2027, 3)):
        end = next_imm_on_or_after(start + timedelta(days=1))
        rows.append(
            (f"SR3-{start:%b%y}", start, end, "SR3", truth.compounded(start, end))
        )

    path = HERE / "fomc_futures_strip.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["label", "reference_start", "reference_end", "symbol", "price"])
        for label, begin, end, symbol, rate in rows:
            price = round((100.0 - rate * 100.0) / FUTURES_TICK) * FUTURES_TICK
            writer.writerow(
                [label, begin.isoformat(), end.isoformat(), symbol, round(price, 6)]
            )

    meetings = HERE / "fomc_meetings.csv"
    with meetings.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["effective_date", "true_rate_after"])
        for meeting, rate in zip(FOMC_MEETINGS, FOMC_PATH[1:], strict=True):
            writer.writerow([meeting.isoformat(), rate])

    for target in (path, meetings):
        _provenance(
            target,
            data_quality="synthetic",
            as_of=FOMC_AS_OF.isoformat(),
            source="Constructed by this generator; no exchange settlement was copied.",
            note=(
                "Settlements are the arithmetic average (SR1) and daily-compounded "
                f"(SR3) overnight rate under the step path {FOMC_PATH} with effective "
                f"dates {[d.isoformat() for d in FOMC_MEETINGS]}, then rounded to the "
                f"{FUTURES_TICK} price tick. The rounding is deliberate: no step path "
                "reprices the rounded strip exactly, so the residual bounded by "
                "PRD-002 AC-4.2 is a real measurement. fomc_meetings.csv carries the "
                "path that generated them so a test can check recovery, not just fit."
            ),
        )


WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
"""Spelled out rather than taken from ``strftime("%A")``, which is
locale-dependent: the same fixture regenerated on a runner with a different
locale would differ from the one committed, and the test that compares them
would fail for a reason nobody could act on."""


def write_bmv_holidays() -> None:
    """The BMV calendar this package implements, dumped for human review."""
    path = HERE / "bmv_holidays.csv"
    rows = []
    for year in range(2022, 2032):
        for day in sorted(BMV.holidays(year)):
            rows.append((year, day.isoformat(), WEEKDAY_NAMES[day.weekday()]))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["year", "date", "weekday"])
        writer.writerows(rows)
    _provenance(
        path,
        data_quality="synthetic",
        source=(
            "Dumped from this package's BMVCalendar, whose rules were read from "
            "QuantLib's ql/time/calendars/mexico.cpp."
        ),
        note=(
            "This is the BMV (stock exchange) calendar, NOT Banxico's banking "
            "calendar, which is what PRD-003 AC-1.2 asks for. They are different "
            "lists. The file exists to be diffed against Banxico's published list "
            "by a human: that check is [manual-check] and has NOT been done, "
            "because this environment cannot reach banxico.org.mx (403 at the "
            "egress proxy). Two lines to confirm first: whether Banxico observes "
            "Holy Thursday, and whether it applies any weekend-observance roll, "
            "which this calendar does not."
        ),
    )


if __name__ == "__main__":
    write_sofr()
    write_treasuries()
    write_holidays()
    write_hull_table()
    write_swaption_cube()
    write_zero_curve()
    write_fomc_strip()
    write_bmv_holidays()
    print("fixtures written to", HERE)
