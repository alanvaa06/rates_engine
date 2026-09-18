"""``rateng``: the same payloads the library returns, on stdout, as one document.

Four commands, all of which take ``--json``. The contract is narrow on
purpose. With ``--json``, stdout carries exactly one JSON document and
everything a human would want to read goes to stderr, so a pipeline never has
to strip narration out of its input. A failure before a result is produced
still puts one document on stdout — ``{"error": ..., "exit_code": ...}`` — and
the traceback goes to stderr.

Exit codes follow the exception taxonomy in :mod:`rates_engine.errors`: ``1``
when the inputs cannot support the calculation, ``2`` when the calculation
itself is impossible on inputs that are fine.

**Config format.** JSON is native, so every command runs on a core install.
YAML needs ``pip install "finport-ratesengine[config]"``, and asking for a
``.yaml`` file without it names that command rather than failing on an import.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from rates_engine.conventions.daycount import year_fraction
from rates_engine.convexity import ConvexityModel, convexity_adjustment
from rates_engine.curves.bootstrap import (
    FuturesNode,
    ParSwapNode,
    RealizedStubNode,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import CURVE_TIME_BASIS, CurveSet, DiscountCurve
from rates_engine.curves.mxn import UNRESOLVED_MXN
from rates_engine.curves.views import all_views
from rates_engine.errors import (
    ConfigurationError,
    MissingDependencyError,
    UndefinedDurationError,
)
from rates_engine.errors import __all__ as EXCEPTION_NAMES
from rates_engine.evidence import DataQuality, Provenance
from rates_engine.fx.delta import DeltaBasis, PremiumAdjustment
from rates_engine.fx.forward import forward_from_curves
from rates_engine.fx.quote import USDMXN
from rates_engine.fx.vannavolga import ATMConvention
from rates_engine.hedge_program import audit_hedge, load_program
from rates_engine.hedging import DEFAULT_SHOCKS_BP, SR3_DV01, shock_table, strip_hedge
from rates_engine.hedging_structures import (
    Exposure,
    ExposureDirection,
    StructureQuote,
    compare_structures,
)
from rates_engine.instruments.swaps import OISSwap, Side
from rates_engine.money import Currency
from rates_engine.pricing import annuity, dv01, par_rate, pv
from rates_engine.reporting.payloads import dumps, error_payload, result_payload
from rates_engine.results import SCHEMA_VERSION
from rates_engine.risk import (
    INTERPOLATION_CAVEAT,
    effective_convexity,
    effective_duration,
    key_rate_dv01,
    money_convexity,
    money_duration,
    pvbp,
)
from rates_engine.volatility.units import VolUnits

__all__ = ["main", "build_parser", "load_config"]

_EXIT_OK = 0


def load_config(path: str | Path) -> dict[str, Any]:
    """Read a JSON or YAML configuration file.

    Args:
        path: Path to a ``.json``, ``.yaml`` or ``.yml`` file.

    Returns:
        The parsed mapping.

    Raises:
        MissingDependencyError: A YAML file was given and ``pyyaml`` is not
            installed. The message names the install command.
        FileNotFoundError: The path does not exist.
        ValueError: The document is not a mapping.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise MissingDependencyError(
                f"reading {path.name} needs PyYAML: "
                'pip install "finport-ratesengine[config]". JSON configs need no extra.'
            ) from exc
        parsed = yaml.safe_load(text)
    else:
        parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path.name} must contain a mapping at the top level")
    return parsed


def _as_date(value: str | date) -> date:
    """Parse a config date, accepting what either loader hands over.

    JSON has no date type, so a date arrives as a string. YAML does, and
    ``safe_load`` turns ``2026-01-15`` into a ``datetime.date`` before this
    code ever sees it. Accepting both is what stops the same config behaving
    differently depending on the extension it was saved under.
    """
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _require_config(config: dict[str, Any] | None, command: str) -> dict[str, Any]:
    """The config, or a named refusal saying which command needed one.

    Every handler in :data:`_COMMANDS` has the same signature so that the MCP
    server can expose the table unchanged. Two of them genuinely take no
    config; the other three go through here, so that calling them without one
    is a sentence rather than whichever ``KeyError`` happens to come first.

    Args:
        config: The configuration mapping, or ``None``.
        command: The command's name, for the message.

    Returns:
        The configuration mapping.

    Raises:
        ConfigurationError: ``config`` is ``None``.
    """
    if config is None:
        raise ConfigurationError(
            f"{command} needs a configuration and was given none. "
            "describe and list-instruments are the two commands that answer "
            "without one."
        )
    return config


def _build_instruments(config: dict[str, Any], as_of: date) -> tuple[Any, ...]:
    """Build calibration instruments from the ``curve`` block of a config."""
    curve = config.get("curve") or {}
    instruments: list[Any] = []

    stub = curve.get("stub")
    if stub:
        instruments.append(
            RealizedStubNode(end=_as_date(stub["end"]), accrual_factor=float(stub["accrual_factor"]))
        )

    convexity = curve.get("convexity") or {}
    model = convexity.get("model", ConvexityModel.NONE)
    sigma = float(convexity.get("sigma", 0.0))
    kappa = convexity.get("kappa")


    for entry in curve.get("futures", ()):
        start, end = _as_date(entry["start"]), _as_date(entry["end"])
        adjustment = convexity_adjustment(
            year_fraction(as_of, start, CURVE_TIME_BASIS),
            year_fraction(as_of, end, CURVE_TIME_BASIS),
            model=model,
            sigma=sigma,
            kappa=float(kappa) if kappa is not None else None,
        )
        instruments.append(
            FuturesNode(
                start=start,
                end=end,
                forward_rate=(100.0 - float(entry["price"])) / 100.0 - adjustment.adjustment,
                label=str(entry.get("label", f"future:{entry['start']}")),
                convexity={
                    "model": model,
                    "sigma": sigma,
                    "kappa": kappa,
                    "adjustment_bp": adjustment.adjustment_bp,
                },
            )
        )

    for entry in curve.get("par_swaps", ()):
        payments = tuple(_as_date(d) for d in entry["payment_dates"])
        instruments.append(
            ParSwapNode(
                start=_as_date(entry["start"]),
                payment_dates=payments,
                year_fractions=tuple(float(x) for x in entry["year_fractions"]),
                quoted_rate=float(entry["rate"]),
                label=str(entry.get("label", f"par:{entry['payment_dates'][-1]}")),
                provenance=Provenance(
                    source=str(entry.get("source", "file")),
                    series_id=str(entry.get("label", "par_swap")),
                    instrument_kind=str(entry.get("instrument_kind", "ois_par")),
                    data_quality=DataQuality(entry.get("data_quality", "observed")),
                ),
            )
        )
    return tuple(instruments)


def _build_swap(config: dict[str, Any]) -> OISSwap:
    """Build the OIS swap described by the ``swap`` block of a config."""
    spec = config["swap"]
    return OISSwap(
        effective=_as_date(spec["effective"]),
        maturity=_as_date(spec["maturity"]),
        fixed_rate=float(spec["fixed_rate"]),
        notional=float(spec.get("notional", 1_000_000.0)),
        side=str(spec.get("side", Side.PAYER)),
        frequency_months=int(spec.get("frequency_months", 12)),
        payment_lag_days=int(spec.get("payment_lag_days", 2)),
    )


def _command_list_instruments(_config: dict[str, Any] | None) -> dict[str, Any]:
    """List the instruments this build can price, and what each one needs.

    Exists so that the MCP tool of the same name has a command to be equal
    to: PRD-002 AC-6.1 holds the server to returning exactly the payload of
    the equivalent ``--json`` command, which requires the equivalent command
    to exist.
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


def _command_describe(_config: dict[str, Any] | None) -> dict[str, Any]:
    """Describe the engine's surface without computing anything."""
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


def _command_bootstrap(config: dict[str, Any] | None) -> dict[str, Any]:
    """Bootstrap the discount curve and export its four views."""
    config = _require_config(config, "bootstrap")
    as_of = _as_date(config["as_of"])
    instruments = _build_instruments(config, as_of)
    result = bootstrap_discount_curve(
        as_of,
        instruments,
        long_end_source=(config.get("curve") or {}).get("long_end_source"),
        strict=bool((config.get("curve") or {}).get("strict", True)),
    )
    views = all_views(result.curve, source_evidence=result.evidence)
    return result_payload(result, views=views.to_dict(), command="bootstrap")


def _command_price(config: dict[str, Any] | None) -> dict[str, Any]:
    """Price the configured swap and report every risk measure that is defined."""
    config = _require_config(config, "price")
    as_of = _as_date(config["as_of"])
    instruments = _build_instruments(config, as_of)
    boot = bootstrap_discount_curve(
        as_of, instruments, long_end_source=(config.get("curve") or {}).get("long_end_source")
    )
    curve_set = CurveSet(boot.curve)
    swap = _build_swap(config)
    sources = (boot.evidence,)

    price = pv(swap, curve_set, source_evidence=sources)
    measures: dict[str, Any] = {
        "pv": price.to_dict(),
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
    return {**price.to_dict(), "measures": measures, "command": "price"}


def _command_hedge(config: dict[str, Any] | None) -> dict[str, Any]:
    """Size the futures strip against the swap and shock the hedged position."""
    config = _require_config(config, "hedge")
    as_of = _as_date(config["as_of"])
    instruments = _build_instruments(config, as_of)
    swap = _build_swap(config)
    settings = config.get("hedge") or {}
    hedge = strip_hedge(
        swap,
        instruments,
        as_of=as_of,
        long_end_source=(config.get("curve") or {}).get("long_end_source"),
        contract_dv01=float(settings.get("contract_dv01", SR3_DV01)),
    )
    shocks = tuple(float(s) for s in settings.get("shocks_bp", DEFAULT_SHOCKS_BP))
    table = shock_table(hedge, shocks)
    return result_payload(hedge, shock_table=table.to_dict(), command="hedge")


def _flat_curve(as_of: date, rate: float, currency: Currency, years: float) -> DiscountCurve:
    """A single-node continuous curve, for the FX commands.

    v3's FX commands take two rates rather than two bootstrapped curves,
    because a treasurer comparing hedge structures has a deposit rate to
    hand and not a calibration set. The curve is built here so the forward
    goes through the same discounting code every other price does, rather
    than through a second exponential written out in the command.
    """
    node = as_of + timedelta(days=max(1, round(365.0 * years)))
    span = year_fraction(as_of, node, CURVE_TIME_BASIS)
    return DiscountCurve(as_of, (node,), (math.exp(-rate * span),), currency=currency)


def _fx_inputs(config: dict[str, Any]) -> tuple[date, dict[str, Any], float]:
    block = config.get("fx") or {}
    if not block:
        raise ConfigurationError(
            "this command needs an `fx` block with spot, delivery, r_domestic and "
            "r_foreign. See `rateng describe --json` for the shape."
        )
    as_of = _as_date(config["as_of"])
    delivery = _as_date(block["delivery"])
    return as_of, block, year_fraction(as_of, delivery, CURVE_TIME_BASIS)


def _command_fx_forward(config: dict[str, Any] | None) -> dict[str, Any]:
    """The USD/MXN forward, with the cross-currency basis kept separate."""
    config = _require_config(config, "fx-forward")
    as_of, block, years = _fx_inputs(config)
    delivery = _as_date(block["delivery"])
    domestic = _flat_curve(as_of, float(block["r_domestic"]), Currency.MXN, years)
    foreign = _flat_curve(as_of, float(block["r_foreign"]), Currency.USD, years)
    result = forward_from_curves(
        USDMXN,
        float(block["spot"]),
        delivery,
        domestic,
        foreign,
        basis_bp=float(block.get("basis_bp", 0.0)),
    )
    return result_payload(result, command="fx-forward")


def _command_hedge_structures(config: dict[str, Any] | None) -> dict[str, Any]:
    """Compare the seven hedge structures against a transaction exposure."""
    config = _require_config(config, "hedge-structures")
    as_of, block, years = _fx_inputs(config)
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
        settlement=_as_date(block["delivery"]),
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


_COMMANDS: dict[str, Callable[[dict[str, Any] | None], dict[str, Any]]] = {
    "describe": _command_describe,
    "list-instruments": _command_list_instruments,
    "bootstrap": _command_bootstrap,
    "price": _command_price,
    "hedge": _command_hedge,
    "fx-forward": _command_fx_forward,
    "hedge-structures": _command_hedge_structures,
}


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for ``rateng``.

    Returns:
        The parser, with one subcommand per entry in the command table.
    """
    parser = argparse.ArgumentParser(
        prog="rateng",
        description="Deterministic SOFR rates engine: curves, futures, swaps, risk, hedging.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _COMMANDS:
        sub = subparsers.add_parser(name, help=f"{name} command")
        if name not in ("describe", "list-instruments"):
            sub.add_argument("--config", required=True, help="JSON or YAML configuration file")
        else:
            sub.add_argument(
                "--config", required=False, help="ignored by describe and list-instruments"
            )
        sub.add_argument(
            "--json",
            action="store_true",
            help="write one JSON document to stdout and all narration to stderr",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when the inputs are unusable, ``2`` when the
        calculation is impossible. With ``--json`` stdout holds exactly one
        JSON document either way.
    """
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config) if args.config else None
        payload = _COMMANDS[args.command](config)
    except BaseException as exc:  # noqa: BLE001 - re-raised below unless --json
        if not args.json:
            raise
        traceback.print_exc(file=sys.stderr)
        failure = error_payload(exc)
        sys.stdout.write(dumps(failure) + "\n")
        return int(failure["exit_code"])

    if args.json:
        sys.stdout.write(dumps(payload) + "\n")
    else:
        print(f"{args.command}: ok", file=sys.stderr)
        sys.stdout.write(dumps(payload) + "\n")
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
