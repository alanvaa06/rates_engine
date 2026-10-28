"""Reading a configuration document, and refusing one that cannot be used.

**Format.** JSON is native, so every command runs on a core install. YAML
needs ``pip install "finport-ratesengine[config]"``, and asking for a
``.yaml`` file without it names that command rather than failing on an
import.

**Every key is accounted for.** A misspelt key used to fall back to its
default without a word: ``notionl`` priced a hundredth of the trade asked
for, ``convexty`` switched the convexity adjustment off, and both exited 0.
:func:`require_config`, which every handler that reads a config calls first,
checks the whole document against :data:`CONFIG_SCHEMA` before anything is
computed. A key the schema does not document is refused with its path and
the closest accepted key -- anywhere in the file, including a block only
another command reads, because one desk file serves every command.

**Adding a key** is one entry in :data:`CONFIG_SCHEMA`, under the block that
holds it, with a ``description`` that states its units; the schema's
docstring lists the fields. Until the entry exists the key is refused as
unknown, and ``tests/test_app_config.py`` fails for any key a handler looks
up that the schema does not document, and for any key the schema documents
that no handler reads.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from rates_engine.core.errors import ConfigurationError, MissingDependencyError
from rates_engine.hedging.futures_strip import DEFAULT_SHOCKS_BP

__all__ = [
    "load_config",
    "as_date",
    "require_config",
    "CONFIG_SCHEMA",
    "require_block",
]

_CURVE_COMMANDS = ["bootstrap", "price", "hedge"]
_FX_COMMANDS = ["fx-forward", "hedge-structures"]

_V03_PAR_SWAP = (
    "the v0.3 par-swap shape (start, payment_dates, year_fractions) is refused. A par "
    "quote is given as the swap it quotes: effective, maturity and rate, with optional "
    "frequency_months (12), payment_lag_days (2) and label; the dates and year fractions "
    "are generated from those terms, payment lag included, which the old shape ignored "
    "and so mispriced a SOFR OIS by several basis points"
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
                    "SR1 or SR3 futures, one entry per reference period; each pins the "
                    "curve at its end date."
                ),
                "items": {
                    "start": {
                        "required": True,
                        "description": (
                            "Reference period start (YYYY-MM-DD): an IMM date for SR3, the "
                            "first of the month for SR1."
                        ),
                    },
                    "end": {
                        "required": True,
                        "description": (
                            "Reference period end, the curve node the contract pins: the "
                            "next IMM date for SR3, the first of the next month for SR1."
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
                    "contract": {
                        "default": None,
                        "description": (
                            'Which future the entry is, "SR1" or "SR3". It sizes the hedge: '
                            "an SR1 is worth 41.67 USD per basis point and an SR3 25.00. "
                            "Left out, the period decides: a calendar month is SR1, an IMM "
                            "quarter SR3, and anything else is sized as SR3 with a "
                            "futures_contract_assumed degradation."
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
                        "suggested_for": ["end"],
                        "description": "End date of the swap the quote is for.",
                    },
                    "rate": {
                        "required": True,
                        "suggested_for": ["fixed_rate"],
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
                    "curve. Absent means no adjustment; given, it names its model."
                ),
                "keys": {
                    "model": {
                        "required": True,
                        "description": (
                            "The model, none, ho_lee or hull_white; required whenever the "
                            "block is given, since a sigma without it priced with no "
                            "adjustment at all."
                        ),
                    },
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
            "effective": {
                "required": True,
                "suggested_for": ["start"],
                "description": "Start of the first accrual period.",
            },
            "maturity": {
                "required": True,
                "suggested_for": ["end"],
                "description": "End of the last accrual period.",
            },
            "fixed_rate": {
                "required": True,
                "suggested_for": ["rate"],
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
    "fixings": {
        "used_by": ["price", "hedge"],
        "description": (
            "Published fixings for a swap that has started, one block per series. A swap "
            "whose floating period began before as_of has accrued at rates no curve "
            "knows; without them price and hedge refuse it and name this block. A swap "
            "with no history ignores it, so one file can carry the desk's SOFR."
        ),
        "keys": {
            "SOFR": {
                "description": (
                    "SOFR as the New York Fed publishes it, each rate dated by the day it "
                    "is for. Read on the SIFMA calendar: a weekend or holiday takes the "
                    "business day before it, so only business days are given, and a row "
                    "dated on a closed day is refused."
                ),
                "keys": {
                    "values": {
                        "required": True,
                        "description": (
                            "One entry per SIFMA business day the swap's current period "
                            "has accrued over, from its start up to as_of; before as_of, a "
                            "business day left out is refused, never interpolated. The "
                            "entry dated as_of is optional: given, it is read and decides "
                            "as_of up to the next business day; left out, those days are "
                            "forecast off the curve, as before the day's print is out."
                        ),
                        "items": {
                            "date": {
                                "required": True,
                                "description": (
                                    "The business day the rate is for, ISO 8601 (YYYY-MM-DD). "
                                    "A weekend or a SIFMA holiday is refused."
                                ),
                            },
                            "rate": {
                                "required": True,
                                "description": (
                                    "The published rate as a decimal a year: 0.043 is 4.30%, "
                                    "not 4.30. Refused beyond 20% "
                                    "(MAX_PLAUSIBLE_OVERNIGHT_FIXING), and the latest one when "
                                    "it is more than four times and 50 bp off the curve's "
                                    "overnight rate (FIXING_CURVE_FACTOR, FIXING_CURVE_GAP): "
                                    "0.43 for 0.043, or 0.05 meaning 0.05%."
                                ),
                            },
                        },
                    },
                    "source": {
                        "default": "config",
                        "description": (
                            "Where the fixings came from (fred, a vendor, a file), "
                            "recorded in their provenance and in every result's evidence."
                        ),
                    },
                    "data_quality": {
                        "default": "observed",
                        "description": (
                            "observed, synthetic, proxy or assumed; anything else is "
                            "refused. Recorded in the provenance, so every result priced on "
                            "the fixings carries it into its worst_quality."
                        ),
                    },
                },
            },
        },
    },
    "risk": {
        "used_by": ["price"],
        "description": "Key-rate settings for price.",
        "keys": {
            "key_tenors": {
                "default": None,
                "type": "list of numbers",
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
                "default": None,
                "description": (
                    "USD per basis point for one contract, applied to every contract. Left "
                    "out, each futures entry is sized at its own tick: 41.67 for SR1, 25.00 "
                    "for SR3. Refused if it contradicts an entry's declared contract, or "
                    "the contract its period names (a calendar month is SR1, an IMM quarter "
                    "SR3)."
                ),
            },
            "require_full_coverage": {
                "default": True,
                "description": (
                    "When true, a strip that leaves part of the swap's span uncovered is "
                    "refused (IncompleteStripError); false sizes the partial hedge. A JSON "
                    "boolean, not a string. The span of a swap that has started runs from "
                    "as_of, and curve.stub covers as_of to its end."
                ),
            },
            "shocks_bp": {
                "default": list(DEFAULT_SHOCKS_BP),
                "type": "list of numbers",
                "description": (
                    "Parallel shocks for the hedged position's shock table, in basis points, "
                    "none of them zero: a zero shock is refused."
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
                "type": "list of strings",
                "description": (
                    "Structure names the programme permits; empty permits every structure."
                ),
            },
            "name": {"default": "unnamed_program", "description": "A label for the payload."},
        },
    },
}
"""Every key any command reads, nested as the config nests them.

The contract :func:`require_config` checks a document against, and what
``rateng describe --json`` publishes under ``config`` (as a copy). Each entry
is a mapping with these fields:

``description``
    What the key holds, in a sentence that states its units and convention.
    Every entry has one.
``required``
    ``True`` when a command that reads the block cannot run without the key.
    Absent, or ``null``, it is refused by name. A required key of an
    optional block is required only when the block is given: an optional
    block that is empty, or holds nothing but comments, counts as left out.
``default``
    What an absent key means. A key whose default is a value is refused when
    given as ``null``; one whose default is ``None`` reads ``null`` as absent.
``used_by``
    The commands that read the key; a nested key without it is read by its
    block's commands. ``required`` and ``null`` are checked only for those
    commands, while an unknown key, a block or list of the wrong shape and a
    list holding the wrong things are refused by every command.
``keys`` / ``items``
    A nested block, or a list of blocks, each a mapping of entries like this.
``type``
    For a key whose value is a list of plain values: ``"list of numbers"``
    (JSON numbers, finite, not booleans) or ``"list of strings"``. A string
    given where the list belongs is refused rather than read a character at
    a time.
``suggested_for``
    Names of other blocks' keys that a caller writing this block is likely
    to mean this key by, such as ``rate`` for ``swap.fixed_rate``. They stay
    unknown keys; the refusal suggests this one instead of sending the
    caller to the block where the name is accepted.
``refused``
    A key recognised in order to refuse it; the text is the migration.

To add a key, add its entry where the config nests it. A new top-level
block is one entry, as ``fixings`` -- the history a seasoned swap needs --
was::

    "fixings": {
        "used_by": ["price", "hedge"],
        "description": "Published fixings for a swap that has started, ...",
        "keys": {"SOFR": {...}},  # one entry per key the block takes
    },

``tests/test_app_config.py`` holds the schema to the handlers: every key a
handler looks up must be documented, every key documented must be read by
some command, ``used_by`` must be exactly the commands that read the key, a
required key must be read by every command that requires it, every list of
plain values must have a ``type``, and leaving out or nulling any key must
end in a refusal or a result, never a ``KeyError`` or a ``TypeError``.
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


def require_config(config: Mapping[str, Any] | None, command: str) -> dict[str, Any]:
    """The config checked against :data:`CONFIG_SCHEMA`, or a refusal naming what is wrong.

    Every handler in :data:`~rates_engine.app.commands.COMMANDS` that reads a
    config calls this first, so the check runs however the handler is reached
    -- the CLI, the MCP server, or a caller of the function itself. Two
    handlers genuinely take no config and do not call it.

    The whole document is checked, not only the blocks ``command`` reads,
    because one file serves several commands and a typo in the ``hedge``
    block is as wrong when ``price`` reads the file as when ``hedge`` does:

    - every key must be documented where it sits, or start with ``_`` (a
      comment, at any depth); anything else is a typo or an invention;
    - a key documented as ``refused`` is refused with its migration;
    - a block documented as a mapping or a list must be one;
    - a list documented with a ``type`` must be a list of those values.

    For the blocks ``command`` reads (``used_by``) it also checks that every
    ``required`` key is there, and that a key whose default is a value is not
    given as ``null``. A ``null`` for a key whose default is ``null`` is the
    same as leaving it out, and so is an optional block that is empty or
    holds only comments: ``hedge_program: {}`` audits nothing, and a comment
    switches nothing off in a block that holds anything else.

    Args:
        config: The configuration mapping, or ``None``.
        command: The command's name, as the CLI spells it (``"price"``).

    Returns:
        A copy of the config as plain ``dict`` and ``list`` values, without
        comment keys, the ``null`` values that mean "absent" and the empty
        optional blocks. The caller's mapping is not modified.

    Raises:
        ConfigurationError: ``config`` is ``None`` or not a mapping, or it
            fails a check above. One refusal lists every problem, each with
            its path, and for a misspelt key the closest accepted one.
    """
    if config is None:
        raise ConfigurationError(
            f"{command} needs a configuration and was given none. "
            "describe and list-instruments are the two commands that answer "
            "without one."
        )
    if not isinstance(config, Mapping):
        raise ConfigurationError(
            f"{command} needs a configuration that is a mapping of keys; got "
            f"{_kind(config)}."
        )
    problems = _Problems(command)
    checked = _check_block(config, CONFIG_SCHEMA, "", None, problems)
    if problems:
        raise ConfigurationError(problems.message())
    return checked


def require_block(block: Any, where: str, *, command: str | None = None) -> dict[str, Any]:
    """One block checked against its entry in :data:`CONFIG_SCHEMA`, as :func:`require_config` would.

    For a builder that a caller can reach without :func:`require_config`, so
    that the builder keeps no list of keys of its own, and a caller who went
    round the whole-file check reads the same refusal as one who did not.
    Every check of :func:`require_config` applies, for every command unless
    ``command`` names one.

    Args:
        block: The block as the caller holds it.
        where: Its path in a config, with list indices
            (``"curve.par_swaps[0]"``).
        command: Check required keys and nulls as this command reads the
            block (``"fx-forward"`` does not need ``fx.volatility``);
            ``None`` checks them for every command.

    Returns:
        The checked copy, as :func:`require_config` makes one.

    Raises:
        ConfigurationError: The block is not a mapping, or fails a check of
            :func:`require_config`; one refusal lists every problem.
    """
    problems = _Problems(command)
    checked = _check_value(block, {"keys": _level_at(where)}, where, None, problems)
    if problems:
        raise ConfigurationError(problems.message())
    return dict(checked)


def _is_comment(key: Any) -> bool:
    """Keys starting with ``_`` are comments, at any depth: JSON has no other kind."""
    return isinstance(key, str) and key.startswith("_")


def _join(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _kind(value: Any) -> str:
    """What a JSON reader would call ``value``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return f"the number {value!r}"
    if isinstance(value, str):
        return f"the string {value!r}"
    if isinstance(value, Mapping):
        return "a mapping"
    if isinstance(value, (list, tuple)):
        return "a list"
    # A YAML date, say: what the loader made of it, in words.
    return f"a {type(value).__name__}"


def _is_number(value: Any) -> bool:
    """A JSON number: not a boolean, which Python counts as an int, nor NaN or infinity."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


_LIST_TYPES: dict[str, tuple[str, Callable[[Any], bool]]] = {
    "list of numbers": ("a finite number", _is_number),
    "list of strings": ("a string", _is_string),
}
"""Each ``type`` a schema entry may declare: what one element must be, and the test."""


def _level_at(where: str) -> dict[str, Any]:
    """The documented keys of the block at ``where``, list indices ignored."""
    level: dict[str, Any] = CONFIG_SCHEMA
    for name in re.sub(r"\[\d*\]", "", where).split(".") if where else ():
        spec = level.get(name) or {}
        level = spec.get("keys") or spec.get("items") or {}
    return level


class _Problems:
    """Everything wrong with one config, gathered so that one refusal says all of it."""

    def __init__(self, command: str | None) -> None:
        self.command = command
        self.unknown: list[tuple[str, dict[str, Any], Any]] = []
        self.refused: list[tuple[str, str, str]] = []
        self.missing: list[tuple[str, dict[str, Any]]] = []
        self.nulls: list[tuple[str, dict[str, Any]]] = []
        self.shapes: list[tuple[str, str, Any]] = []

    def __bool__(self) -> bool:
        return bool(self.unknown or self.refused or self.missing or self.nulls or self.shapes)

    def applies(self, readers: list[str] | None) -> bool:
        """Whether the command reads a block that ``readers`` read."""
        return self.command is None or readers is None or self.command in readers

    def message(self) -> str:
        """The refusal, a sentence per problem, unknown keys first."""
        sentences: list[str] = []
        if self.unknown:
            named = ", ".join(
                f"{_join(path, key)}{_hint(key, level, path)}" for path, level, key in self.unknown
            )
            config = f"the {self.command} config" if self.command else "the config"
            sentences.append(f"unknown key(s) in {config}: {named}.")
            # Once per block, not per entry: every entry of a list accepts
            # the same keys.
            blocks = {re.sub(r"\[\d+\]", "[]", path): level for path, level, _ in self.unknown}
            for path, level in blocks.items():
                where = f"in {path}" if path else "at the top level"
                sentences.append(f"Accepted {where}: {', '.join(_accepted(level))}.")
            sentences.append(
                "A key that is quietly ignored is a setting that does nothing, which is "
                "worse than a config that fails to load; keys starting with '_' are comments."
            )
        grouped: dict[tuple[str, str], list[str]] = {}
        for path, key, text in self.refused:
            grouped.setdefault((path, text), []).append(key)
        for (path, text), keys in grouped.items():
            sentences.append(f"{path or 'The top level'} uses {', '.join(keys)}: {text}.")
        needs = f", and {self.command} needs it" if self.command else ""
        for where, spec in self.missing:
            sentences.append(f"{where} is missing{needs}{_about(spec)}.")
        for where, spec in self.nulls:
            sentences.append(
                f"{where} is null: give a value, or leave the key out for its default, "
                f"{json.dumps(spec.get('default'))}."
            )
        for where, expected, value in self.shapes:
            sentences.append(f"{where} must be {expected}, not {_kind(value)}.")
        sentences.append("`rateng describe --json` lists every key under `config`.")
        return " ".join(sentences)


def _about(spec: dict[str, Any]) -> str:
    """`` (what the key holds)``, from its description."""
    about = str(spec.get("description", ""))
    if about[:1].isupper() and about[1:2].islower():
        about = about[0].lower() + about[1:]
    return f" ({about.rstrip('.')})" if about else ""


def _check_block(
    block: Mapping[Any, Any],
    level: dict[str, Any],
    path: str,
    readers: list[str] | None,
    problems: _Problems,
) -> dict[str, Any]:
    """One mapping checked against its documented keys, and its clean copy."""
    checked: dict[str, Any] = {}
    for key, value in block.items():
        if _is_comment(key):
            continue
        spec = level.get(key) if isinstance(key, str) else None
        if spec is None:
            problems.unknown.append((path, level, key))
            continue
        if "refused" in spec:
            problems.refused.append((path, key, spec["refused"]))
            continue
        uses = spec.get("used_by", readers)
        where = _join(path, key)
        if value is None:
            # null is "absent" where absent means null. Anywhere else it
            # would reach the handler as a value nobody can compute with --
            # float(None) for a notional, bool(None) turning strict off.
            if problems.applies(uses) and spec.get("required"):
                problems.missing.append((where, spec))
            elif problems.applies(uses) and spec.get("default") is not None:
                problems.nulls.append((where, spec))
            continue
        if (
            "keys" in spec
            and not spec.get("required")
            and isinstance(value, Mapping)
            and all(_is_comment(name) for name in value)
        ):
            # An optional block with nothing in it, or nothing but comments,
            # is a block left out: a file switches a block off by emptying
            # it, without losing where it goes. A required block, or an entry
            # of a list, is walked, and names the keys it lacks.
            continue
        checked[key] = _check_value(value, spec, where, uses, problems)
    for key, spec in level.items():
        uses = spec.get("used_by", readers)
        if spec.get("required") and key not in block and problems.applies(uses):
            problems.missing.append((_join(path, key), spec))
    return checked


def _check_value(
    value: Any, spec: dict[str, Any], where: str, readers: list[str] | None, problems: _Problems
) -> Any:
    """A documented key's value, walked when the schema says it is a block or a list of them."""
    if "keys" in spec:
        if not isinstance(value, Mapping):
            problems.shapes.append((where, "a mapping of keys", value))
            return value
        return _check_block(value, spec["keys"], where, readers, problems)
    if "items" in spec:
        if not isinstance(value, (list, tuple)):
            problems.shapes.append((where, "a list", value))
            return value
        entries: list[Any] = []
        for index, entry in enumerate(value):
            at = f"{where}[{index}]"
            if not isinstance(entry, Mapping):
                problems.shapes.append((at, "a mapping of keys", entry))
                continue
            entries.append(_check_block(entry, spec["items"], at, readers, problems))
        return entries
    if "type" in spec:
        # Without this a string where the list belongs was iterated a
        # character at a time: shocks_bp "25" ran the shocks 2 and 5.
        element, accepts = _LIST_TYPES[spec["type"]]
        if not isinstance(value, (list, tuple)):
            problems.shapes.append((where, f"a {spec['type']}", value))
        else:
            for index, item in enumerate(value):
                if not accepts(item):
                    problems.shapes.append((f"{where}[{index}]", element, item))
    return copy.deepcopy(value)


def _homes(level: dict[str, Any] = CONFIG_SCHEMA, prefix: str = "") -> dict[str, list[str]]:
    """For every accepted key name, the blocks that accept it."""
    found: dict[str, list[str]] = {}
    for key, spec in level.items():
        if "refused" not in spec:
            found.setdefault(key, []).append(prefix or "the top level")
        for field, suffix in (("keys", ""), ("items", "[]")):
            nested = spec.get(field)
            if isinstance(nested, dict):
                for name, places in _homes(nested, f"{_join(prefix, key)}{suffix}").items():
                    found.setdefault(name, []).extend(places)
    return found


_HOMES = _homes()


def _accepted(level: dict[str, Any]) -> list[str]:
    """The keys a block accepts. A refused key is documented and never suggested."""
    return sorted(key for key, spec in level.items() if "refused" not in spec)


def _hint(key: Any, level: dict[str, Any], here: str) -> str:
    """The key probably meant, or the block the key belongs in.

    A key the block declares itself ``suggested_for`` comes first: ``rate``
    in ``swap`` is the swap's ``fixed_rate``, not a par quote put in the
    wrong block, though ``curve.par_swaps[]`` is where ``rate`` is accepted.

    Then the closest spelling. The cutoff is 0.75 rather than difflib's 0.6:
    a transposition or a dropped letter scores 0.75 or more (``rsik``
    against ``risk`` is 0.75, ``notionl`` against ``notional`` 0.93), while
    0.6 also matched ``strat`` to ``rate``, a guess that sends the caller the
    wrong way.
    """
    for name, spec in sorted(level.items()):
        if key in spec.get("suggested_for", ()):
            return f" (did you mean {name}?)"
    close = difflib.get_close_matches(str(key), _accepted(level), n=1, cutoff=0.75)
    if close:
        return f" (did you mean {close[0]}?)"
    homes = [home for home in _HOMES.get(str(key), []) if home != (here or "the top level")]
    if homes:
        return f" (it belongs under {' or '.join(homes)})"
    return ""
