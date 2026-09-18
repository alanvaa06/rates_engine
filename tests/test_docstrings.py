"""PRD-001 AC-9.6: every public name is documented, and numeric parameters carry units.

The units half is the one that earns its keep. A rate without a compounding
basis and a DV01 quoted per percent are the two ways this kind of library is
wrong by orders of magnitude while looking entirely reasonable.

**Scope, widened in v3.** This file used to check `rates_engine.__all__`,
157 names. But a module's own `__all__` is what says a name is public, and
the top-level package re-exports well under half of them: 229 names across
the package are public and were never checked here. The gap was not
theoretical — every Garman-Kohlhagen greek documented neither its arguments
nor its units, and vega's unit in particular is the classic error this file
exists to catch, since it returns per unit of volatility and a trader reads
per point.

The rule is now each module's own `__all__`, and a name is checked in the
module that defines it so a re-export is not tested twice.
"""

from __future__ import annotations

import inspect
import pkgutil
from importlib import import_module

import pytest

import rates_engine

UNIT_WORDS = (
    "basis point",
    "bp",
    "USD",
    "years",
    "decimal",
    "percent",
    "per cent",
    "year fraction",
    "rate",
    "date",
)


def _modules():
    yield rates_engine
    for info in pkgutil.walk_packages(rates_engine.__path__, "rates_engine."):
        yield import_module(info.name)


def _public():
    """Every name in some module's ``__all__``, at the module defining it.

    A re-export is skipped rather than checked twice, which is what keeps
    the failure message pointing at the definition a fix has to edit.
    """
    seen: set[tuple[str, str]] = set()
    for module in _modules():
        for name in getattr(module, "__all__", ()):
            obj = getattr(module, name, None)
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            key = (module.__name__, name)
            if key in seen:
                continue
            seen.add(key)
            yield module.__name__, name, obj


PUBLIC = sorted(_public())
FUNCTIONS = [(m, n, o) for m, n, o in PUBLIC if inspect.isfunction(o)]
CLASSES = [(m, n, o) for m, n, o in PUBLIC if inspect.isclass(o)]


def _ids(rows):
    return [f"{m}.{n}" for m, n, _ in rows]


def test_every_module_has_a_docstring():
    missing = [m.__name__ for m in _modules() if not (m.__doc__ or "").strip()]
    assert missing == [], missing


def test_the_scope_is_the_whole_package_not_just_the_re_exports():
    """The widening itself, pinned. If a future refactor narrows this back
    to `rates_engine.__all__` the count collapses and this fails, rather
    than the suite quietly checking a third of what it claims to."""
    assert len(PUBLIC) > len(rates_engine.__all__)
    checked = {module for module, _, _ in PUBLIC}
    assert "rates_engine.fx.garman_kohlhagen" in checked
    assert "rates_engine.hedging_structures" in checked


def test_every_public_name_has_a_docstring():
    missing = [f"{m}.{n}" for m, n, o in PUBLIC if not (o.__doc__ or "").strip()]
    assert missing == [], missing


def test_every_public_method_of_a_public_class_has_a_docstring():
    missing = []
    for module, name, obj in CLASSES:
        for attribute, member in vars(obj).items():
            if attribute.startswith("_"):
                continue
            target = member.fget if isinstance(member, property) else member
            if (inspect.isfunction(target) or inspect.ismethod(target)) and not (
                target.__doc__ or ""
            ).strip():
                missing.append(f"{module}.{name}.{attribute}")
    assert missing == [], missing


@pytest.mark.parametrize(("module", "name", "function"), FUNCTIONS, ids=_ids(FUNCTIONS))
def test_public_functions_document_their_arguments(module, name, function):
    doc = function.__doc__ or ""
    signature = inspect.signature(function)
    parameters = [
        p
        for p in signature.parameters
        if p not in ("self", "args", "kwargs") and not p.startswith("_")
    ]
    if not parameters:
        return
    assert "Args:" in doc, f"{name} documents no arguments"
    undocumented = [p for p in parameters if f"{p}:" not in doc]
    assert undocumented == [], f"{name} does not document {undocumented}"


RETURNING = [
    row
    for row in FUNCTIONS
    if inspect.signature(row[2]).return_annotation
    not in (inspect.Signature.empty, None, "None")
]


@pytest.mark.parametrize(("module", "name", "function"), RETURNING, ids=_ids(RETURNING))
def test_public_functions_document_what_they_return(module, name, function):
    doc = function.__doc__ or ""
    assert "Returns:" in doc or "Raises:" in doc, f"{name} documents no return value"


@pytest.mark.parametrize(
    "name",
    [
        "dv01",
        "pvbp",
        "money_duration",
        "money_convexity",
        "effective_duration",
        "effective_convexity",
        "key_rate_dv01",
        "convexity_adjustment",
        "year_fraction",
        "par_rate",
        "annuity",
    ],
)
def test_numeric_functions_state_their_units(name):
    doc = getattr(rates_engine, name).__doc__ or ""
    assert any(word in doc for word in UNIT_WORDS), f"{name} states no unit"


@pytest.mark.parametrize(
    ("name", "phrase"),
    [
        ("vega", "per **one unit** of volatility"),
        ("volga", "per **one unit** of volatility"),
        ("vanna", "per **one unit** of volatility"),
        ("theta", "**per year**"),
    ],
)
def test_the_greeks_say_per_what(name, phrase):
    """Not covered by the generic unit check, and the one that matters most.

    A trader reads vega as premium per volatility *point* and theta as decay
    per *day*. These return per unit and per year, a factor of 100 and of
    365. "Sensitivity to volatility" satisfies a keyword check and tells a
    caller nothing about which of the two numbers it is, so the phrasing is
    pinned rather than the presence of a word.
    """
    from rates_engine.fx import garman_kohlhagen

    assert phrase in (getattr(garman_kohlhagen, name).__doc__ or "")


def test_every_exception_explains_itself():
    from rates_engine import errors

    for name in errors.__all__:
        doc = getattr(errors, name).__doc__ or ""
        assert len(doc.strip()) > 20, f"{name} has no useful docstring"
