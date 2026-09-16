"""Deterministic SOFR rates engine: curves, futures convexity, swaps, risk, hedging.

A curve is not a result until you can see what it rests on. Every public call
here returns a value *and* an :class:`~rates_engine.evidence.Evidence` chain —
which instruments were used and where they came from, what each one's
repricing residual was, which interpolation joined the nodes, which convexity
model and sigma adjusted the futures, which bump basis a risk number used, and
anything that was degraded rather than refused. The evidence composes: a hedge
built on a proxied curve says so at the top level without the hedging code
knowing what a proxy is.

What cannot be computed honestly is refused, never filled in. The contract for
those refusals is :mod:`rates_engine.errors` and ``docs/ERRORS.md``.

Importing this package touches no network and installs no warning filters.

The shortest correct program::

    from datetime import date, timedelta
    from rates_engine import (
        CurveSet, FuturesNode, OISSwap, RealizedStubNode,
        bootstrap_discount_curve, imm_date, next_imm_on_or_after, par_rate,
    )

    as_of = date(2026, 1, 15)
    start = imm_date(2026, 3)
    instruments = [RealizedStubNode(end=start, accrual_factor=1.0069)]
    period = start
    for index in range(4):
        following = next_imm_on_or_after(period + timedelta(days=1))
        instruments.append(
            FuturesNode(start=period, end=following, forward_rate=0.04, label=f"SR3-{index}")
        )
        period = following

    curve = bootstrap_discount_curve(as_of, tuple(instruments))
    swap = OISSwap(effective=start, maturity=period, fixed_rate=0.04, notional=100_000_000)
    print(par_rate(swap, CurveSet(curve.curve)).value)
"""

from rates_engine.conventions import (
    SIFMA_US,
    BusinessDayConvention,
    Calendar,
    DayCount,
    Schedule,
    SIFMAUSCalendar,
    day_count_from_name,
    imm_date,
    imm_dates,
    next_imm_on_or_after,
    year_fraction,
)
from rates_engine.convexity import (
    ConvexityModel,
    ConvexityResult,
    SigmaEstimate,
    convexity_adjustment,
    realized_sofr_sigma,
)
from rates_engine.curves import (
    BootstrapResult,
    CurveSet,
    CurveView,
    CurveViews,
    DiscountCurve,
    FuturesNode,
    ParSwapNode,
    RealizedStubNode,
    all_views,
    bootstrap_discount_curve,
    forward_curve,
    par_curve,
    zero_curve,
)
from rates_engine.curves.dual import (
    BasisSwapNode,
    DualCurveResult,
    TenorParSwapNode,
    solve_dual_curve,
)
from rates_engine.errors import (
    BootstrapResidualError,
    ConventionError,
    CurveArbitrageError,
    CurveError,
    HedgeError,
    IncompleteStripError,
    InsufficientDataError,
    KeyTenorOutOfRangeError,
    MarketDataError,
    MissingDependencyError,
    MissingFixingError,
    NoTenorQuoteSourceError,
    ProxySourceNotDeclaredError,
    RatesEngineError,
    RiskError,
    UndefinedDurationError,
    UnderdeterminedCurveError,
    UnsupportedConventionError,
)
from rates_engine.evidence import DataQuality, Degradation, Evidence, Provenance
from rates_engine.hedging import (
    SR3_DV01,
    HedgeResult,
    ShockTableResult,
    shock_table,
    strip_hedge,
)
from rates_engine.instruments import (
    FRA,
    Cashflow,
    IRSwap,
    OISSwap,
    Side,
    SOFRFuture1M,
    SOFRFuture3M,
)
from rates_engine.market import (
    CompoundedRate,
    FuturesSettlement,
    MarketSnapshot,
    Series,
    load_series_csv,
    load_settlements_csv,
    load_snapshot_csv,
)
from rates_engine.pricing import PriceResult, annuity, dv01, par_rate, pv
from rates_engine.results import SCHEMA_VERSION, EngineResult
from rates_engine.risk import (
    KeyRateResult,
    RiskResult,
    effective_convexity,
    effective_duration,
    key_rate_duration,
    key_rate_dv01,
    macaulay_duration,
    modified_duration,
    money_convexity,
    money_duration,
    pvbp,
    tent_weights,
)

__version__ = "0.1.0"

__all__ = [
    "BasisSwapNode",
    "BootstrapResidualError",
    "BootstrapResult",
    "BusinessDayConvention",
    "Calendar",
    "Cashflow",
    "CompoundedRate",
    "ConventionError",
    "ConvexityModel",
    "ConvexityResult",
    "CurveArbitrageError",
    "CurveError",
    "CurveSet",
    "CurveView",
    "CurveViews",
    "DataQuality",
    "DayCount",
    "Degradation",
    "DiscountCurve",
    "DualCurveResult",
    "EngineResult",
    "Evidence",
    "FRA",
    "FuturesNode",
    "FuturesSettlement",
    "HedgeError",
    "HedgeResult",
    "IRSwap",
    "IncompleteStripError",
    "InsufficientDataError",
    "KeyRateResult",
    "KeyTenorOutOfRangeError",
    "MarketDataError",
    "MarketSnapshot",
    "MissingDependencyError",
    "MissingFixingError",
    "NoTenorQuoteSourceError",
    "OISSwap",
    "ParSwapNode",
    "PriceResult",
    "Provenance",
    "ProxySourceNotDeclaredError",
    "RatesEngineError",
    "RealizedStubNode",
    "RiskError",
    "RiskResult",
    "SCHEMA_VERSION",
    "SIFMAUSCalendar",
    "SIFMA_US",
    "SOFRFuture1M",
    "SOFRFuture3M",
    "SR3_DV01",
    "Schedule",
    "Series",
    "ShockTableResult",
    "Side",
    "SigmaEstimate",
    "TenorParSwapNode",
    "UndefinedDurationError",
    "UnderdeterminedCurveError",
    "UnsupportedConventionError",
    "__version__",
    "all_views",
    "annuity",
    "bootstrap_discount_curve",
    "convexity_adjustment",
    "day_count_from_name",
    "dv01",
    "effective_convexity",
    "effective_duration",
    "forward_curve",
    "imm_date",
    "imm_dates",
    "key_rate_dv01",
    "key_rate_duration",
    "load_series_csv",
    "load_settlements_csv",
    "load_snapshot_csv",
    "macaulay_duration",
    "modified_duration",
    "money_convexity",
    "money_duration",
    "next_imm_on_or_after",
    "par_curve",
    "par_rate",
    "pv",
    "pvbp",
    "realized_sofr_sigma",
    "shock_table",
    "solve_dual_curve",
    "strip_hedge",
    "tent_weights",
    "year_fraction",
    "zero_curve",
]
