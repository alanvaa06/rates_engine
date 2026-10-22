"""Eight ways to hedge a transaction exposure, priced and laid side by side.

The CFA Level III framing, implemented as arithmetic. A treasurer with a
foreign-currency payable or receivable can leave it open, sell it forward,
buy an option, or build one of four option combinations, and the question
is never which is best — it is what each one costs and what it gives up.

**This module does not recommend.** It returns a table. Cost goes up,
protection goes up, and participation in a favourable move goes down; that
trade-off is reported as labelled fields rather than as a sentence, and
there is no function here whose name contains ``recommend`` because there
is no such function. PRD-003 AC-4.5 asks for a test of that, and
``tests/test_hedge_structures.py`` scans the public surface for it.

**Which option protects depends on the direction.** A treasurer whose
functional currency is the quote currency and who *owes* base currency is
hurt when the pair rises, so protection is a call. One who is *owed* base
currency is hurt when it falls, so protection is a put. Getting this
backwards produces a table of structures that all make the exposure worse,
which is why :class:`Exposure` carries the direction and the structures ask
it rather than assuming a receivable.

**The unhedged row is deliberate.** A comparison whose cheapest option is
still an option hides the fact that doing nothing is free and sometimes
right. It is the zero-cost, zero-protection, full-participation baseline
every other row is read against.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from rates_engine.conventions.currency_pair import CurrencyPair
from rates_engine.conventions.option_kind import OptionKind
from rates_engine.core.errors import ImplausibleInputError, VolUnitsError
from rates_engine.core.evidence import Evidence
from rates_engine.core.results import EngineResult
from rates_engine.models import garman_kohlhagen as gk
from rates_engine.pricing.fx_forward import (
    BASIS_LEG,
    require_plausible_basis,
    require_plausible_rate,
)
from rates_engine.volatility.units import Volatility, VolUnits

__all__ = [
    "STRUCTURE_NAMES",
    "ExposureDirection",
    "Exposure",
    "StructureQuote",
    "StructureResult",
    "StructureComparison",
    "TRADE_OFF_FRAME",
    "compare_structures",
    "zero_cost_collar_strike",
]

STRUCTURE_NAMES: frozenset[str] = frozenset(
    {
        "unhedged",
        "forward",
        "protective_option_atm",
        "protective_option_otm",
        "collar",
        "collar_zero_cost",
        "option_spread",
        "seagull",
    }
)
"""Every structure :func:`compare_structures` builds.

Exported so that :mod:`rates_engine.hedging.program` can validate the names a
policy permits against the names that exist, rather than the two modules
agreeing by coincidence across a string literal.
"""

TRADE_OFF_FRAME: dict[str, str] = {
    "axes": "upfront_cost, worst_case_rate, best_case_rate, upside_participation",
    "monotone": "no",
    "statement": (
        "Full at-the-money protection is the most expensive and the seagull the "
        "cheapest. Between those two the ordering is not monotone: a spread caps "
        "the protection it buys, so it can cost more than a collar and still have "
        "a worse worst case. Read the four columns; they do not collapse onto one "
        "axis, and which point matters depends on the treasury policy rather than "
        "on the numbers."
    ),
}
"""The trade-off, as labelled fields rather than as advice.

AC-4.4. An earlier version of this asserted ``upfront_cost: higher`` implies
``protection: better`` and ``upside_participation: worse`` — a monotonicity
this module's own default structure set does not have. On the PRD's own
market the option spread costs 0.16 and has a *worse* worst case than the
zero-cost collar, because its sold far wing reopens the tail by
construction; participation is not monotone in cost either. Shipping that
claim as a labelled field in every payload was the exact thing AC-4.4
exists to prevent, so the frame now states what the table supports and says
plainly that it does not collapse onto one axis.
"""


class ExposureDirection(StrEnum):
    """Which way the exposure hurts.

    ``PAYABLE``
        Base currency is owed. A rise in the pair costs money, so protection
        is a call.
    ``RECEIVABLE``
        Base currency is expected. A fall in the pair costs money, so
        protection is a put.
    """

    PAYABLE = "payable"
    RECEIVABLE = "receivable"

    @property
    def protective_kind(self) -> OptionKind:
        """The option that limits the loss."""
        return OptionKind.CALL if self is ExposureDirection.PAYABLE else OptionKind.PUT

    @property
    def sign(self) -> float:
        """``-1`` for a payable, ``+1`` for a receivable.

        The sign of the unhedged position's sensitivity to the pair: a
        receivable gains when the pair rises, a payable loses.
        """
        return -1.0 if self is ExposureDirection.PAYABLE else 1.0


@dataclass(frozen=True)
class Exposure:
    """What is being hedged.

    Attributes:
        amount: Size in base currency, positive. The direction is carried
            separately rather than as a sign, because a negative amount and
            a payable are two ways to say the same thing and supporting both
            invites saying it twice.
        direction: Payable or receivable.
        settlement: When it settles.
        pair: The currency pair, base first.
    """

    amount: float
    direction: ExposureDirection
    settlement: date
    pair: CurrencyPair

    def __post_init__(self) -> None:
        if not 0.0 < self.amount < math.inf:
            raise ValueError(
                f"an exposure's amount is a positive size in {self.pair.base.value}; "
                f"its direction says which way it hurts. Got {self.amount!r}."
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the exposure and the pair it is in."""
        return {
            "amount": self.amount,
            "direction": self.direction.value,
            "settlement": self.settlement.isoformat(),
            **self.pair.to_dict(),
        }


@runtime_checkable
class ReadableSmile(Protocol):
    """A volatility source that reports where its number came from.

    Structural rather than nominal so that this module does not depend on
    :mod:`rates_engine.volatility.fx_smile`'s concrete class — and so a test can
    supply a two-line stand-in.
    """

    def volatility_at(self, strike: float) -> Any:
        """The reading at a strike: ``.volatility`` and ``.evidence``."""
        ...


def _check_volatility(value: float) -> None:
    """A single volatility, held to the lognormal band :class:`Volatility` applies.

    FX volatility is lognormal and quoted in percent, so a decimal outside
    ``[5%, 500%]`` is almost certainly a unit slip: 11.5 for 11.5%, or a
    normal-vol number such as 0.0115. Zero is allowed, as the degenerate
    case. A smile or a callable is not checked here: a smile carries its own
    evidence, and a callable is the escape hatch.
    """
    if not 0.0 <= value < math.inf:
        raise ValueError(
            f"volatility is a lognormal volatility as a decimal, zero or more; got "
            f"{value!r}"
        )
    try:
        Volatility(value, VolUnits.LOGNORMAL_DECIMAL)
    except VolUnitsError as exc:
        raise VolUnitsError(
            f"{exc} An FX volatility is a lognormal decimal: 0.115 for 11.5%."
        ) from exc


@dataclass(frozen=True)
class StructureQuote:
    """The market a comparison is struck on.

    Attributes:
        spot: Spot rate, quote currency per unit of base.
        expiry: Years from the spot date — when an option premium is paid —
            to settlement. Measured from the spot date rather than the
            valuation date so that the forward these rates imply is the one
            :func:`~rates_engine.pricing.fx_forward.forward_from_curves`
            returns; an option delivering on the settlement date expires the
            spot lag before it, so the same span is its life.
        r_domestic: Quote currency rate, continuously compounded, as a
            decimal a year on the same time basis as ``expiry``.
        r_foreign: Base currency rate, the same convention.
        volatility: One of three things. A ``float`` is a single lognormal
            volatility for every leg. A :class:`ReadableSmile` — anything
            with ``volatility_at(strike)`` returning a reading, which is
            what :class:`~rates_engine.volatility.fx_smile.VannaVolgaSmile` is —
            prices each leg on its own point of the smile *and* carries
            that reading's evidence, which is the honest way to price a
            collar. A bare callable returning a float also works and is the
            escape hatch, but its evidence cannot be collected because it
            has none to give; the payload says which of the three was used.
        evidence: Evidence of where the rates and the spot came from — the
            curves, typically. Without it a structure priced on an MXN curve
            does not inherit that curve's ASSUMED quality, which two places
            in this package promise it does.
        basis_bp: Cross-currency basis in basis points, a spread on the
            quote currency leg as in :mod:`rates_engine.pricing.fx_forward`
            (:data:`~rates_engine.pricing.fx_forward.BASIS_LEG`). The quote
            currency's rate becomes ``r_domestic + basis``: the forward
            moves exactly as ``forward_from_curves`` moves it, and the
            options and the premium carry use the same shifted rate, which
            is the curve that forward implicitly discounts on. Zero, the
            default, is the parity forward.
    """

    spot: float
    expiry: float
    r_domestic: float
    r_foreign: float
    volatility: float | ReadableSmile | Callable[[float], float]
    evidence: tuple[Evidence, ...] = ()
    basis_bp: float = 0.0

    def __post_init__(self) -> None:
        # Every number here used to be priced whatever it was: a volatility
        # typed in percent, a negative one, a settlement in the past, a rate
        # typed in percent (REVISION risk-app/r14_fx_inputs.py).
        if not 0.0 < self.spot < math.inf:
            raise ValueError(f"a spot rate is positive and finite; got spot={self.spot!r}")
        if not 0.0 < self.expiry < math.inf:
            raise ValueError(
                f"expiry is the years from the spot date to settlement and must be "
                f"positive; got expiry={self.expiry!r}. A settlement on or before the "
                "spot date has nothing left to hedge."
            )
        require_plausible_rate(self.r_domestic, "r_domestic")
        require_plausible_rate(self.r_foreign, "r_foreign")
        require_plausible_basis(self.basis_bp)
        source = self.volatility
        if not isinstance(source, ReadableSmile) and not callable(source):
            _check_volatility(float(source))

    @property
    def r_domestic_effective(self) -> float:
        """The quote currency rate with the basis on it, as a decimal a year."""
        return self.r_domestic + self.basis_bp * 1e-4

    @property
    def _smile(self) -> ReadableSmile | None:
        return self.volatility if isinstance(self.volatility, ReadableSmile) else None

    def vol_at(self, strike: float) -> float:
        """The volatility for a leg struck at ``strike``."""
        source = self.volatility
        if isinstance(source, ReadableSmile):
            return float(source.volatility_at(strike).volatility)
        if callable(source):
            return float(source(strike))
        return float(source)

    def sources(self, strikes: tuple[float, ...]) -> tuple[Evidence, ...]:
        """Every piece of evidence behind a pricing at these strikes.

        The rates' own, plus one reading per distinct strike when the
        volatility came from a smile. A smile that held its volatility flat
        past its quoted pillars carries a ``Degradation``, and collecting it
        here is what stops :meth:`vol_at`'s ``float()`` from throwing that
        away — which it did, silently, until the review caught it.
        """
        collected = list(self.evidence)
        smile = self._smile
        if smile is not None:
            collected.extend(
                smile.volatility_at(strike).evidence for strike in dict.fromkeys(strikes)
            )
        return tuple(collected)

    @property
    def forward(self) -> float:
        """The outright forward these rates and the basis imply."""
        return gk.forward(self.spot, self.expiry, self.r_domestic_effective, self.r_foreign)

    def price(self, strike: float, kind: OptionKind) -> float:
        """One option, per unit of base currency, on the basis-adjusted forward."""
        return gk.price(
            self.spot, strike, self.expiry, self.r_domestic_effective, self.r_foreign,
            self.vol_at(strike), kind,
        )

    def carried(self, premium: float) -> float:
        """A premium paid on the spot date, carried to settlement.

        Args:
            premium: Quote currency per unit of base, paid on the spot date.

        Returns:
            The same amount at settlement, grown at the basis-adjusted quote
            currency rate.
        """
        return premium * math.exp(self.r_domestic_effective * self.expiry)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the market, saying whether a smile was used."""
        return {
            "spot": self.spot,
            "forward": self.forward,
            "expiry": self.expiry,
            "r_domestic": self.r_domestic,
            "r_foreign": self.r_foreign,
            "rate_convention": "continuously compounded, decimal a year",
            "basis_bp": self.basis_bp,
            "basis_leg": BASIS_LEG,
            "r_domestic_effective": self.r_domestic_effective,
            "volatility_source": (
                "smile"
                if self._smile is not None
                else "callable" if callable(self.volatility) else "single"
            ),
            "volatility": None if callable(self.volatility) else self.volatility,
        }


@dataclass(frozen=True)
class _Leg:
    """One bought or sold option inside a structure."""

    kind: OptionKind
    strike: float
    quantity: float  # +1 bought, -1 sold

    def payoff(self, spot: float) -> float:
        return self.quantity * max(self.kind.sign * (spot - self.strike), 0.0)


@dataclass(frozen=True)
class StructureResult(EngineResult):
    """One structure, costed and characterised.

    Attributes:
        name: The structure's name.
        upfront_cost: Net premium in quote currency, per unit of base.
            Positive is paid, negative is received.
        total_cost: The same, scaled by the exposure.
        worst_case_rate: The least favourable effective rate the structure
            can produce at any spot on the settlement date, in quote per
            base. Worked out from the legs, not read off the grid, so it does
            not move with ``grid_width`` and is reported even when the grid
            stops short of the strike that sets it. ``None`` when the
            structure leaves that side open — the rate keeps moving with the
            spot past the outermost strike, so the extreme is set by how far
            the spot goes rather than by the structure — which is
            information rather than a gap.
        best_case_rate: The most favourable, on the same terms, or ``None``
            when that side is open. For a payable the favourable side runs
            down towards a spot of zero, and that floor is not a bound any
            structure bought.
        upside_participation: Fraction of a favourable move retained, in
            ``[0, 1]``: the change in the effective rate as the spot at
            settlement moves from the forward to ``grid_width`` times the
            forward away from it in the favourable direction (down for a
            payable, up for a receivable), over the change in spot. One is
            fully open in the good direction, as the unhedged row is; zero
            is fully fixed, as a full forward is. Measured from the forward
            because every strike here is set off the forward, so mirror-image
            structures — a payable's call and a receivable's put at the same
            offset — keep the same share; a structure that sells a wing
            inside the move reads lower on a wider ``grid_width``.
        hedge_ratio: The fraction of the exposure covered *by a forward*.
            An option structure covers the whole exposure by a different
            mechanism, so this is ``None`` there rather than ``0.0`` —
            which read as "unhedged" and made every option row sort and
            filter as if it were open.
        legs: ``(kind, strike, quantity)`` per option leg.
        forward_amount: Base currency sold or bought forward, signed.
        payoff_grid: Effective rate at each spot on the grid.
        spot_grid: The spots it was evaluated at.
    """

    name: str
    upfront_cost: float
    total_cost: float
    worst_case_rate: float | None
    best_case_rate: float | None
    upside_participation: float
    hedge_ratio: float | None
    legs: tuple[tuple[str, float, float], ...]
    forward_amount: float
    payoff_grid: tuple[float, ...]
    spot_grid: tuple[float, ...]

    def payload_fields(self) -> dict[str, Any]:
        """Numbers and names. No sentence anywhere says which to choose."""
        return {
            "name": self.name,
            "upfront_cost": self.upfront_cost,
            "total_cost": self.total_cost,
            "worst_case_rate": self.worst_case_rate,
            "best_case_rate": self.best_case_rate,
            "upside_participation": self.upside_participation,
            "hedge_ratio": self.hedge_ratio,
            "legs": [
                {"kind": kind, "strike": strike, "quantity": quantity}
                for kind, strike, quantity in self.legs
            ],
            "forward_amount": self.forward_amount,
            "payoff_grid": list(self.payoff_grid),
            "spot_grid": list(self.spot_grid),
        }


def zero_cost_collar_strike(
    quote: StructureQuote,
    protective_strike: float,
    protective_kind: OptionKind,
    *,
    tolerance: float = 1e-12,
    net_premium_to_fund: float | None = None,
) -> float:
    """The strike at which the sold wing exactly pays for what is bought.

    Args:
        quote: The market.
        protective_strike: Strike of the option being bought.
        protective_kind: Which kind that is.
        tolerance: Bisection tolerance on the strike.
        net_premium_to_fund: What the sold wing has to raise. ``None`` means
            the protective option's own premium, which is the collar. A
            seagull passes the *net* cost of its two same-kind legs — the
            bought protection less the far leg it already sold — because
            funding the bought leg alone would sell a wing twice over and
            hand the treasurer a credit for hedging.

    Returns:
        The strike of the opposite-kind option to sell.

    Raises:
        ImplausibleInputError: No strike raises it, which happens when what
            is being funded is worth more than the entire opposite wing.
    """
    target = (
        quote.price(protective_strike, protective_kind)
        if net_premium_to_fund is None
        else net_premium_to_fund
    )
    sold_kind = protective_kind.opposite
    # A sold put pays more the higher its strike; a sold call pays more the
    # lower its strike. Bracket accordingly and bisect.
    low, high = quote.spot * 1e-3, quote.spot * 10.0
    def sold(strike: float) -> float:
        return quote.price(strike, sold_kind)

    if sold_kind is OptionKind.PUT:
        if sold(high) < target:
            raise ImplausibleInputError(
                f"no put strike up to {high:.4f} raises the {target:.6f} needed to pay "
                f"for the {protective_kind.value} at {protective_strike:.4f}. The "
                "protection asked for is worth more than the whole opposite wing, so "
                "there is no zero-cost collar here; widen the protective strike."
            )
        for _ in range(400):
            mid = 0.5 * (low + high)
            if sold(mid) < target:
                low = mid
            else:
                high = mid
            if high - low < tolerance:
                break
        return 0.5 * (low + high)
    if sold(low) < target:
        raise ImplausibleInputError(
            f"no call strike down to {low:.4f} raises the {target:.6f} needed to pay "
            f"for the {protective_kind.value} at {protective_strike:.4f}. There is no "
            "zero-cost collar here; widen the protective strike."
        )
    for _ in range(400):
        mid = 0.5 * (low + high)
        if sold(mid) < target:
            high = mid
        else:
            low = mid
        if high - low < tolerance:
            break
    return 0.5 * (low + high)


def _effective_rate(
    exposure: Exposure,
    quote: StructureQuote,
    legs: tuple[_Leg, ...],
    forward_amount: float,
    forward_rate: float,
    upfront: float,
    spot: float,
) -> float:
    """The rate the treasurer ends up transacting at, all in.

    Everything is expressed per unit of base currency and in quote currency,
    so the number is directly comparable across structures: for a payable it
    is what each unit of base currency ends up costing, and lower is better;
    for a receivable it is what each unit fetches, and higher is better.

    The premium is carried forward to settlement at the quote currency's
    rate, because it is paid on the spot date and the exposure settles
    later, and comparing a cost paid now with one paid then without that
    step understates every option structure.
    """
    carried = quote.carried(upfront)
    hedged_fraction = abs(forward_amount) / exposure.amount
    open_fraction = 1.0 - hedged_fraction
    rate = open_fraction * spot + hedged_fraction * forward_rate
    option_payoff = sum(leg.payoff(spot) for leg in legs)
    # A payable pays the rate and is helped by a positive option payoff; a
    # receivable receives it and is likewise helped. The sign of the premium
    # flips because a payable's "rate" is a cost.
    if exposure.direction is ExposureDirection.PAYABLE:
        return rate - option_payoff + carried
    return rate + option_payoff - carried


def _participation(
    exposure: Exposure,
    forward: float,
    width: float,
    rate_at: Callable[[float], float],
) -> float:
    """The share of a favourable move from the forward that the structure keeps.

    Measured against the unhedged position, which keeps all of it: the
    favourable move is the spot at settlement going from the forward to
    ``width`` times the forward away from it — down for a payable, up for a
    receivable — and the structure keeps whatever its effective rate moves
    by on the way, over what the spot moves by. One means fully open in the
    good direction, zero means fully fixed.

    Two earlier versions were wrong. The first took the rate at the
    *adverse* edge of the grid minus the rate at the favourable one, which
    mixes the protection bought against an adverse move into the share of a
    favourable one: an at-the-money call on a payable, which keeps every
    peso of a fall, read 0.533. The second measured from today's spot to the
    grid's edge, which with USD/MXN's carry puts the forward well above the
    spot: a receivable's at-the-money put, struck at the forward, kept
    nothing of the rise until the forward and read 0.73 at one year where
    the payable's mirror-image call read 1.
    """
    if exposure.direction is ExposureDirection.PAYABLE:
        favourable = forward * (1.0 - width)
        move, kept = forward - favourable, rate_at(forward) - rate_at(favourable)
    else:
        favourable = forward * (1.0 + width)
        move, kept = favourable - forward, rate_at(favourable) - rate_at(forward)
    if move <= 0.0:
        return 0.0
    return max(0.0, min(1.0, kept / move))


_FLAT = 1e-12
"""A tail slope this close to zero is flat: quantities are whole units."""


def _bounds(
    exposure: Exposure,
    legs: tuple[_Leg, ...],
    forward_amount: float,
    rate_at: Callable[[float], float],
) -> tuple[float | None, float | None]:
    """The worst and best effective rate over every spot, or ``None`` where open.

    The effective rate is piecewise linear in the spot at settlement, with
    kinks at the strikes, so its extremes over ``(0, inf)`` are at a kink
    unless a tail keeps moving. Past the highest strike only calls are in
    the money, and below the lowest only puts, so the two tail slopes come
    straight from the legs: the open fraction of the exposure, plus or minus
    each in-the-money leg's quantity. A tail that is not flat is open on the
    side it moves towards — upwards without limit past the top strike, or
    down to the spot's floor at zero below the bottom one, which no
    structure bought — and that side reports ``None``.

    This read the grid's largest and smallest rates, so every open side
    reported the grid's edge: the unhedged payable's worst case was 22.20
    on the default grid and 27.75 on a fifty-percent one.
    """
    open_fraction = 1.0 - abs(forward_amount) / exposure.amount
    helped = 1.0 if exposure.direction is ExposureDirection.RECEIVABLE else -1.0
    upper = open_fraction + helped * sum(
        leg.quantity for leg in legs if leg.kind is OptionKind.CALL
    )
    lower = open_fraction - helped * sum(
        leg.quantity for leg in legs if leg.kind is OptionKind.PUT
    )
    strikes = sorted({leg.strike for leg in legs})
    if not strikes:
        # Linear in the spot: constant when fully forward, open both ways
        # otherwise. Any spot serves as the evaluation point of a constant.
        if abs(open_fraction) <= _FLAT:
            fixed = rate_at(1.0)
            return fixed, fixed
        return None, None
    values = [rate_at(strike) for strike in strikes]
    highest = None if upper > _FLAT or lower < -_FLAT else max(values)
    lowest = None if upper < -_FLAT or lower > _FLAT else min(values)
    if exposure.direction is ExposureDirection.PAYABLE:
        return highest, lowest
    return lowest, highest


def _summarise(
    exposure: Exposure,
    forward: float,
    width: float,
    legs: tuple[_Leg, ...],
    forward_amount: float,
    rate_at: Callable[[float], float],
) -> tuple[float | None, float | None, float]:
    """Worst case, best case and participation."""
    worst, best = _bounds(exposure, legs, forward_amount, rate_at)
    return worst, best, _participation(exposure, forward, width, rate_at)


def _structure(
    name: str,
    exposure: Exposure,
    quote: StructureQuote,
    grid: tuple[float, ...],
    width: float,
    *,
    legs: tuple[_Leg, ...] = (),
    forward_amount: float = 0.0,
    forward_rate: float | None = None,
    hedge_ratio: float | None = None,
) -> StructureResult:
    """Cost one structure and evaluate it across the grid."""
    upfront = sum(leg.quantity * quote.price(leg.strike, leg.kind) for leg in legs)
    sources = quote.sources(tuple(leg.strike for leg in legs))
    outright = quote.forward if forward_rate is None else forward_rate

    def rate_at(spot: float) -> float:
        return _effective_rate(exposure, quote, legs, forward_amount, outright, upfront, spot)

    rates = tuple(rate_at(spot) for spot in grid)
    worst, best, participation = _summarise(
        exposure, quote.forward, width, legs, forward_amount, rate_at
    )
    evidence = Evidence(
        produced_by=f"hedging_structures.{name}",
        fields={
            "structure": name,
            "upfront_cost": upfront,
            "hedge_ratio": hedge_ratio,
            "legs": [
                {"kind": leg.kind.value, "strike": leg.strike, "quantity": leg.quantity}
                for leg in legs
            ],
            "forward_amount": forward_amount,
            "forward_rate": outright,
            "premium_carried_to_settlement": quote.carried(upfront),
            **quote.to_dict(),
            **exposure.to_dict(),
        },
        sources=sources,
    )
    return StructureResult(
        evidence=evidence,
        name=name,
        upfront_cost=upfront,
        total_cost=upfront * exposure.amount,
        worst_case_rate=worst,
        best_case_rate=best,
        upside_participation=participation,
        hedge_ratio=hedge_ratio,
        legs=tuple((leg.kind.value, leg.strike, leg.quantity) for leg in legs),
        forward_amount=forward_amount,
        payoff_grid=rates,
        spot_grid=grid,
    )


@dataclass(frozen=True)
class StructureComparison(EngineResult):
    """Every structure, side by side, and nothing saying which to pick.

    Attributes:
        exposure: What was hedged.
        structures: The rows, in the order they were built.
        spot_grid: The spots every row was evaluated at.
        residual_variance: The variance decomposition for the forward row,
            or ``None`` when no correlation was supplied.
    """

    exposure: Exposure
    structures: tuple[StructureResult, ...]
    spot_grid: tuple[float, ...]
    residual_variance: dict[str, float] | None

    def by_name(self, name: str) -> StructureResult:
        """One row by name.

        Args:
            name: The structure's name.

        Returns:
            Its :class:`StructureResult`.

        Raises:
            KeyError: No such structure.
        """
        for structure in self.structures:
            if structure.name == name:
                return structure
        raise KeyError(
            f"no structure named {name!r}; this comparison holds "
            f"{[s.name for s in self.structures]}"
        )

    def ordered_by_cost(self) -> tuple[StructureResult, ...]:
        """The rows sorted by upfront cost, cheapest first.

        Sorting is not ranking: the cheapest row is the one that protects
        least, which is the whole content of the trade-off.
        """
        return tuple(sorted(self.structures, key=lambda s: s.upfront_cost))

    def payload_fields(self) -> dict[str, Any]:
        """The table, the exposure, and the trade-off as labelled fields."""
        return {
            "exposure": self.exposure.to_dict(),
            "structures": [s.payload_fields() for s in self.structures],
            "spot_grid": list(self.spot_grid),
            "residual_variance": self.residual_variance,
            "trade_off": dict(TRADE_OFF_FRAME),
        }


def _grid(spot: float, points: int, width: float) -> tuple[float, ...]:
    lowest, highest = spot * (1.0 - width), spot * (1.0 + width)
    step = (highest - lowest) / (points - 1)
    return tuple(lowest + step * k for k in range(points))


def compare_structures(
    exposure: Exposure,
    quote: StructureQuote,
    *,
    otm_offset: float = 0.03,
    spread_offset: float = 0.08,
    collar_offset: float = 0.08,
    hedge_ratio: float = 1.0,
    grid_points: int = 21,
    grid_width: float = 0.20,
    correlation: float | None = None,
    foreign_asset_volatility: float | None = None,
) -> StructureComparison:
    """Price eight hedge structures against one exposure and tabulate them.

    Args:
        exposure: What is being hedged.
        quote: The market to strike on.
        otm_offset: How far out of the money the protective wing sits, as a
            fraction of the forward.
        spread_offset: How far out the sold leg of a spread sits.
        collar_offset: How far out, on the *opposite* side, the collar's
            sold wing sits. A separate zero-cost collar is always reported
            alongside it, with the sold strike solved rather than chosen.
        hedge_ratio: Fraction of the exposure the forward row covers, in
            ``[0, 1]``. AC-4.3.
        grid_points: Points in the spot grid, odd so the forward-ish middle
            is included.
        grid_width: Half-width of the grid as a fraction of spot.
        correlation: Correlation between the foreign asset return and the
            exchange rate, for the residual-variance decomposition. ``None``
            skips it rather than assuming zero.
        foreign_asset_volatility: The asset's own volatility, needed with
            ``correlation``.

    Returns:
        The :class:`StructureComparison`.

    Raises:
        ValueError: ``hedge_ratio`` is outside ``[0, 1]``, an offset is not
            positive, only one of the two variance inputs was given, the
            correlation is outside ``[-1, 1]``, or the asset volatility is
            negative or not finite.
    """
    if not 0.0 <= hedge_ratio <= 1.0:
        raise ValueError(f"a hedge ratio is a fraction in [0, 1]; got {hedge_ratio!r}")
    if otm_offset <= 0.0 or spread_offset <= otm_offset:
        raise ValueError(
            f"the spread's sold leg must sit further out than the protective one; "
            f"got otm_offset={otm_offset!r}, spread_offset={spread_offset!r}"
        )
    if collar_offset <= 0.0:
        raise ValueError(f"collar_offset must be positive, got {collar_offset!r}")
    if (correlation is None) != (foreign_asset_volatility is None):
        raise ValueError(
            "the residual-variance decomposition needs both a correlation and the "
            "foreign asset's volatility, or neither. Supplying one and defaulting "
            "the other would report a number nobody asked for."
        )
    if correlation is not None and not -1.0 <= correlation <= 1.0:
        raise ValueError(f"a correlation is in [-1, 1]; got correlation={correlation!r}")
    if foreign_asset_volatility is not None and not 0.0 <= foreign_asset_volatility < math.inf:
        raise ValueError(
            "foreign_asset_volatility is a volatility as a decimal, zero or more; got "
            f"{foreign_asset_volatility!r}"
        )

    grid = _grid(quote.spot, grid_points, grid_width)
    outright = quote.forward
    protective = exposure.direction.protective_kind
    away = 1.0 + otm_offset if protective is OptionKind.CALL else 1.0 - otm_offset
    far = 1.0 + spread_offset if protective is OptionKind.CALL else 1.0 - spread_offset
    atm_strike = outright
    otm_strike = outright * away
    far_strike = outright * far
    signed = exposure.amount * (
        -1.0 if exposure.direction is ExposureDirection.PAYABLE else 1.0
    )

    rows: list[StructureResult] = [
        _structure("unhedged", exposure, quote, grid, grid_width, hedge_ratio=0.0),
        _structure(
            "forward",
            exposure,
            quote,
            grid,
            grid_width,
            forward_amount=signed * hedge_ratio,
            hedge_ratio=hedge_ratio,
        ),
        _structure(
            "protective_option_atm",
            exposure,
            quote,
            grid,
            grid_width,
            legs=(_Leg(protective, atm_strike, 1.0),),
        ),
        _structure(
            "protective_option_otm",
            exposure,
            quote,
            grid,
            grid_width,
            legs=(_Leg(protective, otm_strike, 1.0),),
        ),
    ]

    # Two collars, because AC-4.2 asks for two different things: one that
    # takes its place in the cost ordering, with a sold wing chosen like the
    # spread's, and one whose sold strike is solved so the net premium is
    # exactly zero. Reporting only the second would leave the ordering with
    # a hole in it; reporting only the first would drop the structure a
    # treasurer most often actually asks for.
    collar_sold = outright * (
        1.0 - collar_offset if protective is OptionKind.CALL else 1.0 + collar_offset
    )
    rows.append(
        _structure(
            "collar",
            exposure,
            quote,
            grid,
            grid_width,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective.opposite, collar_sold, -1.0),
            ),
        )
    )

    sold_strike = zero_cost_collar_strike(quote, otm_strike, protective)
    rows.append(
        _structure(
            "collar_zero_cost",
            exposure,
            quote,
            grid,
            grid_width,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective.opposite, sold_strike, -1.0),
            ),
        )
    )
    rows.append(
        _structure(
            "option_spread",
            exposure,
            quote,
            grid,
            grid_width,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective, far_strike, -1.0),
            ),
        )
    )
    # The seagull's sold wing funds what the spread actually costs — the
    # bought protection less the far leg already sold — not the bought leg
    # on its own. Sizing it to the bought leg alone sells the same premium
    # twice and produces a net credit: a table saying a treasurer is paid to
    # hedge, which is a pricing error rather than a bargain.
    spread_cost = rows[-1].upfront_cost
    seagull_sold = zero_cost_collar_strike(
        quote, otm_strike, protective, net_premium_to_fund=spread_cost
    )
    rows.append(
        _structure(
            "seagull",
            exposure,
            quote,
            grid,
            grid_width,
            legs=(
                _Leg(protective, otm_strike, 1.0),
                _Leg(protective, far_strike, -1.0),
                _Leg(protective.opposite, seagull_sold, -1.0),
            ),
        )
    )

    decomposition: dict[str, float] | None = None
    if correlation is not None and foreign_asset_volatility is not None:
        # sigma^2(RDC) = sigma^2(RFC) + sigma^2(RFX) + 2 rho sigma(RFC) sigma(RFX)
        asset = float(foreign_asset_volatility)
        currency = float(quote.vol_at(outright))
        cross = 2.0 * float(correlation) * asset * currency
        total = asset * asset + currency * currency + cross
        # A hedge ratio h leaves (1 - h) of the currency return in the
        # domestic return, R_FC + (1 - h) R_FX, so the currency variance
        # scales with (1 - h)^2 and the cross term with (1 - h). This
        # subtracted h (sigma^2(RFX) + cross), linear in h: right at h = 0
        # and h = 1, the two ends the tests checked, and 0.011265 against
        # 0.009149 at h = 0.8 on the PRD's market.
        left = 1.0 - hedge_ratio
        hedged = asset * asset + left * left * currency * currency + left * cross
        decomposition = {
            "asset_variance": asset * asset,
            "currency_variance": currency * currency,
            "cross_term": cross,
            "total_variance_unhedged": total,
            "total_volatility_unhedged": math.sqrt(max(total, 0.0)),
            "residual_variance_at_hedge_ratio": hedged,
            "residual_volatility_at_hedge_ratio": math.sqrt(max(hedged, 0.0)),
            "hedge_ratio": hedge_ratio,
            "correlation": float(correlation),
        }

    evidence = Evidence(
        produced_by="hedging_structures.compare_structures",
        fields={
            "structures": [s.name for s in rows],
            "protective_kind": protective.value,
            "atm_strike": atm_strike,
            "otm_strike": otm_strike,
            "far_strike": far_strike,
            "collar_sold_strike": collar_sold,
            "collar_zero_cost_sold_strike": sold_strike,
            "hedge_ratio": hedge_ratio,
            "grid_points": grid_points,
            "grid_width": grid_width,
            "residual_variance": decomposition,
            "trade_off": dict(TRADE_OFF_FRAME),
            **quote.to_dict(),
            **exposure.to_dict(),
        },
        sources=tuple(s.evidence for s in rows),
    )
    return StructureComparison(
        evidence=evidence,
        exposure=exposure,
        structures=tuple(rows),
        spot_grid=grid,
        residual_variance=decomposition,
    )
