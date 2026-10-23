"""Vanna-volga: a smile from three or five quotes, reproducing each exactly.

The FX market quotes a smile as three numbers per expiry — the at-the-money
volatility, a risk reversal and a butterfly — and vanna-volga is the
standard way to turn those into a volatility at any strike. It is not a
stochastic model. It is a hedging argument: price at the at-the-money
volatility, then add the market cost of the vega, vanna and volga that the
flat-volatility price fails to hedge, valued with the three instruments the
market actually quotes.

**Why this rather than SABR** (PRD-003 open question 2). Vanna-volga
reproduces the three quoted pillars *exactly*, because at a pillar the
weight vector collapses onto that instrument. SABR fits them, with a
residual. The criterion asks for exact reproduction, so the choice follows
from the criterion rather than from taste.

**Five quotes, piecewise.** With the ten-delta risk reversal and butterfly
as well, there are five pillars and vanna-volga still hedges three greeks,
so it is applied piecewise: the 25-delta triple ``(25P, ATM, 25C)`` between
the two 25-delta strikes — exactly the three-quote smile, unchanged — and
``(10P, 25P, ATM)`` below the 25-delta put and ``(ATM, 25C, 10C)`` above the
25-delta call, out to the ten-delta strikes. Each segment reproduces its
three pillars exactly, so all five reprice, and neighbouring segments meet
at the 25-delta pillar both reproduce, so the smile is continuous there. It
is not smooth there: the slope can jump at the 25-delta strikes, which is
the price of keeping the central smile identical to the three-quote one.
Until the 2026-10-15 review the ten-delta numbers were accepted and
ignored, and the smile read 15.10% at a ten-delta call quoted at 18.00%.

**Which butterfly.** ``SmileQuotes`` takes the *smile* butterfly by
default — ``(sigma(25C) + sigma(25P)) / 2 - sigma(ATM)`` at the smile's own
25-delta strikes — which is what the arithmetic of its wings assumes. A
USD/MXN broker quotes the *market* (broker) butterfly instead: the single
volatility ``sigma(ATM) + BF`` that prices a strangle struck where that
volatility makes each leg 25 delta (Clark 2011; Reiswich and Wystup 2010).
The two differ whenever there is a risk reversal, so reading one as the
other misprices every wing. ``ButterflyConvention.BROKER`` takes the broker
fly and solves for the smile fly that makes this smile reprice the market
strangle, at 25 and, when quoted, at ten delta; the default is unchanged.

**Two conventions, both refused rather than assumed.** A delta does not
name a strike until the convention is stated — that is
:mod:`rates_engine.models.fx_delta`'s problem. "At the money" does not name one
either: the market uses the delta-neutral straddle, the forward, and
occasionally the spot, and they differ by ``exp(±sigma^2 T / 2)``, which at
30% volatility and a year is over four hundred pips. Both have to be given.

**What it is not.** Not arbitrage free. Vanna-volga can produce negative
densities in the wings at high volatility, and this module does not hide
that: :meth:`VannaVolgaSmile.wing_is_reliable` reports it, and the
extrapolation past the outer pillars — the ten-delta ones when quoted, the
25-delta ones otherwise — is flat rather than pretending the construction
still applies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

import numpy as np

from rates_engine.conventions.option_kind import OptionKind
from rates_engine.core.errors import CalibrationError
from rates_engine.core.evidence import DataQuality, Degradation, Evidence
from rates_engine.core.results import EngineResult
from rates_engine.models import garman_kohlhagen as gk
from rates_engine.models.fx_delta import DeltaConvention, strike_from_delta

__all__ = [
    "ATMConvention",
    "ButterflyConvention",
    "VannaVolgaSmile",
    "SmileQuotes",
    "SmileReading",
    "NOT_ARBITRAGE_FREE",
]

NOT_ARBITRAGE_FREE = (
    "Vanna-volga is a hedging argument, not a model with a density behind it. "
    "It reproduces the quoted strikes exactly and interpolates plausibly "
    "between them; far into the wings it can imply a negative density. Readings "
    "outside the quoted delta range are flagged, and past the outer pillars the "
    "volatility is held flat rather than extrapolated."
)


_BROKER_ROUNDS = 50
"""Rounds of solving the 25- and ten-delta smile flies in turn before giving up."""

_BROKER_TOLERANCE = 1e-12
"""Two rounds whose smile flies agree to this, as decimals, have settled."""


class ATMConvention(StrEnum):
    """What "at the money" means, which is three different strikes.

    ``DELTA_NEUTRAL_STRADDLE``
        The strike where a straddle has zero delta:
        ``F exp(+sigma^2 T / 2)`` unadjusted, ``F exp(-sigma^2 T / 2)``
        premium-adjusted. The FX market's usual meaning.
    ``FORWARD``
        The outright forward. Common in emerging-market pairs.
    ``SPOT``
        The spot rate. Rare, and included because a quote sheet that means
        it should not have to be translated by hand.

    No default, for the same reason :class:`~rates_engine.models.fx_delta.
    DeltaConvention` has none: the research gate could not establish which
    one USD/MXN quotes on.
    """

    DELTA_NEUTRAL_STRADDLE = "delta_neutral_straddle"
    FORWARD = "forward"
    SPOT = "spot"


class ButterflyConvention(StrEnum):
    """Which butterfly a quote set holds.

    ``SMILE``
        The smile butterfly, ``(sigma(25C) + sigma(25P)) / 2 - sigma(ATM)``
        with each volatility the smile's own at its own 25-delta strike. The
        wing volatilities follow from it by arithmetic. The default.
    ``BROKER``
        The market, or broker, butterfly that USD/MXN brokers quote:
        ``sigma(ATM) + BF`` is the one volatility that prices a strangle
        whose legs are each 25 delta at that volatility. The smile fly is
        solved for (see :class:`VannaVolgaSmile`), and the wing arithmetic
        is refused because it does not apply.
    """

    SMILE = "smile"
    BROKER = "broker"


@dataclass(frozen=True)
class SmileQuotes:
    """The three or five numbers a market makes a smile from.

    Attributes:
        atm: At-the-money volatility as a decimal.
        risk_reversal_25: ``sigma(25 call) - sigma(25 put)``, as a decimal.
            **Positive for USD/MXN**, where the tail risk is peso weakness
            and the market pays up for dollar calls. Negative would mean it
            pays up for base-currency downside, which is the usual sign for
            a pair whose base is the risk currency — not this one.
        butterfly_25: The 25-delta butterfly as a decimal, in the convention
            :attr:`butterfly_convention` names. By default the **smile**
            butterfly, ``(sigma(25 call) + sigma(25 put)) / 2 - sigma(atm)``
            at the smile's own 25-delta strikes; a broker's screen shows the
            **market** butterfly, which needs ``ButterflyConvention.BROKER``.
        risk_reversal_10: The ten-delta risk reversal, or ``None``.
        butterfly_10: The ten-delta butterfly, or ``None``, in the same
            convention as ``butterfly_25``.
        butterfly_convention: Whether the two butterflies are smile flies
            (the default) or broker flies.
    """

    atm: float
    risk_reversal_25: float
    butterfly_25: float
    risk_reversal_10: float | None = None
    butterfly_10: float | None = None
    butterfly_convention: ButterflyConvention = ButterflyConvention.SMILE

    def wings(self, delta: float) -> tuple[float, float]:
        """Put and call volatilities at a delta, ``atm + fly -+ reversal / 2``.

        Args:
            delta: ``0.25`` or ``0.10``.

        Returns:
            ``(put_volatility, call_volatility)``.

        Raises:
            CalibrationError: The ten-delta pair was asked for and is absent,
                or the butterflies are broker flies, for which this
                arithmetic does not give the smile's volatilities; build the
                smile and read :meth:`VannaVolgaSmile.smile_quotes`.
            ValueError: A delta other than 0.25 or 0.10.
        """
        if self.butterfly_convention is ButterflyConvention.BROKER:
            raise CalibrationError(
                "a broker butterfly does not give the wing volatilities by arithmetic: "
                "atm + fly +- reversal / 2 is the smile's 25-delta volatility only for a "
                "smile fly. Build the VannaVolgaSmile, which solves for the smile fly, "
                "and read its smile_quotes() or pillars()."
            )
        return self._wing_arithmetic(delta)

    def _wing_arithmetic(self, delta: float) -> tuple[float, float]:
        if delta == 0.25:
            reversal, fly = self.risk_reversal_25, self.butterfly_25
        elif delta == 0.10:
            if self.risk_reversal_10 is None or self.butterfly_10 is None:
                raise CalibrationError(
                    "the ten-delta wing was asked for and this quote set has only the "
                    "twenty-five. Supply risk_reversal_10 and butterfly_10, or read the "
                    "smile at a strike instead of at a delta it was not quoted on."
                )
            reversal, fly = self.risk_reversal_10, self.butterfly_10
        else:
            raise ValueError(f"the market quotes wings at 0.25 and 0.10, not {delta!r}")
        return self.atm + fly - 0.5 * reversal, self.atm + fly + 0.5 * reversal

    def to_dict(self) -> dict[str, Any]:
        """Serialise the quotes, absent wings as ``None`` rather than missing."""
        return {
            "atm": self.atm,
            "risk_reversal_25": self.risk_reversal_25,
            "butterfly_25": self.butterfly_25,
            "risk_reversal_10": self.risk_reversal_10,
            "butterfly_10": self.butterfly_10,
            "butterfly_convention": self.butterfly_convention.value,
        }


def _with_fly(quotes: SmileQuotes, name: str, fly: float) -> SmileQuotes:
    """``quotes`` with the butterfly called ``name`` replaced by ``fly``."""
    if name == "butterfly_25":
        return replace(quotes, butterfly_25=fly)
    return replace(quotes, butterfly_10=fly)


@dataclass(frozen=True)
class SmileReading(EngineResult):
    """A volatility read off the smile, and how it was produced.

    Attributes:
        volatility: The implied lognormal volatility as a decimal.
        strike: The strike asked for.
        source: ``"pillar"`` when the strike is one of the quoted ones,
            ``"vanna_volga"`` when it was interpolated, ``"flat_wing"`` when
            it is past the outer pillars and held flat.
        reliable: False when the reading sits outside the quoted delta range,
            where the construction is least trustworthy.
    """

    volatility: float
    strike: float
    source: str
    reliable: bool

    def payload_fields(self) -> dict[str, Any]:
        """The volatility, the strike, and the honesty flags."""
        return {
            "volatility": self.volatility,
            "strike": self.strike,
            "source": self.source,
            "reliable": self.reliable,
            "caveat": NOT_ARBITRAGE_FREE,
        }


@dataclass(frozen=True)
class VannaVolgaSmile:
    """Three or five quotes, their pillar strikes, and a volatility at any strike.

    Attributes:
        spot: Spot rate, quote currency per unit of base.
        expiry: Time to expiry in years.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        quotes: The market's three or five numbers. With five, the ten-delta
            pair extends the smile out to the ten-delta strikes.
        delta_convention: Which of the four delta conventions the wings are
            quoted on. Required.
        atm_convention: What the at-the-money strike means. Required.
    """

    spot: float
    expiry: float
    r_domestic: float
    r_foreign: float
    quotes: SmileQuotes
    delta_convention: DeltaConvention
    atm_convention: ATMConvention
    _cache: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False, init=False
    )
    """Memoised pillars. ``init=False`` matters: as an init field,
    ``dataclasses.replace`` forwarded the *same dict*, so a smile replaced
    with a new spot kept the old one's pillars and mixed them into every
    price. ``replace`` is the package's established way to copy a frozen
    result, so that was a live footgun rather than a theoretical one."""

    def __post_init__(self) -> None:
        if self.expiry <= 0.0:
            raise ValueError(f"a smile needs a positive expiry, got {self.expiry!r}")
        if self.quotes.atm <= 0.0:
            raise ValueError(f"the at-the-money volatility must be positive, got {self.quotes.atm!r}")
        if (self.quotes.risk_reversal_10 is None) != (self.quotes.butterfly_10 is None):
            raise CalibrationError(
                "the ten-delta wing needs both its risk reversal and its butterfly; this "
                "quote set has one of the two. Supply both, or neither for a smile on "
                "the 25-delta quotes alone."
            )
        if self.quotes.butterfly_convention is ButterflyConvention.BROKER:
            for label, fly in (("25", self.quotes.butterfly_25), ("10", self.quotes.butterfly_10)):
                if fly is not None and self.quotes.atm + fly <= 0.0:
                    raise CalibrationError(
                        f"a {label}-delta broker butterfly of {fly} puts the market strangle "
                        "at a non-positive volatility."
                    )
            return
        put_vol, call_vol = self.quotes.wings(0.25)
        if put_vol <= 0.0 or call_vol <= 0.0:
            raise CalibrationError(
                f"the quoted risk reversal and butterfly imply a non-positive wing "
                f"volatility: 25d put {put_vol:.6f}, 25d call {call_vol:.6f}. A butterfly "
                "more negative than half the risk reversal does that, and no smile has it."
            )
        if self.quotes.risk_reversal_10 is not None:
            put_10, call_10 = self.quotes.wings(0.10)
            if put_10 <= 0.0 or call_10 <= 0.0:
                raise CalibrationError(
                    f"the quoted ten-delta risk reversal and butterfly imply a non-positive "
                    f"wing volatility: 10d put {put_10:.6f}, 10d call {call_10:.6f}."
                )

    @property
    def forward(self) -> float:
        """The outright forward implied by the two rates."""
        return gk.forward(self.spot, self.expiry, self.r_domestic, self.r_foreign)

    @property
    def atm_strike(self) -> float:
        """The at-the-money strike, under the stated convention."""
        if self.atm_convention is ATMConvention.SPOT:
            return self.spot
        if self.atm_convention is ATMConvention.FORWARD:
            return self.forward
        drift = 0.5 * self.quotes.atm * self.quotes.atm * self.expiry
        # The delta-neutral straddle strike sits above the forward under an
        # unadjusted delta and below it under a premium-adjusted one, because
        # the adjustment subtracts the premium's own delta from both legs.
        sign = -1.0 if self.delta_convention.is_premium_adjusted else 1.0
        return self.forward * math.exp(sign * drift)

    def smile_quotes(self) -> SmileQuotes:
        """The quotes in the smile-butterfly convention the construction uses.

        The input itself when its butterflies are already smile flies. For
        broker flies, the same quotes with each butterfly replaced by the
        smile fly that makes this smile reprice the market strangle at that
        delta: the strangle struck where ``atm + broker fly`` makes each leg
        that delta, priced at that one volatility, against the same strangle
        priced at the smile's own readings at those strikes. With ten-delta
        quotes the two solves depend on each other — the 25-delta strangle's
        put usually sits in the ten-delta segment — so they are repeated in
        turn until neither fly moves.

        Returns:
            A :class:`SmileQuotes` with ``butterfly_convention`` ``SMILE``.

        Raises:
            CalibrationError: No smile fly within five volatility points of
                the broker one reprices the market strangle, or the two
                solves do not settle.
        """
        cached = self._cache.get("smile_quotes")
        if cached is not None:
            return cached
        result = self.quotes
        if self.quotes.butterfly_convention is ButterflyConvention.BROKER:
            result = self._solve_broker()
        self._cache["smile_quotes"] = result
        return result

    def _solve_broker(self) -> SmileQuotes:
        quotes = self.quotes
        solved = replace(quotes, butterfly_convention=ButterflyConvention.SMILE)
        targets: list[tuple[float, str, float, float]] = [
            (0.25, "butterfly_25", quotes.butterfly_25, quotes.risk_reversal_25)
        ]
        if quotes.butterfly_10 is not None and quotes.risk_reversal_10 is not None:
            targets.append((0.10, "butterfly_10", quotes.butterfly_10, quotes.risk_reversal_10))
        for _ in range(_BROKER_ROUNDS):
            previous = solved
            for delta, name, broker, reversal in targets:
                fly = self._smile_fly(delta, name, broker, reversal, solved)
                solved = _with_fly(solved, name, fly)
            if len(targets) == 1 or all(
                abs(getattr(solved, name) - getattr(previous, name)) <= _BROKER_TOLERANCE
                for _, name, _, _ in targets
            ):
                return solved
        raise CalibrationError(
            f"the 25- and ten-delta smile butterflies did not settle in {_BROKER_ROUNDS} "
            "rounds of solving each against its market strangle; the broker quotes may "
            "not be consistent with one vanna-volga smile."
        )

    def _smile_fly(
        self, delta: float, name: str, broker: float, reversal: float, base: SmileQuotes
    ) -> float:
        """The smile fly at ``delta`` that reprices the market strangle there."""
        from scipy.optimize import brentq

        market_vol = self.quotes.atm + broker
        args = (self.expiry, self.r_domestic, self.r_foreign)
        call_strike = strike_from_delta(
            self.spot, delta, *args, market_vol, OptionKind.CALL, self.delta_convention
        )
        put_strike = strike_from_delta(
            self.spot, delta, *args, market_vol, OptionKind.PUT, self.delta_convention
        )
        target = gk.price(self.spot, call_strike, *args, market_vol, OptionKind.CALL) + gk.price(
            self.spot, put_strike, *args, market_vol, OptionKind.PUT
        )

        def gap(fly: float) -> float:
            trial = VannaVolgaSmile(
                self.spot, self.expiry, self.r_domestic, self.r_foreign,
                _with_fly(base, name, fly), self.delta_convention, self.atm_convention,
            )
            call_vol = trial.volatility_at(call_strike).volatility
            put_vol = trial.volatility_at(put_strike).volatility
            return (
                gk.price(self.spot, call_strike, *args, call_vol, OptionKind.CALL)
                + gk.price(self.spot, put_strike, *args, put_vol, OptionKind.PUT)
                - target
            )

        # Keep both trial wings positive: atm + fly - |reversal| / 2 > 0.
        floor = 0.5 * abs(reversal) - self.quotes.atm + 1e-6
        for width in (0.02, 0.05):
            low, high = max(broker - width, floor), broker + width
            if gap(low) * gap(high) <= 0.0:
                return float(brentq(gap, low, high, xtol=1e-15, maxiter=200))
        raise CalibrationError(
            f"no smile butterfly within five volatility points of the {delta:g}-delta broker "
            f"fly {broker} makes this smile reprice the market strangle at strikes "
            f"{put_strike:.6f} and {call_strike:.6f}; check the quotes and the delta "
            "convention."
        )

    def pillars(self) -> tuple[tuple[float, float], ...]:
        """The three 25-delta and at-the-money points as ``(strike, volatility)``.

        In strike order: the core of the construction, used between the two
        25-delta strikes. The ten-delta points, when quoted, are
        :meth:`wing_pillars`, and :meth:`all_pillars` has all five.
        """
        cached = self._cache.get("pillars")
        if cached is not None:
            return cached
        put_vol, call_vol = self.smile_quotes().wings(0.25)
        put_strike = strike_from_delta(
            self.spot, 0.25, self.expiry, self.r_domestic, self.r_foreign,
            put_vol, OptionKind.PUT, self.delta_convention,
        )
        call_strike = strike_from_delta(
            self.spot, 0.25, self.expiry, self.r_domestic, self.r_foreign,
            call_vol, OptionKind.CALL, self.delta_convention,
        )
        result = tuple(
            sorted(
                ((put_strike, put_vol), (self.atm_strike, self.quotes.atm), (call_strike, call_vol))
            )
        )
        self._cache["pillars"] = result
        return result

    def wing_pillars(self) -> tuple[tuple[float, float], ...]:
        """The two ten-delta points as ``(strike, volatility)``, or ``()``.

        Empty when the quotes have no ten-delta pair. Each strike is the one
        whose delta, under :attr:`delta_convention`, is ten at that wing's own
        volatility, which is how the 25-delta strikes are found too.

        Raises:
            CalibrationError: A ten-delta strike does not lie outside the
                25-delta strike on its side, which no smile does.
        """
        cached = self._cache.get("wing_pillars")
        if cached is not None:
            return cached
        result: tuple[tuple[float, float], ...] = ()
        quotes = self.smile_quotes()
        if quotes.risk_reversal_10 is not None and quotes.butterfly_10 is not None:
            put_vol, call_vol = quotes.wings(0.10)
            put_strike = strike_from_delta(
                self.spot, 0.10, self.expiry, self.r_domestic, self.r_foreign,
                put_vol, OptionKind.PUT, self.delta_convention,
            )
            call_strike = strike_from_delta(
                self.spot, 0.10, self.expiry, self.r_domestic, self.r_foreign,
                call_vol, OptionKind.CALL, self.delta_convention,
            )
            core = self.pillars()
            if not (put_strike < core[0][0] and call_strike > core[-1][0]):
                raise CalibrationError(
                    f"the ten-delta strikes ({put_strike:.6f} put, {call_strike:.6f} call) "
                    f"do not lie outside the 25-delta ones ({core[0][0]:.6f}, "
                    f"{core[-1][0]:.6f}). Ten-delta volatilities that far below the "
                    "25-delta ones are not a smile; check the ten-delta quotes."
                )
            result = ((put_strike, put_vol), (call_strike, call_vol))
        self._cache["wing_pillars"] = result
        return result

    def all_pillars(self) -> tuple[tuple[float, float], ...]:
        """Every quoted point, three or five, as ``(strike, volatility)`` in strike order."""
        wings = self.wing_pillars()
        if not wings:
            return self.pillars()
        return (wings[0], *self.pillars(), wings[-1])

    def _segment(self, strike: float) -> tuple[tuple[float, float], ...]:
        """The three pillars vanna-volga uses at ``strike``.

        The 25-delta triple between the 25-delta strikes, and everywhere when
        there are no ten-delta quotes; below the 25-delta put the triple
        ``(10P, 25P, ATM)``, above the 25-delta call ``(ATM, 25C, 10C)``.
        """
        core = self.pillars()
        wings = self.wing_pillars()
        if not wings or core[0][0] <= strike <= core[-1][0]:
            return core
        if strike < core[0][0]:
            return (wings[0], core[0], core[1])
        return (core[1], core[2], wings[-1])

    def _weights(
        self, strike: float, pillars: tuple[tuple[float, float], ...] | None = None
    ) -> np.ndarray:
        """Weights on three pillars that reproduce the target's greeks.

        Vega, vanna and volga, all evaluated at the at-the-money volatility
        so that the system is the one the hedging argument describes rather
        than a self-referential fit. ``pillars`` defaults to the segment the
        strike falls in, see :meth:`_segment`.
        """
        base = self.quotes.atm
        args = (self.expiry, self.r_domestic, self.r_foreign, base)

        def row(k: float) -> list[float]:
            return [
                gk.vega(self.spot, k, *args),
                gk.vanna(self.spot, k, *args),
                gk.volga(self.spot, k, *args),
            ]

        triple = self._segment(strike) if pillars is None else pillars
        matrix = np.array([row(k) for k, _ in triple], dtype=float).T
        target = np.array(row(strike), dtype=float)
        solution, *_ = np.linalg.lstsq(matrix, target, rcond=None)
        return solution

    def price(self, strike: float, kind: OptionKind = OptionKind.CALL) -> float:
        """The vanna-volga price of a vanilla at ``strike``.

        Vanna-volga on the segment the strike falls in (see the module
        docstring); past the outermost pillar it is the nearest segment's
        formula carried on, which :meth:`volatility_at` does not report.

        Args:
            strike: The strike.
            kind: Call or put. The correction is the same for both, because
                vega, vanna and volga are; parity therefore survives it.

        Returns:
            The premium in quote currency per unit of base.
        """
        base = self.quotes.atm
        flat = gk.price(
            self.spot, strike, self.expiry, self.r_domestic, self.r_foreign, base, kind
        )
        triple = self._segment(strike)
        weights = self._weights(strike, triple)
        correction = 0.0
        for weight, (pillar_strike, pillar_vol) in zip(weights, triple, strict=True):
            market = gk.price(
                self.spot, pillar_strike, self.expiry, self.r_domestic, self.r_foreign,
                pillar_vol, kind,
            )
            flat_pillar = gk.price(
                self.spot, pillar_strike, self.expiry, self.r_domestic, self.r_foreign,
                base, kind,
            )
            correction += weight * (market - flat_pillar)
        return flat + correction

    def _implied(self, strike: float, premium: float, kind: OptionKind) -> float:
        """Invert Garman-Kohlhagen for the volatility, in a maintained bracket.

        The bracket is ``[1e-6, 5.0]``. A premium outside what those two
        volatilities price is refused: the bisection used to converge on
        whichever end it was pushed against and return it as if it were the
        answer, so a premium worth 700% volatility read back as 500%. This
        is the one place the smile refuses a price no volatility implies;
        a vanna-volga price at or below intrinsic is the floor's case.
        """
        low, high = 1e-6, 5.0
        args = (self.expiry, self.r_domestic, self.r_foreign)
        floor = gk.price(self.spot, strike, *args, low, kind)
        ceiling = gk.price(self.spot, strike, *args, high, kind)
        if not floor <= premium <= ceiling:
            raise CalibrationError(
                f"no volatility between {low:g} and {high:.0%} gives the {kind.value} at "
                f"strike {strike} a premium of {premium:.10f}: that range prices it from "
                f"{floor:.10f} to {ceiling:.10f}. The vanna-volga correction has taken the "
                "price where no lognormal volatility reaches; this is the construction "
                f"breaking down, not a solver failure. {NOT_ARBITRAGE_FREE}"
            )
        for _ in range(200):
            mid = 0.5 * (low + high)
            if gk.price(self.spot, strike, *args, mid, kind) < premium:
                low = mid
            else:
                high = mid
        return 0.5 * (low + high)

    def volatility_at(self, strike: float) -> SmileReading:
        """The implied volatility at a strike.

        Args:
            strike: The strike, positive.

        Returns:
            A :class:`SmileReading` saying which of the three ways the
            number was produced and whether it sits inside the quoted range,
            which reaches the ten-delta strikes when they are quoted.

        Raises:
            ValueError: ``strike`` is not positive.
            CalibrationError: No volatility reproduces the vanna-volga price.
        """
        if strike <= 0.0:
            raise ValueError(f"a lognormal smile has no strike at {strike!r}")
        pillars = self.all_pillars()
        lowest, highest = pillars[0][0], pillars[-1][0]

        for pillar_strike, pillar_vol in pillars:
            if math.isclose(strike, pillar_strike, rel_tol=1e-12, abs_tol=0.0):
                return self._reading(pillar_vol, strike, "pillar", True)

        if strike < lowest or strike > highest:
            # Flat rather than extrapolated. The construction has no claim
            # outside the pillars it was built from, and a smile that curves
            # away to a negative density is worse than a flat one.
            edge = pillars[0][1] if strike < lowest else pillars[-1][1]
            return self._reading(edge, strike, "flat_wing", False)

        kind = OptionKind.CALL if strike >= self.atm_strike else OptionKind.PUT
        premium = self.price(strike, kind)
        return self._reading(self._implied(strike, premium, kind), strike, "vanna_volga", True)

    def wing_is_reliable(self, strike: float) -> bool:
        """Whether ``strike`` lies within the outermost quoted pillars."""
        pillars = self.all_pillars()
        return pillars[0][0] <= strike <= pillars[-1][0]

    def _reading(self, volatility: float, strike: float, source: str, reliable: bool) -> SmileReading:
        warnings: tuple[Degradation, ...] = ()
        if not reliable:
            warnings = (
                Degradation(
                    code="outside_quoted_smile",
                    message=(
                        f"strike {strike} is outside the outermost quoted pillars (ten-delta "
                        "when quoted, 25-delta otherwise); the volatility is held flat at "
                        "the nearest one. " + NOT_ARBITRAGE_FREE
                    ),
                    data_quality=DataQuality.ASSUMED,
                ),
            )
        return SmileReading(
            evidence=Evidence(
                produced_by="fx.vannavolga.volatility_at",
                fields={
                    "source": source,
                    "strike": strike,
                    "volatility": volatility,
                    "spot": self.spot,
                    "forward": self.forward,
                    "expiry": self.expiry,
                    "atm_strike": self.atm_strike,
                    "pillars": [
                        {"strike": k, "volatility": v} for k, v in self.pillars()
                    ],
                    "wing_pillars": [
                        {"strike": k, "volatility": v} for k, v in self.wing_pillars()
                    ],
                    "quotes": self.quotes.to_dict(),
                    "smile_butterfly_25": self.smile_quotes().butterfly_25,
                    "smile_butterfly_10": self.smile_quotes().butterfly_10,
                    **self.delta_convention.to_dict(),
                    "atm_convention": self.atm_convention.value,
                    "caveat": NOT_ARBITRAGE_FREE,
                },
                warnings=warnings,
            ),
            volatility=volatility,
            strike=strike,
            source=source,
            reliable=reliable,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the smile with every convention that gives it meaning."""
        return {
            "spot": self.spot,
            "forward": self.forward,
            "expiry": self.expiry,
            "r_domestic": self.r_domestic,
            "r_foreign": self.r_foreign,
            "atm_strike": self.atm_strike,
            "quotes": self.quotes.to_dict(),
            "smile_butterfly_25": self.smile_quotes().butterfly_25,
            "smile_butterfly_10": self.smile_quotes().butterfly_10,
            "pillars": [{"strike": k, "volatility": v} for k, v in self.pillars()],
            "wing_pillars": [{"strike": k, "volatility": v} for k, v in self.wing_pillars()],
            **self.delta_convention.to_dict(),
            "atm_convention": self.atm_convention.value,
            "caveat": NOT_ARBITRAGE_FREE,
        }
