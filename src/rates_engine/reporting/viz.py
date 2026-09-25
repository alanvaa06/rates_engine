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
