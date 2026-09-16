"""Write the example config the README quickstart and the CI smoke test use.

One generator rather than a checked-in file, because the config spans two
years of IMM quarters and a hand-edited copy drifts the moment the dates do.
Both the README and CI read what this produces, so there is one thing to
keep right.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rates_engine.conventions import imm_date, next_imm_on_or_after  # noqa: E402

AS_OF = date(2026, 1, 15)
FLAT_PRICE = 96.0
QUARTERS = 9


def build() -> dict[str, object]:
    """The example configuration: a stub, nine SR3 quarters and a two-year swap.

    Returns:
        The configuration mapping, ready to serialise as JSON.
    """
    start = imm_date(2026, 3)
    futures = []
    period = start
    for index in range(QUARTERS):
        following = next_imm_on_or_after(period + timedelta(days=1))
        futures.append(
            {
                "label": f"SR3-{index + 1}",
                "start": period.isoformat(),
                "end": following.isoformat(),
                "price": FLAT_PRICE,
            }
        )
        period = following
    return {
        "as_of": AS_OF.isoformat(),
        "curve": {
            "stub": {
                "end": start.isoformat(),
                "accrual_factor": 1.0 + 0.04 * ((start - AS_OF).days / 360.0),
            },
            "futures": futures,
            "convexity": {"model": "ho_lee", "sigma": 0.01},
            "long_end_source": None,
        },
        "swap": {
            "effective": start.isoformat(),
            "maturity": period.isoformat(),
            "fixed_rate": 0.04,
            "notional": 100_000_000.0,
            "side": "payer",
        },
        "risk": {"key_tenors": [0.5, 1.0, 1.5, 2.0]},
        "hedge": {"shocks_bp": [-100, -50, -25, -10, 10, 25, 50, 100]},
    }


def main(destination: str) -> int:
    """Write the example config to ``destination``.

    Args:
        destination: Path to write to.

    Returns:
        Process exit code, always ``0``.
    """
    Path(destination).write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "config.json"))
