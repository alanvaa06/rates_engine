"""Currency, carried on the things that have one.

Until v3 this package had one currency and never said so. Every discount
factor was a dollar discount factor, every cashflow a dollar amount, and
that was true by the fact that nothing else existed rather than by anything
written down.

A second currency makes the omission dangerous. Discounting peso cashflows
on the dollar curve produces a number. Adding a dollar present value to a
peso one produces a number. Neither raises, and the evidence chain — the
whole argument of this package — records nothing, because nothing in it
ever knew what a currency was. Both now refuse:
:func:`rates_engine.pricing.pv` checks each flow against the curve that
discounts it, and :meth:`rates_engine.pricing.PriceResult.__add__` checks
the two units.

The fix is the one v2 used for volatility units: the type carries the fact,
so the mistake cannot be made rather than being caught downstream. The
default is :data:`Currency.USD`, which is what keeps the change additive —
every v1 and v2 call means exactly what it meant before, and the existing
test suite is the regression test for that claim.

**Not a second home for the refusal.** :class:`~rates_engine.errors.
CurrencyMismatchError` is imported here and raised from here, and is *not*
in this module's ``__all__``: every refusal has one documented home.

**What this is not.** Not FX conversion. Nothing here turns pesos into
dollars; that needs a rate, a date and a quoting convention, and it lives
in :mod:`rates_engine.fx`. This module only lets two amounts say whether
they are commensurable, and refuses when they are not.
"""

from __future__ import annotations

from enum import StrEnum

from rates_engine.errors import CurrencyMismatchError

__all__ = ["Currency", "require_same_currency"]
# CurrencyMismatchError is imported for use, not re-exported: every refusal
# has exactly one documented home, which is rates_engine.errors.


class Currency(StrEnum):
    """The currencies this build knows.

    ``USD``
        United States dollar. The default everywhere, which is what makes
        the introduction of a currency additive.
    ``MXN``
        Mexican peso.

    Deliberately short. PRD-003's Non-Goals name every other currency as
    out of scope, and an enumeration that lists currencies the engine has
    no curve for would be an invitation.
    """

    USD = "USD"
    MXN = "MXN"


def require_same_currency(left: Currency, right: Currency, *, operation: str) -> Currency:
    """Both currencies, or a refusal naming the operation that needed them equal.

    Args:
        left: One currency.
        right: The other.
        operation: What was being attempted, for the message. Write it as a
            noun phrase: ``"discounting a cashflow"``, not ``"discount"``.

    Returns:
        The shared currency.

    Raises:
        CurrencyMismatchError: They differ.
    """
    if left is not right:
        raise CurrencyMismatchError(
            f"{operation} needs one currency and got two: {left.value} and {right.value}. "
            "There is no implicit conversion here — a rate, a date and a quoting "
            "convention are needed for that, and rates_engine.fx is where they live."
        )
    return left
