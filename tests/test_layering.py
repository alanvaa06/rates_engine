"""The package is layered, and the layers mean something.

``docs/architecture/ARCHITECTURE.md`` §3 is the design this file enforces.
Each top-level package sits on one layer; a package may import from layers
below its own and from nothing at or above it. Two packages on the same layer
do not know about each other — that is what makes them a layer rather than
an arbitrary position in a list.

The previous version of this file ranked twenty-one modules in a single total
order. That proved the graph acyclic and nothing else: ``fx`` ranked below
``instruments`` and ``convexity`` below ``volatility`` for no reason a reader
could recover, and a new module had to be slotted in at a position that
meant nothing. Acyclicity is still checked below; it is now a consequence of
the layering rather than the whole of it.

Known violations are listed in :data:`SAME_LAYER_EXCEPTIONS` with the reason
and the work that removes them. The list may only shrink: an entry whose edge
no longer exists fails :func:`test_every_declared_exception_is_still_needed`,
so a fix cannot leave its exception behind.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import rates_engine

ROOT = Path(rates_engine.__file__).parent

#: Layers, lowest first. Every top-level package or module sits on exactly one.
LAYERS: tuple[frozenset[str], ...] = (
    frozenset({"core"}),
    frozenset({"conventions"}),
    frozenset({"market", "models"}),
    frozenset({"curves", "volatility", "instruments"}),
    frozenset({"pricing"}),
    frozenset({"risk"}),
    frozenset({"hedging"}),
    frozenset({"reporting"}),
    frozenset({"app"}),
    frozenset({"cli", "mcp_server"}),
)

LAYER_OF: dict[str, int] = {name: index for index, layer in enumerate(LAYERS) for name in layer}

#: Edges that break the rules today, each with the reason and the fix.
SAME_LAYER_EXCEPTIONS: dict[tuple[str, str], str] = {}
"""Empty since v0.4. The last entry, ``instruments -> curves``, went when
cashflow projection moved into :mod:`rates_engine.pricing.projection`; a new
entry needs its reason and the work that removes it."""


def _top_level(path: Path) -> str:
    relative = str(path.relative_to(ROOT)).removesuffix(".py")
    return relative.replace("\\", "/").split("/")[0]


def _modules() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _package_of(path: Path) -> list[str]:
    """Dotted parts of the package a module lives in, below ``rates_engine``."""
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    return parts[:-1]


def _targets(node: ast.AST, path: Path) -> list[str]:
    """Top-level rates_engine packages an import statement reaches.

    Relative imports are resolved against the module's own package, and a
    bare ``import rates_engine`` counts as an edge to the root. Missing
    either would let an upward edge through unseen -- the first version of
    this function missed both.
    """
    if isinstance(node, ast.ImportFrom):
        if node.level:
            base = _package_of(path)
            base = base[: len(base) - (node.level - 1)]
            dotted = [*base, *(node.module.split(".") if node.module else [])]
            if dotted:
                return [dotted[0]]
            return [alias.name for alias in node.names]
        module = node.module or ""
        if module == "rates_engine":
            return ["__init__"]
        if module.startswith("rates_engine."):
            return [module.split(".")[1]]
        return []
    if isinstance(node, ast.Import):
        found = []
        for alias in node.names:
            if alias.name == "rates_engine":
                found.append("__init__")
            elif alias.name.startswith("rates_engine."):
                found.append(alias.name.split(".")[1])
        return found
    return []


def _edges() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for path in _modules():
        mine = _top_level(path)
        if mine == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            edges[mine].update(t for t in _targets(node, path) if t != mine)
    return edges


def _violations() -> list[tuple[str, str]]:
    return sorted(
        (source, target)
        for source, targets in _edges().items()
        for target in targets
        if source in LAYER_OF
        and target in LAYER_OF
        and LAYER_OF[target] >= LAYER_OF[source]
    )


def test_every_edge_points_to_a_lower_layer():
    unexplained = [
        f"{s} (layer {LAYER_OF[s]}) -> {t} (layer {LAYER_OF[t]})"
        for s, t in _violations()
        if (s, t) not in SAME_LAYER_EXCEPTIONS
    ]
    assert unexplained == [], unexplained


def test_every_declared_exception_is_still_needed():
    stale = sorted(set(SAME_LAYER_EXCEPTIONS) - set(_violations()))
    assert stale == [], f"fixed, so remove from SAME_LAYER_EXCEPTIONS: {stale}"


def test_no_exception_points_upward():
    # A declared exception may join two packages on the same layer. It may
    # never let a lower layer reach a higher one: that is a cycle waiting for
    # its second edge.
    upward = [
        (s, t) for s, t in SAME_LAYER_EXCEPTIONS if LAYER_OF[t] > LAYER_OF[s]
    ]
    assert upward == [], upward


def test_no_module_uses_a_relative_import():
    # Absolute imports are what this file, the migration scripts and a human
    # grepping for a module all read. A relative one is resolved here, but
    # banning them keeps every edge greppable as ``rates_engine.<package>``.
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in _modules()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.level
    ]
    assert offenders == [], offenders


def test_the_edge_finder_sees_relative_and_bare_imports():
    """The detector is itself tested: an upward edge written as a relative
    import, and a bare ``import rates_engine``, must both be seen."""
    fake = ROOT / "curves" / "views.py"
    relative = ast.parse("from ..hedging import futures_strip").body[0]
    bare = ast.parse("import rates_engine").body[0]
    assert _targets(relative, fake) == ["hedging"]
    assert _targets(bare, fake) == ["__init__"]


def test_nothing_inside_the_package_imports_the_package_root():
    # Importing ``rates_engine`` from inside it works, and makes the module
    # graph depend on the order of the re-exports in __init__.py. Submodules
    # import each other directly.
    offenders = [source for source, targets in _edges().items() if "__init__" in targets]
    assert offenders == [], offenders


def test_the_curve_layer_does_not_know_about_products():
    # The reason ParSwapNode holds dates and year fractions rather than a
    # swap: a curve must be calibratable without the instrument layer.
    assert "instruments" not in _edges().get("curves", set())


def test_models_do_not_know_about_market_objects():
    # A model takes a forward, a strike and a volatility as numbers. The day
    # it imports a curve or a volatility object is the day a formula starts
    # deciding where its inputs come from.
    assert not _edges().get("models", set()) & {"market", "curves", "volatility", "instruments"}


def test_subpackage_inits_re_export_nothing():
    """A name has two homes, the package root and the module defining it.

    A third — the subpackage ``__init__`` — is a surface that drifts: it was
    how ``rates_engine.volatility`` came to export Black and Bachelier after
    they stopped being its business. The ``__init__`` files document their
    layer's contract and import nothing.
    """
    offenders = []
    for path in _modules():
        if path.name != "__init__.py" or path.parent == ROOT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in tree.body):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


def test_every_layer_named_here_exists():
    present = {_top_level(p) for p in _modules()}
    missing = sorted(set(LAYER_OF) - present)
    assert missing == [], missing


def test_every_module_is_assigned_a_layer():
    present = {_top_level(p) for p in _modules()}
    unranked = sorted(present - set(LAYER_OF) - {"__init__", "py"})
    assert unranked == [], f"packages with no declared layer: {unranked}"


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


def test_the_module_map_in_agents_md_lists_every_package():
    """The map is what an agent reads instead of the tree, so a package
    missing from it does not exist as far as the next contributor is
    concerned. The map is package-level — `curves` covers
    `curves/bootstrap.py` and the modules beside it."""
    import re

    text = (ROOT.parent.parent / "AGENTS.md").read_text(encoding="utf-8")
    rows = set(re.findall(r"^\| `([\w_]+)` \|", text, re.M))
    present = {_top_level(p) for p in _modules()} - {"__init__"}
    assert sorted(present - rows) == [], "in the package, missing from AGENTS.md"
    assert sorted(rows - present) == [], "in AGENTS.md, not in the package"


def test_no_test_imports_another_test_module():
    """`tests` is not a package and is not installed, so `import
    tests.test_x` resolves only when the working directory happens to be on
    `sys.path` — true under `python -m pytest`, false under CI's bare
    `pytest`. It therefore passes locally and fails in CI, which is the
    worst failure mode a test can have.

    Shared fixtures belong in `conftest.py`. This caught a real CI break.
    """
    import re

    offenders = []
    for path in sorted((ROOT.parent.parent / "tests").rglob("test_*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(import tests\b|from tests\b)", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], offenders


def test_only_risk_bumps_moves_a_curve_set():
    """`CurveSet.shifted` is called from exactly one place outside `curves`:
    `risk.bumps.shifted`. Every sensitivity, the greeks and the shock table
    go through it, so there is one definition of what a bump is."""
    offenders = []
    for path in _modules():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("curves/") or relative == "risk/bumps.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "shifted"
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], offenders
