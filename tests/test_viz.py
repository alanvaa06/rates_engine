"""The viz command: one HTML page from the payloads, nothing recomputed, nothing recommended."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, timedelta
from importlib import resources

import pytest

from rates_engine.app.commands import COMMANDS, interpolation_gap, viz
from rates_engine.cli import main
from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
from rates_engine.core.errors import ConfigurationError, CurveArbitrageError
from rates_engine.reporting.viz import DATA_MARKER, PANELS, render_page


def _embedded(page: str) -> dict:
    """The mapping the page holds, parsed back out of its script."""
    match = re.search(r"const DATA = (.*?);\n", page)
    assert match, "no DATA assignment in the page"
    return json.loads(match.group(1))


AS_OF = date(2026, 1, 15)


def _rates_config() -> dict:
    """Two years of IMM quarters at a flat futures price, a payer swap across them."""
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
                "price": 96.0,
            }
        )
        period = following
    return {
        "as_of": AS_OF.isoformat(),
        "curve": {
            "stub": {
                "end": start.isoformat(),
                "accrual_factor": 1.0 + 0.04 * ((start - AS_OF).days / 360.0),
            },
            "futures": futures,
        },
        "swap": {
            "effective": start.isoformat(),
            "maturity": period.isoformat(),
            "fixed_rate": 0.04,
            "notional": 100_000_000.0,
            "side": "payer",
        },
        "risk": {"key_tenors": [0.5, 1.0, 2.0]},
        "hedge": {"shocks_bp": [-100, -10, 10, 100]},
    }


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


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rates_engine.cli", *args], capture_output=True, text=True
    )


class TestTemplate:
    """The page ships with the package and holds exactly one place for the data."""

    def test_the_template_ships_with_one_marker(self):
        text = (resources.files("rates_engine.reporting") / "viz.html").read_text(encoding="utf-8")
        assert text.count(DATA_MARKER) == 1

    def test_the_template_is_a_standalone_document_with_no_network(self):
        text = (resources.files("rates_engine.reporting") / "viz.html").read_text(encoding="utf-8")
        assert text.lstrip().lower().startswith("<!doctype html>")
        assert "</html>" in text
        assert "fonts.googleapis.com" not in text
        assert "https://" not in text.split("<script>")[0], "no external stylesheet, font or image"


class TestRenderPage:
    """Python puts the payloads where the template expects them and touches nothing else."""

    def test_the_payloads_round_trip(self):
        payloads = {
            "bootstrap": {"result_type": "BootstrapResult", "x": 1.5, "nested": {"a": [1, 2]}},
            "meta": {"engine_version": "0.0", "generated_at": "2026-09-25T00:00:00+00:00"},
        }
        data = _embedded(render_page(payloads))
        assert data["bootstrap"] == payloads["bootstrap"]
        assert data["meta"] == payloads["meta"]
        for key in PANELS:
            assert key in data, "an absent panel is null, never a missing key"
        assert data["fx"] is None

    def test_a_non_finite_float_becomes_null_like_the_cli(self):
        data = _embedded(render_page({"price": {"value": float("nan")}}))
        assert data["price"]["value"] is None

    def test_a_closing_script_tag_in_a_payload_cannot_end_the_script(self):
        note = "</script><script>alert(1)</script>"
        page = render_page({"price": {"note": note}})
        assert page.count("</script>") == 1
        assert _embedded(page)["price"]["note"] == note

    def test_the_marker_is_gone_from_the_rendered_page(self):
        assert DATA_MARKER not in render_page({})


class TestInterpolationGap:
    """The one number the commands do not print, from the same calibration nodes."""

    def test_it_samples_both_curves_on_the_comparison_dates(self):
        gap = interpolation_gap(_rates_config())
        assert gap["kind"] == "interpolation_comparison"
        assert gap["as_of"] == AS_OF.isoformat()
        n = len(gap["dates"])
        assert n > 10
        assert len(gap["log_linear"]) == len(gap["monotone_convex"]) == len(gap["diff_bp"]) == n
        assert gap["max_forward_difference_date"] in gap["dates"]
        assert max(abs(x) for x in gap["diff_bp"]) == pytest.approx(
            gap["max_forward_difference_bp"], rel=1e-9
        )

    def test_it_refuses_without_a_config(self):
        with pytest.raises(ConfigurationError, match="viz"):
            interpolation_gap(None)


class TestVizUseCase:
    """Three commands, the gap, the renderer, and a receipt that names every panel."""

    def test_without_an_fx_config_the_fx_panels_are_absent(self):
        page, receipt = viz(_rates_config())
        assert receipt["result_type"] == "VizResult"
        assert receipt["path"] is None
        assert receipt["bytes"] == len(page.encode("utf-8"))
        assert receipt["panels"] == {
            "bootstrap": "ok",
            "price": "ok",
            "hedge": "ok",
            "interp": "ok",
            "fx": "absent",
            "structures": "absent",
        }
        data = _embedded(page)
        assert data["bootstrap"] == COMMANDS["bootstrap"](_rates_config())
        assert data["price"]["result_type"] == "PriceResult"
        assert data["hedge"]["result_type"] == "HedgeResult"
        assert data["interp"]["kind"] == "interpolation_comparison"
        assert data["fx"] is None and data["structures"] is None
        assert data["meta"]["as_of"] == AS_OF.isoformat()
        assert data["meta"]["engine_version"]
        assert data["meta"]["generated_at"].endswith("+00:00")

    def test_with_an_fx_config_every_panel_is_ok(self):
        page, receipt = viz(_rates_config(), FX_CONFIG)
        assert receipt["panels"] == dict.fromkeys(PANELS, "ok")
        data = _embedded(page)
        assert data["fx"]["result_type"] == "FXForwardResult"
        assert data["structures"]["result_type"] == "StructureComparison"

    def test_nothing_on_the_page_recommends(self):
        page, _ = viz(_rates_config(), FX_CONFIG)
        assert "recommend" not in page.lower()

    def test_an_interpolation_refusal_is_reported_not_raised(self, monkeypatch):
        import rates_engine.app.commands as commands

        def refuse(*args, **kwargs):
            raise CurveArbitrageError(
                "monotone convex went non-monotone between 2026-03-18 and 2026-06-17"
            )

        monkeypatch.setattr(commands, "compare_interpolations", refuse)
        page, receipt = viz(_rates_config())
        assert receipt["panels"]["interp"] == "reported: CurveArbitrageError"
        assert receipt["panels"]["bootstrap"] == "ok"
        interp = _embedded(page)["interp"]
        assert interp["kind"] == "interpolation_comparison"
        assert interp["error"]["type"] == "CurveArbitrageError"
        assert "went non-monotone" in interp["error"]["message"]

    def test_a_refusal_from_an_inner_command_raises(self):
        config = _rates_config()
        del config["swap"]
        with pytest.raises(KeyError):
            viz(config)

    def test_it_refuses_without_a_config(self):
        with pytest.raises(ConfigurationError, match="viz"):
            viz(None)


class TestCommand:
    """rateng viz: one file written, one receipt on stdout, the same exit codes as everything else."""

    def test_writes_the_page_and_reports_the_panels(self, tmp_path):
        config_path = tmp_path / "c.json"
        config_path.write_text(json.dumps(_rates_config()), encoding="utf-8")
        out = tmp_path / "desk.html"
        done = _run("viz", "--config", str(config_path), "--out", str(out), "--json")
        assert done.returncode == 0, done.stderr
        assert done.stderr == ""
        assert done.stdout.count("\n") == 1
        receipt = json.loads(done.stdout)
        assert receipt["result_type"] == "VizResult"
        assert receipt["path"] == str(out)
        assert receipt["panels"]["fx"] == "absent"
        page = out.read_text(encoding="utf-8")
        assert receipt["bytes"] == len(page.encode("utf-8"))
        assert _embedded(page)["bootstrap"]["result_type"] == "BootstrapResult"

    def test_with_an_fx_config_every_panel_is_ok(self, tmp_path):
        config_path = tmp_path / "c.json"
        config_path.write_text(json.dumps(_rates_config()), encoding="utf-8")
        fx_path = tmp_path / "fx.json"
        fx_path.write_text(json.dumps(FX_CONFIG), encoding="utf-8")
        out = tmp_path / "desk.html"
        done = _run(
            "viz",
            "--config",
            str(config_path),
            "--fx-config",
            str(fx_path),
            "--out",
            str(out),
            "--json",
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["panels"] == dict.fromkeys(PANELS, "ok")
        assert "recommend" not in out.read_text(encoding="utf-8").lower()

    def test_a_refused_config_writes_no_file(self, tmp_path):
        config = _rates_config()
        del config["swap"]
        config_path = tmp_path / "c.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        out = tmp_path / "desk.html"
        done = _run("viz", "--config", str(config_path), "--out", str(out), "--json")
        assert done.returncode == 1
        payload = json.loads(done.stdout)
        assert payload["result_type"] is None
        assert payload["error"]["type"] == "KeyError"
        assert not out.exists()

    def test_a_reported_interpolation_refusal_still_writes_the_file(
        self, tmp_path, monkeypatch, capsys
    ):
        import rates_engine.app.commands as commands

        def refuse(*args, **kwargs):
            raise CurveArbitrageError(
                "monotone convex went non-monotone between 2026-03-18 and 2026-06-17"
            )

        monkeypatch.setattr(commands, "compare_interpolations", refuse)
        config_path = tmp_path / "c.json"
        config_path.write_text(json.dumps(_rates_config()), encoding="utf-8")
        out = tmp_path / "desk.html"
        code = main(["viz", "--config", str(config_path), "--out", str(out), "--json"])
        assert code == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["panels"]["interp"] == "reported: CurveArbitrageError"
        assert "went non-monotone" in out.read_text(encoding="utf-8")

    def test_without_json_the_narration_names_the_file_in_ascii(self, tmp_path):
        config_path = tmp_path / "c.json"
        config_path.write_text(json.dumps(_rates_config()), encoding="utf-8")
        out = tmp_path / "desk.html"
        done = _run("viz", "--config", str(config_path), "--out", str(out))
        assert done.returncode == 0, done.stderr
        assert f"viz: ok -> {out}" in done.stderr
        done.stderr.encode("ascii")
        assert json.loads(done.stdout)["result_type"] == "VizResult"

    def test_out_is_required(self, tmp_path):
        config_path = tmp_path / "c.json"
        config_path.write_text(json.dumps(_rates_config()), encoding="utf-8")
        done = _run("viz", "--config", str(config_path), "--json")
        assert done.returncode == 2
        assert "--out" in done.stderr
