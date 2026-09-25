# `rateng viz` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `viz` subcommand of `rateng` that runs the compute commands on a config, embeds their payloads in one self-contained HTML page, and writes it to a path.

**Architecture:** Three touch points in existing layers. `reporting/viz.py` (layer 7) injects a payload mapping into `reporting/viz.html`, shipped as package data. `app/commands.viz` (layer 8) runs `bootstrap`, `price`, `hedge`, optionally `fx-forward` and `hedge-structures`, computes the interpolation gap through `curves.compare_interpolations`, and returns the page plus a receipt. `cli.py` (layer 9) adds the subparser, writes the file and prints the receipt. `viz` is not in `COMMANDS`, so `rateng-mcp` does not serve it. No pip extra: the page needs the standard library and one template.

**Tech Stack:** Python 3.11+, argparse, `importlib.resources`, `importlib.metadata`, pytest. The page is hand-written HTML, CSS and SVG with no library.

**Spec:** `docs/superpowers/specs/2026-09-24-viz-command-design.md`.

**Conventions that apply everywhere below:**

- Run tests with `.venv/Scripts/python -m pytest <path> -q` from the repo root. Lint with `.venv/Scripts/python -m ruff check src tests` and `.venv/Scripts/python -m mypy src`.
- Console output is ASCII only. No arrows, ticks or em-dashes in `print`, argparse help or narration.
- Tests must not import other test modules (`tests/test_layering.py` checks this). Build configs inline.
- Every commit message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Work on branch `feat/viz-command`, already created and holding the spec.

---

## File structure

| Path | Action | Responsibility |
| --- | --- | --- |
| `src/rates_engine/reporting/viz.html` | create | The page. Static; holds one marker `/*__DATA__*/null`. Full text in Appendix A. |
| `src/rates_engine/reporting/viz.py` | create | `render_page(payloads) -> str`, `template_text()`, `DATA_MARKER`, `PANELS`. |
| `src/rates_engine/app/commands.py` | modify | Add `interpolation_gap(config)` and `viz(config, fx_config=None)`; extend `__all__` and imports. `COMMANDS` unchanged. |
| `src/rates_engine/cli.py` | modify | `viz` subparser after the `COMMANDS` loop; dispatch and file write in `main`. |
| `pyproject.toml` | modify | `package-data` gains `reporting/*.html`. |
| `tests/test_viz.py` | create | Renderer, use case and CLI tests. |
| `AGENTS.md`, `README.md`, `llms.txt`, `CHANGELOG.md`, `docs/ERRORS.md` | modify | One-line mentions, the changelog entry, the reported failure. |

---

### Task 1: Ship the template as package data

**Files:**
- Create: `src/rates_engine/reporting/viz.html` (Appendix A, verbatim)
- Modify: `pyproject.toml:74-75`
- Test: `tests/test_viz.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_viz.py`:

```python
"""The viz command: one HTML page from the payloads, nothing recomputed, nothing recommended."""

from __future__ import annotations

from importlib import resources

DATA_MARKER = "/*__DATA__*/null"


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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py -q`
Expected: 2 failed, `FileNotFoundError` for `viz.html`.

- [ ] **Step 3: Write the template**

Create `src/rates_engine/reporting/viz.html` with the exact content of Appendix A at the end of this plan. Do not reformat it.

- [ ] **Step 4: Declare it as package data**

In `pyproject.toml` change

```toml
[tool.setuptools.package-data]
rates_engine = ["py.typed"]
```

to

```toml
[tool.setuptools.package-data]
rates_engine = ["py.typed", "reporting/*.html"]
```

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py -q`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/rates_engine/reporting/viz.html pyproject.toml tests/test_viz.py
git commit -m "feat(reporting): ship the viz page template as package data

One static HTML page that draws the CLI payloads. Holds a single data
marker; Python fills it in the next commit.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `reporting.viz.render_page`

**Files:**
- Create: `src/rates_engine/reporting/viz.py`
- Test: `tests/test_viz.py`

- [ ] **Step 1: Write the failing tests**

Replace the top of `tests/test_viz.py` so the marker comes from the module, and add the renderer tests:

```python
"""The viz command: one HTML page from the payloads, nothing recomputed, nothing recommended."""

from __future__ import annotations

import json
import re
from importlib import resources

from rates_engine.reporting.viz import DATA_MARKER, PANELS, render_page


def _embedded(page: str) -> dict:
    """The mapping the page holds, parsed back out of its script."""
    match = re.search(r"const DATA = (.*?);\n", page)
    assert match, "no DATA assignment in the page"
    return json.loads(match.group(1))


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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'rates_engine.reporting.viz'`.

- [ ] **Step 3: Write the module**

Create `src/rates_engine/reporting/viz.py`:

```python
"""One HTML page from the engine's payloads: the JSON surface, drawn (layer 7).

The page is a static template shipped next to this module. Python's only job
is to put a mapping of payloads where the template expects it; every chart is
drawn by the page from fields the CLI already prints, and nothing is
recomputed here. The template opens with no data too: every panel then shows
which file would fill it, and a loader button accepts payloads from another
machine.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources
from typing import Any

from rates_engine.reporting.payloads import dumps

__all__ = ["DATA_MARKER", "PANELS", "render_page", "template_text"]

DATA_MARKER = "/*__DATA__*/null"
"""The one place in the template where the payloads go. Exactly one occurrence."""

PANELS: tuple[str, ...] = ("bootstrap", "price", "hedge", "interp", "fx", "structures")
"""The payload keys the page knows how to draw, in page order."""


def template_text() -> str:
    """The template as shipped.

    Read on every call rather than at import, so importing the package
    costs no file read.

    Returns:
        The HTML text.
    """
    return (resources.files("rates_engine.reporting") / "viz.html").read_text(encoding="utf-8")


def render_page(payloads: Mapping[str, Mapping[str, Any] | None]) -> str:
    """Embed the payloads in the template and return the page.

    Args:
        payloads: Up to the six panel keys in :data:`PANELS` plus ``meta``.
            A missing or ``None`` key leaves that panel in its empty state;
            the page then shows the file name that would fill it.

    Returns:
        The complete HTML document.

    Raises:
        ValueError: The template does not hold :data:`DATA_MARKER` exactly
            once, which means the shipped file has been edited by hand.
    """
    template = template_text()
    if template.count(DATA_MARKER) != 1:
        raise ValueError(f"viz.html must hold {DATA_MARKER!r} exactly once")
    data = {key: payloads.get(key) for key in (*PANELS, "meta")}
    # ``dumps`` renders NaN and infinities as null, like the CLI. The escape
    # keeps a payload string from closing the script element: ``<\/`` is a
    # legal JSON escape for ``</`` and the browser's JSON parser undoes it.
    text = dumps(data).replace("</", "<\\/")
    return template.replace(DATA_MARKER, text)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py -q`
Expected: 6 passed.

- [ ] **Step 5: Lint and type-check**

Run: `.venv/Scripts/python -m ruff check src tests && .venv/Scripts/python -m mypy src`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add src/rates_engine/reporting/viz.py tests/test_viz.py
git commit -m "feat(reporting): render_page embeds payloads in the viz template

Serialised through the same dumps the CLI uses, with </ escaped so no
payload string can close the script tag.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `app.commands.interpolation_gap` and `app.commands.viz`

**Files:**
- Modify: `src/rates_engine/app/commands.py` (imports at lines 11-49, `__all__` at 52-62, new functions before `Command = ...` at line 395)
- Test: `tests/test_viz.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viz.py`. Add the imports at the top of the file (keep the existing ones):

```python
from datetime import date, timedelta

import pytest

from rates_engine.app.commands import COMMANDS, interpolation_gap, viz
from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
from rates_engine.core.errors import ConfigurationError, CurveArbitrageError
```

Then add, after `_embedded`:

```python
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
```

And the test classes at the end of the file:

```python
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
            raise CurveArbitrageError("monotone convex went non-monotone between 2026-03-18 and 2026-06-17")

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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py -q`
Expected: collection error, `ImportError: cannot import name 'interpolation_gap'`.

- [ ] **Step 3: Add the imports and `__all__` entries**

In `src/rates_engine/app/commands.py`, after `from dataclasses import replace` add:

```python
from datetime import datetime, timezone
from importlib import metadata
```

Change the errors import to:

```python
from rates_engine.core.errors import (
    BootstrapResidualError,
    ConfigurationError,
    CurveArbitrageError,
    UndefinedDurationError,
)
```

After `from rates_engine.curves.bootstrap import bootstrap_discount_curve` add:

```python
from rates_engine.curves.comparison import compare_interpolations
```

After `from rates_engine.reporting.payloads import result_payload` add:

```python
from rates_engine.reporting.viz import PANELS, render_page
```

Extend `__all__`:

```python
__all__ = [
    "COMMANDS",
    "Command",
    "describe",
    "list_instruments",
    "bootstrap",
    "price",
    "hedge",
    "fx_forward",
    "hedge_structures",
    "interpolation_gap",
    "viz",
]
```

- [ ] **Step 4: Add the two functions**

Insert immediately before the line `Command = Callable[[dict[str, Any] | None], dict[str, Any]]`:

```python
def interpolation_gap(config: dict[str, Any] | None) -> dict[str, Any]:
    """Both interpolations on the config's calibration nodes, and the gap between them.

    The one number the commands do not print. Both fits reprice every
    instrument; they differ only in the instantaneous forward between nodes,
    which no instrument constrains.

    Args:
        config: The whole configuration mapping. ``None`` is refused by name.

    Returns:
        A mapping with ``kind`` ``"interpolation_comparison"``, the sampled
        ``dates``, the instantaneous forward under ``log_linear`` and
        ``monotone_convex`` on each, their difference ``diff_bp``, and the
        comparison's own maxima.

    Raises:
        ConfigurationError: No config, or a malformed calibration block.
        CurveArbitrageError: Either bootstrap produced a non-monotone curve.
        BootstrapResidualError: Either bootstrap missed its tolerance.
    """
    config = require_config(config, "viz")
    as_of = as_date(config["as_of"])
    comparison = compare_interpolations(
        as_of,
        calibration_nodes(config, as_of),
        step_days=7,
        long_end_source=(config.get("curve") or {}).get("long_end_source"),
    )
    dates = comparison.sample_dates
    log_linear = [comparison.log_linear.instantaneous_forward(day) for day in dates]
    monotone = [comparison.monotone_convex.instantaneous_forward(day) for day in dates]
    return {
        "kind": "interpolation_comparison",
        "as_of": as_of.isoformat(),
        "dates": [day.isoformat() for day in dates],
        "log_linear": log_linear,
        "monotone_convex": monotone,
        "diff_bp": [(b - a) * 1e4 for a, b in zip(log_linear, monotone, strict=True)],
        "max_residual_bp": comparison.max_residual_bp,
        "max_forward_difference_bp": comparison.max_forward_difference_bp,
        "max_forward_difference_date": comparison.max_forward_difference_date.isoformat(),
        "max_df_difference": comparison.max_df_difference,
    }


def _engine_version() -> str:
    """The installed distribution's version, or ``"unknown"`` outside an install.

    Read from the distribution metadata rather than from the package root,
    because nothing inside the package imports the package root.
    """
    try:
        return metadata.version("finport-ratesengine")
    except metadata.PackageNotFoundError:
        return "unknown"


def viz(
    config: dict[str, Any] | None, fx_config: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """The payloads drawn on one HTML page, and a receipt naming every panel.

    Not a :data:`Command`: it takes two configs and returns HTML, so it hangs
    off the CLI parser directly and the MCP server does not serve it.

    Args:
        config: The curve, swap and hedge configuration. ``None`` is refused.
        fx_config: The configuration with the ``fx`` block. ``None`` leaves
            the FX and structures panels absent.

    Returns:
        ``(page, receipt)``. The receipt is a ``VizResult`` payload whose
        ``panels`` maps each panel to ``"ok"``, ``"absent"`` or
        ``"reported: <ExceptionType>"``; ``path`` is ``None`` here and the
        CLI fills it after writing.

    Raises:
        ConfigurationError: No config.
        RatesEngineError: Whatever an inner command raises, unchanged. Only
            the interpolation gap's own refusals are reported instead.
    """
    config = require_config(config, "viz")
    payloads: dict[str, dict[str, Any] | None] = {}
    panels: dict[str, str] = {}
    payloads["bootstrap"] = bootstrap(config)
    payloads["price"] = price(config)
    payloads["hedge"] = hedge(config)
    panels.update(bootstrap="ok", price="ok", hedge="ok")
    try:
        payloads["interp"] = interpolation_gap(config)
        panels["interp"] = "ok"
    except (CurveArbitrageError, BootstrapResidualError) as exc:
        payloads["interp"] = {
            "kind": "interpolation_comparison",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        panels["interp"] = f"reported: {type(exc).__name__}"
    if fx_config is None:
        payloads["fx"] = None
        payloads["structures"] = None
        panels.update(fx="absent", structures="absent")
    else:
        payloads["fx"] = fx_forward(fx_config)
        payloads["structures"] = hedge_structures(fx_config)
        panels.update(fx="ok", structures="ok")
    payloads["meta"] = {
        "engine_version": _engine_version(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_date(config["as_of"]).isoformat(),
    }
    page = render_page(payloads)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "result_type": "VizResult",
        "path": None,
        "bytes": len(page.encode("utf-8")),
        "panels": {key: panels[key] for key in PANELS},
        "evidence": None,
    }
    return page, receipt
```

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py -q`
Expected: 14 passed.

- [ ] **Step 6: Run the layering and side-effect guards**

Run: `.venv/Scripts/python -m pytest tests/test_layering.py tests/test_import_side_effects.py tests/test_cli_json.py tests/test_cli_v3.py tests/test_mcp_server.py -q`
Expected: all passed. In particular `test_describe_needs_no_config` still passes because `COMMANDS` is unchanged.

- [ ] **Step 7: Lint and type-check**

Run: `.venv/Scripts/python -m ruff check src tests && .venv/Scripts/python -m mypy src`
Expected: no findings. If ruff reorders the imports under rule `I`, accept its ordering.

- [ ] **Step 8: Commit**

```bash
git add src/rates_engine/app/commands.py tests/test_viz.py
git commit -m "feat(app): viz use case renders the payloads into one page

Runs bootstrap, price and hedge, optionally the two FX commands, and the
interpolation gap from the same calibration nodes. The gap's own refusal
is reported in the panel rather than raised; everything else raises as
the command it came from would. Not in COMMANDS.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The `viz` subcommand

**Files:**
- Modify: `src/rates_engine/cli.py`
- Test: `tests/test_viz.py`

- [ ] **Step 1: Write the failing tests**

Add to the imports at the top of `tests/test_viz.py`:

```python
import subprocess
import sys

from rates_engine.cli import main
```

Add after `FX_CONFIG`:

```python
def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rates_engine.cli", *args], capture_output=True, text=True
    )
```

Add at the end of the file:

```python
class TestCommand:
    """`rateng viz`: one file written, one receipt on stdout, the same exit codes as everything else."""

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
            "viz", "--config", str(config_path), "--fx-config", str(fx_path),
            "--out", str(out), "--json",
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

    def test_a_reported_interpolation_refusal_still_writes_the_file(self, tmp_path, monkeypatch, capsys):
        import rates_engine.app.commands as commands

        def refuse(*args, **kwargs):
            raise CurveArbitrageError("monotone convex went non-monotone between 2026-03-18 and 2026-06-17")

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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py::TestCommand -q`
Expected: 6 failed. The subprocess ones fail with argparse `invalid choice: 'viz'` on stderr and return code 2; the in-process one raises `SystemExit`.

- [ ] **Step 3: Add the subparser and the dispatch**

Replace `src/rates_engine/cli.py` from the imports down with:

```python
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

from rates_engine.app.commands import COMMANDS, viz
from rates_engine.app.config import load_config
from rates_engine.reporting.payloads import dumps, error_payload

__all__ = ["main", "build_parser"]

_EXIT_OK = 0


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for ``rateng``.

    Returns:
        The parser, with one subcommand per entry in the command table and
        ``viz``, which is not a table entry because it writes a file rather
        than printing a payload.
    """
    parser = argparse.ArgumentParser(
        prog="rateng",
        description="Deterministic SOFR rates engine: curves, futures, swaps, risk, hedging.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub = subparsers.add_parser(name, help=f"{name} command")
        if name not in ("describe", "list-instruments"):
            sub.add_argument("--config", required=True, help="JSON or YAML configuration file")
        else:
            sub.add_argument(
                "--config", required=False, help="ignored by describe and list-instruments"
            )
        sub.add_argument(
            "--json",
            action="store_true",
            help="write one JSON document to stdout and all narration to stderr",
        )
    page = subparsers.add_parser("viz", help="draw the payloads on one HTML page")
    page.add_argument(
        "--config", required=True, help="JSON or YAML configuration for the curve, swap and hedge"
    )
    page.add_argument(
        "--fx-config",
        required=False,
        help="configuration with the fx block; without it the FX panels stay empty",
    )
    page.add_argument("--out", required=True, help="path of the HTML file to write")
    page.add_argument(
        "--json",
        action="store_true",
        help="write one JSON receipt to stdout and all narration to stderr",
    )
    return parser


def _write_page(args: argparse.Namespace, config: dict[str, Any] | None) -> dict[str, Any]:
    """Render the page, write it, and return the receipt with ``path`` filled in."""
    fx_config = load_config(args.fx_config) if args.fx_config else None
    page, receipt = viz(config, fx_config)
    out = Path(args.out)
    out.write_text(page, encoding="utf-8")
    receipt["path"] = str(out)
    return receipt


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when the inputs are unusable, ``2`` when the
        calculation is impossible. With ``--json`` stdout holds exactly one
        JSON document either way.
    """
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config) if args.config else None
        if args.command == "viz":
            payload = _write_page(args, config)
        else:
            payload = COMMANDS[args.command](config)
    except BaseException as exc:  # noqa: BLE001 - re-raised below unless --json
        if not args.json:
            raise
        traceback.print_exc(file=sys.stderr)
        failure = error_payload(exc)
        sys.stdout.write(dumps(failure) + "\n")
        return int(failure["exit_code"])

    if not args.json:
        where = f" -> {args.out}" if args.command == "viz" else ""
        print(f"{args.command}: ok{where}", file=sys.stderr)
    sys.stdout.write(dumps(payload) + "\n")
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
```

Keep the module docstring at the top of the file as it is, and add this sentence to its end: `The one subcommand outside the table is ``viz``, which writes an HTML page and prints a receipt naming what landed.`

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_viz.py -q`
Expected: 20 passed.

- [ ] **Step 5: Run the CLI suites that share the parser**

Run: `.venv/Scripts/python -m pytest tests/test_cli_json.py tests/test_cli_v3.py tests/test_mcp_server.py -q`
Expected: all passed.

- [ ] **Step 6: Lint and type-check**

Run: `.venv/Scripts/python -m ruff check src tests && .venv/Scripts/python -m mypy src`
Expected: no findings.

- [ ] **Step 7: Try it by hand once**

Run:

```bash
.venv/Scripts/python scripts/write_example_config.py /tmp/c.json && .venv/Scripts/rateng viz --config /tmp/c.json --out /tmp/desk.html && .venv/Scripts/python -c "print(open('/tmp/desk.html', encoding='utf-8').read()[:60])"
```

Expected: stderr `viz: ok -> /tmp/desk.html`, stdout one JSON receipt, then `<!doctype html>`. If `write_example_config.py` writes a config without a `swap` block, use the config from `tests/test_viz.py::_rates_config` instead. Open the file in a browser and confirm the curve, risk and hedge sections draw and the FX sections show their empty state.

- [ ] **Step 8: Commit**

```bash
git add src/rates_engine/cli.py tests/test_viz.py
git commit -m "feat(cli): rateng viz writes the page and prints a receipt

--config, optional --fx-config, --out. Same exit codes as every other
command; a refusal writes no file.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Documentation

**Files:**
- Modify: `AGENTS.md:277-279` (new paragraph after), `AGENTS.md:305-307` (map rows), `AGENTS.md:319` (CLI block)
- Modify: `README.md:102` (CLI block), `README.md:105-108` (paragraph after)
- Modify: `llms.txt:19-24`
- Modify: `CHANGELOG.md:17` (new `### Added` before `### Changed (breaking)`)
- Modify: `docs/ERRORS.md:403-405` (after the degradations table)

- [ ] **Step 1: AGENTS.md, the CLI block**

After the line

```
rateng hedge-structures --config fx.json --json # eight structures, costed side by side
```

add

```
rateng viz --config c.json --fx-config fx.json --out desk.html  # every payload above, drawn on one page
```

- [ ] **Step 2: AGENTS.md, the map rows**

Replace the three rows:

```
| `reporting` | 7 | JSON payloads and error payloads; `viz` (the payloads drawn on one HTML page, template shipped beside it) |
| `app` | 8 | The use cases: `config` (loading, dates), `builders` (config blocks to domain objects), `commands` (one function per command and `COMMANDS`, the table every interface dispatches through; `viz`, which is not in the table) |
| `cli` | 9 | `rateng bootstrap / price / hedge / describe / list-instruments / fx-forward / hedge-structures / viz` |
```

- [ ] **Step 3: AGENTS.md, the bite**

After the paragraph that begins `**Nothing here reads the network except` and before `## Where things live`, add:

```
**`rateng viz` is not in `COMMANDS`, and `rateng-mcp` does not serve it.**
A `Command` maps one config to one JSON payload and the MCP server mirrors
that table byte for byte. `viz` takes two configs and writes HTML, so it
hangs off the CLI parser directly and prints a `VizResult` receipt naming
each panel's state instead of a payload. The page recomputes nothing: every
chart reads fields of the payloads the other commands print. The one
number they do not print, the instantaneous-forward gap between the two
interpolations, comes from `curves.compare_interpolations`, and when
monotone convex refuses nodes that log-linear accepted that refusal is
reported in the panel rather than raised. No pip extra: the template is
package data, and the page makes no network request.
```

- [ ] **Step 4: README.md**

After the line

```
rateng hedge-structures --config fx.json --json  # eight structures, costed side by side
```

add

```
rateng viz --config c.json --fx-config fx.json --out desk.html  # every payload above on one page
```

After the paragraph that begins `` `python scripts/write_example_config.py config.json` `` add a paragraph:

```
`rateng viz` draws every payload on one self-contained HTML page: the four
curve views and the gap between the two interpolations, key-rate DV01, the
futures strip with its shock table, the FX forward beside its basis, and the
eight hedge-structure payoffs with one brought forward at a time. No extra
and no network; the page also accepts `--json` files dropped onto it, so
payloads from `rateng-mcp` or another machine fit. It compares and draws; it
does not rank.
```

- [ ] **Step 5: llms.txt**

Replace

```
Three ways in. As a library, importing `rates_engine`. As a CLI, `rateng`,
where `bootstrap`, `price`, `hedge`, `describe`, `list-instruments`,
`fx-forward` and `hedge-structures` take `--json` and put one parseable
document on stdout with all narration on stderr. As an MCP server,
```

with

```
Three ways in. As a library, importing `rates_engine`. As a CLI, `rateng`,
where `bootstrap`, `price`, `hedge`, `describe`, `list-instruments`,
`fx-forward` and `hedge-structures` take `--json` and put one parseable
document on stdout with all narration on stderr, and `viz` draws those
payloads on one self-contained HTML page. As an MCP server,
```

- [ ] **Step 6: CHANGELOG.md**

Before `### Changed (breaking)` under `## [Unreleased]`, add:

```
### Added

- **`rateng viz`.** One self-contained HTML page from the payloads:
  `--config` runs `bootstrap`, `price` and `hedge`; `--fx-config` adds
  `fx-forward` and `hedge-structures`; `--out` names the file. The page
  draws the four curve views, the instantaneous-forward gap between the two
  interpolations, key-rate DV01, the futures strip with its shock table, the
  FX forward beside its basis, and the eight hedge-structure payoffs, and it
  accepts `--json` files dropped onto it. Every number is a field of a
  payload the CLI already prints. No pip extra: the template is package data
  and the page makes no network request. The command prints a `VizResult`
  receipt naming each panel's state (`ok`, `absent`, or
  `reported: <ExceptionType>` for the interpolation gap) and is not in
  `COMMANDS`, so `rateng-mcp` does not serve it.

```

- [ ] **Step 7: docs/ERRORS.md**

After the paragraph that begins `Read them with` in the section `## Not an exception: degradations`, add:

```
`rateng viz` reports one more failure instead of raising it. Its
interpolation-gap panel bootstraps the config's nodes a second time under
monotone convex, and that fit can raise `CurveArbitrageError` or
`BootstrapResidualError` where log-linear did not. The page is still worth
writing without that one panel, so the command writes it, puts
`{"error": {"type": ..., "message": ...}}` in the panel's payload, and says
`"interp": "reported: CurveArbitrageError"` in its receipt. The exit code
stays 0. Every other refusal inside `viz` raises as it would from the
command it came from, with that command's exit code, and no file is written.
```

- [ ] **Step 8: Run the whole suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all passed. `tests/test_layering.py::test_the_module_map_in_agents_md_lists_every_package` reads the map rows you edited; keep the package names in backticks in the first column.

- [ ] **Step 9: Commit**

```bash
git add AGENTS.md README.md llms.txt CHANGELOG.md docs/ERRORS.md
git commit -m "docs: rateng viz in the map, the CLI block, the changelog and ERRORS

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review against the spec

- Renderer, marker, `</` escape, `dumps` for non-finite floats: Task 2.
- Package data: Task 1.
- `viz` runs three commands, optional two FX commands, interpolation gap with `long_end_source` pass-through, reported failure for `CurveArbitrageError` and `BootstrapResidualError`, `meta`, receipt with `path` `None`: Task 3. The receipt also carries `"evidence": null`, which the spec did not list; it follows the payload rule that an absent value is null rather than a missing key.
- CLI flags, subparser after the loop, dispatch before the table, ASCII narration, no file on refusal, same exit codes: Task 4.
- Template changes (doctype, no fonts, `embedded` chip, error panel, meta footer, loader, key-rate lookup): Appendix A.
- Tests 1 to 8 from the spec: Tasks 1 to 4. Test 6 (config without `swap`) asserts the `KeyError` the `price` command raises today; it is exit 1 and recoverable, which is the spec's requirement.
- Docs: Task 5.
- Out of scope confirmed: `COMMANDS`, `describe` and the MCP server are untouched; no version bump.

---

## Appendix A: `src/rates_engine/reporting/viz.html`

Write this file byte for byte. It holds `/*__DATA__*/null` exactly once, in the first line of the script.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rateng Desk</title>
<style>
:root{
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  --mono:ui-monospace,"Cascadia Mono",Consolas,"Courier New",monospace;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10); --wash:rgba(11,11,11,.04);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --pos:#2a78d6; --neg:#e34948; --dim:#c3c2b7; --seq:#86b6ef;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b; --info:#2a78d6;
  --tip-bg:#0b0b0b; --tip-ink:#ffffff; --tip-ink2:#c3c2b7;
  color-scheme:light;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10); --wash:rgba(255,255,255,.05);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --pos:#3987e5; --neg:#e66767; --dim:#4a4a47; --seq:#256abf;
    --tip-bg:#fcfcfb; --tip-ink:#0b0b0b; --tip-ink2:#52514e;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10); --wash:rgba(255,255,255,.05);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --pos:#3987e5; --neg:#e66767; --dim:#4a4a47; --seq:#256abf;
  --tip-bg:#fcfcfb; --tip-ink:#0b0b0b; --tip-ink2:#52514e;
}
html,body{margin:0;background:var(--page)}
body{color:var(--ink);font-family:var(--sans);font-size:14px;line-height:1.45;-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}
[hidden]{display:none!important}
.wrap{max-width:1180px;margin:0 auto;padding-inline:16px;padding-block:20px 56px}
header.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 24px;padding-bottom:14px;border-bottom:1px solid var(--border)}
header.top h1{font-size:22px;font-weight:600;margin:0;letter-spacing:-.01em}
header.top .sub{color:var(--ink2);margin:2px 0 0;font-size:13px}
.loader{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border:1px solid var(--border);border-radius:5px;background:var(--surface);color:var(--ink);font:500 13px var(--sans);cursor:pointer}
.btn:hover{background:var(--wash)}
.btn:focus-within,.btn:focus-visible,button:focus-visible,.chart:focus-visible,tr[tabindex]:focus-visible{outline:2px solid var(--s1);outline-offset:2px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font:400 12px var(--mono);padding:3px 8px;border-radius:999px;border:1px solid var(--border);color:var(--ink2);background:var(--surface)}
.chip.embedded{border-style:dashed}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:12px 14px;min-width:0}
.tile .lbl{font-size:12px;color:var(--ink2);letter-spacing:.02em}
.tile .val{font-size:26px;font-weight:600;line-height:1.15;margin-top:4px;overflow-wrap:anywhere}
.tile .val small{font-size:13px;font-weight:500;color:var(--muted);margin-left:4px}
.tile .note{font-size:12px;color:var(--muted);margin-top:4px}
h2{font-size:15px;font-weight:600;margin:34px 0 4px;letter-spacing:.01em}
h2 + .lede{margin:0 0 12px;color:var(--ink2);max-width:72ch}
.grid{display:grid;gap:12px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:12px 14px 10px;min-width:0;grid-column:span 12}
.card.w6{grid-column:span 6}.card.w7{grid-column:span 7}.card.w5{grid-column:span 5}.card.w8{grid-column:span 8}.card.w4{grid-column:span 4}
@media (max-width:820px){.card.w6,.card.w7,.card.w5,.card.w8,.card.w4{grid-column:span 12}}
.card-h{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.card-h h3{font-size:14px;font-weight:600;margin:0}
.card-h .unit{font-size:12px;color:var(--muted);font-weight:400;margin-left:6px}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:5px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--ink2);font:500 12px var(--sans);padding:4px 10px;cursor:pointer}
.seg button.on{background:var(--wash);color:var(--ink)}
.chart{position:relative;min-height:120px;outline-offset:2px}
.chart svg text{font-family:var(--mono);font-size:11px}
.tip{position:absolute;z-index:3;pointer-events:none;background:var(--tip-bg);color:var(--tip-ink);border-radius:5px;padding:7px 9px;font-size:12px;min-width:140px;box-shadow:0 4px 14px rgba(0,0,0,.18)}
.tip .tip-h{color:var(--tip-ink2);font-family:var(--mono);font-size:11px;margin-bottom:4px}
.tip .tip-r{display:flex;align-items:center;gap:7px;line-height:1.6;white-space:nowrap}
.tip .key{display:inline-block;width:12px;height:2px;border-radius:1px;flex:none}
.tip .key.rect{height:9px;border-radius:2px}
.tip strong{font-family:var(--mono);font-weight:500}
.tip .nm{color:var(--tip-ink2)}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin:6px 0 2px;font-size:12px;color:var(--ink2)}
.legend .it{display:inline-flex;align-items:center;gap:6px;border:0;background:transparent;color:inherit;font:inherit;padding:2px 0;cursor:default}
.legend .it.click{cursor:pointer;border-radius:4px;padding:2px 6px}
.legend .it.click:hover{background:var(--wash)}
.legend .it.on{color:var(--ink);font-weight:600}
.legend .sw{width:14px;height:2px;border-radius:1px;flex:none}
.legend .sw.rect{height:10px;border-radius:2px}
.cap{font-size:12px;color:var(--ink2);margin:8px 0 0;max-width:80ch}
.cap b{font-weight:600;color:var(--ink)}
.cap .m,.sub .m,footer .m{font-family:var(--mono);font-size:11.5px}
.scroll{overflow-x:auto;margin-top:4px}
table.dt{border-collapse:collapse;width:100%;font-size:12.5px}
table.dt th{font-weight:500;color:var(--ink2);text-align:left;padding:6px 8px;border-bottom:1px solid var(--axis);white-space:nowrap}
table.dt td{padding:5px 8px;border-bottom:1px solid var(--grid);white-space:nowrap;vertical-align:middle}
table.dt .num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
table.dt tr[tabindex]{cursor:pointer}
table.dt tr[tabindex]:hover{background:var(--wash)}
table.dt tr.on{background:var(--wash);font-weight:600}
.bar{display:inline-block;height:8px;border-radius:0 4px 4px 0;background:var(--seq);vertical-align:middle;margin-right:6px}
.status{display:inline-flex;align-items:center;gap:5px;font-size:12px}
.status::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--muted)}
.status.good::before{background:var(--good)}.status.warn::before{background:var(--warn)}.status.serious::before{background:var(--serious)}.status.crit::before{background:var(--crit)}.status.info::before{background:var(--info)}
.empty{padding:28px 10px;color:var(--muted);text-align:center;font-size:13px}
.empty.refused{color:var(--ink2);text-align:left}
.empty.refused b{color:var(--crit);font-weight:600;font-family:var(--mono)}
.fxrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px 16px;margin:4px 0 10px}
.fxrow .k{font-size:12px;color:var(--ink2)}.fxrow .v{font-family:var(--mono);font-size:15px;font-weight:500}
footer{margin-top:40px;padding-top:12px;border-top:1px solid var(--border);color:var(--muted);font-size:12px;max-width:80ch}
@media (prefers-reduced-motion:no-preference){.seg button,.legend .it,.btn{transition:background .12s}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>Rateng Desk</h1>
      <p class="sub" id="subtitle">Payloads read straight from <span class="m">rateng &lt;command&gt; --json</span></p>
    </div>
    <div class="loader">
      <label class="btn" for="files">Load --json files<input id="files" type="file" accept=".json,application/json" multiple hidden></label>
      <div class="chips" id="loaded"></div>
    </div>
  </header>

  <div class="kpis" id="kpis"></div>

  <h2>Curve</h2>
  <p class="lede">One bootstrap, four views. Both interpolations reprice every input exactly; they differ only in the instantaneous forward between nodes, which no instrument constrains.</p>
  <div class="grid">
    <section class="card w8" id="c-curve">
      <div class="card-h"><h3>Zero, par and forward<span class="unit">annual rate</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-curve"></p>
    </section>
    <section class="card w4" id="c-df">
      <div class="card-h"><h3>Discount factors</h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-df"></p>
    </section>
    <section class="card w8" id="c-interp">
      <div class="card-h"><h3>Interpolation gap<span class="unit">instantaneous forward, weekly samples</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-interp"></p>
    </section>
    <section class="card w4" id="c-resid">
      <div class="card-h"><h3>Fit residuals</h3></div>
      <div class="tbl"></div>
      <p class="cap" id="cap-resid"></p>
    </section>
  </div>

  <h2>Swap risk</h2>
  <p class="lede">Every measure is per basis point unless its unit says otherwise. A key-rate profile depends on where the nodes sit, so compare profiles only across curves built the same way.</p>
  <div class="grid">
    <section class="card w6" id="c-kr">
      <div class="card-h"><h3>Key-rate DV01<span class="unit">USD per bp, tent bumps on nodes</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-kr"></p>
    </section>
    <section class="card w6" id="c-meas">
      <div class="card-h"><h3>All measures</h3></div>
      <div class="tbl"></div>
      <p class="cap" id="cap-meas"></p>
    </section>
  </div>

  <h2>Futures strip hedge</h2>
  <p class="lede">Contracts per IMM period that neutralise the swap on the quote basis, then the P&amp;L of swap and strip together under parallel shocks. The net is the convexity the strip cannot hedge.</p>
  <div class="grid">
    <section class="card w6" id="c-strip">
      <div class="card-h"><h3>Contracts per period<span class="unit">SR3, USD 25 per bp each</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-strip"></p>
    </section>
    <section class="card w6" id="c-shock">
      <div class="card-h"><h3>Swap vs strip P&amp;L<span class="unit">USD by parallel shock</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-shock"></p>
    </section>
    <section class="card w6" id="c-net">
      <div class="card-h"><h3>Net P&amp;L after hedge<span class="unit">USD, the convexity residual</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-net"></p>
    </section>
    <section class="card w6" id="c-dv01">
      <div class="card-h"><h3>Two parallels</h3></div>
      <div class="tbl"></div>
      <p class="cap" id="cap-dv01"></p>
    </section>
  </div>

  <h2>FX forward and basis</h2>
  <p class="lede">The parity forward from two curves, and the cross-currency basis reported beside it rather than folded in.</p>
  <div class="grid">
    <section class="card w12" id="c-fx">
      <div class="card-h"><h3>Forward points bridge<span class="unit">pips, quote-leg basis</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="fxrow" id="fxrow"></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-fx"></p>
    </section>
  </div>

  <h2>Hedge structures</h2>
  <p class="lede">Eight structures costed side by side on one exposure. The engine compares; it does not rank. Pick a structure to bring its payoff forward.</p>
  <div class="grid">
    <section class="card w7" id="c-pay">
      <div class="card-h"><h3>Effective rate at settlement<span class="unit">MXN per USD, by spot</span></h3><div class="seg"><button class="on" data-view="chart">Chart</button><button data-view="table">Table</button></div></div>
      <div class="chart" tabindex="0"></div><div class="tbl" hidden></div>
      <p class="cap" id="cap-pay"></p>
    </section>
    <section class="card w5" id="c-cmp">
      <div class="card-h"><h3>Cost and range</h3></div>
      <div class="tbl"></div>
      <p class="cap" id="cap-cmp"></p>
    </section>
  </div>

  <footer id="foot"></footer>
</div>

<script>
(function(){
'use strict';
const DATA = /*__DATA__*/null;
const S = {bootstrap:null,price:null,hedge:null,fx:null,structures:null,interp:null,meta:null,emphasis:null,loaded:[]};
if (DATA) { for (const k of Object.keys(DATA)) { if (k==='meta') { S.meta = DATA.meta || null; continue; } if (DATA[k]) { S[k]=DATA[k]; S.loaded.push({name:k+'.json', kind:k, embedded:true}); } } }

const $ = (sel, root) => (root||document).querySelector(sel);
const NS = 'http://www.w3.org/2000/svg';
function sv(tag, attrs, style){ const e=document.createElementNS(NS,tag); if(attrs) for(const k in attrs) e.setAttribute(k, attrs[k]); if(style) Object.assign(e.style, style); return e; }
function h(tag, attrs, ...kids){ const e=document.createElement(tag); if(attrs) for(const k in attrs){ const v=attrs[k]; if(k==='class') e.className=v; else if(k==='text') e.textContent=v; else if(k==='style') e.style.cssText=v; else if(k.startsWith('on')) e.addEventListener(k.slice(2), v); else if(v!==null&&v!==undefined) e.setAttribute(k, v); } for(const c of kids){ if(c===null||c===undefined) continue; e.append(c.nodeType? c : document.createTextNode(String(c))); } return e; }

const toMs = s => { const [y,m,d]=String(s).split('-').map(Number); return Date.UTC(y,m-1,d); };
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtDate = (ms, full) => { const d=new Date(ms); return full ? `${String(d.getUTCDate()).padStart(2,'0')} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}` : `${MON[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`; };
const num = (v, d=0) => (v===null||v===undefined||Number.isNaN(v)) ? '—' : Number(v).toLocaleString('en-US',{minimumFractionDigits:d, maximumFractionDigits:d});
const pct = (v, d=2) => num(v*100, d)+'%';
const bp = (v, d=1) => num(v, d)+' bp';
const compact = v => { const a=Math.abs(v); if(a>=1e9) return (v/1e9).toFixed(2)+'B'; if(a>=1e6) return (v/1e6).toFixed(2)+'M'; if(a>=1e4) return (v/1e3).toFixed(1)+'K'; return num(v, a<10?2:0); };

function niceNum(r, round){ const e=Math.floor(Math.log10(r)); const f=r/Math.pow(10,e); let nf; if(round){ nf = f<1.5?1: f<3?2: f<7?5:10; } else { nf = f<=1?1: f<=2?2: f<=5?5:10; } return nf*Math.pow(10,e); }
function ticks(lo, hi, n){ n=n||5; if(!(hi>lo)){ const p=(Math.abs(lo)||1)*0.1; hi=lo+p; lo=lo-p; } const range=niceNum(hi-lo,false); const step=niceNum(range/(n-1),true); const a=Math.floor(lo/step)*step, b=Math.ceil(hi/step)*step; const out=[]; for(let v=a; v<=b+step/2; v+=step) out.push(+v.toFixed(10)); return {lo:a,hi:b,step,ticks:out}; }

function tipEl(){ return h('div',{class:'tip',hidden:''}); }
function placeTip(tip, host, x, y){ tip.hidden=false; const tw=tip.offsetWidth, th=tip.offsetHeight, W=host.clientWidth; let left=x+12; if(left+tw>W-4) left=Math.max(4, x-tw-12); let top=y-th/2; top=Math.max(4, Math.min(top, host.clientHeight-th-4)); tip.style.left=left+'px'; tip.style.top=top+'px'; }
function tipRow(color, value, name, rect){ return h('div',{class:'tip-r'}, h('span',{class:'key'+(rect?' rect':''), style:'background:'+color}), h('strong',{text:value}), h('span',{class:'nm',text:name})); }

/* ---------- line chart ---------- */
function renderLine(host, spec){
  host.replaceChildren();
  const W=Math.max(300, host.clientWidth||640), H=spec.height||250;
  const m={l:spec.ml||60, r:spec.endLabels?110:22, t:16, b:34};
  const iw=W-m.l-m.r, ih=H-m.t-m.b;
  const xs=spec.x, n=xs.length; const xmin=Math.min(...xs), xmax=Math.max(...xs);
  const ys=[]; spec.series.forEach(s=>s.y.forEach(v=>ys.push(v))); if(spec.includeZero) ys.push(0); (spec.hlines||[]).forEach(l=>ys.push(l.y));
  const yt=ticks(Math.min(...ys), Math.max(...ys), 5);
  const X=v=> m.l + (xmax===xmin?0.5:(v-xmin)/(xmax-xmin))*iw;
  const Y=v=> m.t + ih - (v-yt.lo)/(yt.hi-yt.lo)*ih;
  const svg=sv('svg',{width:W,height:H,viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':spec.label||''},{display:'block'});
  yt.ticks.forEach(t=>{ const y=Y(t); svg.append(sv('line',{x1:m.l,x2:m.l+iw,y1:y,y2:y},{stroke:'var(--grid)',strokeWidth:1})); const tx=sv('text',{x:m.l-8,y:y+4,'text-anchor':'end'},{fill:'var(--muted)'}); tx.textContent=spec.yFmt(t); svg.append(tx); });
  if(yt.lo<=0 && yt.hi>=0){ const y=Y(0); svg.append(sv('line',{x1:m.l,x2:m.l+iw,y1:y,y2:y},{stroke:'var(--axis)'})); }
  const count=Math.min(spec.xTicks||6, n); const seen=new Set(); for(let k=0;k<count;k++){ const i=Math.round(k*(n-1)/Math.max(1,count-1)); if(seen.has(i)) continue; seen.add(i); const x=X(xs[i]); const tx=sv('text',{x,y:m.t+ih+18,'text-anchor':k===0?'start':k===count-1?'end':'middle'},{fill:'var(--muted)'}); tx.textContent=spec.xFmt(xs[i]); svg.append(tx); }
  (spec.vlines||[]).forEach(l=>{ const x=X(l.x); svg.append(sv('line',{x1:x,x2:x,y1:m.t,y2:m.t+ih},{stroke:'var(--axis)',strokeWidth:1})); const tx=sv('text',{x:x+4,y:m.t+10},{fill:'var(--ink2)',fontFamily:'var(--sans)'}); tx.textContent=l.label; svg.append(tx); });
  (spec.hlines||[]).forEach(l=>{ const y=Y(l.y); svg.append(sv('line',{x1:m.l,x2:m.l+iw,y1:y,y2:y},{stroke:'var(--axis)',strokeWidth:1})); const tx=sv('text',{x:m.l+iw-4,y:y-4,'text-anchor':'end'},{fill:'var(--ink2)',fontFamily:'var(--sans)'}); tx.textContent=l.label; svg.append(tx); });
  const order=[...spec.series.keys()].sort((a,b)=> (spec.series[a].dim?0:1)-(spec.series[b].dim?0:1));
  order.forEach(i=>{ const s=spec.series[i]; const d=s.y.map((v,j)=> (j?'L':'M')+X(xs[j]).toFixed(1)+','+Y(v).toFixed(1)).join(' ');
    if(spec.area && !s.dim){ svg.append(sv('path',{d:d+` L${X(xs[n-1]).toFixed(1)},${(m.t+ih).toFixed(1)} L${X(xs[0]).toFixed(1)},${(m.t+ih).toFixed(1)} Z`},{fill:s.color,opacity:.10})); }
    svg.append(sv('path',{d,fill:'none','stroke-linejoin':'round','stroke-linecap':'round'},{stroke:s.dim?'var(--dim)':s.color,strokeWidth:s.dim?1.5:2}));
    if(!s.dim){ const pts = spec.markers ? xs.map((_,j)=>j) : [n-1]; pts.forEach(j=>{ const ex=X(xs[j]), ey=Y(s.y[j]); svg.append(sv('circle',{cx:ex,cy:ey,r:6},{fill:'var(--surface)'})); svg.append(sv('circle',{cx:ex,cy:ey,r:4},{fill:s.color})); }); }
  });
  if(spec.endLabels){ const c=spec.series.map((s,i)=>({y:Y(s.y[n-1]),name:s.name,dim:s.dim})).filter(c=>!c.dim).sort((a,b)=>a.y-b.y); let ok=c.length<=4; for(let k=1;k<c.length;k++) if(c[k].y-c[k-1].y<13) ok=false; if(ok) c.forEach(cc=>{ const tx=sv('text',{x:X(xs[n-1])+10,y:cc.y+4},{fill:'var(--ink2)',fontFamily:'var(--sans)'}); tx.textContent=cc.name; svg.append(tx); }); }
  const cross=sv('line',{x1:0,x2:0,y1:m.t,y2:m.t+ih},{stroke:'var(--axis)',strokeWidth:1,visibility:'hidden'}); svg.append(cross);
  const dots=spec.series.map(s=>{ const g=sv('g',{},{visibility:'hidden'}); g.append(sv('circle',{r:6},{fill:'var(--surface)'}), sv('circle',{r:4},{fill:s.dim?'var(--dim)':s.color})); svg.append(g); return g; });
  const hit=sv('rect',{x:m.l,y:m.t,width:iw,height:ih},{fill:'transparent',cursor:'crosshair'}); svg.append(hit);
  const tip=tipEl(); host.append(svg,tip);
  let cur=-1;
  function nearest(px){ const v=xmin+(px-m.l)/iw*(xmax-xmin); let best=0,bd=Infinity; for(let i=0;i<n;i++){ const d=Math.abs(xs[i]-v); if(d<bd){bd=d;best=i;} } return best; }
  function show(i){ cur=i; const x=X(xs[i]); cross.setAttribute('x1',x); cross.setAttribute('x2',x); cross.style.visibility='visible';
    spec.series.forEach((s,k)=>{ dots[k].setAttribute('transform',`translate(${x},${Y(s.y[i])})`); dots[k].style.visibility='visible'; });
    tip.replaceChildren(h('div',{class:'tip-h',text:spec.xFmt(xs[i], true)}));
    const rows=spec.series.map((s)=>s); const emph=rows.filter(s=>!s.dim), rest=rows.filter(s=>s.dim);
    emph.concat(spec.tipAll===false?[]:rest).forEach(s=> tip.append(tipRow(s.dim?'var(--dim)':s.color, spec.yFmt(s.y[i], true), s.name)));
    placeTip(tip, host, x, m.t+ih/2); }
  function hide(){ cur=-1; cross.style.visibility='hidden'; dots.forEach(g=>g.style.visibility='hidden'); tip.hidden=true; }
  hit.addEventListener('pointermove',e=>{ const r=svg.getBoundingClientRect(); show(nearest(e.clientX-r.left)); });
  hit.addEventListener('pointerleave',hide);
  host.onkeydown = e => { if(e.key==='ArrowRight'){ show(Math.min(n-1,(cur<0?0:cur)+1)); e.preventDefault(); } else if(e.key==='ArrowLeft'){ show(Math.max(0,(cur<0?n-1:cur)-1)); e.preventDefault(); } else if(e.key==='Escape') hide(); };
  host.onblur = hide;
}

/* ---------- column chart ---------- */
function renderBars(host, spec){
  host.replaceChildren();
  const W=Math.max(300, host.clientWidth||640), H=spec.height||240;
  const m={l:spec.ml||64, r:16, t:24, b:34}; const iw=W-m.l-m.r, ih=H-m.t-m.b;
  const vals=spec.vals, n=vals.length;
  const yt=ticks(Math.min(0,...vals), Math.max(0,...vals), 5);
  const Y=v=> m.t + ih - (v-yt.lo)/(yt.hi-yt.lo)*ih;
  const band=iw/n, thick=Math.min(24, band*0.6);
  const svg=sv('svg',{width:W,height:H,viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':spec.label||''},{display:'block'});
  yt.ticks.forEach(t=>{ const y=Y(t); svg.append(sv('line',{x1:m.l,x2:m.l+iw,y1:y,y2:y},{stroke:'var(--grid)'})); const tx=sv('text',{x:m.l-8,y:y+4,'text-anchor':'end'},{fill:'var(--muted)'}); tx.textContent=spec.yFmt(t); svg.append(tx); });
  const y0=Y(0); svg.append(sv('line',{x1:m.l,x2:m.l+iw,y1:y0,y2:y0},{stroke:'var(--axis)'}));
  const imax=vals.indexOf(Math.max(...vals)), imin=vals.indexOf(Math.min(...vals));
  const tip=tipEl();
  vals.forEach((v,i)=>{ const x=m.l+band*i+(band-thick)/2; const y1=Y(v); const top=Math.min(y0,y1), hgt=Math.abs(y0-y1); const r=Math.min(4, hgt/2); const xr=x+thick;
    let d; if(v>=0){ d=`M${x},${y0} L${x},${top+r} Q${x},${top} ${x+r},${top} L${xr-r},${top} Q${xr},${top} ${xr},${top+r} L${xr},${y0} Z`; } else { const bot=top+hgt; d=`M${x},${y0} L${x},${bot-r} Q${x},${bot} ${x+r},${bot} L${xr-r},${bot} Q${xr},${bot} ${xr},${bot-r} L${xr},${y0} Z`; }
    const color=spec.colorFor?spec.colorFor(v,i):'var(--s1)'; const p=sv('path',{d},{fill:color}); svg.append(p);
    const cx=x+thick/2; const ct=sv('text',{x:cx,y:m.t+ih+18,'text-anchor':'middle'},{fill:'var(--muted)'}); ct.textContent=spec.cats[i]; svg.append(ct);
    const lab = spec.labelMode==='all' || (spec.labelMode!=='none' && (i===imax||i===imin));
    if(lab){ const t=sv('text',{x:cx,y: v>=0? top-6 : top+hgt+13,'text-anchor':'middle'},{fill:'var(--ink2)'}); t.textContent=spec.yFmt(v,true); svg.append(t); }
    const hit=sv('rect',{x:m.l+band*i,y:m.t,width:band,height:ih},{fill:'transparent',cursor:'default'});
    hit.addEventListener('pointerenter',()=>{ p.style.opacity=.72; tip.replaceChildren(h('div',{class:'tip-h',text:spec.cats[i]})); tip.append(tipRow(color, spec.yFmt(v,true), spec.unitName||'', true)); (spec.extra?spec.extra(i):[]).forEach(([k,val])=> tip.append(h('div',{class:'tip-r'}, h('strong',{text:val}), h('span',{class:'nm',text:k})))); placeTip(tip, host, cx, Math.min(y0,y1)); });
    hit.addEventListener('pointerleave',()=>{ p.style.opacity=1; tip.hidden=true; });
    svg.append(hit);
  });
  host.append(svg, tip);
}

/* ---------- horizontal bridge (waterfall) ---------- */
function renderBridge(host, spec){
  host.replaceChildren();
  const W=Math.max(300, host.clientWidth||640); const rowH=40, n=spec.rows.length; const m={l:120,r:90,t:10,b:30}; const H=m.t+m.b+rowH*n;
  const iw=W-m.l-m.r; const all=[0]; spec.rows.forEach(r=>{all.push(r.from,r.to);});
  const xt=ticks(Math.min(...all), Math.max(...all), 6); const X=v=> m.l + (v-xt.lo)/(xt.hi-xt.lo)*iw;
  const svg=sv('svg',{width:W,height:H,viewBox:`0 0 ${W} ${H}`,role:'img'},{display:'block'});
  xt.ticks.forEach(t=>{ const x=X(t); svg.append(sv('line',{x1:x,x2:x,y1:m.t,y2:m.t+rowH*n},{stroke:'var(--grid)'})); const tx=sv('text',{x,y:H-10,'text-anchor':'middle'},{fill:'var(--muted)'}); tx.textContent=spec.xFmt(t); svg.append(tx); });
  if(xt.lo<=0&&xt.hi>=0) svg.append(sv('line',{x1:X(0),x2:X(0),y1:m.t,y2:m.t+rowH*n},{stroke:'var(--axis)'}));
  const tip=tipEl();
  spec.rows.forEach((r,i)=>{ const y=m.t+rowH*i+(rowH-24)/2; const a=X(r.from), b=X(r.to); const x0=Math.min(a,b), w=Math.abs(b-a); const rr=Math.min(4,w/2); const yb=y+24;
    let d; if(b>=a){ d=`M${x0},${y} L${x0+w-rr},${y} Q${x0+w},${y} ${x0+w},${y+rr} L${x0+w},${yb-rr} Q${x0+w},${yb} ${x0+w-rr},${yb} L${x0},${yb} Z`; } else { d=`M${x0+w},${y} L${x0+rr},${y} Q${x0},${y} ${x0},${y+rr} L${x0},${yb-rr} Q${x0},${yb} ${x0+rr},${yb} L${x0+w},${yb} Z`; }
    const p=sv('path',{d},{fill:r.color}); svg.append(p);
    const lt=sv('text',{x:m.l-10,y:y+16,'text-anchor':'end'},{fill:'var(--ink2)',fontFamily:'var(--sans)'}); lt.textContent=r.label; svg.append(lt);
    const vt=sv('text',{x:(b>=a? x0+w : x0) + (b>=a?8:-8), y:y+16,'text-anchor': b>=a?'start':'end'},{fill:'var(--ink2)'}); vt.textContent=spec.xFmt(r.to-r.from, true); svg.append(vt);
    const hit=sv('rect',{x:m.l,y:m.t+rowH*i,width:iw,height:rowH},{fill:'transparent'});
    hit.addEventListener('pointerenter',()=>{ p.style.opacity=.72; tip.replaceChildren(h('div',{class:'tip-h',text:r.label})); tip.append(tipRow(r.color, spec.xFmt(r.to-r.from,true), 'points', true)); (r.extra||[]).forEach(([k,v])=>tip.append(h('div',{class:'tip-r'}, h('strong',{text:v}), h('span',{class:'nm',text:k})))); placeTip(tip, host, x0+w/2, y+12); });
    hit.addEventListener('pointerleave',()=>{ p.style.opacity=1; tip.hidden=true; });
    svg.append(hit);
  });
  host.append(svg, tip);
}

function legend(items, onPick){ const d=h('div',{class:'legend',role:onPick?'group':null}); items.forEach(it=>{ const b=h(onPick?'button':'span',{class:'it'+(onPick?' click':'')+(it.on?' on':''), type:onPick?'button':null, 'aria-pressed':onPick?String(!!it.on):null}, h('span',{class:'sw'+(it.rect?' rect':''), style:'background:'+(it.dim?'var(--dim)':it.color)}), it.name); if(onPick) b.addEventListener('click',()=>onPick(it.key)); d.append(b); }); return d; }
function table(host, cols, rows, rowOpts){ host.replaceChildren(); const t=h('table',{class:'dt'}); const tr=h('tr'); cols.forEach(c=>tr.append(h('th',{class:c.num?'num':null,text:c.label}))); t.append(h('thead',null,tr)); const tb=h('tbody'); rows.forEach((r,ri)=>{ const o=rowOpts?rowOpts(ri):null; const tr=h('tr', o?{tabindex:'0',class:o.on?'on':null,onclick:o.onclick,onkeydown:e=>{ if(e.key==='Enter'||e.key===' '){ o.onclick(); e.preventDefault(); } }}:null); r.forEach((cell,i)=>tr.append(h('td',{class:cols[i].num?'num':null}, cell))); tb.append(tr); }); t.append(tb); host.append(h('div',{class:'scroll'},t)); }
function empty(card, what){ const c=$('.chart',card), t=$('.tbl',card); if(c) c.replaceChildren(h('div',{class:'empty',text:'Load '+what+' to fill this panel.'})); if(t) t.replaceChildren(); const cap=$('.cap',card); if(cap) cap.textContent=''; }
function refused(card, payload){ const err=payload.error||{}; const box=()=>h('div',{class:'empty refused'}, 'The engine refused this panel: ', h('b',{text:err.type||'error'}), '. ', h('span',{text:err.message||''})); const c=$('.chart',card), t=$('.tbl',card); if(c) c.replaceChildren(box()); if(t) t.replaceChildren(box()); const cap=$('.cap',card); if(cap) cap.textContent='Reported rather than raised: the rest of the page stands. See docs/ERRORS.md.'; }
function statusFor(q){ return {observed:'good',derived:'info',proxy:'warn',assumed:'serious'}[q]||''; }
function statusChip(q){ return h('span',{class:'status '+statusFor(q),text:q}); }

/* ---------- sections ---------- */
function renderKPIs(){
  const k=$('#kpis'); k.replaceChildren();
  const p=S.price, hd=S.hedge, fx=S.fx, b=S.bootstrap;
  const inst = p && p.evidence && p.evidence.fields && p.evidence.fields.instrument;
  const tile=(lbl,val,unit,note)=> h('div',{class:'tile'}, h('div',{class:'lbl',text:lbl}), h('div',{class:'val'}, val, unit?h('small',{text:unit}):null), h('div',{class:'note',text:note||''}));
  const worst = b && b.node_quality && b.node_quality.length ? b.node_quality.reduce((w,q)=> ['observed','derived','proxy','assumed'].indexOf(q)>['observed','derived','proxy','assumed'].indexOf(w)?q:w,'observed') : '—';
  k.append(
    tile('Swap PV', p?compact(p.value):'—', p?p.unit:'', inst? `${inst.side} ${compact(inst.notional)} at ${pct(inst.fixed_rate,3)}, to ${inst.maturity}` : 'price.json'),
    tile('Par rate', p?pct(p.measures.par_rate.value,4):'—', '', p?`annuity ${num(p.measures.annuity.value,4)} y`:'price.json'),
    tile('DV01', p?num(p.measures.dv01.value,0):'—', p?'USD/bp':'', p?`money convexity ${compact(p.measures.money_convexity.value)}`:'price.json'),
    tile('Strip hedge', hd?num(hd.total_contracts,1):'—', hd?'SR3':'', hd?`hedge ratio ${num(hd.hedge_ratio,4)}, ${Object.keys(hd.contracts).length} periods`:'hedge.json'),
    tile('USD/MXN outright', fx?num(fx.outright,4):'—', '', fx?`CIP ${num(fx.cip_forward,4)}, basis ${num(fx.basis_bp,0)} bp, ${fx.delivery}`:'fx-forward.json'),
    tile('Curve quality', worst, '', b?`${Object.keys(b.residuals_bp).length} nodes, worst residual ${bp(Math.max(...Object.values(b.residuals_bp).map(Math.abs)),3)}`:'bootstrap.json'),
  );
  const asof = (S.meta&&S.meta.as_of) || (b&&b.curve&&b.curve.as_of) || (S.interp&&S.interp.as_of) || null;
  $('#subtitle').replaceChildren(asof ? `Valuation date ${asof}. ` : '', 'Payloads read straight from ', h('span',{class:'m',text:'rateng <command> --json'}), '.');
}

function renderCurves(){
  const b=S.bootstrap, card=$('#c-curve'), dfc=$('#c-df');
  if(!b){ empty(card,'bootstrap.json'); empty(dfc,'bootstrap.json'); $('#c-resid .tbl').replaceChildren(h('div',{class:'empty',text:'Load bootstrap.json to fill this panel.'})); $('#cap-resid').textContent=''; return; }
  const v=b.views; const x=v.zero.dates.map(toMs);
  const series=[{name:'Zero',y:v.zero.values,color:'var(--s1)'},{name:'Par',y:v.par.values,color:'var(--s2)'},{name:'Forward',y:v.forward.values,color:'var(--s3)'}];
  renderLine($('.chart',card),{x,xFmt:fmtDate,yFmt:(t,f)=>pct(t,f?4:2),endLabels:true,series,label:'Zero, par and forward rates by node date'});
  $('.chart',card).append(legend(series.map(s=>({name:s.name,color:s.color}))));
  table($('.tbl',card),[{label:'Node'},{label:'Discount',num:1},{label:'Zero',num:1},{label:'Par',num:1},{label:'Forward',num:1}], v.zero.dates.map((d,i)=>[d,num(v.discount.values[i],6),pct(v.zero.values[i],4),pct(v.par.values[i],4),pct(v.forward.values[i],4)]));
  $('#cap-curve').replaceChildren('Zero is continuously compounded on the curve basis. Forward is the discrete forward between consecutive nodes. Par is the OIS par rate from the first node to each node. Interpolation: ', h('span',{class:'m',text:(b.curve&&b.curve.interpolation)||'log_linear_df'}), '.');
  renderLine($('.chart',dfc),{x,xFmt:fmtDate,yFmt:(t,f)=>num(t,f?6:3),area:true,ml:64,series:[{name:'Discount factor',y:v.discount.values,color:'var(--s1)'}],label:'Discount factors by node date'});
  table($('.tbl',dfc),[{label:'Node'},{label:'P(0,T)',num:1}], v.discount.dates.map((d,i)=>[d,num(v.discount.values[i],8)]));
  $('#cap-df').textContent = 'Monotone here by construction: the bootstrap checks it. A hand-built curve does not, so call require_monotone() when you mean to assert it.';
  const keys=Object.keys(b.residuals_bp);
  table($('#c-resid .tbl'),[{label:'Instrument'},{label:'Residual',num:1},{label:'Quality'}], keys.map((k,i)=>[k,bp(b.residuals_bp[k],4),statusChip(b.node_quality[i]||'')]));
  $('#cap-resid').textContent = (b.dropped_instruments&&b.dropped_instruments.length? `Dropped: ${b.dropped_instruments.join(', ')}. ` : 'Nothing dropped. ') + 'Quality is the worst of the inputs behind each node, and every price on this curve inherits it.';
}

function renderInterp(){
  const it=S.interp, card=$('#c-interp'); if(!it){ empty(card,'interp.json (compare_interpolations)'); return; }
  if(it.error){ refused(card, it); return; }
  const x=it.dates.map(toMs);
  const series=[{name:'Log-linear DF',y:it.log_linear,color:'var(--s1)'},{name:'Monotone convex',y:it.monotone_convex,color:'var(--s2)'}];
  renderLine($('.chart',card),{x,xFmt:fmtDate,yFmt:(t,f)=>pct(t,f?4:2),endLabels:true,series,vlines:[{x:toMs(it.max_forward_difference_date),label:'max gap'}],label:'Instantaneous forward under two interpolations'});
  $('.chart',card).append(legend(series.map(s=>({name:s.name,color:s.color}))));
  table($('.tbl',card),[{label:'Date'},{label:'Log-linear',num:1},{label:'Monotone convex',num:1},{label:'Gap',num:1}], it.dates.map((d,i)=>[d,pct(it.log_linear[i],4),pct(it.monotone_convex[i],4),bp(it.diff_bp[i],2)]));
  $('#cap-interp').replaceChildren('Same instruments, both fits reprice every one within ', h('b',{text:bp(it.max_residual_bp,4)}), '. The largest instantaneous-forward gap is ', h('b',{text:bp(it.max_forward_difference_bp,2)}), ' on ', h('b',{text:it.max_forward_difference_date}), '; the largest discount-factor gap is ', h('span',{class:'m',text:Number(it.max_df_difference).toExponential(2)}), '. Neither is the winner. The gap is what the quotes do not constrain.');
}

function renderRisk(){
  const p=S.price, card=$('#c-kr'); if(!p){ empty(card,'price.json'); $('#c-meas .tbl').replaceChildren(h('div',{class:'empty',text:'Load price.json to fill this panel.'})); $('#cap-meas').textContent=''; return; }
  const kr=p.measures.key_rate_dv01;
  const krEntries=Object.entries(kr.values||{});
  const pairs=kr.key_tenors.map(t=>{ const hit=krEntries.find(([k])=>Math.abs(parseFloat(k)-t)<1e-9); return hit? {t, v:Number(hit[1])} : null; }).filter(Boolean);
  const cats=pairs.map(x=>x.t+'y'), vals=pairs.map(x=>x.v);
  renderBars($('.chart',card),{cats,vals,yFmt:(v,f)=>num(v,0),colorFor:v=> v>=0?'var(--pos)':'var(--neg)',labelMode:'all',unitName:'USD/bp',label:'Key-rate DV01 by tenor'});
  $('.chart',card).append(legend([{name:'Gains when that tenor rises',color:'var(--pos)',rect:true},{name:'Loses',color:'var(--neg)',rect:true}]));
  table($('.tbl',card),[{label:'Tenor'},{label:'DV01',num:1}], pairs.map(x=>[x.t+'y',num(x.v,2)]));
  $('#cap-kr').replaceChildren('Bucket sum ', h('b',{text:num(kr.total,0)}), ' against parallel ', h('b',{text:num(p.measures.dv01.value,0)}), ' USD/bp, ', h('span',{class:'m',text:kr.bump_shape}), ' shocks of ', h('span',{class:'m',text:kr.bump_bp+' bp'}), ' on curve nodes. A different node set gives a different profile that reprices the same instruments.');
  const rows=[]; for(const [k,m] of Object.entries(p.measures)){ if(m&&typeof m==='object'&&'value' in m&&k!=='key_rate_dv01') rows.push([k, typeof m.value==='number'?num(m.value, Math.abs(m.value)<10?6:2):String(m.value), m.unit||'']); }
  table($('#c-meas .tbl'),[{label:'Measure'},{label:'Value',num:1},{label:'Unit'}], rows);
  $('#cap-meas').textContent='effective_duration and effective_convexity normalise by price, so they are undefined on a par swap and this large on an off-par one. Size hedges with dv01 and money_convexity. macaulay_duration and modified_duration need a bond and are not served.';
}

function renderHedge(){
  const hd=S.hedge; const cs=$('#c-strip'), csh=$('#c-shock'), cn=$('#c-net');
  if(!hd){ empty(cs,'hedge.json'); empty(csh,'hedge.json'); empty(cn,'hedge.json'); $('#c-dv01 .tbl').replaceChildren(h('div',{class:'empty',text:'Load hedge.json to fill this panel.'})); $('#cap-dv01').textContent=''; return; }
  const names=Object.keys(hd.contracts); const vals=names.map(k=>hd.contracts[k]);
  renderBars($('.chart',cs),{cats:names,vals,yFmt:(v,f)=>num(v,f?2:0),labelMode:'extremes',unitName:'contracts',extra:i=>[[ 'period', hd.periods[names[i]].join(' to ') ],[ 'bucket delta', num(hd.bucketed_delta_by_instrument[names[i]],0)+' USD/bp' ]],label:'Futures contracts per IMM period'});
  table($('.tbl',cs),[{label:'Contract'},{label:'Period'},{label:'Contracts',num:1},{label:'Bucket delta',num:1}], names.map(k=>[k,hd.periods[k].join(' to '),num(hd.contracts[k],2),num(hd.bucketed_delta_by_instrument[k],0)]));
  $('#cap-strip').replaceChildren('Each contract is ', h('b',{text:'USD '+num(hd.contract_dv01,2)+' per bp'}), ' on its nominal quarter. The realised accrual runs 91 or 98 days and is the wrong thing to size with. Total ', h('b',{text:num(hd.total_contracts,1)}), ' contracts.');
  const rows=hd.shock_table.rows; const xs=rows.map(r=>r.shock_bp);
  const ser=[{name:'Swap',y:rows.map(r=>r.swap_pnl),color:'var(--s1)'},{name:'Strip',y:rows.map(r=>r.strip_pnl),color:'var(--s2)'}];
  renderLine($('.chart',csh),{x:xs,xFmt:(v,f)=>(v>0?'+':'')+num(v,0)+' bp',yFmt:(v,f)=>f?num(v,0):compact(v),markers:true,endLabels:false,includeZero:true,xTicks:xs.length,ml:70,series:ser,label:'Swap and strip P&L by parallel shock'});
  $('.chart',csh).append(legend(ser.map(s=>({name:s.name,color:s.color}))));
  table($('.tbl',csh),[{label:'Shock'},{label:'Swap',num:1},{label:'Strip',num:1},{label:'Net',num:1},{label:'Swap DV01 after',num:1}], rows.map(r=>[(r.shock_bp>0?'+':'')+r.shock_bp+' bp',num(r.swap_pnl,0),num(r.strip_pnl,0),num(r.net_pnl,0),num(r.swap_dv01_post_shock,0)]));
  $('#cap-shock').textContent='Each contract is settled on the actual move of its own forward, so the strip is first-order neutral under any shock on either basis.';
  renderBars($('.chart',cn),{cats:rows.map(r=>(r.shock_bp>0?'+':'')+r.shock_bp+' bp'),vals:rows.map(r=>r.net_pnl),yFmt:(v,f)=>f?num(v,0):compact(v),colorFor:v=>v>=0?'var(--pos)':'var(--neg)',labelMode:'all',unitName:'USD',extra:i=>[[ 'per DV01 bp', num(rows[i].net_per_dv01_bp,3) ],[ 'implied convexity', compact(rows[i].implied_position_money_convexity) ]],label:'Net P&L after hedge by shock'});
  table($('.tbl',cn),[{label:'Shock'},{label:'Net',num:1},{label:'Net per DV01 bp',num:1},{label:'Implied money convexity',num:1}], rows.map(r=>[(r.shock_bp>0?'+':'')+r.shock_bp+' bp',num(r.net_pnl,0),num(r.net_per_dv01_bp,4),num(r.implied_position_money_convexity,0)]));
  $('#cap-net').textContent='Negative both ways: a payer swap is short convexity against a linear strip. The residual scales with the square of the shock.';
  table($('#c-dv01 .tbl'),[{label:'Quantity'},{label:'USD/bp',num:1},{label:'Basis'}],[
    ['swap_dv01',num(hd.swap_dv01,2),hd.swap_dv01_bump_basis],['quote_parallel_dv01',num(hd.quote_parallel_dv01,2),hd.bump_basis],['bucket_sum',num(hd.bucket_sum,2),hd.bump_basis],['strip_dv01',num(hd.strip_dv01,2),hd.bump_basis],['difference',num(hd.node_vs_instrument_difference,2),'']]);
  $('#cap-dv01').textContent=hd.node_vs_instrument_note||'';
}

function renderFX(){
  const fx=S.fx, card=$('#c-fx'), row=$('#fxrow'); row.replaceChildren();
  if(!fx){ empty(card,'fx-forward.json'); return; }
  const kv=(k,v)=>h('div',null,h('div',{class:'k',text:k}),h('div',{class:'v',text:v}));
  row.append(kv('Pair',fx.pair),kv('Spot',num(fx.spot,4)),kv('CIP forward',num(fx.cip_forward,4)),kv('Outright',num(fx.outright,4)),kv('Basis',num(fx.basis_bp,0)+' bp, '+fx.basis_leg+' leg'),kv('Delivery',fx.delivery));
  renderBridge($('.chart',card),{xFmt:(v,f)=>num(v,f?1:0),rows:[
    {label:'Parity points',from:0,to:fx.cip_points,color:'var(--s1)',extra:[['rate',num(fx.cip_forward,4)]]},
    {label:'Basis',from:fx.cip_points,to:fx.cip_points+fx.basis_points_contribution,color:fx.basis_points_contribution>=0?'var(--pos)':'var(--neg)',extra:[['component',num(fx.basis_component,4)]]},
    {label:'Outright points',from:0,to:fx.forward_points,color:'var(--s1)',extra:[['rate',num(fx.outright,4)]]}]});
  table($('.tbl',card),[{label:'Component'},{label:'Pips',num:1},{label:'Rate',num:1}],[['Spot','',num(fx.spot,4)],['Parity points',num(fx.cip_points,1),num(fx.cip_forward,4)],['Basis contribution',num(fx.basis_points_contribution,1),num(fx.basis_component,4)],['Outright',num(fx.forward_points,1),num(fx.outright,4)]]);
  $('#cap-fx').textContent=fx.cip_note||'';
}

function renderStructures(){
  const st=S.structures, cp=$('#c-pay'), cc=$('#c-cmp');
  if(!st){ empty(cp,'hedge-structures.json'); cc.querySelector('.tbl').replaceChildren(h('div',{class:'empty',text:'Load hedge-structures.json to fill this panel.'})); $('#cap-cmp').textContent=''; return; }
  const names=st.structures.map(s=>s.name);
  if(!S.emphasis||!names.includes(S.emphasis)) S.emphasis=(st.program_audit&&st.program_audit.proposed_instrument&&names.includes(st.program_audit.proposed_instrument))?st.program_audit.proposed_instrument:names[0];
  const series=st.structures.map(s=>({name:s.name.replace(/_/g,' '),key:s.name,y:s.payoff_grid,color:'var(--s1)',dim:s.name!==S.emphasis}));
  const vl=[]; if(S.fx&&S.fx.spot) vl.push({x:S.fx.spot,label:'spot '+num(S.fx.spot,2)}); if(S.fx&&S.fx.outright) vl.push({x:S.fx.outright,label:'fwd '+num(S.fx.outright,2)});
  renderLine($('.chart',cp),{x:st.spot_grid,xFmt:(v,f)=>num(v,2),yFmt:(v,f)=>num(v,f?4:2),series,vlines:vl,ml:56,label:'Effective rate by spot at settlement, one structure emphasised'});
  const pick=key=>{ S.emphasis=key; renderStructures(); };
  $('.chart',cp).append(legend(series.map(s=>({name:s.name,key:s.key,color:'var(--s1)',dim:s.dim,on:!s.dim})),pick));
  table($('.tbl',cp),[{label:'Spot'}].concat(names.map(n=>({label:n.replace(/_/g,' '),num:1}))), st.spot_grid.map((sp,i)=>[num(sp,2)].concat(st.structures.map(s=>num(s.payoff_grid[i],4)))));
  const e=st.exposure; $('#cap-pay').replaceChildren('Exposure ', h('b',{text:compact(e.amount)+' '+e.base+' '+e.direction}), ' settling ', h('b',{text:e.settlement}), '. Effective rate in ', h('span',{class:'m',text:e.quote+' per '+e.base}), ' after each structure pays out. Unhedged is the diagonal.');
  const maxc=Math.max(...st.structures.map(s=>Math.abs(s.total_cost)))||1;
  table($('.tbl',cc),[{label:'Structure'},{label:'Cost '+e.quote,num:1},{label:'Worst',num:1},{label:'Best',num:1},{label:'Upside',num:1}], st.structures.map(s=>[s.name.replace(/_/g,' '), h('span',null,h('span',{class:'bar',style:'width:'+Math.round(48*Math.abs(s.total_cost)/maxc)+'px'}),num(s.total_cost,0)), num(s.worst_case_rate,3), num(s.best_case_rate,3), num(s.upside_participation,2)]), i=>({on:st.structures[i].name===S.emphasis,onclick:()=>pick(st.structures[i].name)}));
  const a=st.program_audit; const rv=st.residual_variance;
  $('#cap-cmp').replaceChildren(st.trade_off&&st.trade_off.statement?st.trade_off.statement+' ':'', h('br'), a? h('span',null, h('span',{class:'status '+({info:'info',warning:'warn',error:'crit',breach:'crit'}[(a.violations[0]||{}).severity]||'good'),text:(a.violations[0]||{}).code||'no findings'}), ' ', (a.violations[0]||{}).message||'', ' Programme ', h('span',{class:'m',text:a.program.name}), ', band ', h('span',{class:'m',text:num(a.program.lower_bound,2)+' to '+num(a.program.upper_bound,2)}), '.') : '', rv? h('span',null, h('br'), 'Residual volatility ', h('b',{text:pct(rv.residual_volatility_at_hedge_ratio,2)}), ' at hedge ratio ', h('b',{text:num(rv.hedge_ratio,2)}), ', from ', h('b',{text:pct(rv.total_volatility_unhedged,2)}), ' unhedged, correlation ', h('span',{class:'m',text:num(rv.correlation,2)}), '.') : '');
}

function renderLoaded(){ const c=$('#loaded'); c.replaceChildren(); if(!S.loaded.length){ c.append(h('span',{class:'chip',text:'nothing loaded'})); return; } S.loaded.forEach(f=>c.append(h('span',{class:'chip'+(f.embedded?' embedded':''),text:(f.embedded?'embedded ':'')+f.name}))); }

function renderFooter(){ const f=$('#foot'); f.replaceChildren(); const m=S.meta; if(m){ f.append(h('span',{class:'m',text:'finport-ratesengine '+(m.engine_version||'?')}), ', generated '+(m.generated_at||'')+'. '); } f.append('Every number on this page is a field of a ', h('span',{class:'m',text:'rateng --json'}), ' payload. Nothing is recomputed here. Exit code 1 means inputs unusable, 2 means the calculation is impossible.'); }

function classify(p){ const t=p&&p.result_type; if(t==='BootstrapResult'&&p.views) return 'bootstrap'; if(t==='PriceResult'&&p.measures) return 'price'; if(t==='HedgeResult') return 'hedge'; if(t==='FXForwardResult') return 'fx'; if(t==='StructureComparison') return 'structures'; if(p&&p.kind==='interpolation_comparison') return 'interp'; return null; }

function renderAll(){ renderKPIs(); renderCurves(); renderInterp(); renderRisk(); renderHedge(); renderFX(); renderStructures(); renderLoaded(); renderFooter(); }

$('#files').addEventListener('change', async e=>{ const files=[...e.target.files]; const bad=[]; for(const f of files){ try{ const p=JSON.parse(await f.text()); const k=classify(p); if(!k){ bad.push(f.name); continue; } S[k]=p; S.loaded=S.loaded.filter(x=>x.kind!==k); S.loaded.push({name:f.name,kind:k,embedded:false}); if(k==='structures') S.emphasis=null; } catch(err){ bad.push(f.name); } } renderAll(); if(bad.length){ $('#loaded').append(h('span',{class:'chip',style:'color:var(--crit)',text:'not a rateng payload: '+bad.join(', ')})); } e.target.value=''; });

document.addEventListener('click', e=>{ const b=e.target.closest('.seg button'); if(!b) return; const card=b.closest('.card'); const view=b.dataset.view; card.querySelectorAll('.seg button').forEach(x=>x.classList.toggle('on',x===b)); $('.chart',card).hidden = view!=='chart'; $('.tbl',card).hidden = view!=='table'; });

let rt=null; window.addEventListener('resize', ()=>{ clearTimeout(rt); rt=setTimeout(renderAll,120); });
renderAll();
})();
</script>
</body>
</html>
```
