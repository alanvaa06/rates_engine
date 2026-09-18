"""Shared fixtures: one snapshot, one strip, one curve, built the same way everywhere.

Every numerical test in the suite stands on ``flat_curve`` or ``strip``, so a
change in how a curve is built shows up in one place rather than twenty.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from rates_engine.conventions import imm_date, next_imm_on_or_after
from rates_engine.curves import (
    CurveSet,
    FuturesNode,
    RealizedStubNode,
    bootstrap_discount_curve,
)
from rates_engine.instruments import OISSwap, Side
from rates_engine.market import load_series_csv, load_snapshot_csv
from rates_engine.market.snapshot import MarketSnapshot
from rates_engine.pricing import par_rate

FIXTURES = Path(__file__).parent / "fixtures"
CME_PUBLISHED = FIXTURES / "cme_published"
AS_OF = date(2026, 1, 15)
FLAT_FORWARD = 0.04
STRIP_LENGTH = 9


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Directory holding the versioned fixtures."""
    return FIXTURES


@pytest.fixture(scope="session")
def snapshot() -> MarketSnapshot:
    """A snapshot of the synthetic SOFR fixings, as of the suite's valuation date."""
    return MarketSnapshot(
        as_of=AS_OF,
        series={"SOFR": load_series_csv(FIXTURES / "sofr_fixings.csv", "SOFR")},
    )


@pytest.fixture(scope="session")
def treasury_snapshot() -> MarketSnapshot:
    """A snapshot including Treasury par yields, which load as proxies."""
    return load_snapshot_csv(
        AS_OF,
        {"SOFR": FIXTURES / "sofr_fixings.csv"},
    )


@pytest.fixture
def as_of() -> date:
    """The valuation date the suite uses."""
    return AS_OF


@pytest.fixture
def strip() -> tuple:
    """A realised stub plus nine quarterly futures on a flat 4% forward.

    Flat on purpose: a flat forward curve makes every analytic cross-check
    available, so a test that fails is failing on the engine rather than on
    the shape of the curve it was handed.
    """
    start = imm_date(2026, 3)
    stub_accrual = 1.0 + FLAT_FORWARD * ((start - AS_OF).days / 360.0)
    instruments = [RealizedStubNode(end=start, accrual_factor=stub_accrual)]
    period = start
    for index in range(STRIP_LENGTH):
        following = next_imm_on_or_after(period + timedelta(days=1))
        instruments.append(
            FuturesNode(
                start=period,
                end=following,
                forward_rate=FLAT_FORWARD,
                label=f"SR3-{index + 1}",
                convexity={"model": "ho_lee", "sigma": 0.01},
            )
        )
        period = following
    return tuple(instruments)


@pytest.fixture
def flat_curve(strip, as_of):
    """The bootstrapped result for :func:`strip`."""
    return bootstrap_discount_curve(as_of, strip)


@pytest.fixture
def curve_set(flat_curve) -> CurveSet:
    """A single-curve set built on :func:`flat_curve`."""
    return CurveSet(flat_curve.curve)


@pytest.fixture
def strip_span(strip) -> tuple[date, date]:
    """First and last IMM date the strip covers."""
    futures = [i for i in strip if isinstance(i, FuturesNode)]
    return futures[0].start, futures[-1].end


@pytest.fixture
def par_swap(curve_set, strip_span) -> OISSwap:
    """A two-year payer OIS on USD 100m, struck exactly at its own par rate."""
    start, end = strip_span
    provisional = OISSwap(
        effective=start,
        maturity=end,
        fixed_rate=FLAT_FORWARD,
        notional=100_000_000.0,
        side=Side.PAYER,
    )
    return OISSwap(
        effective=start,
        maturity=end,
        fixed_rate=par_rate(provisional, curve_set).value,
        notional=100_000_000.0,
        side=Side.PAYER,
    )


def published(name: str) -> Path | None:
    """A third-party fixture if it has been supplied, else ``None``.

    Args:
        name: File name inside ``tests/fixtures/cme_published``.

    Returns:
        The path when the file exists, otherwise ``None`` so the caller can
        skip rather than invent a substitute.
    """
    candidate = CME_PUBLISHED / name
    return candidate if candidate.exists() else None


def require_published(*names: str, ac: str) -> list[Path]:
    """Skip the calling test unless every named third-party fixture exists.

    Args:
        *names: File names inside ``tests/fixtures/cme_published``.
        ac: The acceptance criterion this comparison serves, named in the skip
            message so that ``scripts/audit_acceptance.py`` can attribute the
            skip exactly rather than guessing from the module it sits in.

    Returns:
        The paths, when they all exist.
    """
    missing = [n for n in names if published(n) is None]
    if missing:
        pytest.skip(
            f"PRD-001 AC-{ac}: third-party fixture not supplied: "
            + ", ".join(missing)
            + f". See {CME_PUBLISHED / 'README.md'} — this comparison is against a "
            "published number and is skipped rather than faked."
        )
    return [CME_PUBLISHED / n for n in names]


@pytest.fixture(scope="session")
def option_curve():
    """A fifteen-year flat 4% continuous curve, for the option tests.

    Flat and analytic on purpose: every option identity below is exact, so a
    failure is the pricer's and not the curve's shape.
    """
    import math
    from datetime import timedelta

    from rates_engine.curves import DiscountCurve

    nodes = tuple(AS_OF + timedelta(days=365 * k) for k in range(1, 16))
    return DiscountCurve(AS_OF, nodes, tuple(math.exp(-0.04 * k) for k in range(1, 16)))


@pytest.fixture
def option_curve_set(option_curve) -> CurveSet:
    """A single-curve set on :func:`option_curve`."""
    return CurveSet(option_curve)


@pytest.fixture
def underlying_swap():
    """The 5y10y swap the swaption tests are written on."""
    from datetime import timedelta

    return OISSwap(
        effective=AS_OF + timedelta(days=365 * 5),
        maturity=AS_OF + timedelta(days=365 * 15),
        fixed_rate=0.04,
        notional=100_000_000.0,
    )


@pytest.fixture
def forward_swap_rate(underlying_swap, option_curve_set) -> float:
    """The 5y10y forward swap rate under :func:`option_curve`."""
    return par_rate(underlying_swap, option_curve_set).value


@pytest.fixture
def atm_swaption(underlying_swap, forward_swap_rate):
    """An at-the-money payer swaption on the 5y10y."""
    from datetime import timedelta

    from rates_engine.instruments import Swaption

    return Swaption(
        expiry=AS_OF + timedelta(days=365 * 5),
        underlying=underlying_swap,
        strike=forward_swap_rate,
        side=Side.PAYER,
        notional=100_000_000.0,
    )
