"""AC-9.6: every public name is documented, and numeric parameters carry units.

The units half is the one that earns its keep. A rate without a compounding
basis and a DV01 quoted per percent are the two ways this kind of library is
wrong by orders of magnitude while looking entirely reasonable.
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


def test_every_module_has_a_docstring():
    missing = [m.__name__ for m in _modules() if not (m.__doc__ or "").strip()]
    assert missing == [], missing


def test_every_exported_name_has_a_docstring():
    missing = []
    for name in rates_engine.__all__:
        obj = getattr(rates_engine, name)
        if inspect.isclass(obj) or inspect.isfunction(obj):
            if not (obj.__doc__ or "").strip():
                missing.append(name)
    assert missing == [], missing


def test_every_public_method_of_an_exported_class_has_a_docstring():
    missing = []
    for name in rates_engine.__all__:
        obj = getattr(rates_engine, name)
        if not inspect.isclass(obj):
            continue
        for attribute, member in vars(obj).items():
            if attribute.startswith("_"):
                continue
            target = member.fget if isinstance(member, property) else member
            if (inspect.isfunction(target) or inspect.ismethod(target)) and not (
                target.__doc__ or ""
            ).strip():
                missing.append(f"{name}.{attribute}")
    assert missing == [], missing


@pytest.mark.parametrize(
    "name",
    [
        n
        for n in rates_engine.__all__
        if inspect.isfunction(getattr(rates_engine, n))
    ],
)
def test_exported_functions_document_their_arguments(name):
    function = getattr(rates_engine, name)
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


@pytest.mark.parametrize(
    "name",
    [
        n
        for n in rates_engine.__all__
        if inspect.isfunction(getattr(rates_engine, n))
        and inspect.signature(getattr(rates_engine, n)).return_annotation
        not in (inspect.Signature.empty, None, "None")
    ],
)
def test_exported_functions_document_what_they_return(name):
    doc = getattr(rates_engine, name).__doc__ or ""
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


def test_every_exception_explains_itself():
    from rates_engine import errors

    for name in errors.__all__:
        doc = getattr(errors, name).__doc__ or ""
        assert len(doc.strip()) > 20, f"{name} has no useful docstring"
