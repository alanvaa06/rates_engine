"""What the FX modules print is ASCII.

Error messages reach stderr, and a Windows console on a legacy code page
fails on characters outside it, so the package keeps its console output
ASCII. Docstrings and comments are not console output and may use any
character. The first version of the spot-date work added a ``±`` to the rate
band's refusal (Alan Vazquez on PR #13, finding 2); this scans every string
literal that is not a docstring in the FX modules for one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import rates_engine

ROOT = Path(rates_engine.__file__).parent
FX_MODULES = (
    "conventions/currency_pair.py",
    "conventions/settlement.py",
    "models/garman_kohlhagen.py",
    "models/fx_delta.py",
    "pricing/fx_forward.py",
    "volatility/fx_smile.py",
    "hedging/fx_structures.py",
)


def _non_ascii_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and not node.value.isascii()
    ]


@pytest.mark.parametrize("module", FX_MODULES)
def test_every_message_is_ascii(module):
    assert _non_ascii_literals(ROOT / module) == []


def test_the_rate_band_says_plus_or_minus_in_ascii():
    from rates_engine.core.errors import ImplausibleInputError
    from rates_engine.pricing.fx_forward import require_plausible_basis, require_plausible_rate

    for check, value in ((lambda v: require_plausible_rate(v, "r_domestic"), 9.5),
                         (require_plausible_basis, 900.0)):
        with pytest.raises(ImplausibleInputError) as excinfo:
            check(value)
        assert "+/-" in str(excinfo.value)
        assert str(excinfo.value).isascii()
