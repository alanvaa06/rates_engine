"""``rateng``: the use cases in :mod:`rates_engine.app.commands`, on stdout, as one document.

An adapter and nothing else: argument parsing, the config file, and the
stdout/stderr contract. Every command takes ``--json``. With it, stdout
carries exactly one JSON document and everything a human would want to read
goes to stderr, so a pipeline never has to strip narration out of its input.
A failure before a result is produced still puts one document on stdout --
``{"error": ..., "exit_code": ...}`` -- and the traceback goes to stderr.
That includes a command line the parser rejects: a missing ``--config``, an
unknown flag or ``--json=1`` is a
:class:`~rates_engine.core.errors.ConfigurationError`, exit code 1, with the
usage on stderr. ``--help`` with ``--json`` is a ``Usage`` document, exit
code 0, with the help text on stderr. Without ``--json`` the parser behaves
as argparse always has: help on stdout, and usage with exit code 2 for an
error.

Exit codes follow the exception taxonomy in :mod:`rates_engine.core.errors`:
``1`` when the inputs cannot support the calculation, ``2`` when the
calculation itself is impossible on inputs that are fine.

**Config format.** JSON is native; YAML needs the ``config`` extra. See
:func:`rates_engine.app.config.load_config`. ``describe`` and
``list-instruments`` accept ``--config`` and do not open it.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any, NoReturn

from rates_engine.app.commands import COMMANDS, CONFIG_FREE
from rates_engine.app.config import load_config
from rates_engine.core.errors import ConfigurationError
from rates_engine.core.results import SCHEMA_VERSION
from rates_engine.reporting.payloads import dumps, error_payload

__all__ = ["main", "build_parser"]

_EXIT_OK = 0


class _Parser(argparse.ArgumentParser):
    """An argument parser that keeps stdout to one document under ``--json``.

    argparse writes to stdout or exits on its own in two places: a bad
    command line prints the usage and calls ``sys.exit(2)``, leaving stdout
    empty, and ``--help`` prints the help text there and exits 0. Under
    ``--json`` both break the one promise the flag makes. The error is raised
    as a :class:`ConfigurationError` for :func:`main` to turn into the error
    document; the help becomes a ``Usage`` document, its text going to
    stderr. Subcommand parsers are built from the same class, so they behave
    alike.
    """

    errors_as_documents = False

    def error(self, message: str) -> NoReturn:
        if not self.errors_as_documents:
            super().error(message)
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise ConfigurationError(
            f"{self.prog}: {message}. Run `{self.prog} --help` for the arguments it takes."
        )

    def print_help(self, file: Any = None) -> None:
        if not self.errors_as_documents:
            super().print_help(file)
            return
        text = self.format_help()
        sys.stderr.write(text)
        document = {
            "schema_version": SCHEMA_VERSION,
            "result_type": "Usage",
            "command": self.prog.removeprefix("rateng").strip() or None,
            "usage": self.format_usage().removeprefix("usage:").strip(),
            "help": text,
            "evidence": None,
        }
        sys.stdout.write(dumps(document) + "\n")


def build_parser(*, errors_as_documents: bool = False) -> argparse.ArgumentParser:
    """The argument parser for ``rateng``.

    Args:
        errors_as_documents: Raise :class:`ConfigurationError` on a bad
            command line instead of printing the usage and exiting with code
            2, and answer ``--help`` with a ``Usage`` document on stdout and
            the text on stderr, which is what :func:`main` asks for under
            ``--json``.

    Returns:
        The parser, with one subcommand per entry in the command table.
    """
    parser = _Parser(
        prog="rateng",
        description="Deterministic SOFR rates engine: curves, futures, swaps, risk, hedging.",
    )
    parser.errors_as_documents = errors_as_documents
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub = subparsers.add_parser(name, help=f"{name} command")
        sub.errors_as_documents = errors_as_documents
        if name not in CONFIG_FREE:
            sub.add_argument("--config", required=True, help="JSON or YAML configuration file")
        else:
            sub.add_argument(
                "--config",
                required=False,
                help="accepted and not opened: describe and list-instruments read no config",
            )
        sub.add_argument(
            "--json",
            action="store_true",
            help="write one JSON document to stdout and all narration to stderr",
        )
    return parser


def _asks_for_json(arguments: list[str]) -> bool:
    """Whether the command line asks for ``--json``, before it has been parsed.

    Read from the raw arguments because the question has to be answered for
    a command line the parser is about to reject. An abbreviation counts,
    because argparse accepts ``--js`` for ``--json``, and so does a value
    glued on with ``=``, which argparse rejects for a flag.
    """
    for argument in arguments:
        flag = argument.split("=", 1)[0]
        if len(flag) > 2 and "--json".startswith(flag):
            return True
    return False


def _write_failure(exc: BaseException) -> int:
    """Put the error document on stdout and return its exit code."""
    failure = error_payload(exc)
    sys.stdout.write(dumps(failure) + "\n")
    return int(failure["exit_code"])


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when the inputs are unusable, ``2`` when the
        calculation is impossible. With ``--json`` stdout holds exactly one
        JSON document either way, a rejected command line included.
    """
    arguments = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser(errors_as_documents=_asks_for_json(arguments))
    try:
        args = parser.parse_args(arguments)
    except ConfigurationError as exc:
        # Only raised under --json; the parser has already put the usage on
        # stderr, which is the narration, so there is no traceback to add.
        return _write_failure(exc)
    try:
        # The configless commands document --config as ignored, so it is
        # not opened: a path that does not exist must not fail them.
        reads_config = args.config and args.command not in CONFIG_FREE
        config = load_config(args.config) if reads_config else None
        payload = COMMANDS[args.command](config)
    except BaseException as exc:  # noqa: BLE001 - re-raised below unless --json
        if not args.json:
            raise
        traceback.print_exc(file=sys.stderr)
        return _write_failure(exc)

    if args.json:
        sys.stdout.write(dumps(payload) + "\n")
    else:
        print(f"{args.command}: ok", file=sys.stderr)
        sys.stdout.write(dumps(payload) + "\n")
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
