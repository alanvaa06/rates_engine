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

from rates_engine.conventions.calendar import SIFMA_US  # noqa: E402

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


if __name__ == "__main__":
    write_sofr()
    write_treasuries()
    write_holidays()
    write_hull_table()
    print("fixtures written to", HERE)
