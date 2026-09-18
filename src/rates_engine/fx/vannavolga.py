"""Vanna-volga: a smile from three quotes, reproducing all three exactly.

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

**Two conventions, both refused rather than assumed.** A delta does not
name a strike until the convention is stated — that is
:mod:`rates_engine.fx.delta`'s problem. "At the money" does not name one
either: the market uses the delta-neutral straddle, the forward, and
occasionally the spot, and they differ by ``exp(±sigma^2 T / 2)``, which at
30% volatility and a year is over four hundred pips. Both have to be given.

**What it is not.** Not arbitrage free. Vanna-volga can produce negative
densities in the wings at high volatility, and this module does not hide
that: :meth:`VannaVolgaSmile.wing_is_reliable` reports it, and the
extrapolation past the outer pillars is flat rather than pretending the
construction still applies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from rates_engine.errors import CalibrationError
from rates_engine.evidence import DataQuality, Degradation, Evidence
from rates_engine.fx import garman_kohlhagen as gk
from rates_engine.fx.delta import DeltaConvention, strike_from_delta
from rates_engine.results import EngineResult
from rates_engine.volatility.kinds import OptionKind

__all__ = [
    "ATMConvention",
    "VannaVolgaSmile",
    "SmileQuotes",
    "SmileReading",
    "NOT_ARBITRAGE_FREE",
]

NOT_ARBITRAGE_FREE = (
    "Vanna-volga is a hedging argument, not a model with a density behind it. "
    "It reproduces the three quoted strikes exactly and interpolates plausibly "
    "between them; far into the wings it can imply a negative density. Readings "
    "outside the quoted delta range are flagged, and past the outer pillars the "
    "volatility is held flat rather than extrapolated."
)


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

    No default, for the same reason :class:`~rates_engine.fx.delta.
    DeltaConvention` has none: the research gate could not establish which
    one USD/MXN quotes on.
    """

    DELTA_NEUTRAL_STRADDLE = "delta_neutral_straddle"
    FORWARD = "forward"
    SPOT = "spot"


@dataclass(frozen=True)
class SmileQuotes:
    """The three or five numbers a market makes a smile from.

    Attributes:
        atm: At-the-money volatility as a decimal.
        risk_reversal_25: ``sigma(25 call) - sigma(25 put)``, as a decimal.
            Negative when the market pays up for downside on the base
            currency, which is the usual sign for USD/MXN.
        butterfly_25: ``(sigma(25 call) + sigma(25 put)) / 2 - sigma(atm)``.
        risk_reversal_10: The ten-delta risk reversal, or ``None``.
        butterfly_10: The ten-delta butterfly, or ``None``.
    """

    atm: float
    risk_reversal_25: float
    butterfly_25: float
    risk_reversal_10: float | None = None
    butterfly_10: float | None = None

    def wings(self, delta: float) -> tuple[float, float]:
        """Put and call volatilities at a delta.

        Args:
            delta: ``0.25`` or ``0.10``.

        Returns:
            ``(put_volatility, call_volatility)``.

        Raises:
            CalibrationError: The ten-delta pair was asked for and is absent.
            ValueError: A delta other than 0.25 or 0.10.
        """
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
        }


@dataclass(frozen=True)
class SmileReading(EngineResult):
    """A volatility read off the smile, and how it was produced.

    Attributes:
        volatility: The implied lognormal volatility as a decimal.
        strike: The strike asked for.
        source: ``"pillar"`` when the strike is one of the three quoted,
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
    """Three quotes, three pillar strikes, and a volatility at any strike.

    Attributes:
        spot: Spot rate, quote currency per unit of base.
        expiry: Time to expiry in years.
        r_domestic: Quote currency rate.
        r_foreign: Base currency rate.
        quotes: The market's three or five numbers.
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
    _cache: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.expiry <= 0.0:
            raise ValueError(f"a smile needs a positive expiry, got {self.expiry!r}")
        if self.quotes.atm <= 0.0:
            raise ValueError(f"the at-the-money volatility must be positive, got {self.quotes.atm!r}")
        put_vol, call_vol = self.quotes.wings(0.25)
        if put_vol <= 0.0 or call_vol <= 0.0:
            raise CalibrationError(
                f"the quoted risk reversal and butterfly imply a non-positive wing "
                f"volatility: 25d put {put_vol:.6f}, 25d call {call_vol:.6f}. A butterfly "
                "more negative than half the risk reversal does that, and no smile has it."
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

    def pillars(self) -> tuple[tuple[float, float], ...]:
        """The three quoted points as ``(strike, volatility)``, in strike order."""
        cached = self._cache.get("pillars")
        if cached is not None:
            return cached
        put_vol, call_vol = self.quotes.wings(0.25)
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

    def _weights(self, strike: float) -> np.ndarray:
        """Weights on the three pillars that reproduce the target's greeks.

        Vega, vanna and volga, all evaluated at the at-the-money volatility
        so that the system is the one the hedging argument describes rather
        than a self-referential fit.
        """
        base = self.quotes.atm
        args = (self.expiry, self.r_domestic, self.r_foreign, base)

        def row(k: float) -> list[float]:
            return [
                gk.vega(self.spot, k, *args),
                gk.vanna(self.spot, k, *args),
                gk.volga(self.spot, k, *args),
            ]

        matrix = np.array([row(k) for k, _ in self.pillars()], dtype=float).T
        target = np.array(row(strike), dtype=float)
        solution, *_ = np.linalg.lstsq(matrix, target, rcond=None)
        return solution

    def price(self, strike: float, kind: OptionKind = OptionKind.CALL) -> float:
        """The vanna-volga price of a vanilla at ``strike``.

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
        weights = self._weights(strike)
        correction = 0.0
        for weight, (pillar_strike, pillar_vol) in zip(weights, self.pillars(), strict=True):
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
        """Invert Garman-Kohlhagen for the volatility, in a maintained bracket."""
        low, high = 1e-6, 5.0
        args = (self.expiry, self.r_domestic, self.r_foreign)
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
            number was produced and whether it sits inside the quoted range.

        Raises:
            ValueError: ``strike`` is not positive.
        """
        if strike <= 0.0:
            raise ValueError(f"a lognormal smile has no strike at {strike!r}")
        pillars = self.pillars()
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
        intrinsic = gk.price(
            self.spot, strike, self.expiry, self.r_domestic, self.r_foreign, 1e-9, kind
        )
        if premium <= intrinsic:
            raise CalibrationError(
                f"the vanna-volga correction at strike {strike} takes the price to "
                f"{premium:.10f}, at or below its {intrinsic:.10f} intrinsic, so no "
                "volatility implies it. This is the construction breaking down, not a "
                f"solver failure. {NOT_ARBITRAGE_FREE}"
            )
        return self._reading(self._implied(strike, premium, kind), strike, "vanna_volga", True)

    def wing_is_reliable(self, strike: float) -> bool:
        """Whether ``strike`` lies within the quoted pillars."""
        pillars = self.pillars()
        return pillars[0][0] <= strike <= pillars[-1][0]

    def _reading(self, volatility: float, strike: float, source: str, reliable: bool) -> SmileReading:
        warnings: tuple[Degradation, ...] = ()
        if not reliable:
            warnings = (
                Degradation(
                    code="outside_quoted_smile",
                    message=(
                        f"strike {strike} is outside the quoted 25-delta pillars; the "
                        "volatility is held flat at the nearest one. " + NOT_ARBITRAGE_FREE
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
                    "quotes": self.quotes.to_dict(),
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
            "pillars": [{"strike": k, "volatility": v} for k, v in self.pillars()],
            **self.delta_convention.to_dict(),
            "atm_convention": self.atm_convention.value,
            "caveat": NOT_ARBITRAGE_FREE,
        }
