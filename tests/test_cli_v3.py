"""The two v3 commands, end to end, and the same document over MCP.

PRD-003 does not name new CLI commands, but the engine's contract is that
anything it computes is reachable as one JSON document — and the MCP server
takes its tool table straight from the CLI's, so a command added here is a
tool there with no second serialisation to keep in step.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import date

import pytest

from rates_engine import mcp_server
from rates_engine.app import commands
from rates_engine.reporting.payloads import dumps

AS_OF = date(2026, 9, 16)

FX_CONFIG = {
    "as_of": AS_OF.isoformat(),
    "fx": {
        "spot": 18.50,
        "delivery": "2026-12-15",
        "r_domestic": 0.0950,
        "r_foreign": 0.0420,
        "basis_bp": -25.0,
        "volatility": 0.115,
    },
    "exposure": {
        "amount": 1_000_000.0,
        "direction": "payable",
        "hedge_ratio": 0.8,
        "correlation": -0.30,
        "foreign_asset_volatility": 0.10,
        "proposed_instrument": "collar",
    },
    "hedge_program": {
        "name": "treasury_2026",
        "target_hedge_ratio": 0.80,
        "discretion_band": 0.10,
        "rebalance_frequency": "monthly",
        "allowed_instruments": ["forward", "collar"],
    },
}


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "fx.json"
    path.write_text(json.dumps(FX_CONFIG), encoding="utf-8")
    return path


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "rates_engine.cli", *args], capture_output=True, text=True
    )


class TestFxForward:
    def test_it_prints_one_document(self, config_path):
        done = _run("fx-forward", "--config", str(config_path), "--json")
        assert done.returncode == 0, done.stderr
        assert done.stdout.count("\n") == 1
        assert json.loads(done.stdout)["schema_version"] == "1.0"

    def test_it_decomposes_the_forward(self, config_path):
        payload = json.loads(_run("fx-forward", "--config", str(config_path), "--json").stdout)
        assert payload["outright"] == pytest.approx(
            payload["cip_forward"] + payload["basis_component"]
        )
        assert payload["basis_bp"] == -25.0

    def test_the_points_are_in_pips(self, config_path):
        payload = json.loads(_run("fx-forward", "--config", str(config_path), "--json").stdout)
        assert payload["pip"] == 1e-4
        assert payload["forward_points"] > 0.0

    def test_a_missing_fx_block_refuses_by_name(self, tmp_path):
        path = tmp_path / "bare.json"
        path.write_text(json.dumps({"as_of": AS_OF.isoformat()}), encoding="utf-8")
        done = _run("fx-forward", "--config", str(path), "--json")
        assert done.returncode == 1
        assert "fx" in json.loads(done.stdout)["error"]["message"]


class TestHedgeStructures:
    def test_it_prints_the_table(self, config_path):
        payload = json.loads(
            _run("hedge-structures", "--config", str(config_path), "--json").stdout
        )
        names = [s["name"] for s in payload["structures"]]
        assert "collar_zero_cost" in names
        assert "seagull" in names

    def test_it_reports_the_trade_off_as_labelled_axes(self, config_path):
        payload = json.loads(
            _run("hedge-structures", "--config", str(config_path), "--json").stdout
        )
        assert "upfront_cost" in payload["trade_off"]["axes"]
        assert payload["trade_off"]["monotone"] == "no"

    def test_it_carries_the_variance_decomposition_when_asked(self, config_path):
        payload = json.loads(
            _run("hedge-structures", "--config", str(config_path), "--json").stdout
        )
        assert payload["residual_variance"]["correlation"] == -0.30

    def test_the_programme_audit_rides_along_when_one_is_configured(self, config_path):
        payload = json.loads(
            _run("hedge-structures", "--config", str(config_path), "--json").stdout
        )
        assert payload["program_audit"]["compliant"] is True

    def test_a_breach_is_reported_rather_than_refused(self, tmp_path):
        config = json.loads(json.dumps(FX_CONFIG))
        config["exposure"]["hedge_ratio"] = 0.20
        path = tmp_path / "breach.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("hedge-structures", "--config", str(path), "--json")
        assert done.returncode == 0
        audit = json.loads(done.stdout)["program_audit"]
        assert audit["compliant"] is False
        assert any(v["severity"] == "breach" for v in audit["violations"])

    def test_no_recommendation_appears_anywhere_in_the_document(self, config_path):
        text = _run("hedge-structures", "--config", str(config_path), "--json").stdout
        assert "recommend" not in text.lower()

    def test_a_missing_exposure_block_refuses_by_name(self, tmp_path):
        config = {k: v for k, v in FX_CONFIG.items() if k != "exposure"}
        path = tmp_path / "noexp.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        done = _run("hedge-structures", "--config", str(path), "--json")
        assert done.returncode == 1
        assert "exposure" in json.loads(done.stdout)["error"]["message"]


class TestDescribeKnowsAboutV3:
    def test_it_lists_the_new_commands(self):
        payload = json.loads(_run("describe", "--json").stdout)
        assert {"fx-forward", "hedge-structures"} <= set(payload["commands"])

    def test_it_lists_both_currencies(self):
        payload = json.loads(_run("describe", "--json").stdout)
        assert payload["currencies"] == ["USD", "MXN"]

    def test_it_names_the_four_delta_conventions_and_no_default(self):
        payload = json.loads(_run("describe", "--json").stdout)["fx"]
        assert len(payload["delta_conventions"]) == 4
        assert payload["delta_convention_default"] is None
        assert payload["atm_convention_default"] is None

    def test_it_says_it_does_not_recommend(self):
        payload = json.loads(_run("describe", "--json").stdout)
        assert payload["recommends"] is False
        assert "does not rank" in payload["recommends_note"]

    def test_it_publishes_the_unresolved_mxn_conventions(self):
        payload = json.loads(_run("describe", "--json").stdout)
        assert "tiie_day_count" in payload["unresolved_conventions"]["MXN"]

    def test_it_names_the_bmv_calendar_rather_than_banxico(self):
        payload = json.loads(_run("describe", "--json").stdout)
        assert payload["calendars_v3"] == ["BMV"]


class TestTheyReachMCPUnchanged:
    """The tool table is the CLI's table, so this is structural."""

    @pytest.mark.parametrize("name", ["fx-forward", "hedge-structures"])
    def test_the_tool_exists_and_is_described(self, name):
        assert name in mcp_server.TOOLS
        assert mcp_server.TOOL_DESCRIPTIONS[name].strip()

    @pytest.mark.parametrize("name", ["fx-forward", "hedge-structures"])
    def test_the_tool_returns_the_cli_document_byte_for_byte(self, name, config_path):
        done = _run(name, "--config", str(config_path), "--json")
        assert done.returncode == 0, done.stderr
        served = mcp_server.call_tool(name, json.loads(config_path.read_text()))
        assert served == done.stdout.rstrip("\n")

    @pytest.mark.parametrize("name", ["fx-forward", "hedge-structures"])
    def test_the_tool_is_the_command(self, name):
        assert mcp_server.TOOLS[name] is commands.COMMANDS[name]

    def test_the_hedge_description_says_it_does_not_recommend(self):
        assert "does not recommend" in mcp_server.TOOL_DESCRIPTIONS["hedge-structures"]

    @pytest.mark.parametrize("name", ["fx-forward", "hedge-structures"])
    def test_calling_without_a_config_refuses_by_name(self, name):
        from rates_engine.core.errors import ConfigurationError

        with pytest.raises(ConfigurationError, match=name):
            mcp_server.call_tool(name)

    def test_the_document_is_the_same_twice(self, config_path):
        config = json.loads(config_path.read_text())
        assert mcp_server.call_tool("fx-forward", config) == dumps(
            commands.COMMANDS["fx-forward"](config)
        )


class TestTheTwoCommandsAgreeOnTheForward:
    """The review's finding 3 under 1.4: hedge-structures ignored `basis_bp`.

    Same config, two commands: fx-forward applied the basis and
    hedge-structures' forward row locked at the parity forward, 1,941 pips
    apart at one year on a -100 bp basis
    (`REVISION-rates_engine-2026-10-15-repro/risk-app/r16_basis_consistency.py`).
    The reference is QuantLib 1.44, not this package: flat continuously
    compounded ACT/365F curves at 9.5% MXN and 4.2% USD, the spot date
    2026-09-18 from `ql.JointCalendar(ql.UnitedStates(ql.UnitedStates.
    FederalReserve), ql.Mexico()).advance(2026-09-16, 2, ql.Days)`, and
    `S [P_usd(T)/P_usd(spot)] / [P_mxn(T)/P_mxn(spot)] = 19.50128422562187`,
    with the documented zero-spread basis applied over the QuantLib
    `Actual365Fixed` span from spot to delivery, 0.9945205479452055.
    """

    QUANTLIB_PARITY = 19.50128422562187
    YEARS_FROM_SPOT = 0.9945205479452055

    @pytest.fixture
    def config(self):
        config = json.loads(json.dumps(FX_CONFIG))
        config["fx"]["delivery"] = "2027-09-16"
        config["fx"]["basis_bp"] = -100.0
        config["exposure"]["hedge_ratio"] = 1.0
        del config["hedge_program"]
        return config

    def test_the_forward_matches_quantlib_with_the_basis(self, config):
        payload = commands.COMMANDS["fx-forward"](config)
        assert payload["cip_forward"] == pytest.approx(self.QUANTLIB_PARITY, rel=1e-12)
        assert payload["outright"] == pytest.approx(
            self.QUANTLIB_PARITY * math.exp(-0.01 * self.YEARS_FROM_SPOT), rel=1e-12
        )

    def test_the_forward_row_locks_at_the_same_outright(self, config):
        outright = commands.COMMANDS["fx-forward"](config)["outright"]
        table = commands.COMMANDS["hedge-structures"](config)
        row = next(s for s in table["structures"] if s["name"] == "forward")
        assert row["worst_case_rate"] == pytest.approx(outright, rel=1e-12)
        assert row["best_case_rate"] == pytest.approx(outright, rel=1e-12)

    def test_the_payload_shows_the_basis_it_used(self, config):
        table = commands.COMMANDS["hedge-structures"](config)
        fields = table["evidence"]["fields"]
        assert fields["basis_bp"] == -100.0
        assert fields["basis_leg"] == "quote"
        assert fields["r_domestic_effective"] == pytest.approx(0.0950 - 0.0100)

    def test_the_options_run_from_the_spot_date_like_the_forward(self, config):
        """The premium is paid on the spot date and an option delivering on
        the settlement date expires the spot lag before it, so the option's
        life is the forward's span, not the valuation date's."""
        table = commands.COMMANDS["hedge-structures"](config)
        assert table["evidence"]["fields"]["expiry"] == pytest.approx(
            self.YEARS_FROM_SPOT, rel=1e-12
        )

    def test_the_table_inherits_the_peso_curves_assumption(self, config):
        """Its rates come off a peso curve, and `llms.txt` promises every
        price discounted on one reads `assumed`."""
        table = commands.COMMANDS["hedge-structures"](config)
        assert table["evidence"]["data_quality"] == "assumed"


class TestTheRatesSayWhatTheyAre:
    """`r_domestic` and `r_foreign` are continuous ACT/365F, and now say so.

    A money-market quote (simple, ACT/360, how SOFR and TIIE are published)
    is a different number for the same rate; read as continuous it moves the
    one-year USD/MXN forward by hundreds of pips with nothing to show for it.
    """

    def test_the_forward_records_the_convention_of_its_inputs(self):
        payload = commands.COMMANDS["fx-forward"](json.loads(json.dumps(FX_CONFIG)))
        inputs = {p["series_id"]: p for p in payload["evidence"]["inputs"]}
        for key in ("fx.r_domestic", "fx.r_foreign"):
            assert inputs[key]["instrument_kind"] == "continuously_compounded_rate_act365f"
            assert "ACT/365F" in inputs[key]["notes"]

    def test_the_builder_documents_it(self):
        from rates_engine.app.builders import flat_curve, fx_inputs

        for function in (fx_inputs, flat_curve):
            doc = function.__doc__ or ""
            assert "continuously compounded" in doc and "ACT/365F" in doc


class TestTheFxBlockIsCheckedBeforeItIsUsed:
    """`REVISION-rates_engine-2026-10-15-repro/risk-app/r14_fx_inputs.py`:
    every one of these was priced and reported exit 0."""

    @staticmethod
    def _config(**fx):
        config = json.loads(json.dumps(FX_CONFIG))
        config["fx"].update(fx)
        return config

    def test_a_volatility_in_percent_refuses(self):
        from rates_engine.core.errors import VolUnitsError

        with pytest.raises(VolUnitsError):
            commands.COMMANDS["hedge-structures"](self._config(volatility=11.5))

    def test_a_negative_volatility_refuses(self):
        with pytest.raises(ValueError, match="volatility"):
            commands.COMMANDS["hedge-structures"](self._config(volatility=-0.115))

    @pytest.mark.parametrize("command", ["fx-forward", "hedge-structures"])
    def test_a_delivery_in_the_past_refuses(self, command):
        with pytest.raises(ValueError, match="not after"):
            commands.COMMANDS[command](self._config(delivery="2026-06-15"))

    @pytest.mark.parametrize("command", ["fx-forward", "hedge-structures"])
    def test_a_rate_in_percent_refuses(self, command):
        from rates_engine.core.errors import ImplausibleInputError

        with pytest.raises(ImplausibleInputError, match="r_domestic"):
            commands.COMMANDS[command](self._config(r_domestic=9.5))

    def test_a_rate_in_percent_is_exit_one_with_a_document(self, tmp_path):
        path = tmp_path / "percent.json"
        path.write_text(json.dumps(self._config(r_domestic=9.5)), encoding="utf-8")
        done = _run("fx-forward", "--config", str(path), "--json")
        assert done.returncode == 1
        error = json.loads(done.stdout)["error"]
        assert error["type"] == "ImplausibleInputError"
        assert "ACT/365F" in error["message"]

    @pytest.mark.parametrize("missing", ["spot", "r_foreign"])
    def test_a_missing_key_refuses_by_name(self, missing):
        from rates_engine.core.errors import ConfigurationError

        config = json.loads(json.dumps(FX_CONFIG))
        del config["fx"][missing]
        with pytest.raises(ConfigurationError, match=missing):
            commands.COMMANDS["fx-forward"](config)

    def test_a_missing_volatility_refuses_by_name(self):
        from rates_engine.core.errors import ConfigurationError

        config = json.loads(json.dumps(FX_CONFIG))
        del config["fx"]["volatility"]
        with pytest.raises(ConfigurationError, match="volatility"):
            commands.COMMANDS["hedge-structures"](config)
