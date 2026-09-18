"""PRD-002 AC-6.1 to PRD-002 AC-6.4: the server is transport, and these tests hold it to that.

The point of the design under test is that almost nothing here needs the MCP
SDK. The tools are the CLI's own handlers, so byte equality with ``--json``
is structural; the error mapping is a function; the absence of side effects
is a property of the handlers. Only the transport wiring needs the SDK, and
only that is skipped when it is absent.

The two guards below are the load-bearing ones. ``test_no_file_is_written``
and ``test_no_socket_is_opened`` replace :func:`open` and
:class:`socket.socket` with things that raise, so a handler that touches
either fails the test rather than being noticed later.
"""

from __future__ import annotations

import builtins
import json
import socket
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from rates_engine import cli, mcp_server
from rates_engine.conventions import imm_date, next_imm_on_or_after
from rates_engine.errors import (
    ConfigurationError,
    CurveArbitrageError,
    IncompatibleDependencyError,
    MissingDependencyError,
    RatesEngineError,
)
from rates_engine.reporting.payloads import dumps

CONFIGLESS = ("describe", "list-instruments")
NEEDS_CONFIG = ("bootstrap", "price", "hedge")
#: The v3 commands need an `fx` block the v1 config shape does not carry, so
#: they are exercised through their own tests rather than this file's fixture.
NOT_EXERCISED_HERE = ("fx-forward", "hedge-structures")


def _config(as_of, *, price: float = 96.0) -> dict:
    """The same shape ``tests/test_cli_json.py`` drives the CLI with."""
    start = imm_date(2026, 3)
    futures, period = [], imm_date(2026, 3)
    for index in range(9):
        following = next_imm_on_or_after(period + timedelta(days=1))
        futures.append(
            {
                "label": f"SR3-{index + 1}",
                "start": period.isoformat(),
                "end": following.isoformat(),
                "price": price,
            }
        )
        period = following
    return {
        "as_of": as_of.isoformat(),
        "curve": {
            "stub": {
                "end": start.isoformat(),
                "accrual_factor": 1.0 + 0.04 * ((start - as_of).days / 360.0),
            },
            "futures": futures,
            "convexity": {"model": "ho_lee", "sigma": 0.01},
            "long_end_source": None,
        },
        "swap": {
            "effective": start.isoformat(),
            "maturity": period.isoformat(),
            "fixed_rate": 0.04,
            "notional": 100_000_000.0,
            "side": "payer",
        },
        "hedge": {"shocks_bp": [-100, -10, 10, 100]},
    }


@pytest.fixture
def config(as_of) -> dict:
    return _config(as_of)


@pytest.fixture
def config_path(tmp_path, config) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class TestTheToolTableMirrorsTheCLI:
    """A tool that drifts from its command is the failure mode this prevents."""

    def test_the_tools_are_the_commands(self):
        assert set(mcp_server.TOOLS) == set(cli._COMMANDS)

    def test_this_file_accounts_for_every_tool(self):
        """Adding a command must not quietly leave it untested here."""
        assert set(NEEDS_CONFIG) | set(CONFIGLESS) | set(NOT_EXERCISED_HERE) == set(
            mcp_server.TOOLS
        )

    def test_each_tool_is_the_command_itself(self):
        for name, handler in mcp_server.TOOLS.items():
            assert handler is cli._COMMANDS[name]

    def test_every_tool_is_described(self):
        assert set(mcp_server.TOOL_DESCRIPTIONS) == set(mcp_server.TOOLS)
        assert all(d.strip() for d in mcp_server.TOOL_DESCRIPTIONS.values())

    def test_an_unknown_tool_says_what_it_does_have(self):
        with pytest.raises(KeyError) as excinfo:
            mcp_server.call_tool("bootstrap_curve")
        assert "bootstrap" in str(excinfo.value)


class TestByteEqualityWithTheCLI:
    """PRD-002 AC-6.1: the tool returns the document the command prints, exactly."""

    @pytest.mark.parametrize("name", NEEDS_CONFIG)
    def test_it_matches_the_in_process_command(self, name, config):
        assert mcp_server.call_tool(name, config) == dumps(cli._COMMANDS[name](config))

    @pytest.mark.parametrize("name", CONFIGLESS)
    def test_a_configless_tool_matches_too(self, name):
        assert mcp_server.call_tool(name) == dumps(cli._COMMANDS[name](None))

    @pytest.mark.parametrize("name", NEEDS_CONFIG + CONFIGLESS)
    def test_it_matches_the_subprocess_byte_for_byte(self, name, config_path):
        done = subprocess.run(
            [sys.executable, "-m", "rates_engine.cli", name, "--config", str(config_path), "--json"],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        served = mcp_server.call_tool(
            name, json.loads(config_path.read_text()) if name in NEEDS_CONFIG else None
        )
        assert served == done.stdout.rstrip("\n")

    @pytest.mark.parametrize("name", NEEDS_CONFIG + CONFIGLESS)
    def test_the_result_is_one_parseable_document(self, name, config):
        text = mcp_server.call_tool(name, config if name in NEEDS_CONFIG else None)
        assert "\n" not in text
        assert json.loads(text)["schema_version"] == "1.0"

    def test_calling_twice_gives_the_same_bytes(self, config):
        assert mcp_server.call_tool("price", config) == mcp_server.call_tool("price", config)


class TestErrorsReachTheCallerNamed:
    """PRD-002 AC-6.2: the engine's own exception, not a mute wrapper."""

    def test_the_named_exception_propagates(self, config):
        broken = json.loads(json.dumps(config))
        broken["curve"]["futures"][3]["price"] = 130.0
        with pytest.raises(RatesEngineError) as excinfo:
            mcp_server.call_tool("bootstrap", broken)
        assert excinfo.value.exit_code in (1, 2)

    def test_it_is_the_specific_class_not_the_base(self, config):
        broken = json.loads(json.dumps(config))
        broken["curve"]["futures"][3]["price"] = 130.0
        with pytest.raises(CurveArbitrageError):
            mcp_server.call_tool("bootstrap", broken)

    def test_the_message_names_the_instrument(self, config):
        broken = json.loads(json.dumps(config))
        broken["curve"]["futures"][3]["price"] = 130.0
        with pytest.raises(RatesEngineError) as excinfo:
            mcp_server.call_tool("bootstrap", broken)
        assert "SR3-4" in str(excinfo.value)

    @pytest.mark.parametrize("name", NEEDS_CONFIG)
    def test_a_missing_config_is_a_sentence(self, name):
        """The CLI cannot reach this — argparse requires --config. The MCP
        tools take it as an optional argument, so the refusal has to be a
        named one rather than whichever KeyError comes first."""
        with pytest.raises(ConfigurationError) as excinfo:
            mcp_server.call_tool(name)
        assert name in str(excinfo.value)
        assert "describe and list-instruments" in str(excinfo.value)
        assert excinfo.value.exit_code == 1

    def test_a_malformed_config_is_not_a_silent_success(self, config):
        """Not an anticipated refusal, so not a RatesEngineError — but it must
        still raise rather than come back as a curve built from what parsed."""
        broken = json.loads(json.dumps(config))
        del broken["curve"]["futures"][0]["price"]
        with pytest.raises(KeyError):
            mcp_server.call_tool("bootstrap", broken)

    def test_the_failure_document_names_the_exception(self, config):
        broken = json.loads(json.dumps(config))
        broken["curve"]["futures"][3]["price"] = 130.0
        with pytest.raises(RatesEngineError) as excinfo:
            mcp_server.call_tool("bootstrap", broken)
        payload = json.loads(mcp_server._failure(excinfo.value))
        assert payload["error"]["type"] == "CurveArbitrageError"
        assert payload["exit_code"] == excinfo.value.exit_code

    def test_the_same_failure_the_cli_reports(self, config_path, tmp_path):
        broken = json.loads(config_path.read_text())
        broken["curve"]["futures"][3]["price"] = 130.0
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(broken), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, "-m", "rates_engine.cli", "bootstrap", "--config", str(path), "--json"],
            capture_output=True,
            text=True,
        )
        with pytest.raises(RatesEngineError) as excinfo:
            mcp_server.call_tool("bootstrap", broken)
        served = json.loads(mcp_server._failure(excinfo.value))
        assert served == json.loads(done.stdout)
        assert done.returncode == served["exit_code"]


class TestNoSideEffects:
    """PRD-002 AC-6.4: no file, no socket, no model."""

    @pytest.mark.parametrize("name", NEEDS_CONFIG + CONFIGLESS)
    def test_no_file_is_written(self, name, config, monkeypatch):
        real_open = builtins.open

        def guarded(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError(f"tool {name!r} opened {file!r} for writing")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", guarded)
        mcp_server.call_tool(name, config if name in NEEDS_CONFIG else None)

    @pytest.mark.parametrize("name", NEEDS_CONFIG + CONFIGLESS)
    def test_no_socket_is_opened(self, name, config, monkeypatch):
        def guarded(*args, **kwargs):
            raise AssertionError(f"tool {name!r} opened a socket")

        monkeypatch.setattr(socket, "socket", guarded)
        monkeypatch.setattr(socket, "create_connection", guarded)
        mcp_server.call_tool(name, config if name in NEEDS_CONFIG else None)

    @pytest.mark.parametrize("name", NEEDS_CONFIG + CONFIGLESS)
    def test_nothing_is_printed(self, name, config, monkeypatch, capsys):
        mcp_server.call_tool(name, config if name in NEEDS_CONFIG else None)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_the_guard_itself_catches_a_write(self, tmp_path, monkeypatch):
        """If the guard let a write through silently the two tests above would
        pass for the wrong reason."""
        real_open = builtins.open

        def guarded(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("caught")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", guarded)
        with pytest.raises(AssertionError, match="caught"):
            open(tmp_path / "x.txt", "w")

    def test_the_module_names_no_model_client(self):
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        for forbidden in ("anthropic", "openai", "requests.post", "urllib.request"):
            assert forbidden not in source

    def test_importing_the_module_starts_nothing(self):
        done = subprocess.run(
            [sys.executable, "-c", "import rates_engine.mcp_server"],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0
        assert done.stdout == ""


class TestTheMissingSDKIsASentence:
    """PRD-002 AC-6.3: the extra is named, not a bare ModuleNotFoundError."""

    def test_building_without_the_sdk_names_the_extra(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mcp", None)
        with pytest.raises(MissingDependencyError) as excinfo:
            mcp_server.build_server()
        assert "finport-ratesengine[mcp]" in str(excinfo.value)
        assert not isinstance(excinfo.value, IncompatibleDependencyError)

    def test_it_points_at_the_cli_as_the_way_round(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mcp", None)
        with pytest.raises(MissingDependencyError) as excinfo:
            mcp_server.build_server()
        assert "rateng CLI with --json" in str(excinfo.value)

    def test_it_is_not_a_bare_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mcp", None)
        with pytest.raises(MissingDependencyError) as excinfo:
            mcp_server.build_server()
        assert isinstance(excinfo.value.__cause__, ImportError)
        assert not isinstance(excinfo.value, ImportError)

    @staticmethod
    def _sdk_present_but_moved(monkeypatch):
        """An importable ``mcp`` whose server class is not where we look.

        Built rather than skipped, so the case is exercised with or without
        the real SDK installed — a stand-in package satisfies ``import mcp``
        and the submodule set to ``None`` makes the class import fail, which
        is exactly the shape of a version mismatch.
        """
        import types

        monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
        monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)

    def test_an_sdk_at_the_wrong_version_is_not_reported_as_absent(self, monkeypatch):
        """The failure that shipped: mcp 2.x renamed FastMCP to MCPServer, and
        the refusal said "not installed" about an SDK that was installed. The
        two conditions have different fixes, so they are different sentences."""
        self._sdk_present_but_moved(monkeypatch)
        with pytest.raises(IncompatibleDependencyError) as excinfo:
            mcp_server.build_server()
        message = str(excinfo.value)
        assert "installed but does not expose" in message
        assert "mcp>=2.0,<3" in message
        assert "FastMCP in mcp 1.x" in message

    def test_the_incompatible_case_is_still_catchable_as_a_missing_extra(self, monkeypatch):
        self._sdk_present_but_moved(monkeypatch)
        with pytest.raises(MissingDependencyError):
            mcp_server.build_server()

    def test_the_module_imports_without_the_sdk(self):
        """The import must not need the extra, or the error above would never
        get the chance to be raised."""
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['mcp'] = None; "
                "import rates_engine.mcp_server as m; print(sorted(m.TOOLS))",
            ],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert "bootstrap" in done.stdout


class TestTransportWiring:
    """The only part that needs the SDK, skipped by name when it is absent."""

    @pytest.fixture
    def server(self):
        pytest.importorskip(
            "mcp.server.mcpserver",
            reason="the MCP SDK is not installed; install the [mcp] extra to run this",
        )
        return mcp_server.build_server()

    def test_it_registers_every_tool(self, server):
        import anyio

        registered = {t.name for t in anyio.run(server.list_tools)}
        assert registered == set(mcp_server.TOOLS)

    def test_a_registered_tool_returns_the_cli_document(self, server, config):
        import anyio

        result = anyio.run(lambda: server.call_tool("bootstrap", {"config": config}))
        assert result.is_error is False
        text = result.content[0].text
        assert json.loads(text) == json.loads(dumps(cli._COMMANDS["bootstrap"](config)))

    def test_a_refusal_reaches_the_caller_named(self, server, config):
        """PRD-002 AC-6.2 through the transport, which is where it can be lost.

        Anything but the SDK's own ToolError is flattened into "Error
        executing tool <name>" with the message dropped — the mute wrapper
        the criterion forbids. This asserts the message survives.
        """
        import anyio
        from mcp.server.mcpserver.exceptions import ToolError

        broken = json.loads(json.dumps(config))
        broken["curve"]["futures"][3]["price"] = 130.0
        with pytest.raises(ToolError) as excinfo:
            anyio.run(lambda: server.call_tool("bootstrap", {"config": broken}))
        message = str(excinfo.value)
        assert "CurveArbitrageError" in message
        assert "SR3-4" in message

    def test_a_missing_config_also_reaches_the_caller_named(self, server):
        import anyio
        from mcp.server.mcpserver.exceptions import ToolError

        with pytest.raises(ToolError) as excinfo:
            anyio.run(lambda: server.call_tool("price", {"config": None}))
        assert "ConfigurationError" in str(excinfo.value)

    def test_the_console_script_is_installed(self):
        import shutil

        pytest.importorskip("mcp.server.mcpserver", reason="the MCP SDK is not installed")
        assert shutil.which("rateng-mcp"), "the [mcp] extra should put rateng-mcp on PATH"
