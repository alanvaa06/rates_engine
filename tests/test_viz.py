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
