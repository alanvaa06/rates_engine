"""Call or put on a rate, and the three vocabularies that mean the same thing.

A payer swaption, a caplet and a call on the forward rate are one option.
A receiver swaption, a floorlet and a put are the other. The market uses three
sets of words because the products are traded by different desks, and code
that carries all three ends up with a boolean called ``is_payer`` threaded
through the pricers. One enum, and a translation at the boundary.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["OptionKind"]


class OptionKind(StrEnum):
    """Which way an option on a rate pays.

    ``CALL``
        Pays when the rate rises above the strike. A payer swaption, a caplet.
    ``PUT``
        Pays when the rate falls below it. A receiver swaption, a floorlet.
    """

    CALL = "call"
    PUT = "put"

    @property
    def sign(self) -> float:
        """``+1`` for a call, ``-1`` for a put.

        The two pricers differ by this sign and nothing else, which is worth
        making visible rather than writing each formula out twice.
        """
        return 1.0 if self is OptionKind.CALL else -1.0

    @property
    def opposite(self) -> OptionKind:
        """The other one, for parity relations."""
        return OptionKind.PUT if self is OptionKind.CALL else OptionKind.CALL

    @classmethod
    def from_swaption_side(cls, side: str) -> OptionKind:
        """Translate ``"payer"`` or ``"receiver"`` into a kind.

        Args:
            side: The swaption side.

        Returns:
            ``CALL`` for a payer, ``PUT`` for a receiver.

        Raises:
            ValueError: ``side`` is neither.
        """
        if side == "payer":
            return cls.CALL
        if side == "receiver":
            return cls.PUT
        raise ValueError(f"swaption side must be 'payer' or 'receiver', got {side!r}")

    @classmethod
    def from_cap_floor(cls, kind: str) -> OptionKind:
        """Translate ``"cap"`` or ``"floor"`` into a kind.

        Args:
            kind: The product name.

        Returns:
            ``CALL`` for a cap, ``PUT`` for a floor.

        Raises:
            ValueError: ``kind`` is neither.
        """
        if kind == "cap":
            return cls.CALL
        if kind == "floor":
            return cls.PUT
        raise ValueError(f"kind must be 'cap' or 'floor', got {kind!r}")
