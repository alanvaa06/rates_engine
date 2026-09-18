"""AC-9.5: every exception is in ``docs/ERRORS.md``, saying what to do about it.

A refusal contract that drifts from the code is worse than none: it tells a
caller to catch something that no longer exists, or stays silent about the one
they will actually hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rates_engine import errors

DOC = Path(__file__).resolve().parents[1] / "docs" / "ERRORS.md"


@pytest.fixture(scope="module")
def document() -> str:
    assert DOC.exists(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


def test_every_exception_is_documented(document):
    missing = [name for name in errors.__all__ if name not in document]
    assert missing == [], missing


def test_no_exception_is_documented_that_does_not_exist(document):
    # Builtins are allowed to appear: the document explains that reaching for
    # macaulay_duration gets a NotImplementedError rather than an
    # AttributeError, and that sentence is the point of the section.
    import builtins
    import re

    mentioned = set(re.findall(r"`([A-Z][A-Za-z]*Error)`", document))
    known = set(errors.__all__) | {
        name for name in dir(builtins) if name.endswith("Error")
    }
    unknown = sorted(mentioned - known)
    assert unknown == [], unknown


def test_the_document_states_the_exit_codes(document):
    assert "exit code" in document.lower()
    assert "1" in document and "2" in document


def test_every_exception_says_whether_it_is_recoverable(document):
    # The taxonomy table is what carries this, so it has to be there.
    assert "Recoverable" in document or "recoverable" in document


def test_the_exit_codes_on_the_classes_are_one_or_two():
    for name in errors.__all__:
        exception = getattr(errors, name)
        assert exception.exit_code in (1, 2), name


def test_every_exception_descends_from_the_base():
    for name in errors.__all__:
        exception = getattr(errors, name)
        assert issubclass(exception, errors.RatesEngineError), name


def test_data_errors_are_recoverable_and_calculation_errors_are_not():
    assert errors.MissingFixingError.exit_code == 1
    assert errors.UnsupportedConventionError.exit_code == 1
    assert errors.ProxySourceNotDeclaredError.exit_code == 1
    assert errors.CurveArbitrageError.exit_code == 2
    assert errors.IncompleteStripError.exit_code == 2
    assert errors.UndefinedDurationError.exit_code == 2


def test_the_document_is_linked_from_the_package_docstring():
    import rates_engine

    assert "ERRORS.md" in (rates_engine.__doc__ or "")
