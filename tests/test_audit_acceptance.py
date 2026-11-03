"""scripts/audit_acceptance.py fails when the suite it runs fails.

The audit is the answer to "are the acceptance criteria met", and a
criterion whose test is red is not met however many test files name it. The
script used to return 0 for a suite with every test failing, because its
exit code looked only at whether each criterion was *named* by a test.

The suite run is replaced by a stand-in that writes the JUnit report pytest
would have written, so these tests do not run the suite inside the suite.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_acceptance.py"


@pytest.fixture
def audit(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("audit_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The report is written beside ROOT; keep it out of the checkout.
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def _pytest_that(monkeypatch, *, returncode: int, tests: int, failures: int, errors: int = 0):
    """Stand in for the pytest run: write its JUnit report, return its exit code."""

    def run(args, **kwargs):
        target = next(a.split("=", 1)[1] for a in args if str(a).startswith("--junit-xml="))
        Path(target).write_text(
            f'<testsuites><testsuite tests="{tests}" skipped="0" '
            f'failures="{failures}" errors="{errors}"></testsuite></testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, returncode, "", "")

    monkeypatch.setattr(subprocess, "run", run)


def _every_criterion_covered(monkeypatch, audit):
    (_, path), = audit.prds("001")[0]
    covered = {("001", item): {"test_stand_in.py"} for item in audit.criteria(path)}
    monkeypatch.setattr(audit, "coverage", lambda: covered)


def test_a_failing_suite_fails_the_audit(audit, monkeypatch, capsys):
    _every_criterion_covered(monkeypatch, audit)
    _pytest_that(monkeypatch, returncode=1, tests=487, failures=487)
    assert audit.main(["001"]) != 0
    out = capsys.readouterr().out
    # The summary used to read "487 passed, ..., 487 failed": tests minus skips.
    assert "suite: 0 passed, 0 skipped, 487 failed, 0 errored" in out
    assert "SUITE FAILED" in out


def test_a_suite_that_could_not_collect_fails_the_audit(audit, monkeypatch):
    # Collection errors are errors, not failures, and pytest exits 2 for them.
    _every_criterion_covered(monkeypatch, audit)
    _pytest_that(monkeypatch, returncode=2, tests=3, failures=0, errors=3)
    assert audit.main(["001"]) != 0


def test_a_green_suite_with_every_criterion_covered_passes(audit, monkeypatch):
    _every_criterion_covered(monkeypatch, audit)
    _pytest_that(monkeypatch, returncode=0, tests=10, failures=0)
    assert audit.main(["001"]) == 0
