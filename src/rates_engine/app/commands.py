"""The use cases, one function each, and the table every interface dispatches through.

Every function has the same signature -- an optional configuration mapping
in, a JSON-ready payload out -- which is what lets the CLI and the MCP server
expose :data:`COMMANDS` unchanged. A failure raises the named refusal from
:mod:`rates_engine.core.errors`; turning it into an exit code or a tool error
is the adapter's job, not this module's.

**The config is checked before anything is computed.** Every function that
reads a config starts with :func:`~rates_engine.app.config.require_config`,
which checks the whole document against
:data:`~rates_engine.app.config.CONFIG_SCHEMA` and hands back a clean copy, so
the check holds however the function is reached: through :data:`COMMANDS`,
or called directly.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from rates_engine.app.builders import (
    calibration_nodes,
    fixings_snapshot,
    flat_curve,
    fx_inputs,
    ois_swap,
)
from rates_engine.app.config import CONFIG_SCHEMA, as_date, require_config
from rates_engine.conventions.currency_pair import USDMXN
from rates_engine.conventions.indices import INDICES, UNRESOLVED_MXN
from rates_engine.core.errors import (
    ConfigurationError,
    MissingFixingError,
    UndefinedDurationError,
)
from rates_engine.core.errors import __all__ as EXCEPTION_NAMES
from rates_engine.core.money import Currency
from rates_engine.core.results import SCHEMA_VERSION
from rates_engine.curves.bootstrap import FuturesNode, bootstrap_discount_curve
from rates_engine.curves.discount import CurveSet
from rates_engine.curves.views import all_views
from rates_engine.hedging.futures_strip import (
    DEFAULT_SHOCKS_BP,
    _contract_sizing,
    shock_table,
    strip_hedge,
)
from rates_engine.hedging.fx_structures import (
    Exposure,
    ExposureDirection,
    StructureQuote,
    compare_structures,
)
from rates_engine.hedging.program import audit_hedge, load_program
from rates_engine.models.fx_delta import DeltaBasis, PremiumAdjustment
from rates_engine.pricing.calibration import SwapQuoteNode
from rates_engine.pricing.fx_forward import forward_from_curves
from rates_engine.pricing.linear import annuity, par_rate, pv
from rates_engine.reporting.payloads import result_payload
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

if TYPE_CHECKING:
    from datetime import date

    from rates_engine.app.builders import FXInputs
    from rates_engine.core.evidence import Evidence
    from rates_engine.instruments.swaps import OISSwap
    from rates_engine.market.snapshot import MarketSnapshot
    from rates_engine.pricing.fx_forward import FXForwardResult

__all__ = [
    "COMMANDS",
    "CONFIG_FREE",
    "Command",
    "describe",
    "list_instruments",
    "bootstrap",
    "price",
    "hedge",
    "fx_forward",
    "hedge_structures",
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
        # SIFMA for SOFR fixings and dollar rolls; the Federal Reserve's for the
        # dollar leg of a USD/MXN spot settlement (conventions.settlement).
        "calendars": ["SIFMA_US", "FEDERAL_RESERVE"],
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
        "config": {
            "format": "JSON; YAML with the config extra",
            "commands_without_config": sorted(CONFIG_FREE),
            "unknown_keys": (
                "refused: every key in the file, in every block, has to be one "
                "documented here, or the command raises ConfigurationError naming its "
                "path and the closest accepted key, before computing anything."
            ),
            "comments": "keys starting with '_' are ignored at any depth",
            "nulls": (
                "null is the same as leaving the key out where its default is null, "
                "and missing where the key is required; anywhere else it is refused"
            ),
            "empty_blocks": (
                "an optional block that is empty, or holds only comments, is the same "
                "as leaving it out; a required block, or a list entry, still names the "
                "keys it lacks"
            ),
            "lists": (
                "a key whose type is 'list of numbers' or 'list of strings' takes a "
                "JSON list of those values; a string or a number there is refused"
            ),
            # A copy: the payload is the caller's to edit, the schema is what
            # every later config in the process is checked against.
            "keys": copy.deepcopy(CONFIG_SCHEMA),
        },
        "evidence": None,
    }


def bootstrap(config: dict[str, Any] | None) -> dict[str, Any]:
    """Bootstrap the discount curve and export its four views.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``BootstrapResult`` payload with a ``views`` block holding the four views.

    Raises:
        MissingFixingError: A ``curve.futures`` entry has started, or a
            ``curve.par_swaps`` entry starts before ``as_of`` once rolled;
            the message names the entry and what to give instead.
    """
    config = require_config(config, "bootstrap")
    as_of = as_date(config["as_of"])
    instruments = calibration_nodes(config, as_of)
    _refuse_history_in_curve(instruments, as_of)
    result = bootstrap_discount_curve(
        as_of,
        instruments,
        long_end_source=(config.get("curve") or {}).get("long_end_source"),
        strict=bool((config.get("curve") or {}).get("strict", True)),
    )
    views = all_views(result.curve, source_evidence=result.evidence)
    return result_payload(result, views=views.to_dict(), command="bootstrap")


_PYTHON_REMEDY = re.compile(r"\s*Pass the published fixings as fixings=.*$", re.DOTALL)

_CONFIG_REMEDY = (
    "In a config, the history goes under fixings.SOFR.values: one {\"date\", \"rate\"} "
    "entry per SIFMA business day the swap has accrued over, the rate as a decimal "
    "(0.043 for 4.30%); `rateng describe --json` documents the block."
)


def _config_remedy(error: MissingFixingError) -> MissingFixingError:
    """The swap's refusal with the remedy a config can use in place of the Python one.

    The pricer's message ends by telling a Python caller to pass
    ``fixings=MarketSnapshot(...)``, which a file cannot do. What is missing
    is kept, and the key that supplies it replaces the keyword. Only a
    refusal of the configured swap's own valuation is rewritten so: the
    ``fixings`` block reaches the swap and nothing else, and a curve entry
    that needs history is refused before, by :func:`_refuse_history_in_curve`.
    """
    reason = _PYTHON_REMEDY.sub("", str(error)).rstrip(" .")
    return MissingFixingError(f"{reason}. {_CONFIG_REMEDY}")


def _refuse_history_in_curve(instruments: tuple[Any, ...], as_of: date) -> None:
    """Refuse a curve entry that would need fixings, naming it and what to give instead.

    A curve node takes no fixings: the ``fixings`` block prices the swap. A
    futures contract that has started, or a par swap whose schedule rolls
    its start before ``as_of``, used to reach the bootstrap and come back
    as the swap's refusal, rewritten to "the history goes under
    fixings.SOFR.values": a key that cannot fix either, so a user who
    followed it looped. Checked before anything is built, so every
    ``MissingFixingError`` after it is the swap's own.

    Raises:
        MissingFixingError: An entry needs history. The message names
            ``curve.futures[i]`` or ``curve.par_swaps[i]`` and the way out:
            the rest of a started contract's period goes in ``curve.stub``;
            a par quote is a swap that starts on or after ``as_of``.
    """
    futures = [node for node in instruments if isinstance(node, FuturesNode)]
    for number, node in enumerate(futures):
        if node.start < as_of:
            raise MissingFixingError(
                f"curve.futures[{number}] ({node.label}) runs from {node.start} to {node.end} "
                f"and as_of is {as_of}: the contract has started, and the part of its period "
                "before as_of is history, which a curve node cannot be calibrated on. The "
                "fixings block prices the swap, not the curve. Drop the entry and give the "
                f'rest of its period as curve.stub: {{"end": "{node.end}", "accrual_factor": '
                "the growth from as_of to that end}, the contract's 1 + F x tau over its "
                f"whole period divided by the SOFR compounded from {node.start} up to as_of."
            )
    quotes = [node for node in instruments if isinstance(node, SwapQuoteNode)]
    for number, quote in enumerate(quotes):
        swap = quote.swap
        first = swap.span[0]
        if first >= as_of:
            continue
        calendar = swap.roll_calendar
        rolled = (
            f"starts on {first} once its schedule rolls {swap.effective}"
            if first != swap.effective
            else f"starts on {first}"
        )
        open_today = calendar.is_business_day(as_of)
        opening = as_of if open_today else calendar.next_business_day(as_of)
        weekend = (
            ""
            if open_today
            else (
                f" Or value the curve as of {calendar.previous_business_day(as_of)}, the "
                "business day its quotes are from."
            )
        )
        name = f"curve.par_swaps[{number}]" + (f" ({quote.label})" if quote.label else "")
        raise MissingFixingError(
            f"{name} {rolled}, before as_of {as_of}, so its rate would rest on SOFR "
            "fixings, and a curve quote takes none: the fixings block prices the swap, not "
            "the curve, and adding the fixing changes nothing. A par quote is the rate of a "
            "swap that starts on or after as_of; give its effective date as a business day "
            f"on or after as_of, {opening} or its spot date.{weekend}"
        )


def price(config: dict[str, Any] | None) -> dict[str, Any]:
    """Price the configured swap and report every risk measure that is defined.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.
            A swap that has started needs its SOFR history in the
            ``fixings`` block.

    Returns:
        The ``pv`` payload with a ``measures`` block. A measure undefined on
        this swap is reported as refused rather than omitted.

    Raises:
        MissingFixingError: The swap has started and the ``fixings`` block
            does not hold the history it needs, and the message names that
            key; or a curve entry needs history, and the message names the
            entry (as for ``bootstrap``).
    """
    config = require_config(config, "price")
    as_of = as_date(config["as_of"])
    instruments = calibration_nodes(config, as_of)
    _refuse_history_in_curve(instruments, as_of)
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
    fixings = fixings_snapshot(config, as_of, boot.curve)
    try:
        return _price_measures(config, swap, curve_set, sources, fixings)
    except MissingFixingError as error:
        raise _config_remedy(error) from error


def _price_measures(
    config: dict[str, Any],
    swap: OISSwap,
    curve_set: CurveSet,
    sources: tuple[Evidence, ...],
    fixings: MarketSnapshot | None,
) -> dict[str, Any]:
    """The ``price`` payload: the swap's value and every risk measure defined on it."""
    value = pv(swap, curve_set, source_evidence=sources, fixings=fixings)
    measures: dict[str, Any] = {
        "pv": value.to_dict(),
        "par_rate": par_rate(swap, curve_set, source_evidence=sources, fixings=fixings).to_dict(),
        "annuity": annuity(swap, curve_set, source_evidence=sources).to_dict(),
        "dv01": dv01(swap, curve_set, source_evidence=sources, fixings=fixings).to_dict(),
        "pvbp": pvbp(swap, curve_set, source_evidence=sources, fixings=fixings).to_dict(),
        "money_duration": money_duration(
            swap, curve_set, source_evidence=sources, fixings=fixings
        ).to_dict(),
        "money_convexity": money_convexity(
            swap, curve_set, source_evidence=sources, fixings=fixings
        ).to_dict(),
    }
    # Normalised measures are undefined on a par swap. Reporting the refusal is
    # the honest answer, and it keeps the key present rather than missing.
    for name, function in (
        ("effective_duration", effective_duration),
        ("effective_convexity", effective_convexity),
    ):
        try:
            measures[name] = function(
                swap, curve_set, source_evidence=sources, fixings=fixings
            ).to_dict()
        except UndefinedDurationError as exc:
            measures[name] = {"value": None, "refused": str(exc)}

    key_tenors = (config.get("risk") or {}).get("key_tenors")
    measures["key_rate_dv01"] = (
        key_rate_dv01(
            swap,
            curve_set,
            tuple(float(t) for t in key_tenors),
            source_evidence=sources,
            fixings=fixings,
        ).to_dict()
        if key_tenors
        else None
    )
    return {**value.to_dict(), "measures": measures, "command": "price"}


def hedge(config: dict[str, Any] | None) -> dict[str, Any]:
    """Size the futures strip against the swap and shock the hedged position.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.
            The optional ``hedge`` block takes ``shocks_bp`` (basis points),
            ``contract_dv01`` (USD per basis point, for every contract;
            left out, each futures entry is sized at its own contract's
            tick, SR1 USD 41.67 or SR3 USD 25.00) and
            ``require_full_coverage`` (``true`` by default; ``false`` sizes a
            strip that leaves part of the swap uncovered). A swap that
            has started needs its SOFR history in the ``fixings`` block, and
            its coverage is checked from ``as_of``, with ``curve.stub``
            covering ``as_of`` to its end.

    Returns:
        The ``HedgeResult`` payload with a ``shock_table`` block.

    Raises:
        ConfigurationError: ``hedge.require_full_coverage`` is not a boolean;
            ``hedge.shocks_bp`` contains a zero shock; a
            ``curve.futures[i].contract`` contradicts that entry's dates; or
            ``hedge.contract_dv01`` contradicts an entry's contract. Each
            message starts with the key at fault.
        MissingFixingError: The swap has started and the ``fixings`` block
            does not hold the history it needs, and the message names that
            key; or a curve entry needs history, and the message names the
            entry (as for ``bootstrap``).
    """
    config = require_config(config, "hedge")
    as_of = as_date(config["as_of"])
    instruments = calibration_nodes(config, as_of)
    _refuse_history_in_curve(instruments, as_of)
    swap = ois_swap(config)
    settings = config.get("hedge") or {}
    # No default tick here. Passing SR3's 25.00 whenever the block left it
    # out sized every SR1 entry 1.67 times over; None lets each contract be
    # sized at its own.
    contract_dv01 = settings.get("contract_dv01")
    # Checked rather than coerced: bool("false") is True, so coercing would
    # enforce the check a config asked to waive.
    require_full_coverage = settings.get("require_full_coverage", True)
    if not isinstance(require_full_coverage, bool):
        raise ConfigurationError(
            f"hedge.require_full_coverage is {require_full_coverage!r}; it must be true or "
            "false (true, the default, refuses a strip with a gap in the swap's span)"
        )
    # Checked here as well as in shock_table, and before the hedge is sized,
    # so that the refusal names the key in the file that holds the zero.
    shocks = tuple(float(s) for s in settings.get("shocks_bp", DEFAULT_SHOCKS_BP))
    zeros = [index for index, shock in enumerate(shocks) if shock == 0.0]
    if zeros:
        raise ConfigurationError(
            f"hedge.shocks_bp[{zeros[0]}] is 0. A zero shock moves nothing and its implied "
            "money convexity, 2 x net / shock^2, is undefined; drop it from the grid"
        )
    # The contract checks strip_hedge makes, made here first and by the same
    # function, entry by entry, for the same reason: the library knows a
    # contract by its label, and the refusal should name the key in the file.
    # The first pass leaves the tick out, so a contract that contradicts its
    # own dates is told apart from a contract_dv01 that contradicts it.
    tick = float(contract_dv01) if contract_dv01 is not None else None
    futures = [node for node in instruments if isinstance(node, FuturesNode)]
    for number, node in enumerate(futures):
        try:
            _contract_sizing(node, node.label, None)
        except ConfigurationError as error:
            raise ConfigurationError(f"curve.futures[{number}].contract: {error}") from error
        if tick is None:
            continue
        try:
            _contract_sizing(node, node.label, tick)
        except ConfigurationError as error:
            entry = f"curve.futures[{number}]" + (".contract" if node.contract else "")
            raise ConfigurationError(f"hedge.contract_dv01 contradicts {entry}: {error}") from error
    long_end_source = (config.get("curve") or {}).get("long_end_source")
    # The fixings are checked against the curve before the hedge is sized on
    # them, which takes the curve: one bootstrap more, only when the file
    # carries fixings, against the two per contract the sizing makes.
    fixings = fixings_snapshot(
        config,
        as_of,
        bootstrap_discount_curve(as_of, instruments, long_end_source=long_end_source).curve
        if config.get("fixings")
        else None,
    )
    try:
        result = strip_hedge(
            swap,
            instruments,
            as_of=as_of,
            long_end_source=long_end_source,
            contract_dv01=float(contract_dv01) if contract_dv01 is not None else None,
            require_full_coverage=require_full_coverage,
            fixings=fixings,
        )
        table = shock_table(result, shocks)
    except MissingFixingError as error:
        raise _config_remedy(error) from error
    return result_payload(result, shock_table=table.to_dict(), command="hedge")


def _usdmxn_forward(inputs: FXInputs) -> FXForwardResult:
    """The one USD/MXN forward both FX commands report, from the ``fx`` block.

    Parity from the spot date on two flat curves, with the block's basis and
    the rates' convention in the evidence. Shared rather than written out in
    each command: when hedge-structures built its own, it ignored
    ``basis_bp`` and measured from ``as_of``, and its forward row sat 1,941
    pips from fx-forward's at a year on -100 bp.
    """
    return forward_from_curves(
        USDMXN,
        inputs.spot,
        inputs.delivery,
        flat_curve(inputs.as_of, inputs.r_domestic, Currency.MXN, inputs.years),
        flat_curve(inputs.as_of, inputs.r_foreign, Currency.USD, inputs.years),
        basis_bp=inputs.basis_bp,
        inputs=inputs.provenance,
    )


def fx_forward(config: dict[str, Any] | None) -> dict[str, Any]:
    """The USD/MXN forward, with the cross-currency basis kept separate.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``FXForwardResult`` payload, the basis reported apart from the
        covered-interest-parity forward, parity run from the spot date, and
        the two rates' convention (continuous, ACT/365F) in the evidence.
    """
    config = require_config(config, "fx-forward")
    return result_payload(_usdmxn_forward(fx_inputs(config)), command="fx-forward")


def hedge_structures(config: dict[str, Any] | None) -> dict[str, Any]:
    """Compare the eight hedge structures against a transaction exposure.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        The ``StructureComparison`` payload, with a ``program_audit`` block
        when the config carries a ``hedge_program``.
    """
    # require_config has refused a missing exposure block or fx.volatility,
    # both required for this command, by path.
    config = require_config(config, "hedge-structures")
    inputs = fx_inputs(config)
    exposure_block = config["exposure"]
    # The forward fx-forward reports. Its evidence carries the peso curve's
    # assumptions into every row, and its span is the structures' expiry:
    # the premium is paid on the spot date, and an option delivering on the
    # settlement date expires the spot lag before it.
    forward = _usdmxn_forward(inputs)
    quote = StructureQuote(
        spot=inputs.spot,
        expiry=forward.year_fraction,
        r_domestic=inputs.r_domestic,
        r_foreign=inputs.r_foreign,
        volatility=float(inputs.block["volatility"]),
        evidence=(forward.evidence,),
        basis_bp=inputs.basis_bp,
    )
    exposure = Exposure(
        amount=float(exposure_block["amount"]),
        direction=ExposureDirection(str(exposure_block["direction"])),
        settlement=inputs.delivery,
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


Command = Callable[[dict[str, Any] | None], dict[str, Any]]
"""A use case: an optional configuration mapping in, a JSON-ready payload out."""

CONFIG_FREE: frozenset[str] = frozenset({"describe", "list-instruments"})
"""The commands that answer from the build itself and read no configuration.

The CLI does not open a ``--config`` passed to them, and neither calls
:func:`~rates_engine.app.config.require_config`, so a config handed to them
over MCP is ignored, as their docstrings say, rather than checked.
"""

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
