"""Deterministic SOFR rates engine: curves, futures convexity, swaps, risk, hedging.

A curve is not a result until you can see what it rests on. Every public call
here returns a value *and* an :class:`~rates_engine.core.evidence.Evidence` chain —
which instruments were used and where they came from, what each one's
repricing residual was, which interpolation joined the nodes, which convexity
model and sigma adjusted the futures, which bump basis a risk number used, and
anything that was degraded rather than refused. The evidence composes: a hedge
built on a proxied curve says so at the top level without the hedging code
knowing what a proxy is.

What cannot be computed honestly is refused, never filled in. The contract for
those refusals is :mod:`rates_engine.core.errors` and ``docs/ERRORS.md``.

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

from rates_engine.conventions.calendar import (
    SIFMA_US,
    BusinessDayConvention,
    Calendar,
    SIFMAUSCalendar,
)
from rates_engine.conventions.daycount import (
    DayCount,
    day_count_from_name,
    year_fraction,
)
from rates_engine.conventions.option_kind import OptionKind
from rates_engine.conventions.schedule import (
    Schedule,
    imm_date,
    imm_dates,
    next_imm_on_or_after,
)
from rates_engine.conventions.side import Side
from rates_engine.core.errors import (
    BootstrapResidualError,
    CalibrationError,
    ConfigurationError,
    ConventionError,
    CurrencyMismatchError,
    CurveArbitrageError,
    CurveError,
    CurveMismatchError,
    DeltaConventionError,
    ExpansionBreakdownError,
    HedgeError,
    ImplausibleInputError,
    IncompatibleDependencyError,
    IncompleteStripError,
    InsufficientDataError,
    KeyTenorOutOfRangeError,
    MarketDataError,
    MissingDependencyError,
    MissingFixingError,
    MissingForwardError,
    NoTenorQuoteSourceError,
    PolicyBreachError,
    ProxySourceNotDeclaredError,
    RatesEngineError,
    RiskError,
    ShiftRequiredError,
    SliceNotQuotedError,
    UndefinedDurationError,
    UnderdeterminedCurveError,
    UnresolvedConventionError,
    UnsupportedConventionError,
    VolatilityError,
    VolUnitsError,
)
from rates_engine.core.evidence import (
    DataQuality,
    Degradation,
    Evidence,
    Provenance,
)
from rates_engine.core.money import (
    Currency,
    require_same_currency,
)
from rates_engine.core.results import (
    SCHEMA_VERSION,
    EngineResult,
)
from rates_engine.curves.bootstrap import (
    BootstrapResult,
    FuturesNode,
    ParSwapNode,
    RealizedStubNode,
    bootstrap_discount_curve,
)
from rates_engine.curves.comparison import (
    InterpolationComparison,
    compare_interpolations,
)
from rates_engine.curves.discount import (
    CurveSet,
    DiscountCurve,
)
from rates_engine.curves.dual import (
    BasisSwapNode,
    DualCurveResult,
    TenorParSwapNode,
    solve_dual_curve,
)
from rates_engine.curves.interpolation import MonotoneConvex
from rates_engine.curves.parametric import (
    FOMCStepCurve,
    FOMCStepFit,
    NelsonSiegel,
    NelsonSiegelFit,
    fit_fomc_step_curve,
    fit_nelson_siegel,
)
from rates_engine.curves.views import (
    CurveView,
    CurveViews,
    all_views,
    forward_curve,
    par_curve,
    zero_curve,
)
from rates_engine.hedging.futures_strip import (
    SR3_DV01,
    HedgeResult,
    ShockTableResult,
    shock_table,
    strip_hedge,
)
from rates_engine.hedging.fx_structures import (
    Exposure,
    ExposureDirection,
    StructureComparison,
    StructureQuote,
    StructureResult,
    compare_structures,
)
from rates_engine.hedging.program import (
    HedgeProgram,
    ProgramAudit,
    RebalanceFrequency,
    Severity,
    audit_hedge,
    load_program,
)
from rates_engine.instruments.capfloor import (
    CapFloor,
    Caplet,
)
from rates_engine.instruments.cashflow import Cashflow
from rates_engine.instruments.fra import FRA
from rates_engine.instruments.futures import (
    SOFRFuture1M,
    SOFRFuture3M,
)
from rates_engine.instruments.swaps import (
    IRSwap,
    OISSwap,
)
from rates_engine.instruments.swaption import Swaption
from rates_engine.market.estimators import (
    SigmaEstimate,
    realized_sofr_sigma,
)
from rates_engine.market.providers.file import (
    FuturesSettlement,
    load_series_csv,
    load_settlements_csv,
    load_snapshot_csv,
)
from rates_engine.market.snapshot import (
    CompoundedRate,
    MarketSnapshot,
    Series,
)
from rates_engine.models import (
    bachelier,
    black,
)
from rates_engine.models.convexity import (
    ConvexityModel,
    ConvexityResult,
    convexity_adjustment,
)
from rates_engine.models.sabr import (
    SABRCalibration,
    SABRParameters,
    density_diagnostics,
)
from rates_engine.models.sabr import calibrate as calibrate_sabr
from rates_engine.pricing.linear import (
    ParametricComparison,
    PriceResult,
    annuity,
    par_rate,
    price_on_parametric,
    pv,
)
from rates_engine.pricing.options import (
    OptionPriceResult,
    cap_floor_pv,
    caplet_pv,
    model_for,
    swaption_pv,
)
from rates_engine.risk.bumps import tent_weights
from rates_engine.risk.greeks import (
    GreeksResult,
    option_greeks,
)
from rates_engine.risk.sensitivities import (
    KeyRateResult,
    RiskResult,
    dv01,
    effective_convexity,
    effective_duration,
    key_rate_duration,
    key_rate_dv01,
    macaulay_duration,
    modified_duration,
    money_convexity,
    money_duration,
    pvbp,
)
from rates_engine.volatility.cube import (
    CubePoint,
    CubeQuote,
    StrikeConvention,
    VolCube,
)
from rates_engine.volatility.units import (
    Volatility,
    VolUnits,
)

__version__ = "0.3.0"

__all__ = [
    "all_views",
    "annuity",
    "audit_hedge",
    "bachelier",
    "BasisSwapNode",
    "black",
    "bootstrap_discount_curve",
    "BootstrapResidualError",
    "BootstrapResult",
    "BusinessDayConvention",
    "Calendar",
    "calibrate_sabr",
    "CalibrationError",
    "cap_floor_pv",
    "CapFloor",
    "Caplet",
    "caplet_pv",
    "Cashflow",
    "compare_interpolations",
    "compare_structures",
    "CompoundedRate",
    "ConfigurationError",
    "ConventionError",
    "convexity_adjustment",
    "ConvexityModel",
    "ConvexityResult",
    "CubePoint",
    "CubeQuote",
    "Currency",
    "CurrencyMismatchError",
    "CurveArbitrageError",
    "CurveError",
    "CurveMismatchError",
    "CurveSet",
    "CurveView",
    "CurveViews",
    "DataQuality",
    "day_count_from_name",
    "DayCount",
    "Degradation",
    "DeltaConventionError",
    "density_diagnostics",
    "DiscountCurve",
    "DualCurveResult",
    "dv01",
    "effective_convexity",
    "effective_duration",
    "EngineResult",
    "Evidence",
    "ExpansionBreakdownError",
    "Exposure",
    "ExposureDirection",
    "fit_fomc_step_curve",
    "fit_nelson_siegel",
    "FOMCStepCurve",
    "FOMCStepFit",
    "forward_curve",
    "FRA",
    "FuturesNode",
    "FuturesSettlement",
    "GreeksResult",
    "HedgeError",
    "HedgeProgram",
    "HedgeResult",
    "imm_date",
    "imm_dates",
    "ImplausibleInputError",
    "IncompatibleDependencyError",
    "IncompleteStripError",
    "PolicyBreachError",
    "InsufficientDataError",
    "InterpolationComparison",
    "IRSwap",
    "key_rate_duration",
    "key_rate_dv01",
    "KeyRateResult",
    "KeyTenorOutOfRangeError",
    "load_program",
    "load_series_csv",
    "load_settlements_csv",
    "load_snapshot_csv",
    "macaulay_duration",
    "MarketDataError",
    "MarketSnapshot",
    "MissingDependencyError",
    "MissingFixingError",
    "MissingForwardError",
    "model_for",
    "modified_duration",
    "money_convexity",
    "money_duration",
    "MonotoneConvex",
    "NelsonSiegel",
    "NelsonSiegelFit",
    "next_imm_on_or_after",
    "NoTenorQuoteSourceError",
    "OISSwap",
    "option_greeks",
    "OptionKind",
    "OptionPriceResult",
    "par_curve",
    "par_rate",
    "ParametricComparison",
    "ParSwapNode",
    "price_on_parametric",
    "PriceResult",
    "ProgramAudit",
    "Provenance",
    "ProxySourceNotDeclaredError",
    "pv",
    "pvbp",
    "RatesEngineError",
    "realized_sofr_sigma",
    "RealizedStubNode",
    "RebalanceFrequency",
    "require_same_currency",
    "RiskError",
    "RiskResult",
    "SABRCalibration",
    "SABRParameters",
    "Schedule",
    "SCHEMA_VERSION",
    "Series",
    "Severity",
    "ShiftRequiredError",
    "shock_table",
    "ShockTableResult",
    "Side",
    "SIFMA_US",
    "SIFMAUSCalendar",
    "SigmaEstimate",
    "SliceNotQuotedError",
    "SOFRFuture1M",
    "SOFRFuture3M",
    "solve_dual_curve",
    "SR3_DV01",
    "StrikeConvention",
    "strip_hedge",
    "StructureComparison",
    "StructureQuote",
    "StructureResult",
    "Swaption",
    "swaption_pv",
    "TenorParSwapNode",
    "tent_weights",
    "UndefinedDurationError",
    "UnderdeterminedCurveError",
    "UnresolvedConventionError",
    "UnsupportedConventionError",
    "Volatility",
    "VolatilityError",
    "VolCube",
    "VolUnits",
    "VolUnitsError",
    "year_fraction",
    "zero_curve",
    "__version__",
]
