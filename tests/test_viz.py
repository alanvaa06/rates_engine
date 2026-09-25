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
