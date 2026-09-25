"""``rateng``: the use cases in :mod:`rates_engine.app.commands`, on stdout, as one document.

An adapter and nothing else: argument parsing, the config file, and the
stdout/stderr contract. Every command takes ``--json``. With it, stdout
carries exactly one JSON document and everything a human would want to read
goes to stderr, so a pipeline never has to strip narration out of its input.
A failure before a result is produced still puts one document on stdout --
``{"error": ..., "exit_code": ...}`` -- and the traceback goes to stderr.

Exit codes follow the exception taxonomy in :mod:`rates_engine.core.errors`:
``1`` when the inputs cannot support the calculation, ``2`` when the
calculation itself is impossible on inputs that are fine.

**Config format.** JSON is native; YAML needs the ``config`` extra. See
:func:`rates_engine.app.config.load_config`.

The one subcommand outside the table is ``viz``, which writes an HTML page
and prints a receipt naming what landed.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

from rates_engine.app.commands import COMMANDS, viz
from rates_engine.app.config import load_config
from rates_engine.reporting.payloads import dumps, error_payload

__all__ = ["main", "build_parser"]

_EXIT_OK = 0


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for ``rateng``.

    Returns:
        The parser, with one subcommand per entry in the command table and
        ``viz``, which is not a table entry because it writes a file rather
        than printing a payload.
    """
    parser = argparse.ArgumentParser(
        prog="rateng",
        description="Deterministic SOFR rates engine: curves, futures, swaps, risk, hedging.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
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
    page = subparsers.add_parser("viz", help="draw the payloads on one HTML page")
    page.add_argument(
        "--config", required=True, help="JSON or YAML configuration for the curve, swap and hedge"
    )
    page.add_argument(
        "--fx-config",
        required=False,
        help="configuration with the fx block; without it the FX panels stay empty",
    )
    page.add_argument("--out", required=True, help="path of the HTML file to write")
    page.add_argument(
        "--json",
        action="store_true",
        help="write one JSON receipt to stdout and all narration to stderr",
    )
    return parser


def _write_page(args: argparse.Namespace, config: dict[str, Any] | None) -> dict[str, Any]:
    """Render the page, write it, and return the receipt with ``path`` filled in."""
    fx_config = load_config(args.fx_config) if args.fx_config else None
    page, receipt = viz(config, fx_config)
    out = Path(args.out)
    out.write_text(page, encoding="utf-8")
    receipt["path"] = str(out)
    return receipt


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
        if args.command == "viz":
            payload = _write_page(args, config)
        else:
            payload = COMMANDS[args.command](config)
    except BaseException as exc:  # noqa: BLE001 - re-raised below unless --json
        if not args.json:
            raise
        traceback.print_exc(file=sys.stderr)
        failure = error_payload(exc)
        sys.stdout.write(dumps(failure) + "\n")
        return int(failure["exit_code"])

    if not args.json:
        where = f" -> {args.out}" if args.command == "viz" else ""
        print(f"{args.command}: ok{where}", file=sys.stderr)
    sys.stdout.write(dumps(payload) + "\n")
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
