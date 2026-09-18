"""Volatility carries its own units, because the alternative is a factor of ten thousand.

A rates desk quotes normal volatility in basis points and lognormal volatility
in percent, and both arrive in code as a bare ``float``. Pass 0.30 to a
function expecting a normal vol and you have asked for 3,000 basis points; pass
0.0060 to one expecting lognormal and you have asked for 0.6%. Neither raises,
both price, and the answer is wrong by orders of magnitude.

PRD-002 AC-1.4 asks for a refusal when the magnitude is implausible. That is
the right backstop and the wrong primary defence: it only fires on the cases
that happen to be far out, and it cannot tell a deliberate extreme from a unit
slip. So the unit lives in the type. :class:`Volatility` knows what it is,
converts explicitly, and refuses to hand a lognormal number to something
expecting a normal one. The magnitude band then covers what remains — the
right enum with an absurd number in it.

**The one conversion that is not a conversion.** ``sigma_normal ~ sigma_lognormal * F``
holds at the money and only there; it is the leading term of an expansion, not
an identity. :meth:`Volatility.atm_equivalent` computes it and says so in its
name and docstring, and nothing in this package uses it to convert a quote —
it exists to cross-check two independent inversions, which is what AC-1.3 asks
for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rates_engine.errors import VolUnitsError

__all__ = [
    "VolUnits",
    "Volatility",
    "NORMAL_MAX_BP",
    "NORMAL_MIN_BP",
    "LOGNORMAL_MIN_DECIMAL",
    "LOGNORMAL_MAX_DECIMAL",
]

NORMAL_MAX_BP = 1000.0
"""Above this a normal vol is almost certainly a lognormal quote in disguise.

Ten percent of absolute rate movement a year. Swaption normal vols live around
60 to 150 bp; 1,000 bp is not a stressed market, it is 10% lognormal read as a
decimal (PRD-002 AC-1.4).
"""

NORMAL_MIN_BP = 0.1
"""Below this a positive normal vol is almost certainly a percent-quoted number
divided by 100 twice. Zero is allowed separately as the degenerate case."""

LOGNORMAL_MIN_DECIMAL = 0.05
"""Below 5% a lognormal vol is almost certainly a normal quote in decimals.

PRD-002 AC-1.4 proposed 0.5%, and 0.5% does not catch the mistake the AC is
written to catch. Swaption normal vols live at 60 to 150 bp, which as decimals
are 0.006 to 0.015 — all of it above 0.005, so the whole population of
normal-quotes-passed-as-lognormal would sail through. Lognormal swaption vols
live at 15% to 60%. A floor of 5% sits in the empty space between the two
populations and separates them cleanly, which 0.5% does not.

:meth:`Volatility.unchecked` exists for the genuinely extreme case.
"""

LOGNORMAL_MAX_DECIMAL = 5.0
"""Above 500% a lognormal vol is almost certainly basis points read as percent."""


class VolUnits(StrEnum):
    """How a volatility number is quoted.

    ``NORMAL_BP``
        Absolute (Bachelier) volatility in basis points a year. The market's
        quoting convention for swaptions since rates went negative.
    ``NORMAL_DECIMAL``
        The same thing as a decimal rate, so 100 bp is ``0.01``.
    ``LOGNORMAL_PERCENT``
        Relative (Black) volatility in percent, so 30% is ``30.0``.
    ``LOGNORMAL_DECIMAL``
        The same as a decimal, so 30% is ``0.30``.
    """

    NORMAL_BP = "normal_bp"
    NORMAL_DECIMAL = "normal_decimal"
    LOGNORMAL_PERCENT = "lognormal_percent"
    LOGNORMAL_DECIMAL = "lognormal_decimal"

    @property
    def is_normal(self) -> bool:
        """True for the two absolute-volatility spellings."""
        return self in (VolUnits.NORMAL_BP, VolUnits.NORMAL_DECIMAL)

    @property
    def is_lognormal(self) -> bool:
        """True for the two relative-volatility spellings."""
        return not self.is_normal


@dataclass(frozen=True)
class Volatility:
    """A volatility that knows what it is.

    Attributes:
        value: The number, in whatever :attr:`units` says.
        units: The quoting convention.

    Raises:
        VolUnitsError: The magnitude is implausible for the declared units —
            the residual case the type system cannot catch, where the enum is
            right and the number is not. Exactly zero is allowed and means the
            degenerate deterministic case.
        ValueError: The value is negative.
    """

    value: float
    units: VolUnits

    def __post_init__(self) -> None:
        if self.value < 0.0:
            raise ValueError(f"volatility must be non-negative, got {self.value!r}")
        if self.value == 0.0:
            return
        if self.units.is_normal:
            in_bp = self.value if self.units is VolUnits.NORMAL_BP else self.value * 1e4
            if in_bp > NORMAL_MAX_BP:
                raise VolUnitsError(
                    f"{self.value} {self.units.value} is {in_bp:,.0f} bp of normal volatility, "
                    f"above the plausible ceiling of {NORMAL_MAX_BP:,.0f} bp. A lognormal quote "
                    "passed as a normal one looks exactly like this."
                )
            if in_bp < NORMAL_MIN_BP:
                raise VolUnitsError(
                    f"{self.value} {self.units.value} is {in_bp:.4f} bp of normal volatility, "
                    f"below the plausible floor of {NORMAL_MIN_BP} bp. Check whether a percent "
                    "figure was divided by one hundred twice."
                )
            return
        decimal = (
            self.value / 100.0 if self.units is VolUnits.LOGNORMAL_PERCENT else self.value
        )
        if decimal < LOGNORMAL_MIN_DECIMAL:
            raise VolUnitsError(
                f"{self.value} {self.units.value} is {decimal:.5%} of lognormal volatility, "
                f"below the plausible floor of {LOGNORMAL_MIN_DECIMAL:.1%}. A normal quote in "
                "decimals passed as a lognormal one looks exactly like this."
            )
        if decimal > LOGNORMAL_MAX_DECIMAL:
            raise VolUnitsError(
                f"{self.value} {self.units.value} is {decimal:.0%} of lognormal volatility, "
                f"above the plausible ceiling of {LOGNORMAL_MAX_DECIMAL:.0%}. Check whether "
                "basis points were read as percent."
            )

    @classmethod
    def unchecked(cls, value: float, units: VolUnits) -> Volatility:
        """Build a volatility skipping the plausibility band.

        For the two cases the band cannot serve: exercising its own limits in
        tests, and a market genuinely outside it. It skips the *magnitude*
        check only — the units are still carried and still refuse to convert
        into each other, which is the part that stops the four-orders-of-
        magnitude mistake.

        Args:
            value: The number, non-negative.
            units: The quoting convention, still required.

        Returns:
            The :class:`Volatility`.

        Raises:
            ValueError: The value is negative.
        """
        if value < 0.0:
            raise ValueError(f"volatility must be non-negative, got {value!r}")
        instance = object.__new__(cls)
        object.__setattr__(instance, "value", float(value))
        object.__setattr__(instance, "units", units)
        return instance

    @classmethod
    def normal_bp(cls, value: float) -> Volatility:
        """Build a normal volatility from a basis-point quote."""
        return cls(value, VolUnits.NORMAL_BP)

    @classmethod
    def lognormal_percent(cls, value: float) -> Volatility:
        """Build a lognormal volatility from a percent quote."""
        return cls(value, VolUnits.LOGNORMAL_PERCENT)

    @property
    def is_normal(self) -> bool:
        """True when this is an absolute (Bachelier) volatility."""
        return self.units.is_normal

    def as_normal_decimal(self) -> float:
        """The absolute volatility as a decimal rate a year.

        Returns:
            The value in decimal form, so 100 bp comes back as ``0.01``.

        Raises:
            VolUnitsError: This is a lognormal volatility. There is no
                unit conversion between the two — only a model-dependent
                equivalence that holds at the money and nowhere else. Use
                :meth:`atm_equivalent_normal` and read what it says first.
        """
        if not self.is_normal:
            raise VolUnitsError(
                f"{self.units.value} is a relative volatility and cannot be converted to an "
                "absolute one by scaling. The two describe different dynamics; the familiar "
                "sigma_normal = sigma_lognormal * F holds at the money only, as the leading "
                "term of an expansion. Call atm_equivalent_normal(forward) if that is what "
                "you mean, and read its docstring."
            )
        return self.value * 1e-4 if self.units is VolUnits.NORMAL_BP else self.value

    def as_lognormal_decimal(self) -> float:
        """The relative volatility as a decimal a year.

        Returns:
            The value in decimal form, so 30% comes back as ``0.30``.

        Raises:
            VolUnitsError: This is a normal volatility; see
                :meth:`as_normal_decimal` for why no conversion exists.
        """
        if self.is_normal:
            raise VolUnitsError(
                f"{self.units.value} is an absolute volatility and cannot be converted to a "
                "relative one by scaling. See as_normal_decimal for why, and "
                "atm_equivalent_lognormal(forward) for the at-the-money equivalence."
            )
        return self.value / 100.0 if self.units is VolUnits.LOGNORMAL_PERCENT else self.value

    def as_normal_bp(self) -> float:
        """The absolute volatility in basis points.

        Returns:
            The value in basis points.

        Raises:
            VolUnitsError: This is a lognormal volatility.
        """
        return self.as_normal_decimal() * 1e4

    def atm_equivalent_normal(self, forward: float) -> Volatility:
        """The normal volatility matching this lognormal one *at the money*.

        ``sigma_normal = sigma_lognormal * F``. This is the leading term of an
        expansion around the money, not a unit conversion: away from the
        forward the two models disagree about the smile, and at long expiries
        they disagree at the money too. It is here to cross-check two
        independent implied-vol inversions, which is what PRD-002 AC-1.3 asks
        for, and for nothing else.

        Args:
            forward: The forward rate as a decimal.

        Returns:
            A normal :class:`Volatility` in basis points.

        Raises:
            VolUnitsError: This is already a normal volatility.
        """
        return Volatility.normal_bp(self.as_lognormal_decimal() * forward * 1e4)

    def atm_equivalent_lognormal(self, forward: float) -> Volatility:
        """The lognormal volatility matching this normal one at the money.

        The inverse of :meth:`atm_equivalent_normal`, with the same caveat:
        approximate, and only near the forward.

        Args:
            forward: The forward rate as a decimal. Must be positive — a
                lognormal volatility is undefined at a non-positive forward.

        Returns:
            A lognormal :class:`Volatility` as a decimal.

        Raises:
            VolUnitsError: This is already a lognormal volatility.
            ValueError: ``forward`` is not positive.
        """
        if forward <= 0.0:
            raise ValueError(
                f"a lognormal volatility is undefined at a forward of {forward!r}; "
                "this is why the market quotes normal vol"
            )
        return Volatility(self.as_normal_decimal() / forward, VolUnits.LOGNORMAL_DECIMAL)

    def to_dict(self) -> dict[str, object]:
        """Serialise the number together with the units that give it meaning."""
        return {"value": self.value, "units": self.units.value}
