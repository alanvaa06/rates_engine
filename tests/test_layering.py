"""The module graph is a layered acyclic graph, and stays one.

Every dependency in this package points the same way: at things that were
decided earlier. ``errors`` and ``evidence`` know about nothing;
``conventions`` knows about ``errors``; ``curves`` knows about
``conventions``; ``hedging`` knows about ``pricing``. Nothing points back up.

This is worth a test rather than a convention because the first upward edge
is never the expensive one — it is the second, which is now allowed to exist
because the first one does. The concrete thing it prevents here: a curve
module reaching into ``instruments`` to price a par swap, which would make it
impossible to calibrate a curve without the product layer.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import rates_engine

ROOT = Path(rates_engine.__file__).parent

#: Layers, earliest first. A module may import from anything at or before its
#: own position and from nothing after it.
LAYERS: tuple[str, ...] = (
    "errors",
    "evidence",
    "money",
    "results",
    "conventions",
    "market",
    "convexity",
    "volatility",
    "curves",
    "instruments",
    "pricing",
    "optionpricing",
    "risk",
    "hedging",
    "diagnostics",
    "reporting",
    "cli",
    "mcp_server",
)

RANK = {name: index for index, name in enumerate(LAYERS)}


def _top_level(path: Path) -> str:
    relative = str(path.relative_to(ROOT)).removesuffix(".py")
    return relative.replace("\\", "/").split("/")[0]


def _edges() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        mine = _top_level(path)
        if mine == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if not node.module.startswith("rates_engine"):
                    continue
                target = node.module.removeprefix("rates_engine").lstrip(".")
                top = target.split(".")[0] if target else "__init__"
                if top and top != mine:
                    edges[mine].add(top)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("rates_engine."):
                        top = alias.name.split(".")[1]
                        if top != mine:
                            edges[mine].add(top)
    return edges


def test_no_module_imports_from_a_later_layer():
    upward = [
        f"{source} -> {target}"
        for source, targets in _edges().items()
        for target in targets
        if source in RANK and target in RANK and RANK[target] > RANK[source]
    ]
    assert upward == [], upward


def test_nothing_inside_the_package_imports_the_package_root():
    # Importing ``rates_engine`` from inside it works, and makes the module
    # graph depend on the order of the re-exports in __init__.py. Submodules
    # import each other directly.
    offenders = [
        source for source, targets in _edges().items() if "__init__" in targets
    ]
    assert offenders == [], offenders


def test_the_curve_layer_does_not_know_about_products():
    # The reason ParSwapNode holds dates and year fractions rather than a
    # swap: a curve must be calibratable without the instrument layer.
    assert "instruments" not in _edges().get("curves", set())


def test_every_layer_named_here_exists():
    present = {_top_level(p) for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts}
    missing = [layer for layer in LAYERS if layer not in present]
    assert missing == [], missing


def test_every_module_is_assigned_a_layer():
    present = {_top_level(p) for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts}
    unranked = sorted(present - set(LAYERS) - {"__init__", "py"})
    assert unranked == [], f"modules with no declared layer: {unranked}"


def test_the_graph_is_acyclic():
    edges = _edges()
    colour: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        if colour.get(node) == 2:
            return
        assert colour.get(node) != 1, f"cycle: {' -> '.join([*trail, node])}"
        colour[node] = 1
        for nxt in sorted(edges.get(node, ())):
            visit(nxt, [*trail, node])
        colour[node] = 2

    for start in sorted(edges):
        visit(start, [])
