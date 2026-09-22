"""Reading a configuration document, and refusing one that cannot be used.

**Format.** JSON is native, so every command runs on a core install. YAML
needs ``pip install "finport-ratesengine[config]"``, and asking for a
``.yaml`` file without it names that command rather than failing on an
import.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from rates_engine.core.errors import ConfigurationError, MissingDependencyError

__all__ = ["load_config", "as_date", "require_config"]


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
