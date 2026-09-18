"""Check every acceptance criterion in every PRD against the tests, and report.

The project's own principle applied to itself: a claim that the acceptance
criteria are met is not a result until you can see what it rests on. This
walks each PRD for criteria, finds which test module names each one, runs the
suite, and prints a line per criterion with what actually happened — including
which ones are only partly covered because their third-party half is skipped.

**Criteria are named with their PRD.** PRD-001 and PRD-002 both have an
``AC-3.1`` and they are different criteria, so a test claiming coverage writes
``PRD-002 AC-3.1``. A bare ``AC-3.1`` in a test is not attributed to anything
and the criterion reads as uncovered, which is the right failure: an
ambiguous claim is not a claim.

Run it from the repository root:

    python scripts/audit_acceptance.py            # every shipped PRD
    python scripts/audit_acceptance.py 002        # just one
    python scripts/audit_acceptance.py --all      # including unstarted ones

Exit code 0 when every criterion is covered, 1 when any is not.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRD_DIR = ROOT / "docs" / "forge" / "prd"
TESTS = ROOT / "tests"

#: A criterion as a test must write it: the PRD, then the identifier.
REFERENCE = re.compile(r"PRD-(\d{3})\s+AC-(\d+\.\d+)")
#: A criterion as a PRD writes it, inside that PRD's own document.
DECLARATION = re.compile(r"AC-(\d+\.\d+)")
#: The heading a PRD gains once its phase has been built. A PRD without one
#: has not been started, so its criteria are not failures — they are the
#: backlog. ``--all`` audits those too.
SHIPPED = "## Estado de implementación"


def prds(only: str | None = None, *, every: bool = False) -> tuple[list[tuple[str, Path]], list[str]]:
    """The PRDs to audit, as ``(number, path)``, in numeric order.

    Args:
        only: A PRD number such as ``"002"``, or ``None`` for the default set.
        every: Include PRDs that have not been built yet.

    Returns:
        The PRDs to audit, and the numbers of those held back as unstarted.

    Raises:
        SystemExit: ``only`` names a PRD that does not exist.
    """
    found: list[tuple[str, Path]] = []
    unstarted: list[str] = []
    for path in sorted(PRD_DIR.glob("*.md")):
        match = re.match(r"(\d{3})-", path.name)
        if not match:
            continue
        number = match.group(1)
        if only is not None and number != only:
            continue
        if only is None and not every and SHIPPED not in path.read_text(encoding="utf-8"):
            unstarted.append(number)
            continue
        found.append((number, path))
    if only and not found:
        raise SystemExit(f"no PRD numbered {only} in {PRD_DIR}")
    return found, unstarted


def criteria(path: Path) -> list[str]:
    """Every acceptance criterion identifier in one PRD, in document order.

    Args:
        path: The PRD document.

    Returns:
        Identifiers like ``"3.7"``, sorted numerically rather than as text so
        that 10.2 follows 9.8.
    """
    found = set(DECLARATION.findall(path.read_text(encoding="utf-8")))
    return sorted(found, key=lambda item: tuple(int(part) for part in item.split(".")))


def coverage() -> dict[tuple[str, str], set[str]]:
    """Which test modules name each criterion.

    Returns:
        ``(prd, criterion)`` to the set of test file names mentioning it.
    """
    found: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in sorted(TESTS.glob("test_*.py")):
        for prd, item in set(REFERENCE.findall(path.read_text(encoding="utf-8"))):
            found[(prd, item)].add(path.name)
    return found


def run_suite() -> tuple[str, set[tuple[str, str]]]:
    """Run the suite and collect which tests were skipped.

    Returns:
        The summary line, and the ``(prd, criterion)`` pairs whose
        third-party half was skipped.
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
            skipped.update(REFERENCE.findall(text))
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


def main(argv: list[str] | None = None) -> int:
    """Print the audit and return an exit code.

    Args:
        argv: Optionally one PRD number, such as ``["002"]``, or ``["--all"]``.

    Returns:
        ``0`` when every criterion is covered, ``1`` otherwise.
    """
    arguments = sys.argv[1:] if argv is None else argv
    every = "--all" in arguments
    selected = next((a for a in arguments if not a.startswith("-")), None)
    documents, unstarted = prds(selected, every=every)
    covered = coverage()
    summary, skipped = run_suite()

    total = 0
    uncovered: list[str] = []
    partial: list[str] = []
    for prd, path in documents:
        items = criteria(path)
        total += len(items)
        print(f"\nPRD-{prd}  ({path.name}): {len(items)} acceptance criteria")
        print(f"{'criterion':<20} {'covered by':<40} outcome")
        print("-" * 92)
        for item in items:
            name = f"PRD-{prd} AC-{item}"
            modules = sorted(covered.get((prd, item), ()))
            if not modules:
                uncovered.append(name)
                outcome = "NOT COVERED"
            elif (prd, item) in skipped:
                partial.append(name)
                outcome = "covered; a third-party comparison is skipped"
            else:
                outcome = "covered"
            print(f"{name:<20} {', '.join(modules):<40} {outcome}")

    print("-" * 92)
    print(f"{total} acceptance criteria, {len(uncovered)} uncovered, {len(partial)} partial")
    print(f"suite: {summary}")
    if unstarted:
        print(
            "not audited (no implementation status recorded yet): "
            + ", ".join(f"PRD-{n}" for n in unstarted)
            + ". Pass --all to include them."
        )
    if partial:
        print(
            "\nPartial criteria compare against numbers published by a third party. "
            "See tests/fixtures/cme_published/README.md for what supplying them turns on."
        )
    if uncovered:
        print(f"\nUNCOVERED: {', '.join(uncovered)}")
    return 1 if uncovered else 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
