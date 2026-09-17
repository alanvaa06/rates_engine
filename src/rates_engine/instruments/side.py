"""Which side of a trade is held, and the sign that follows from it.

Its own module because both :mod:`rates_engine.instruments.swaps` and
:mod:`rates_engine.instruments.fra` need it, and one importing a private
helper from the other is the shape that turns into a cycle the next time
something is added.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Side", "fixed_leg_sign"]


class Side(StrEnum):
    """Which side of a two-legged trade is held.

    ``PAYER``
        Pays the fixed rate and receives the index.
    ``RECEIVER``
        Receives the fixed rate and pays the index.
    """

    PAYER = "payer"
    RECEIVER = "receiver"


def fixed_leg_sign(side: str) -> float:
    """The sign of the fixed leg's cashflows for a given side.

    A payer's fixed flows are negative and its floating flows positive; a
    receiver is the exact negation. Every DV01 and duration in this package
    inherits that, which is why the sign is computed in one place.

    Args:
        side: ``"payer"`` or ``"receiver"``. A plain string compares equal to
            the corresponding :class:`Side` member.

    Returns:
        ``-1.0`` for a payer, ``+1.0`` for a receiver.

    Raises:
        ValueError: ``side`` is neither, named in the message. Never guessed
            at: a sign error here inverts every risk number silently.
    """
    if side == Side.PAYER:
        return -1.0
    if side == Side.RECEIVER:
        return 1.0
    raise ValueError(
        f"side must be '{Side.PAYER}' or '{Side.RECEIVER}', got {side!r}"
    )
