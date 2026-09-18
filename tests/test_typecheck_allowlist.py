"""The mypy allowlist is allowed to shrink. This is what stops it growing.

``[tool.mypy]``'s ``ignore_errors`` override names modules whose type errors
are deferred rather than fixed, and mypy is happy for that list to get longer:
appending a module silences a new failure exactly as well as fixing one, and
costs nothing at review time. So the cost lives here.

The ceiling is **zero**. The reference project this one mirrors carries
forty-five deferred modules because they are inherited debt; this package is
new and has no such excuse. Raising the ceiling is the single move this test
exists to make expensive.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import rates_engine

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
SRC = Path(rates_engine.__file__).parent

#: How many modules may have their type errors deferred. Zero, and the only
#: direction it may move is down, which from zero means not at all.
CEILING = 0


def _allowlisted() -> list[str]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return [
        module
        for override in config["tool"]["mypy"].get("overrides", [])
        if override.get("ignore_errors")
        for module in override["module"]
    ]


def test_the_allowlist_is_within_its_ceiling():
    modules = _allowlisted()
    assert len(modules) <= CEILING, (
        f"{len(modules)} modules have deferred type errors, ceiling is {CEILING}: {modules}. "
        "Fix the module rather than raising this number."
    )


def test_every_allowlisted_module_exists():
    # A stale entry suppresses nothing, still counts against the ceiling, and
    # still reads like outstanding work.
    missing = []
    for module in _allowlisted():
        relative = module.removeprefix("rates_engine.").replace(".", "/")
        if not (SRC / f"{relative}.py").exists() and not (SRC / relative).is_dir():
            missing.append(module)
    assert missing == [], missing


def test_mypy_targets_the_version_the_package_promises():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert config["tool"]["mypy"]["python_version"] == "3.11"
    assert config["project"]["requires-python"] == ">=3.11"


def test_the_package_ships_a_py_typed_marker():
    assert (SRC / "py.typed").exists()
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "py.typed" in config["tool"]["setuptools"]["package-data"]["rates_engine"]
