"""``rateng-mcp``: the CLI's payloads over stdio, and nothing else.

The server is transport. It does not reason, does not decide, and does not
reformat: every tool calls the same handler the corresponding ``--json``
command calls and returns the same document, so PRD-002 AC-6.1's byte
equality is a structural property rather than something kept true by hand.

**Why the handlers are ordinary functions.** The tools live in
:data:`TOOLS` as plain callables over a config mapping. The SDK is imported
inside :func:`main` and wraps them. That keeps the contract testable without
the SDK installed — the equality with the CLI, the error mapping, the absence
of side effects — and leaves only the transport wiring behind an import.
It also means the module imports on a core install, so ``rateng-mcp``'s
missing-extra message is a sentence rather than a traceback.

**What it will not do** (AC-6.4): write a file, open a socket, or invoke a
model. ``tests/test_mcp_server.py`` asserts all three, the first two by
running the handlers under guards that make either fatal.

**The SDK is pinned to 2.x** (``mcp>=2.0,<3``, as PRD-002 specifies), where
the server class is ``MCPServer`` — it was ``FastMCP`` in 1.x. A refusal here
distinguishes the SDK being absent from the SDK being present at a version
this code does not speak, because telling someone to install what they have
already installed sends them the wrong way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rates_engine.cli import _COMMANDS
from rates_engine.errors import (
    IncompatibleDependencyError,
    MissingDependencyError,
    RatesEngineError,
)
from rates_engine.reporting.payloads import dumps, error_payload

__all__ = ["TOOLS", "TOOL_DESCRIPTIONS", "call_tool", "build_server", "main"]

TOOLS: dict[str, Callable[[dict[str, Any] | None], dict[str, Any]]] = dict(_COMMANDS)
"""Every tool, each the same callable the CLI dispatches to.

Taken from the CLI's table rather than re-declared, so a command added there
cannot silently fail to appear here, and a tool here cannot drift from the
command it claims to mirror.
"""

TOOL_DESCRIPTIONS: dict[str, str] = {
    "describe": "Capabilities, conventions, models and exit codes. Needs no config.",
    "list-instruments": "Instruments this build prices, and what each needs. No config.",
    "bootstrap": "Build the discount curve from a config and return it with its four views.",
    "price": "Price the configured swap and return every risk measure that is defined.",
    "hedge": "Size a futures strip against the swap and shock the hedged position.",
    "fx-forward": (
        "USD/MXN forward from spot and two rates, with the cross-currency basis "
        "reported separately from the covered-interest-parity part."
    ),
    "hedge-structures": (
        "Compare seven hedge structures against a transaction exposure: cost, worst "
        "case, best case, upside participation. Compares; does not recommend."
    ),
}
"""One line per tool, for the agent choosing between them."""


def call_tool(name: str, config: dict[str, Any] | None = None) -> str:
    """Run a tool and return the JSON document the CLI would print.

    Args:
        name: One of :data:`TOOLS`.
        config: The configuration mapping, or ``None`` for the tools that
            take none.

    Returns:
        A single JSON document, byte-identical to what
        ``rateng <name> --json`` writes to stdout.

    Raises:
        KeyError: No such tool.
        RatesEngineError: The engine refused. Propagated rather than
            flattened, so :func:`build_server` can map it onto a tool error
            that still names the exception (AC-6.2).
    """
    if name not in TOOLS:
        raise KeyError(f"no tool named {name!r}; this server exposes {sorted(TOOLS)}")
    return dumps(TOOLS[name](config))


def _failure(exc: BaseException) -> str:
    """The error document, for a transport that wants one rather than a raise."""
    return dumps(error_payload(exc))


def build_server() -> Any:
    """Construct the MCP server with every tool registered.

    Returns:
        The SDK's server object, ready to run.

    Raises:
        MissingDependencyError: The MCP SDK is not installed. The message
            names the extra that installs it (AC-6.3) rather than letting a
            ``ModuleNotFoundError`` reach the caller.
        IncompatibleDependencyError: The SDK is installed at a version whose
            server class this code does not know.
    """
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise MissingDependencyError(
            "the MCP server needs the SDK: "
            'pip install "finport-ratesengine[mcp]". Everything the server exposes is '
            "also available from the rateng CLI with --json, which needs no extra."
        ) from exc

    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as exc:
        # The SDK is there; its server class is not where this code looks.
        # 1.x called it mcp.server.fastmcp.FastMCP. Saying "not installed"
        # here would send the caller to reinstall what they already have.
        raise IncompatibleDependencyError(
            "the MCP SDK is installed but does not expose mcp.server.mcpserver.MCPServer, "
            "which this build is written against. That class was FastMCP in mcp 1.x. "
            'Install the range this package pins: pip install "finport-ratesengine[mcp]" '
            '(mcp>=2.0,<3). Everything the server exposes is also available from the '
            "rateng CLI with --json, which needs no extra."
        ) from exc

    server = MCPServer("finport-ratesengine")

    def register(name: str) -> None:
        @server.tool(name=name, description=TOOL_DESCRIPTIONS[name])
        def _tool(config: dict[str, Any] | None = None) -> str:
            try:
                return call_tool(name, config)
            except RatesEngineError as exc:
                # AC-6.2: the named exception reaches the caller with its own
                # message. It has to be the SDK's ToolError specifically —
                # any other exception is flattened into "Error executing tool
                # <name>" with the message dropped, which is the mute wrapper
                # the criterion exists to forbid.
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    for name in TOOLS:
        register(name)
    return server


def main(argv: list[str] | None = None) -> int:
    """Run the server on stdio.

    Args:
        argv: Accepted and ignored; the server takes no arguments.

    Returns:
        ``0`` on a clean shutdown.

    Raises:
        MissingDependencyError: The SDK is not installed.
    """
    del argv
    build_server().run()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
