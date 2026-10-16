"""Reading a configuration document, and refusing one that cannot be used.

**Format.** JSON is native, so every command runs on a core install. YAML
needs ``pip install "finport-ratesengine[config]"``, and asking for a
``.yaml`` file without it names that command rather than failing on an
import.

**Every key is accounted for.** A misspelt key used to fall back to its
default without a word: ``notionl`` priced a hundredth of the trade asked
for, ``convexty`` switched the convexity adjustment off, and both exited 0.
:func:`track_reads` wraps a config in a mapping that records which keys the
command looked up, and :func:`refuse_unknown_keys` turns anything the command
reached but neither read nor finds in :data:`CONFIG_SCHEMA` into a
:class:`~rates_engine.core.errors.ConfigurationError` naming its path. The
check is driven by what the code reads rather than by a list of allowed keys
per command, so a key a command starts reading is accepted the moment it is
read; the schema only decides whether a key the command did *not* read
belongs to another command (one file serves ``bootstrap``, ``price`` and
``hedge``) or to nobody.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from rates_engine.core.errors import ConfigurationError, MissingDependencyError
from rates_engine.hedging.futures_strip import DEFAULT_SHOCKS_BP, SR3_DV01

__all__ = [
    "load_config",
    "as_date",
    "require_config",
    "CONFIG_SCHEMA",
    "track_reads",
    "refuse_unknown_keys",
]

_CURVE_COMMANDS = ["bootstrap", "price", "hedge"]
_FX_COMMANDS = ["fx-forward", "hedge-structures"]

_V03_PAR_SWAP = (
    "the v0.3 par-swap shape (start, payment_dates, year_fractions) is refused with "
    "the migration: give effective, maturity and rate, and the dates are generated "
    "with the payment lag the old shape ignored"
)

CONFIG_SCHEMA: dict[str, Any] = {
    "as_of": {
        "used_by": [*_CURVE_COMMANDS, *_FX_COMMANDS],
        "required": True,
        "description": (
            "Valuation date, ISO 8601 (YYYY-MM-DD); YAML may hand it over as a date. "
            "Every curve time and every forward is measured from it."
        ),
    },
    "curve": {
        "used_by": _CURVE_COMMANDS,
        "description": (
            "The SOFR calibration set the discount curve is bootstrapped from: a "
            "realised stub, SR3 futures and OIS par quotes, one curve node each, "
            "solved in maturity order."
        ),
        "keys": {
            "stub": {
                "description": (
                    "The SOFR accrual already realised between as_of and the first "
                    "futures period."
                ),
                "keys": {
                    "end": {
                        "required": True,
                        "description": "Date the stub runs to, normally the first futures start.",
                    },
                    "accrual_factor": {
                        "required": True,
                        "description": (
                            "Realised compounded growth over the stub, 1 + r * tau with tau "
                            "on ACT/360: a factor such as 1.0069, not a rate."
                        ),
                    },
                },
            },
            "futures": {
                "description": (
                    "SR3 futures, one entry per reference quarter; each pins the curve "
                    "at its end date."
                ),
                "items": {
                    "start": {
                        "required": True,
                        "description": "Reference period start, an IMM date (YYYY-MM-DD).",
                    },
                    "end": {
                        "required": True,
                        "description": (
                            "Reference period end, the next IMM date: the curve node the "
                            "contract pins."
                        ),
                    },
                    "price": {
                        "required": True,
                        "description": (
                            "Settlement price, 100 minus the rate in percent, so 96.0 is 4%; "
                            "convexity-adjusted per the convexity block before it enters "
                            "the curve."
                        ),
                    },
                    "label": {
                        "default": "future:<start>",
                        "description": (
                            "Name in the residuals, the evidence and the hedge's contract "
                            "table."
                        ),
                    },
                },
            },
            "par_swaps": {
                "description": (
                    "SOFR OIS par quotes, each given as the swap it quotes and calibrated "
                    "with the pricer itself, payment lag included."
                ),
                "items": {
                    "effective": {
                        "required": True,
                        "description": "Start date of the swap the quote is for.",
                    },
                    "maturity": {
                        "required": True,
                        "description": "End date of the swap the quote is for.",
                    },
                    "rate": {
                        "required": True,
                        "description": "Par fixed rate as a decimal a year: 0.041 is 4.1%.",
                    },
                    "frequency_months": {
                        "default": 12,
                        "description": "Payment frequency of both legs, in months.",
                    },
                    "payment_lag_days": {
                        "default": 2,
                        "description": "Business days from each accrual end to its payment.",
                    },
                    "label": {
                        "default": "",
                        "description": "Name in the residuals and the evidence, and its series id.",
                    },
                    "source": {
                        "default": "file",
                        "description": "Where the quote came from, recorded in its provenance.",
                    },
                    "instrument_kind": {
                        "default": "ois_par",
                        "description": (
                            "What the quote is, recorded in its provenance; "
                            "treasury_par_yield for a Treasury proxy."
                        ),
                    },
                    "data_quality": {
                        "default": "observed",
                        "description": (
                            "observed, synthetic, proxy or assumed. A proxy is refused "
                            "unless curve.long_end_source is treasury_proxy."
                        ),
                    },
                    "start": {"refused": _V03_PAR_SWAP},
                    "payment_dates": {"refused": _V03_PAR_SWAP},
                    "year_fractions": {"refused": _V03_PAR_SWAP},
                },
            },
            "convexity": {
                "description": (
                    "How futures rates are convexity-adjusted before they enter the "
                    "curve. Absent means no adjustment."
                ),
                "keys": {
                    "model": {"default": "none", "description": "none, ho_lee or hull_white."},
                    "sigma": {
                        "default": 0.0,
                        "description": (
                            "Normal volatility of the short rate as a decimal a year: 1% "
                            "is 0.01, not 1.0."
                        ),
                    },
                    "kappa": {
                        "default": None,
                        "description": (
                            "Hull-White mean reversion, per year; required by hull_white "
                            "and ignored by the other models."
                        ),
                    },
                },
            },
            "long_end_source": {
                "default": None,
                "description": (
                    "null admits no proxy quote; treasury_proxy admits par_swaps whose "
                    "data_quality is proxy, and marks the curve with the degradation."
                ),
            },
            "strict": {
                "used_by": ["bootstrap"],
                "default": True,
                "description": (
                    "Read by bootstrap only. false records an instrument left outside the "
                    "residual tolerance in dropped_instruments instead of raising "
                    "BootstrapResidualError; price and hedge always bootstrap strictly."
                ),
            },
        },
    },
    "swap": {
        "used_by": ["price", "hedge"],
        "required": True,
        "description": "The OIS swap price values and hedge sizes the futures strip against.",
        "keys": {
            "effective": {"required": True, "description": "Start of the first accrual period."},
            "maturity": {"required": True, "description": "End of the last accrual period."},
            "fixed_rate": {
                "required": True,
                "description": "Fixed rate as a decimal a year: 0.04 is 4%.",
            },
            "notional": {
                "default": 1_000_000.0,
                "description": "Notional in units of the index's currency (USD for SOFR).",
            },
            "side": {
                "default": "payer",
                "description": "payer (pays fixed) or receiver, in lower case.",
            },
            "frequency_months": {
                "default": 12,
                "description": "Fixed leg payment frequency, in months.",
            },
            "payment_lag_days": {
                "default": 2,
                "description": "Business days from each accrual end to its payment.",
            },
            "index": {
                "default": "SOFR",
                "description": (
                    "The overnight rate the floating leg compounds: SOFR, or TIIE_FONDEO, "
                    "which price refuses because the curve block quotes dollar instruments."
                ),
            },
        },
    },
    "risk": {
        "used_by": ["price"],
        "description": "Key-rate settings for price.",
        "keys": {
            "key_tenors": {
                "default": None,
                "description": (
                    "Key tenors for key_rate_dv01, in years on the curve's ACT/365F time "
                    "axis, as numbers. Absent leaves key_rate_dv01 null."
                ),
            },
        },
    },
    "hedge": {
        "used_by": ["hedge"],
        "description": "Settings for the futures-strip hedge and its shock table.",
        "keys": {
            "contract_dv01": {
                "default": SR3_DV01,
                "description": "USD per basis point for one contract: 25.0 for SR3.",
            },
            "shocks_bp": {
                "default": list(DEFAULT_SHOCKS_BP),
                "description": (
                    "Parallel shocks for the hedged position's shock table, in basis points."
                ),
            },
        },
    },
    "fx": {
        "used_by": _FX_COMMANDS,
        "required": True,
        "description": "USD/MXN market inputs for fx-forward and hedge-structures.",
        "keys": {
            "spot": {
                "required": True,
                "description": "USD/MXN spot, pesos per dollar (quote currency per unit of base).",
            },
            "delivery": {
                "required": True,
                "description": (
                    "Delivery date of the forward and settlement date of the exposure; "
                    "the years to it are measured from as_of on ACT/365F."
                ),
            },
            "r_domestic": {
                "required": True,
                "description": (
                    "Peso (quote currency) rate as a decimal a year, continuously "
                    "compounded, on ACT/365F time from as_of: 0.095 is 9.5%. A money-market "
                    "rate (simple, ACT/360) has to be converted first; passed as it is, "
                    "the forward is off by hundreds of pips at a year."
                ),
            },
            "r_foreign": {
                "required": True,
                "description": (
                    "Dollar (base currency) rate in the same convention: a decimal a year, "
                    "continuously compounded, on ACT/365F time from as_of."
                ),
            },
            "basis_bp": {
                "default": 0.0,
                "description": (
                    "Cross-currency basis in basis points, a flat continuously compounded "
                    "zero spread on the peso leg: outright = CIP forward * exp(basis_bp / "
                    "10000 * T). Not the par basis a cross-currency swap quotes; negative "
                    "lowers the outright."
                ),
            },
            "volatility": {
                "used_by": ["hedge-structures"],
                "required": True,
                "description": (
                    "Lognormal (Garman-Kohlhagen) volatility as a decimal a year, one "
                    "number for every strike: 0.115 is 11.5%."
                ),
            },
        },
    },
    "exposure": {
        "used_by": ["hedge-structures"],
        "required": True,
        "description": "The transaction exposure the hedge structures are compared against.",
        "keys": {
            "amount": {
                "required": True,
                "description": (
                    "Size in dollars (the base currency), positive; direction carries the sign."
                ),
            },
            "direction": {
                "required": True,
                "description": (
                    "payable (dollars owed, so a rise in USD/MXN costs money) or receivable."
                ),
            },
            "hedge_ratio": {
                "default": 1.0,
                "description": (
                    "Fraction of the exposure the forward row covers, in [0, 1]; also the "
                    "ratio the hedge programme audits."
                ),
            },
            "correlation": {
                "default": None,
                "description": (
                    "Correlation between the foreign asset's return and USD/MXN, for the "
                    "residual-variance decomposition; give it with foreign_asset_volatility "
                    "or not at all."
                ),
            },
            "foreign_asset_volatility": {
                "default": None,
                "description": (
                    "The foreign asset's own volatility as a decimal a year; needed with "
                    "correlation."
                ),
            },
            "proposed_instrument": {
                "default": None,
                "description": (
                    "Structure name the hedge programme audits; read only when "
                    "hedge_program is given."
                ),
            },
        },
    },
    "hedge_program": {
        "used_by": ["hedge-structures"],
        "description": (
            "The hedging policy the proposal is audited against; optional, and strict "
            "about its own keys."
        ),
        "keys": {
            "target_hedge_ratio": {
                "required": True,
                "description": "Target fraction of the exposure hedged, in [0, 1].",
            },
            "discretion_band": {
                "required": True,
                "description": (
                    "How far either side of the target is still compliant, as a fraction."
                ),
            },
            "rebalance_frequency": {
                "required": True,
                "description": "daily, weekly, monthly, quarterly or at_inception.",
            },
            "allowed_instruments": {
                "default": [],
                "description": (
                    "Structure names the programme permits; empty permits every structure."
                ),
            },
            "name": {"default": "unnamed_program", "description": "A label for the payload."},
        },
    },
}
"""Every key a command reads, nested as the config nests them.

Each entry has a ``description`` stating units and conventions, and where
they apply ``required``, ``default`` and ``used_by`` (the commands that read
it; a nested key without one is read by its block's commands). ``keys`` holds
a nested block and ``items`` the entries of a list. An entry with ``refused``
is a key a command recognises in order to reject it with a migration.

This is what ``rateng describe --json`` publishes under ``config``, and what
:func:`refuse_unknown_keys` consults for a key the command did not read.
``tests/test_app_config.py`` holds it to the code: every key a command asks
for must be here, and ``used_by`` must be exactly the commands that read it.
"""


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


def as_date(value: str | date) -> date:
    """Parse a config date, accepting what either loader hands over.

    JSON has no date type, so a date arrives as a string. YAML does, and
    ``safe_load`` turns ``2026-01-15`` into a ``datetime.date`` before this
    code ever sees it. Accepting both is what stops the same config behaving
    differently depending on the extension it was saved under.

    Args:
        value: An ISO date string or a date.

    Returns:
        The date.
    """
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def require_config(config: dict[str, Any] | None, command: str) -> dict[str, Any]:
    """The config, or a named refusal saying which command needed one.

    Every handler in :data:`~rates_engine.app.commands.COMMANDS` has the same
    signature so that every interface can expose the table unchanged. Two of
    them genuinely take no config; the others go through here, so that
    calling them without one is a sentence rather than whichever ``KeyError``
    happens to come first.

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


class _ReadTracker(dict[Any, Any]):
    """A config mapping that remembers which of its keys were looked up.

    A ``dict`` subclass rather than a ``Mapping``, so that a command body
    checking ``isinstance(block, dict)`` or passing a block on as a ``dict``
    behaves exactly as it does on the plain config. Nested mappings, and
    mappings inside lists, are wrapped when the tracker is built, so a read
    anywhere in the tree is recorded on the node it happened at.

    A *read* is ``block[key]``, ``block.get(key)``, ``key in block`` for a key
    that is present, ``pop`` and ``setdefault``. ``items()`` and ``values()``
    read every key, and so does copying the block -- ``dict(block)``,
    ``{**block}``, ``block.copy()``, ``copy.deepcopy`` -- because CPython
    copies a ``dict`` subclass that overrides ``__iter__`` through
    ``__getitem__``, and code that copies a block has taken responsibility for
    all of it (``load_program`` refuses unknown programme keys itself).
    Listing the keys -- iteration, ``keys()``, ``len`` -- is not a read: it is
    what a validator does before it reads.
    """

    def __init__(self, source: Mapping[Any, Any], missing: list[tuple[Any, Any, KeyError]]) -> None:
        super().__init__((key, _tracked(value, missing)) for key, value in source.items())
        self._given: tuple[Any, ...] = tuple(dict.keys(self))
        self._read: set[Any] = set()
        self._asked: set[Any] = set()
        self._missing = missing

    def __getitem__(self, key: Any) -> Any:
        self._asked.add(key)
        if not dict.__contains__(self, key):
            # Raised rather than re-raised from dict, so the dispatcher can
            # tell this lookup's KeyError from any other by identity and turn
            # only this one into a sentence naming the path.
            error = KeyError(key)
            self._missing.append((self, key, error))
            raise error
        self._read.add(key)
        return dict.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._asked.add(key)
        if dict.__contains__(self, key):
            self._read.add(key)
            return dict.__getitem__(self, key)
        return default

    def __contains__(self, key: object) -> bool:
        self._asked.add(key)
        present = dict.__contains__(self, key)
        if present:
            self._read.add(key)
        return present

    def pop(self, key: Any, *default: Any) -> Any:
        self._asked.add(key)
        self._read.add(key)
        return dict.pop(self, key, *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._asked.add(key)
        self._read.add(key)
        return dict.setdefault(self, key, default)

    def __delitem__(self, key: Any) -> None:
        self._read.add(key)
        dict.__delitem__(self, key)

    def items(self) -> Any:
        self._read.update(self._given)
        return dict.items(self)

    def values(self) -> Any:
        self._read.update(self._given)
        return dict.values(self)

    def __iter__(self) -> Iterator[Any]:
        # Overridden only to exist: a dict subclass with its own __iter__ is
        # copied by dict(), {**x} and .copy() through keys() and
        # __getitem__, which is what records a copy as a read.
        return dict.__iter__(self)


def _tracked(value: Any, missing: list[tuple[Any, Any, KeyError]]) -> Any:
    """``value`` with every mapping in it, at any depth, wrapped in a tracker."""
    if isinstance(value, Mapping):
        return _ReadTracker(value, missing)
    if isinstance(value, list):
        return [_tracked(element, missing) for element in value]
    if isinstance(value, tuple):
        return tuple(_tracked(element, missing) for element in value)
    return value


def track_reads(config: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``config`` that records which keys are read, for :func:`refuse_unknown_keys`.

    The copy compares equal to ``config`` and leaves it untouched; nested
    blocks, and blocks inside lists, are tracked too.

    Args:
        config: The configuration mapping, as loaded.

    Returns:
        The tracking copy, a ``dict``.
    """
    return _ReadTracker(config, [])


def _is_comment(key: Any) -> bool:
    """Keys starting with ``_`` are comments, at any depth: JSON has no other kind."""
    return isinstance(key, str) and key.startswith("_")


def _join(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _level(spec: Any, field: str) -> dict[str, Any] | None:
    """The documented keys one level down from ``spec``, if it documents any."""
    if isinstance(spec, dict) and isinstance(spec.get(field), dict):
        return spec[field]
    return None


def _entered(
    node: _ReadTracker,
    level: dict[str, Any] | None = CONFIG_SCHEMA,
    path: str = "",
    pattern: str = "",
) -> Iterator[tuple[str, str, _ReadTracker, dict[str, Any] | None]]:
    """Every block the command entered, with its path and its documented keys.

    A block is entered when the command read the key holding it, so a block
    it never touched -- another command's -- is not walked: what is in it is
    that command's business. Comment keys are not walked either.

    Yields:
        ``(path, pattern, block, level)``: the path with list indices
        (``curve.futures[2]``), the same with ``[]`` for every index, the
        tracker, and the schema level documenting its keys (``None`` for a
        block the schema does not describe).
    """
    yield path, pattern, node, level
    for key in node._given:
        if key not in node._read or _is_comment(key):
            continue
        spec = level.get(key) if level is not None else None
        value = dict.get(node, key)
        child_path, child_pattern = _join(path, key), _join(pattern, key)
        if isinstance(value, _ReadTracker):
            yield from _entered(value, _level(spec, "keys"), child_path, child_pattern)
        elif isinstance(value, (list, tuple)):
            items = _level(spec, "items")
            for index, element in enumerate(value):
                if isinstance(element, _ReadTracker):
                    yield from _entered(
                        element, items, f"{child_path}[{index}]", f"{child_pattern}[]"
                    )


def _homes(level: dict[str, Any] = CONFIG_SCHEMA, prefix: str = "") -> dict[str, list[str]]:
    """For every documented key name, the blocks that accept it."""
    found: dict[str, list[str]] = {}
    for key, spec in level.items():
        if "refused" not in spec:
            found.setdefault(key, []).append(prefix or "the top level")
        for field, suffix in (("keys", ""), ("items", "[]")):
            nested = _level(spec, field)
            if nested is not None:
                for name, places in _homes(nested, f"{_join(prefix, key)}{suffix}").items():
                    found.setdefault(name, []).extend(places)
    return found


_HOMES = _homes()


def _accepted(node: _ReadTracker, level: dict[str, Any] | None) -> list[str]:
    """What a block accepts: its documented keys plus any the command asked for."""
    documented = {key for key, spec in (level or {}).items() if "refused" not in spec}
    asked = {str(key) for key in node._asked if not _is_comment(key)}
    return sorted(documented | asked)


def _hint(key: Any, accepted: list[str], here: str) -> str:
    """The key probably meant, or the block the key belongs in."""
    close = difflib.get_close_matches(str(key), accepted, n=1)
    if close:
        return f" (did you mean {close[0]}?)"
    homes = [home for home in _HOMES.get(str(key), []) if home != (here or "the top level")]
    if homes:
        return f" (it belongs under {' or '.join(homes)})"
    return ""


def refuse_unknown_keys(
    config: Mapping[str, Any], command: str, *, failure: BaseException | None = None
) -> None:
    """Refuse any key the command reached without reading it or knowing it.

    Walks every block the command entered. A key there passes if the command
    read it, if it starts with ``_`` (a comment), or if :data:`CONFIG_SCHEMA`
    documents it at that place -- a key some other command reads, or an
    optional one that does not apply on this path. Anything else is a typo or
    an invention, and is refused with its path, the closest accepted key and
    the full list of what the block accepts. Blocks the command never entered
    are left alone, because one file serves several commands.

    With ``failure``, the command has already raised. A ``KeyError`` from one
    of the tracker's own lookups becomes a refusal naming the missing path;
    unknown keys are reported in preference to any other failure, because a
    misspelt key is the likeliest reason a required one is missing. With
    nothing to report the call returns and the caller re-raises ``failure``.

    Args:
        config: A mapping from :func:`track_reads`, after the command ran. Any
            other mapping is accepted and not checked.
        command: The command's name, for the message.
        failure: What the command raised, if it raised.

    Raises:
        ConfigurationError: An unknown key, or a missing one the command looked
            up. Chained to ``failure`` when there is one.
    """
    if not isinstance(config, _ReadTracker):
        return
    places: dict[int, tuple[str, dict[str, Any] | None]] = {}
    unknown: list[tuple[str, _ReadTracker, dict[str, Any] | None, Any]] = []
    for path, _, node, level in _entered(config):
        places[id(node)] = (path, level)
        documented = level or {}
        unknown.extend(
            (path, node, level, key)
            for key in node._given
            if key not in node._read and not _is_comment(key) and key not in documented
        )
    missing = [(node, key) for node, key, error in config._missing if error is failure]
    if not unknown and not missing:
        return

    sentences: list[str] = []
    for node, key in missing:
        path, level = places.get(id(node), ("", None))
        spec = (level or {}).get(key) if isinstance(key, str) else None
        about = str(spec.get("description", "")) if isinstance(spec, dict) else ""
        if about[:1].isupper() and about[1:2].islower():
            about = about[0].lower() + about[1:]
        detail = f" ({about.rstrip('.')})" if about else ""
        sentences.append(f"{_join(path, key)} is missing, and {command} needs it{detail}.")
    if unknown:
        named = ", ".join(
            f"{_join(path, key)}{_hint(key, _accepted(node, level), path)}"
            for path, node, level, key in unknown
        )
        sentences.append(f"unknown key(s) in the {command} config: {named}.")
        blocks = {path: (node, level) for path, node, level, _ in unknown}
        for path, (node, level) in blocks.items():
            where = f"in {path}" if path else "at the top level"
            sentences.append(f"Accepted {where}: {', '.join(_accepted(node, level))}.")
        sentences.append(
            "A key that is quietly ignored is a setting that does nothing, which is "
            "worse than a config that fails to load; keys starting with '_' are comments."
        )
    sentences.append("`rateng describe --json` lists every key under `config`.")
    raise ConfigurationError(" ".join(sentences)) from failure
