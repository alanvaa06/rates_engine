"""The one way this package moves a curve and reprices: the bump.

Every sensitivity here is a finite difference of present value under a
shifted curve, and they used to spell that out one by one --
``pv(instrument, curve_set.shifted(shift)).value`` in six places, each
building a full evidence record around a number it then threw away. This
module is the single primitive they share: a validated shift, a shape, and
a reprice that returns the bare value.

**What a shift is.** :meth:`~rates_engine.curves.discount.DiscountCurve.shifted`
moves the continuously compounded zero curve by a constant or by a function
of time, on the discount and projection curves alike. A parallel basis point
is the constant ``1e-4``; a key-rate shock is a tent (:func:`tent_weights`).

**Central differences.** ``(PV(y - h) - PV(y + h)) / 2``, not a one-sided
bump. The symmetric form cancels the second-order term exactly, which is what
lets a receiver and a payer agree in magnitude to machine precision and what
lets the key-rate profile sum back to the parallel DV01. A one-sided bump
would leave a curvature residual in both places, and it would look like a bug
in the key rates rather than in the differencing.
"""

from __future__ import annotations

from collections.abc import Callable

from rates_engine.curves.discount import CurveSet
from rates_engine.pricing.linear import Priceable, discounted_value

__all__ = ["BUMP_BP", "Shift", "shift_from_bp", "tent_weights", "tent_shift", "shifted", "repriced"]

BUMP_BP = 1.0
"""Default bump size in basis points for every risk measure."""

Shift = Callable[[float], float] | float
"""A zero-rate shift in decimal: a constant, or a function of time in years."""


def shift_from_bp(bump_bp: float) -> float:
    """The decimal zero-rate shift for a bump in basis points.

    Args:
        bump_bp: Bump size in basis points, strictly positive.

    Returns:
        ``bump_bp * 1e-4``.

    Raises:
        ValueError: ``bump_bp`` is not positive.
    """
    if bump_bp <= 0.0:
        raise ValueError(f"bump_bp must be positive, got {bump_bp!r}")
    return bump_bp * 1e-4


def tent_weights(time: float, key_tenors: tuple[float, ...]) -> tuple[float, ...]:
    """Weights of each key tenor's triangular shock at one point in time.

    The weights are a partition of unity: they are non-negative and sum to
    exactly one at every time, so the shocks add up to a parallel shift. Below
    the first key tenor the first shock carries all the weight, and above the
    last the last one does, which is what keeps the property true outside the
    grid as well as inside it.

    Args:
        time: Time in years from the valuation date.
        key_tenors: Key tenors in years, strictly increasing.

    Returns:
        One weight per key tenor, in the same order.

    Raises:
        ValueError: ``key_tenors`` is empty or not strictly increasing.
    """
    if not key_tenors:
        raise ValueError("key_tenors must not be empty")
    if any(b <= a for a, b in zip(key_tenors, key_tenors[1:], strict=False)):
        raise ValueError(f"key_tenors must be strictly increasing, got {key_tenors}")
    if len(key_tenors) == 1:
        return (1.0,)
    if time <= key_tenors[0]:
        return (1.0,) + (0.0,) * (len(key_tenors) - 1)
    if time >= key_tenors[-1]:
        return (0.0,) * (len(key_tenors) - 1) + (1.0,)
    weights = [0.0] * len(key_tenors)
    for index in range(len(key_tenors) - 1):
        left, right = key_tenors[index], key_tenors[index + 1]
        if left <= time <= right:
            share = (time - left) / (right - left)
            weights[index] = 1.0 - share
            weights[index + 1] = share
            break
    return tuple(weights)


def tent_shift(index: int, key_tenors: tuple[float, ...], shift: float) -> Callable[[float], float]:
    """The tent shock of one key tenor, scaled to ``shift`` at its peak.

    Args:
        index: Which key tenor's tent.
        key_tenors: Key tenors in years, strictly increasing.
        shift: Peak height in decimal rate; negative for the down bump.

    Returns:
        A function of time in years giving the zero-rate shift there.
    """

    def shape(time: float) -> float:
        return shift * tent_weights(time, key_tenors)[index]

    return shape


def shifted(curve_set: CurveSet, shift: Shift) -> CurveSet:
    """Both curves of ``curve_set`` with their zero rates moved by ``shift``.

    The one call in the package that moves a curve for risk. Measures that
    need more than a present value on the moved curves -- option greeks, the
    shock table's post-shock DV01 -- take the curve set from here.

    Args:
        curve_set: The unshifted curves.
        shift: A constant decimal shift or a function of time in years.

    Returns:
        The shifted curve set; the original is untouched.
    """
    return curve_set.shifted(shift)


def repriced(instrument: Priceable, curve_set: CurveSet, shift: Shift) -> float:
    """Present value after shifting both curves of ``curve_set`` by ``shift``.

    Args:
        instrument: Anything with cashflows.
        curve_set: The unshifted curves.
        shift: A constant decimal shift or a function of time in years.

    Returns:
        The present value on the shifted curves, in their currency.
    """
    return discounted_value(instrument, shifted(curve_set, shift))
