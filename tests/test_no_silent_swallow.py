"""PRD-001 AC-9.4: no handler in ``src/`` catches everything and says nothing.

``except Exception: pass`` is the shape that turns a bug into a wrong number:
the exception carried the only evidence that the computation did not happen,
and discarding it leaves a caller holding a partial result they cannot tell
apart from a complete one.

Two scans. The first finds broad handlers whose body says nothing. The second
finds broad handlers that *do* something but never surface what they caught —
a handler that binds ``as exc`` and re-raises, chains, or puts the exception
into the value it returns is reporting; one that does none of those is
swallowing, whatever its body looks like.

The allowlist is keyed by file rather than by line, so ordinary edits above a
handler do not break this test while a genuinely new handler still does. Every
entry needs a reason, because an exemption without one is a TODO wearing a
decision's clothes. It is checked in both directions: an unlisted site fails,
and so does a listed site that is no longer there.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import rates_engine

SRC = Path(rates_engine.__file__).parent


class Allowed(NamedTuple):
    """One permitted broad handler: where it is, and why it is allowed."""

    path: str
    reason: str


#: Empty, and worth keeping empty. There is one broad handler in the package —
#: the CLI's, which the ``--json`` contract requires so that a failure still
#: leaves one parseable document on stdout — and it does not need an exemption
#: because it surfaces what it caught: it prints the traceback to stderr, puts
#: the exception's type, message and exit code into the payload, and re-raises
#: when ``--json`` was not asked for. The machinery below stays so that the
#: first handler which does need an exemption has to argue for it in writing.
ALLOWED: tuple[Allowed, ...] = ()

_BROAD = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in _BROAD
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(element, ast.Name) and element.id in _BROAD
            for element in handler.type.elts
        )
    return False


def _surfaces_the_exception(handler: ast.ExceptHandler) -> bool:
    """True when the handler passes on what it caught rather than eating it."""
    name = handler.name
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if name and isinstance(node, ast.Name) and node.id == name:
            return True
    return False


def _broad_handlers() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
                continue
            relative = str(path.relative_to(SRC))
            body = node.body
            silent = len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Continue))
            if silent:
                found.append((relative, node.lineno, "empty body"))
            elif not _surfaces_the_exception(node):
                found.append((relative, node.lineno, "never surfaces what it caught"))
    return found


def test_no_unlisted_silent_handler():
    offenders = [
        f"{path}:{line} ({why})"
        for path, line, why in _broad_handlers()
        if path not in {entry.path for entry in ALLOWED}
    ]
    assert offenders == [], offenders


def test_every_allowlist_entry_still_exists():
    # A stale exemption suppresses nothing and reads like outstanding work.
    present = {path for path, _, _ in _broad_handlers()}
    stale = [entry.path for entry in ALLOWED if entry.path not in present]
    assert stale == [], f"allowlist names handlers that are gone: {stale}"


def test_every_allowlist_entry_has_a_reason():
    assert all(len(entry.reason) > 40 for entry in ALLOWED)


def test_the_allowlist_is_empty():
    # Not a tautology: it is the assertion that this package currently needs
    # no exemption at all. Adding one has to break this test first.
    assert ALLOWED == (), f"exemptions in force: {[e.path for e in ALLOWED]}"


def test_the_broad_handler_the_cli_needs_is_not_counted_as_silent():
    # The scan has to be able to tell a reporting handler from a swallowing
    # one, or the allowlist above would have to grow to hold the CLI.
    import ast as ast_module

    source = (SRC / "cli.py").read_text(encoding="utf-8")
    handlers = [
        node
        for node in ast_module.walk(ast_module.parse(source))
        if isinstance(node, ast_module.ExceptHandler) and _is_broad(node)
    ]
    assert handlers, "expected the CLI to catch broadly for the --json contract"
    assert all(_surfaces_the_exception(handler) for handler in handlers)


def test_no_bare_except_anywhere():
    bare: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                bare.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert bare == [], bare


def test_narrow_handlers_are_not_flagged():
    # The scan must not be so blunt that it discourages a precise handler.
    handlers = 0
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        handlers += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and not _is_broad(node)
        )
    assert handlers > 0, "no narrow handlers found; the scan is not exercised"
