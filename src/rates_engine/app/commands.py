"""The use cases, one function each, and the table every interface dispatches through.

Every function has the same signature -- an optional configuration mapping
in, a JSON-ready payload out -- which is what lets the CLI and the MCP server
expose :data:`COMMANDS` unchanged. A failure raises the named refusal from
:mod:`rates_engine.core.errors`; turning it into an exit code or a tool error
is the adapter's job, not this module's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from importlib import metadata
from typing import Any

from rates_engine.app.builders import calibration_nodes, flat_curve, fx_inputs, ois_swap
from rates_engine.app.config import as_date, require_config
from rates_engine.conventions.currency_pair import USDMXN
from rates_engine.conventions.indices import INDICES, UNRESOLVED_MXN
from rates_engine.core.errors import (
    BootstrapResidualError,
    ConfigurationError,
    CurveArbitrageError,
    UndefinedDurationError,
)
from rates_engine.core.errors import __all__ as EXCEPTION_NAMES
from rates_engine.core.money import Currency
from rates_engine.core.results import SCHEMA_VERSION
from rates_engine.curves.bootstrap import bootstrap_discount_curve
from rates_engine.curves.comparison import compare_interpolations
from rates_engine.curves.discount import CurveSet
from rates_engine.curves.views import all_views
from rates_engine.hedging.futures_strip import DEFAULT_SHOCKS_BP, SR3_DV01, shock_table, strip_hedge
from rates_engine.hedging.fx_structures import (
    Exposure,
    ExposureDirection,
    StructureQuote,
    compare_structures,
)
from rates_engine.hedging.program import audit_hedge, load_program
from rates_engine.models.fx_delta import DeltaBasis, PremiumAdjustment
from rates_engine.pricing.fx_forward import forward_from_curves
from rates_engine.pricing.linear import annuity, par_rate, pv
from rates_engine.reporting.payloads import result_payload
from rates_engine.reporting.viz import PANELS, render_page
from rates_engine.risk.sensitivities import (
    INTERPOLATION_CAVEAT,
    dv01,
    effective_convexity,
    effective_duration,
    key_rate_dv01,
    money_convexity,
    money_duration,
    pvbp,
)
from rates_engine.volatility.fx_smile import ATMConvention
from rates_engine.volatility.units import VolUnits

__all__ = [
    "COMMANDS",
    "Command",
    "describe",
    "list_instruments",
    "bootstrap",
    "price",
    "hedge",
    "fx_forward",
    "hedge_structures",
    "interpolation_gap",
    "viz",
]


def list_instruments(_config: dict[str, Any] | None) -> dict[str, Any]:
    """List the instruments this build can price, and what each one needs.

    Exists so that the MCP tool of the same name has a command to be equal
    to: PRD-002 AC-6.1 holds the server to returning exactly the payload of
    the equivalent ``--json`` command, which requires the equivalent command
    to exist.

    Args:
        _config: Ignored; accepted so every command shares one signature.

    Returns:
        The ``InstrumentCatalogue`` payload.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "result_type": "InstrumentCatalogue",
        "linear": [
            {"name": "OISSwap", "index": "compounded_sofr", "curves": ["discount"]},
            {"name": "IRSwap", "index": "term_sofr", "curves": ["discount", "tenor"]},
            {"name": "FRA", "index": "term_sofr", "curves": ["discount", "tenor"]},
            {"name": "SOFRFuture1M", "settlement": "arithmetic_average", "dv01_usd": 41.67},
            {"name": "SOFRFuture3M", "settlement": "daily_compounded", "dv01_usd": 25.0},
        ],
        "options": [
            {
                "name": "Swaption",
                "style": "european",
                "models": ["bachelier", "black"],
                "numeraire": "swap_annuity",
            },
            {
                "name": "CapFloor",
                "style": "strip_of_european",
                "models": ["bachelier", "black"],
                "numeraire": "discounted_accrual",
            },
        ],
        "not_implemented": {
            "FixedRateBond": "v1.1; brings Macaulay and modified duration with it",
            "Bermudan swaptions": "out of scope for v2",
            "CMS": "out of scope for v2",
        },
        "evidence": None,
    }


def describe(_config: dict[str, Any] | None) -> dict[str, Any]:
    """Describe the engine's surface without computing anything.

    Args:
        _config: Ignored; accepted so every command shares one signature.

    Returns:
        The ``Description`` payload: capabilities, conventions, models and exit codes.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "result_type": "Description",
        "distribution": "finport-ratesengine",
        "import_name": "rates_engine",
        "console_script": "rateng",
        "commands": [
            "bootstrap", "price", "hedge", "describe", "list-instruments",
            "fx-forward", "hedge-structures",
        ],
        "currencies": [c.value for c in Currency],
        "day_counts": ["ACT/360", "ACT/365F", "30/360"],
        "calendars": ["SIFMA_US"],
        "rate_indices": sorted(INDICES),
        "interpolation": ["log_linear_df", "monotone_convex"],
        "convexity_models": ["none", "ho_lee", "hull_white"],
        "option_models": ["bachelier", "black"],
        "volatility_units": [u.value for u in VolUnits],
        "smile_model": "sabr_hagan_2002",
        "parametric_curves": ["nelson_siegel", "fomc_step"],
        "calendars_v3": ["BMV"],
        "fx": {
            "pairs": [USDMXN.name],
            "option_model": "garman_kohlhagen",
            "smile_model": "vanna_volga",
            "delta_conventions": [
                f"{basis.value} {adjustment.value}"
                for basis in DeltaBasis
                for adjustment in PremiumAdjustment
            ],
            "atm_conventions": [c.value for c in ATMConvention],
            "delta_convention_default": None,
            "atm_convention_default": None,
        },
        "hedge_structures": [
            "unhedged", "forward", "protective_option_atm", "protective_option_otm",
            "collar", "collar_zero_cost", "option_spread", "seagull",
        ],
        "recommends": False,
        "recommends_note": (
            "This engine compares hedge structures and does not rank them. There is "
            "no function whose name contains 'recommend'; the payload reports cost, "
            "worst case, best case and upside participation, and the trade-off "
            "between them as labelled axes. Which point on that line is right is a "
            "treasury policy question."
        ),
        "unresolved_conventions": {
            "MXN": [name for name, _ in UNRESOLVED_MXN],
        },
        "curve_views": ["discount", "zero", "par", "forward"],
        "risk_measures": [
            "dv01",
            "key_rate_dv01",
            "key_rate_duration",
            "pvbp",
            "money_duration",
            "money_convexity",
            "effective_duration",
            "effective_convexity",
        ],
        "not_implemented": {
            "macaulay_duration": "needs a single yield, and therefore a bond (v1.1)",
            "modified_duration": "needs a single yield, and therefore a bond (v1.1)",
        },
        "exceptions": sorted(EXCEPTION_NAMES),
        "exit_codes": {"0": "ok", "1": "inputs unusable", "2": "calculation impossible"},
        "key_rate_caveat": INTERPOLATION_CAVEAT,
        "evidence": None,
    }


def bootstrap(config: dict[str, Any] | None) -> dict[str, Any]:
    """Bootstrap the discount curve and export its four views.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``BootstrapResult`` payload with a ``views`` block holding the four views.
    """
    config = require_config(config, "bootstrap")
    as_of = as_date(config["as_of"])
    instruments = calibration_nodes(config, as_of)
    result = bootstrap_discount_curve(
        as_of,
        instruments,
        long_end_source=(config.get("curve") or {}).get("long_end_source"),
        strict=bool((config.get("curve") or {}).get("strict", True)),
    )
    views = all_views(result.curve, source_evidence=result.evidence)
    return result_payload(result, views=views.to_dict(), command="bootstrap")


def price(config: dict[str, Any] | None) -> dict[str, Any]:
    """Price the configured swap and report every risk measure that is defined.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``pv`` payload with a ``measures`` block. A measure undefined on
        this swap is reported as refused rather than omitted.
    """
    config = require_config(config, "price")
    as_of = as_date(config["as_of"])
    instruments = calibration_nodes(config, as_of)
    swap = ois_swap(config)
    if swap.currency is not Currency.USD:
        # The curve block quotes a SOFR stub, SR3 futures and SOFR OIS par:
        # dollar quotes. Building a peso curve from them would be the
        # relabelling this package refuses everywhere else.
        raise ConfigurationError(
            f"the swap floats on {swap.index.name} ({swap.currency.value}), but the "
            "curve block quotes SOFR instruments, from which only a USD curve can be "
            "built. Price peso swaps through the library (bootstrap_mxn_curve or "
            "SwapQuoteNode on TIIE swaps); the CLI has no peso quote block yet."
        )
    boot = bootstrap_discount_curve(
        as_of, instruments, long_end_source=(config.get("curve") or {}).get("long_end_source")
    )
    curve_set = CurveSet(boot.curve)
    sources = (boot.evidence,)

    value = pv(swap, curve_set, source_evidence=sources)
    measures: dict[str, Any] = {
        "pv": value.to_dict(),
        "par_rate": par_rate(swap, curve_set, source_evidence=sources).to_dict(),
        "annuity": annuity(swap, curve_set, source_evidence=sources).to_dict(),
        "dv01": dv01(swap, curve_set, source_evidence=sources).to_dict(),
        "pvbp": pvbp(swap, curve_set, source_evidence=sources).to_dict(),
        "money_duration": money_duration(swap, curve_set, source_evidence=sources).to_dict(),
        "money_convexity": money_convexity(swap, curve_set, source_evidence=sources).to_dict(),
    }
    # Normalised measures are undefined on a par swap. Reporting the refusal is
    # the honest answer, and it keeps the key present rather than missing.
    for name, function in (
        ("effective_duration", effective_duration),
        ("effective_convexity", effective_convexity),
    ):
        try:
            measures[name] = function(swap, curve_set, source_evidence=sources).to_dict()
        except UndefinedDurationError as exc:
            measures[name] = {"value": None, "refused": str(exc)}

    key_tenors = (config.get("risk") or {}).get("key_tenors")
    measures["key_rate_dv01"] = (
        key_rate_dv01(
            swap, curve_set, tuple(float(t) for t in key_tenors), source_evidence=sources
        ).to_dict()
        if key_tenors
        else None
    )
    return {**value.to_dict(), "measures": measures, "command": "price"}


def hedge(config: dict[str, Any] | None) -> dict[str, Any]:
    """Size the futures strip against the swap and shock the hedged position.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``HedgeResult`` payload with a ``shock_table`` block.
    """
    config = require_config(config, "hedge")
    as_of = as_date(config["as_of"])
    instruments = calibration_nodes(config, as_of)
    swap = ois_swap(config)
    settings = config.get("hedge") or {}
    result = strip_hedge(
        swap,
        instruments,
        as_of=as_of,
        long_end_source=(config.get("curve") or {}).get("long_end_source"),
        contract_dv01=float(settings.get("contract_dv01", SR3_DV01)),
    )
    shocks = tuple(float(s) for s in settings.get("shocks_bp", DEFAULT_SHOCKS_BP))
    table = shock_table(result, shocks)
    return result_payload(result, shock_table=table.to_dict(), command="hedge")


def fx_forward(config: dict[str, Any] | None) -> dict[str, Any]:
    """The USD/MXN forward, with the cross-currency basis kept separate.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``FXForwardResult`` payload, the basis reported apart from the
        covered-interest-parity forward.
    """
    config = require_config(config, "fx-forward")
    as_of, block, years = fx_inputs(config)
    delivery = as_date(block["delivery"])
    domestic = flat_curve(as_of, float(block["r_domestic"]), Currency.MXN, years)
    foreign = flat_curve(as_of, float(block["r_foreign"]), Currency.USD, years)
    result = forward_from_curves(
        USDMXN,
        float(block["spot"]),
        delivery,
        domestic,
        foreign,
        basis_bp=float(block.get("basis_bp", 0.0)),
    )
    return result_payload(result, command="fx-forward")


def hedge_structures(config: dict[str, Any] | None) -> dict[str, Any]:
    """Compare the seven hedge structures against a transaction exposure.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``StructureComparison`` payload, with a ``program_audit`` block
        when the config carries a ``hedge_program``.
    """
    config = require_config(config, "hedge-structures")
    as_of, block, years = fx_inputs(config)
    exposure_block = config.get("exposure") or {}
    if not exposure_block:
        raise ConfigurationError(
            "hedge-structures needs an `exposure` block with amount and direction "
            "('payable' or 'receivable')."
        )
    quote = StructureQuote(
        spot=float(block["spot"]),
        expiry=years,
        r_domestic=float(block["r_domestic"]),
        r_foreign=float(block["r_foreign"]),
        volatility=float(block["volatility"]),
    )
    exposure = Exposure(
        amount=float(exposure_block["amount"]),
        direction=ExposureDirection(str(exposure_block["direction"])),
        settlement=as_date(block["delivery"]),
        pair=USDMXN,
    )
    comparison = compare_structures(
        exposure,
        quote,
        hedge_ratio=float(exposure_block.get("hedge_ratio", 1.0)),
        correlation=(
            float(exposure_block["correlation"])
            if exposure_block.get("correlation") is not None
            else None
        ),
        foreign_asset_volatility=(
            float(exposure_block["foreign_asset_volatility"])
            if exposure_block.get("foreign_asset_volatility") is not None
            else None
        ),
    )
    payload: dict[str, Any] = {"command": "hedge-structures"}
    program_block = config.get("hedge_program")
    if program_block:
        program = load_program(dict(program_block))
        audit = audit_hedge(
            program,
            float(exposure_block.get("hedge_ratio", 1.0)),
            proposed_instrument=exposure_block.get("proposed_instrument"),
        )
        payload["program_audit"] = audit.to_dict()
        # In the chain, not beside it. As a sibling key, a breach left the
        # document's own evidence reading "observed" while the audit
        # nested inside it read "assumed" — a consumer checking the top
        # was told the document was clean.
        comparison = replace(
            comparison,
            evidence=comparison.evidence.with_sources(audit.evidence),
        )
    return result_payload(comparison, **payload)


def interpolation_gap(config: dict[str, Any] | None) -> dict[str, Any]:
    """Both interpolations on the config's calibration nodes, and the gap between them.

    The one number the commands do not print. Both fits reprice every
    instrument; they differ only in the instantaneous forward between nodes,
    which no instrument constrains.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        A mapping with ``kind`` ``"interpolation_comparison"``, the sampled
        ``dates``, the instantaneous forward under ``log_linear`` and
        ``monotone_convex`` on each, their difference ``diff_bp``, and the
        comparison's own maxima.

    Raises:
        ConfigurationError: No config, or a malformed calibration block.
        CurveArbitrageError: Either bootstrap produced a non-monotone curve.
        BootstrapResidualError: Either bootstrap missed its tolerance.
    """
    config = require_config(config, "viz")
    as_of = as_date(config["as_of"])
    comparison = compare_interpolations(
        as_of,
        calibration_nodes(config, as_of),
        step_days=7,
        long_end_source=(config.get("curve") or {}).get("long_end_source"),
    )
    dates = comparison.sample_dates
    log_linear = [comparison.log_linear.instantaneous_forward(day) for day in dates]
    monotone = [comparison.monotone_convex.instantaneous_forward(day) for day in dates]
    return {
        "kind": "interpolation_comparison",
        "as_of": as_of.isoformat(),
        "dates": [day.isoformat() for day in dates],
        "log_linear": log_linear,
        "monotone_convex": monotone,
        "diff_bp": [(b - a) * 1e4 for a, b in zip(log_linear, monotone, strict=True)],
        "max_residual_bp": comparison.max_residual_bp,
        "max_forward_difference_bp": comparison.max_forward_difference_bp,
        "max_forward_difference_date": comparison.max_forward_difference_date.isoformat(),
        "max_df_difference": comparison.max_df_difference,
    }


def _engine_version() -> str:
    """The installed distribution's version, or ``"unknown"`` outside an install.

    Read from the distribution metadata rather than from the package root,
    because nothing inside the package imports the package root.
    """
    try:
        return metadata.version("finport-ratesengine")
    except metadata.PackageNotFoundError:
        return "unknown"


def viz(
    config: dict[str, Any] | None, fx_config: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """The payloads drawn on one HTML page, and a receipt naming every panel.

    Not a :data:`Command`: it takes two configs and returns HTML, so it hangs
    off the CLI parser directly and the MCP server does not serve it.

    Args:
        config: The curve, swap and hedge configuration. ``None`` is refused.
        fx_config: The configuration with the ``fx`` block. ``None`` leaves
            the FX and structures panels absent.

    Returns:
        ``(page, receipt)``. The receipt is a ``VizResult`` payload whose
        ``panels`` maps each panel to ``"ok"``, ``"absent"`` or
        ``"reported: <ExceptionType>"``; ``path`` is ``None`` here and the
        CLI fills it after writing.

    Raises:
        ConfigurationError: No config.
        RatesEngineError: Whatever an inner command raises, unchanged. Only
            the interpolation gap's own refusals are reported instead.
    """
    config = require_config(config, "viz")
    payloads: dict[str, dict[str, Any] | None] = {}
    panels: dict[str, str] = {}
    payloads["bootstrap"] = bootstrap(config)
    payloads["price"] = price(config)
    payloads["hedge"] = hedge(config)
    panels.update(bootstrap="ok", price="ok", hedge="ok")
    try:
        payloads["interp"] = interpolation_gap(config)
        panels["interp"] = "ok"
    except (CurveArbitrageError, BootstrapResidualError) as exc:
        payloads["interp"] = {
            "kind": "interpolation_comparison",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        panels["interp"] = f"reported: {type(exc).__name__}"
    if fx_config is None:
        payloads["fx"] = None
        payloads["structures"] = None
        panels.update(fx="absent", structures="absent")
    else:
        payloads["fx"] = fx_forward(fx_config)
        payloads["structures"] = hedge_structures(fx_config)
        panels.update(fx="ok", structures="ok")
    payloads["meta"] = {
        "engine_version": _engine_version(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "as_of": as_date(config["as_of"]).isoformat(),
    }
    page = render_page(payloads)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "result_type": "VizResult",
        "path": None,
        "bytes": len(page.encode("utf-8")),
        "panels": {key: panels[key] for key in PANELS},
        "evidence": None,
    }
    return page, receipt


Command = Callable[[dict[str, Any] | None], dict[str, Any]]
"""A use case: an optional configuration mapping in, a JSON-ready payload out."""

COMMANDS: dict[str, Command] = {
    "describe": describe,
    "list-instruments": list_instruments,
    "bootstrap": bootstrap,
    "price": price,
    "hedge": hedge,
    "fx-forward": fx_forward,
    "hedge-structures": hedge_structures,
}
"""Every use case by the name each interface exposes it under.

The CLI builds one subcommand per entry and the MCP server one tool per entry,
so a command added here reaches both, and neither can drift from the other.
"""
