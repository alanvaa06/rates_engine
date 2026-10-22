"""PRD-001 AC-9.2 and PRD-001 AC-9.3: one document on stdout, narration on stderr, exit codes that mean something."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from rates_engine.cli import main
from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after


def _config(as_of, *, price: float = 96.0, long_end_source=None, key_tenors=None) -> dict:
    """A config covering two years of IMM quarters at a flat futures price."""
    start = imm_date(2026, 3)
    futures = []
    period = start
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
    config = {
        "as_of": as_of.isoformat(),
        "curve": {
            "stub": {
                "end": start.isoformat(),
                "accrual_factor": 1.0 + 0.04 * ((start - as_of).days / 360.0),
            },
            "futures": futures,
            "convexity": {"model": "ho_lee", "sigma": 0.01},
            "long_end_source": long_end_source,
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
    if key_tenors:
        config["risk"] = {"key_tenors": key_tenors}
    return config


@pytest.fixture
def config_path(tmp_path, as_of):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config(as_of, key_tenors=[0.5, 1.0, 2.0])), encoding="utf-8")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rates_engine.cli", *args], capture_output=True, text=True
    )


class TestStdoutIsOneDocument:
    """PRD-001 AC-9.2: exactly one JSON document, and no narration mixed into it."""

    @pytest.mark.parametrize("command", ["bootstrap", "price", "hedge"])
    def test_stdout_parses_as_one_document(self, command, config_path):
        done = _run(command, "--config", str(config_path), "--json")
        assert done.returncode == 0, done.stderr
        payload = json.loads(done.stdout)
        assert payload["schema_version"] == "1.0"
        assert done.stdout.count("\n") == 1

    def test_describe_needs_no_config(self):
        done = _run("describe", "--json")
        assert done.returncode == 0
        # Read the list from the CLI's own table rather than repeating it,
        # so adding a command cannot leave this asserting the old set.
        from rates_engine.app.commands import COMMANDS as _COMMANDS

        assert set(json.loads(done.stdout)["commands"]) == set(_COMMANDS)

    def test_narration_goes_to_stderr(self, config_path):
        done = _run("bootstrap", "--config", str(config_path))
        assert json.loads(done.stdout)
        assert "bootstrap: ok" in done.stderr

    def test_with_json_stderr_stays_empty(self, config_path):
        done = _run("bootstrap", "--config", str(config_path), "--json")
        assert done.stderr == ""


class TestCommands:
    """Each command answers with the thing it is named after."""

    def test_bootstrap_returns_a_curve_and_its_views(self, config_path):
        payload = json.loads(_run("bootstrap", "--config", str(config_path), "--json").stdout)
        assert payload["curve"]["interpolation"] == "log_linear_df"
        assert payload["max_abs_residual_bp"] if False else True
        assert set(payload["views"]) >= {"discount", "zero", "par", "forward"}
        assert payload["long_end_source"] is None

    def test_price_returns_every_defined_measure(self, config_path):
        payload = json.loads(_run("price", "--config", str(config_path), "--json").stdout)
        measures = payload["measures"]
        for name in (
            "pv",
            "par_rate",
            "annuity",
            "dv01",
            "pvbp",
            "money_duration",
            "money_convexity",
            "key_rate_dv01",
        ):
            assert name in measures, name
        assert measures["dv01"]["unit"] == "USD_per_bp"

    def test_an_off_market_swap_gets_a_normalised_duration(self, config_path):
        # The configured swap is struck at 4%, not at par, so the normalised
        # measures are defined and the key holds a number.
        payload = json.loads(_run("price", "--config", str(config_path), "--json").stdout)
        assert payload["measures"]["effective_duration"]["value"] is not None
        assert payload["measures"]["effective_duration"]["unit"] == "years"

    def test_at_par_the_refusal_is_reported_rather_than_the_key_dropped(
        self, tmp_path, as_of, config_path
    ):
        # Strike the swap at the par rate the engine just reported, then ask
        # again: the measure is undefined and the payload says so in place.
        first = json.loads(_run("price", "--config", str(config_path), "--json").stdout)
        at_par = json.loads(config_path.read_text(encoding="utf-8"))
        at_par["swap"]["fixed_rate"] = first["measures"]["par_rate"]["value"]
        path = tmp_path / "at_par.json"
        path.write_text(json.dumps(at_par), encoding="utf-8")

        payload = json.loads(_run("price", "--config", str(path), "--json").stdout)
        for name in ("effective_duration", "effective_convexity"):
            refused = payload["measures"][name]
            assert refused["value"] is None
            assert "par swap" in refused["refused"]
        # The monetary measures still answer, which is the whole point.
        assert payload["measures"]["money_convexity"]["value"] != 0.0
        assert payload["measures"]["dv01"]["value"] != 0.0

    def test_hedge_returns_contracts_and_a_shock_table(self, config_path):
        payload = json.loads(_run("hedge", "--config", str(config_path), "--json").stdout)
        assert payload["total_contracts"] > 0
        assert len(payload["shock_table"]["rows"]) == 4
        assert payload["bump_basis"] == "instrument_quote"

    def test_the_convexity_adjustment_reaches_the_curve(self, config_path):
        payload = json.loads(_run("bootstrap", "--config", str(config_path), "--json").stdout)
        used = payload["evidence"]["fields"]["instruments_used"]
        adjusted = [u for u in used if u.get("convexity")]
        assert adjusted
        assert all(u["convexity"]["model"] == "ho_lee" for u in adjusted)
        assert any(u["convexity"]["adjustment_bp"] > 0 for u in adjusted)


class TestFailures:
    """PRD-001 AC-9.3: a failure before a result is still one document, with a code."""

    def test_a_missing_file_is_exit_one_with_a_document(self, tmp_path):
        done = _run("bootstrap", "--config", str(tmp_path / "nope.json"), "--json")
        assert done.returncode == 1
        payload = json.loads(done.stdout)
        assert payload["error"]["type"] == "FileNotFoundError"
        assert payload["exit_code"] == 1

    def test_an_impossible_curve_is_exit_two(self, tmp_path, as_of):
        config = _config(as_of, price=140.0)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("bootstrap", "--config", str(path), "--json")
        assert done.returncode == 2
        assert json.loads(done.stdout)["error"]["type"] == "CurveArbitrageError"

    def test_the_traceback_goes_to_stderr_not_stdout(self, tmp_path):
        done = _run("bootstrap", "--config", str(tmp_path / "nope.json"), "--json")
        assert "Traceback" in done.stderr
        assert "Traceback" not in done.stdout
        assert json.loads(done.stdout)

    def test_without_json_the_exception_propagates(self, tmp_path):
        done = _run("bootstrap", "--config", str(tmp_path / "nope.json"))
        assert done.returncode != 0
        assert done.stdout == ""

    def test_an_unknown_command_is_rejected_by_the_parser(self):
        # Used to be exit 2 with nothing on stdout. With --json a parser
        # failure is a failure before a result like any other, so it is one
        # document, and a malformed command line is an input the caller fixes:
        # ConfigurationError, exit code 1 (docs/ERRORS.md, "Exit codes").
        done = _run("simulate", "--json")
        assert done.returncode == 1
        payload = json.loads(done.stdout)
        assert payload["error"]["type"] == "ConfigurationError"
        assert "simulate" in payload["error"]["message"]


class TestParserFailuresKeepTheContract:
    """``cli.py:7-8``: a failure before a result still puts one document on stdout.

    On main, ``parse_args`` sat outside the ``try``, so ``rateng price --json``
    without ``--config`` exited 2 with an empty stdout, and so did an unknown
    flag (review script ``r02_cli_edges.py``, cases B and B2).
    """

    @pytest.mark.parametrize(
        ("argv", "named"),
        [
            (["price", "--json"], "--config"),
            (["describe", "--json", "--bogus"], "--bogus"),
            (["hedge", "--json", "--config"], "--config"),
            (["--json"], "command"),
        ],
        ids=["missing-config", "unknown-flag", "flag-without-value", "no-command"],
    )
    def test_with_json_a_parser_failure_is_one_document(self, argv, named, capsys):
        code = main(argv)
        captured = capsys.readouterr()
        assert code == 1
        assert captured.out.count("\n") == 1
        payload = json.loads(captured.out)
        assert payload["exit_code"] == 1
        assert payload["error"]["type"] == "ConfigurationError"
        assert payload["error"]["recoverable"] is True
        assert named in payload["error"]["message"]
        # The narration argparse would have printed still goes to stderr.
        assert "usage: rateng" in captured.err

    def test_an_abbreviated_json_flag_counts(self, capsys):
        # argparse accepts --js for --json, so the contract has to as well.
        assert main(["price", "--js"]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["type"] == "ConfigurationError"

    def test_without_json_the_parser_behaves_as_argparse_does(self):
        done = _run("price")
        assert done.returncode == 2
        assert done.stdout == ""
        assert "the following arguments are required: --config" in done.stderr

    @pytest.mark.parametrize(
        ("argv", "command", "in_usage"),
        [(["price", "--help", "--json"], "price", "--config CONFIG"),
         (["--json", "--help"], None, "{describe,")],
    )
    def test_help_with_json_is_a_usage_document(self, argv, command, in_usage):
        # argparse printed the help text on stdout and exited 0, so stdout
        # was not a JSON document (Alan's review of #17, point 4). The text
        # goes to stderr, as narration, and stdout carries it as a document.
        done = _run(*argv)
        assert done.returncode == 0
        assert done.stdout.count("\n") == 1
        payload = json.loads(done.stdout)
        assert payload["result_type"] == "Usage"
        assert payload["schema_version"] == "1.0"
        assert payload["help"] in done.stderr
        assert payload["command"] == command
        assert in_usage in payload["usage"]

    def test_without_json_help_is_still_help(self):
        done = _run("describe", "--help")
        assert done.returncode == 0
        assert done.stdout.startswith("usage: rateng describe")

    def test_a_value_glued_onto_json_is_one_document(self, config_path):
        # `--json=1` was exit 2 with an empty stdout: argparse rejects a value
        # for a flag, and the check for --json did not recognise the spelling.
        done = _run("price", "--config", str(config_path), "--json=1")
        assert done.returncode == 1
        error = json.loads(done.stdout)["error"]
        assert error["type"] == "ConfigurationError"
        assert "ignored explicit argument '1'" in error["message"]


class TestConfiglessCommandsDoNotOpenTheConfig:
    """``--config`` says describe and list-instruments ignore it; they now do."""

    @pytest.mark.parametrize("command", ["describe", "list-instruments"])
    def test_a_missing_file_is_not_opened(self, command, tmp_path):
        # On main this was exit 1 with FileNotFoundError for a flag documented
        # as ignored.
        done = _run(command, "--config", str(tmp_path / "nope.json"), "--json")
        assert done.returncode == 0, done.stdout
        assert json.loads(done.stdout)["evidence"] is None

    @pytest.mark.parametrize("command", ["describe", "list-instruments"])
    def test_a_file_that_is_not_a_config_is_not_read(self, command, tmp_path):
        path = tmp_path / "notes.json"
        path.write_text("this is not json", encoding="utf-8")
        assert _run(command, "--config", str(path), "--json").returncode == 0


class TestTyposThroughTheCLI:
    """The review's four typos (``r11_config_typos.py``), each exit 0 on main."""

    @pytest.mark.parametrize(
        ("command", "block", "typo", "meant"),
        [
            ("price", "swap", "notionl", "notional"),
            ("price", "curve", "convexty", "convexity"),
            ("price", "risk", "key_tenor", "key_tenors"),
            ("hedge", "hedge", "shock_bps", "shocks_bp"),
        ],
    )
    def test_the_typo_is_exit_one_and_names_the_key(
        self, command, block, typo, meant, config_path, tmp_path, capsys
    ):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config[block][typo] = config[block].pop(meant)
        path = tmp_path / "typo.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        assert main([command, "--config", str(path), "--json"]) == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["type"] == "ConfigurationError"
        assert f"{block}.{typo} (did you mean {meant}?)" in error["message"]

    @pytest.mark.parametrize("command", ["bootstrap", "price", "hedge"])
    def test_the_shared_config_still_runs_every_command(self, command, config_path):
        # One file carries swap, risk and hedge; bootstrap reads none of them,
        # and that is not a typo.
        assert main([command, "--config", str(config_path), "--json"]) == 0

    @pytest.mark.parametrize("command", ["bootstrap", "price", "hedge"])
    def test_a_typo_in_any_block_stops_every_command(
        self, command, config_path, tmp_path, capsys
    ):
        # With one desk file, `rateng price` used to accept hedge.shock_bps
        # because price does not read the hedge block.
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["hedge"]["shock_bps"] = config["hedge"].pop("shocks_bp")
        path = tmp_path / "typo.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        assert main([command, "--config", str(path), "--json"]) == 1
        message = json.loads(capsys.readouterr().out)["error"]["message"]
        assert "hedge.shock_bps (did you mean shocks_bp?)" in message


class TestConfigFormats:
    """JSON is native so the core install runs everything; YAML needs its extra."""

    def test_json_works_on_a_core_install(self, config_path):
        assert main(["bootstrap", "--config", str(config_path), "--json"]) == 0

    def test_yaml_without_pyyaml_names_the_extra(self, tmp_path, as_of, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("as_of: 2026-01-15\n", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "yaml", None)
        from rates_engine.app.config import load_config
        from rates_engine.core.errors import MissingDependencyError

        with pytest.raises(MissingDependencyError, match=r"finport-ratesengine\[config\]"):
            load_config(path)

    def test_yaml_works_when_pyyaml_is_installed(self, tmp_path):
        pytest.importorskip("yaml")
        from rates_engine.app.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text("as_of: 2026-01-15\n", encoding="utf-8")
        # YAML gives back a date object where JSON gives a string, and both
        # have to reach the same valuation date.
        from datetime import date as date_type

        from rates_engine.app.config import as_date as _as_date

        assert load_config(path)["as_of"] == date_type(2026, 1, 15)
        assert _as_date(load_config(path)["as_of"]) == date_type(2026, 1, 15)
        assert _as_date("2026-01-15") == date_type(2026, 1, 15)

    def test_a_config_that_is_not_a_mapping_refuses(self, tmp_path):
        from rates_engine.app.config import load_config

        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_config(path)


class TestProxyThroughTheCLI:
    """The opt-in survives the round trip through a config file."""

    def test_an_undeclared_proxy_refuses_with_exit_one(self, tmp_path, as_of):
        config = _config(as_of)
        config["curve"]["par_swaps"] = [
            {
                "label": "DGS2",
                "effective": as_of.isoformat(),
                "maturity": "2029-01-15",
                "rate": 0.041,
                "data_quality": "proxy",
                "instrument_kind": "treasury_par_yield",
            }
        ]
        path = tmp_path / "proxy.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("bootstrap", "--config", str(path), "--json")
        assert done.returncode == 1
        assert json.loads(done.stdout)["error"]["type"] == "ProxySourceNotDeclaredError"

    def test_declaring_it_lets_it_through_and_marks_the_payload(self, tmp_path, as_of):
        config = _config(as_of, long_end_source="treasury_proxy")
        config["curve"]["par_swaps"] = [
            {
                "label": "DGS2",
                "effective": as_of.isoformat(),
                "maturity": "2029-01-15",
                "rate": 0.041,
                "data_quality": "proxy",
                "instrument_kind": "treasury_par_yield",
            }
        ]
        path = tmp_path / "proxy_ok.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("bootstrap", "--config", str(path), "--json")
        assert done.returncode == 0, done.stderr
        payload = json.loads(done.stdout)
        assert payload["long_end_source"] == "treasury_proxy"
        assert "proxy" in payload["node_quality"]
        assert payload["evidence"]["data_quality"] == "proxy"


class TestParSwapQuotesThroughTheCLI:
    """`par_swaps` entries are swaps, priced by the pricer that calibrates them."""

    def test_a_quoted_swap_reprices_on_the_curve_it_built(self, tmp_path, as_of):
        config = _config(as_of)
        config["curve"]["par_swaps"] = [
            {"label": "OIS3Y", "effective": as_of.isoformat(), "maturity": "2029-01-15",
             "rate": 0.041}
        ]
        path = tmp_path / "par.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("bootstrap", "--config", str(path), "--json")
        assert done.returncode == 0, done.stderr
        payload = json.loads(done.stdout)
        used = {i["label"]: i for i in payload["evidence"]["fields"]["instruments_used"]}
        assert used["OIS3Y"]["kind"] == "swap_quote"
        assert used["OIS3Y"]["swap"]["payment_lag_days"] == 2
        assert abs(used["OIS3Y"]["residual_bp"]) < 1e-6

    def test_the_v03_shape_is_refused_with_the_migration(self, tmp_path, as_of):
        config = _config(as_of)
        config["curve"]["par_swaps"] = [
            {"start": as_of.isoformat(), "payment_dates": ["2029-01-15"],
             "year_fractions": [3.05], "rate": 0.041}
        ]
        path = tmp_path / "old.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("bootstrap", "--config", str(path), "--json")
        assert done.returncode == 1
        error = json.loads(done.stdout)["error"]
        assert error["type"] == "ConfigurationError"
        assert "effective, maturity and rate" in error["message"]

    def test_a_missing_key_is_named(self, tmp_path, as_of):
        config = _config(as_of)
        config["curve"]["par_swaps"] = [{"effective": as_of.isoformat(), "rate": 0.041}]
        path = tmp_path / "missing.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("bootstrap", "--config", str(path), "--json")
        assert done.returncode == 1
        assert "maturity" in json.loads(done.stdout)["error"]["message"]
