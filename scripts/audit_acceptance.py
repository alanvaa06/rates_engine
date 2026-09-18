"""Check every acceptance criterion in PRD-001 against the tests, and report.

The project's own principle applied to itself: a claim that the acceptance
criteria are met is not a result until you can see what it rests on. This
walks PRD-001 for criteria, finds which test module names each one, runs the
suite, and prints a line per criterion with what actually happened — including
which ones are only partly covered because their third-party half is skipped.

Run it from the repository root:

    python scripts/audit_acceptance.py

Exit code 0 when every criterion is covered, 1 when any is not.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRD = ROOT / "docs" / "forge" / "prd" / "001-v1-curves-futures-swaps.md"
TESTS = ROOT / "tests"


def criteria() -> list[str]:
    """Every acceptance criterion identifier in PRD-001, in document order.

    Returns:
        Identifiers like ``"3.7"``, sorted numerically rather than as text so
        that 10.2 follows 9.8.
    """
    found = set(re.findall(r"AC-(\d+\.\d+)", PRD.read_text(encoding="utf-8")))
    return sorted(found, key=lambda item: tuple(int(part) for part in item.split(".")))


def coverage() -> dict[str, set[str]]:
    """Which test modules name each criterion.

    Returns:
        Criterion identifier to the set of test file names mentioning it.
    """
    found: dict[str, set[str]] = defaultdict(set)
    for path in sorted(TESTS.glob("test_*.py")):
        for item in set(re.findall(r"AC-(\d+\.\d+)", path.read_text(encoding="utf-8"))):
            found[item].add(path.name)
    return found


def run_suite() -> tuple[str, set[str]]:
    """Run the suite and collect which tests were skipped.

    Returns:
        The summary line, and the criteria whose third-party half was skipped.
    """
    report = ROOT / ".audit-report.json"
    try:
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rs",
             f"--junit-xml={report.with_suffix('.xml')}"],
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        import xml.etree.ElementTree as ElementTree

        tree = ElementTree.parse(report.with_suffix(".xml"))
        # classname is dotted and prefixed with the directory, e.g.
        # "tests.test_futures_settlement.TestSR1Settlement"; the module is the
        # first part that looks like a test module.
        # The skip message names its acceptance criterion, so attribution is
        # exact rather than inferred from the module a skip happens to sit in
        # — a module usually holds several criteria, only one of which is
        # partly covered.
        skipped = set()
        for case in tree.iter("testcase"):
            node = case.find("skipped")
            if node is None:
                continue
            text = (node.get("message") or "") + (node.text or "")
            skipped.update(re.findall(r"AC-(\d+\.\d+)", text))
        totals = tree.getroot().find("testsuite") or tree.getroot()
        summary = (
            f"{int(totals.get('tests', 0)) - int(totals.get('skipped', 0))} passed, "
            f"{totals.get('skipped', 0)} skipped, {totals.get('failures', 0)} failed, "
            f"{totals.get('errors', 0)} errored"
        )
        return summary, skipped
    finally:
        for leftover in (report, report.with_suffix(".xml")):
            leftover.unlink(missing_ok=True)


def main() -> int:
    """Print the audit and return an exit code.

    Returns:
        ``0`` when every criterion is covered, ``1`` otherwise.
    """
    items, covered = criteria(), coverage()
    summary, skipped = run_suite()
    skipped_criteria = skipped

    print(f"{'criterion':<10} {'covered by':<40} outcome")
    print("-" * 82)
    uncovered: list[str] = []
    partial: list[str] = []
    for item in items:
        modules = sorted(covered.get(item, ()))
        if not modules:
            uncovered.append(item)
            outcome = "NOT COVERED"
        elif item in skipped_criteria:
            partial.append(item)
            outcome = "covered; a third-party comparison is skipped"
        else:
            outcome = "covered"
        print(f"AC-{item:<7} {', '.join(modules):<40} {outcome}")

    print("-" * 82)
    print(f"{len(items)} acceptance criteria, {len(uncovered)} uncovered, {len(partial)} partial")
    print(f"suite: {summary}")
    if partial:
        print(
            "\nPartial criteria compare against numbers published by a third party. "
            "See tests/fixtures/cme_published/README.md for what supplying them turns on."
        )
    if uncovered:
        print(f"\nUNCOVERED: {', '.join('AC-' + item for item in uncovered)}")
    return 1 if uncovered else 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
