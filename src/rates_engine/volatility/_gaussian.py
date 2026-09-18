"""The standard normal, computed through ``erfc`` so the tails stay accurate.

``0.5 * (1 + erf(x / sqrt(2)))`` loses every significant digit once ``erf``
approaches −1, which is where deep out-of-the-money options live. ``erfc``
does not, so the complementary form is used on both sides.
"""

from __future__ import annotations

import math

__all__ = ["standard_normal_cdf", "standard_normal_pdf", "SQRT_TWO", "INV_SQRT_TWO_PI"]

SQRT_TWO = math.sqrt(2.0)
"""Square root of two, named so the formulas below read as they are written."""

INV_SQRT_TWO_PI = 1.0 / math.sqrt(2.0 * math.pi)
"""The standard normal density's normalising constant."""


def standard_normal_cdf(x: float) -> float:
    """Cumulative distribution of the standard normal.

    Args:
        x: The point to evaluate at.

    Returns:
        The probability of a standard normal falling at or below ``x``,
        accurate into both tails.
    """
    return 0.5 * math.erfc(-x / SQRT_TWO)


def standard_normal_pdf(x: float) -> float:
    """Density of the standard normal.

    Args:
        x: The point to evaluate at.

    Returns:
        The density, which underflows to zero rather than raising past about
        forty standard deviations.
    """
    if abs(x) > 40.0:
        return 0.0
    return INV_SQRT_TWO_PI * math.exp(-0.5 * x * x)
