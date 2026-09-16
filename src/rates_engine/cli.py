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
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Any

from rates_engine.convexity import ConvexityModel, convexity_adjustment
from rates_engine.curves.bootstrap import (
    FuturesNode,
    ParSwapNode,
    RealizedStubNode,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import CURVE_TIME_BASIS, CurveSet
from rates_engine.curves.views import all_views
from rates_engine.errors import MissingDependencyError, UndefinedDurationError
from rates_engine.evidence import DataQuality, Provenance
from rates_engine.hedging import DEFAULT_SHOCKS_BP, SR3_DV01, shock_table, strip_hedge
from rates_engine.instruments.swaps import OISSwap, Side
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

    from rates_engine.conventions.daycount import year_fraction

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


def _command_describe(_config: dict[str, Any] | None) -> dict[str, Any]:
    """Describe the engine's surface without computing anything."""
    from rates_engine import errors as error_module

    return {
        "schema_version": SCHEMA_VERSION,
        "result_type": "Description",
        "distribution": "finport-ratesengine",
        "import_name": "rates_engine",
        "console_script": "rateng",
        "commands": ["bootstrap", "price", "hedge", "describe"],
        "day_counts": ["ACT/360", "ACT/365F", "30/360"],
        "calendars": ["SIFMA_US"],
        "interpolation": ["log_linear_df"],
        "convexity_models": ["none", "ho_lee", "hull_white"],
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
        "exceptions": sorted(error_module.__all__),
        "exit_codes": {"0": "ok", "1": "inputs unusable", "2": "calculation impossible"},
        "key_rate_caveat": INTERPOLATION_CAVEAT,
        "evidence": None,
    }


def _command_bootstrap(config: dict[str, Any]) -> dict[str, Any]:
    """Bootstrap the discount curve and export its four views."""
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


def _command_price(config: dict[str, Any]) -> dict[str, Any]:
    """Price the configured swap and report every risk measure that is defined."""
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


def _command_hedge(config: dict[str, Any]) -> dict[str, Any]:
    """Size the futures strip against the swap and shock the hedged position."""
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


_COMMANDS = {
    "describe": _command_describe,
    "bootstrap": _command_bootstrap,
    "price": _command_price,
    "hedge": _command_hedge,
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
        if name != "describe":
            sub.add_argument("--config", required=True, help="JSON or YAML configuration file")
        else:
            sub.add_argument("--config", required=False, help="ignored by describe")
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
        payload = _COMMANDS[args.command](config)  # type: ignore[arg-type]
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
