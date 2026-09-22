"""Option greeks by bump and full reprice: delta, gamma, vega, theta.

Split from :mod:`rates_engine.risk.sensitivities` because an option's greeks
bump two things -- the curve and the volatility -- and read the price from
:mod:`rates_engine.pricing.options` rather than from a cashflow sum. The rate
bump is the same zero-curve shift every other measure uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from rates_engine.core.evidence import Evidence
from rates_engine.core.results import EngineResult
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.capfloor import CapFloor
from rates_engine.instruments.swaption import Swaption
from rates_engine.pricing.collateral import curve_warnings
from rates_engine.pricing.options import cap_floor_pv, forward_swap_rate, model_for, swaption_pv
from rates_engine.risk.bumps import BUMP_BP, shifted
from rates_engine.volatility.units import Volatility, VolUnits

__all__ = ["GreeksResult", "option_greeks"]


@dataclass(frozen=True)
class GreeksResult(EngineResult):
    """An option's sensitivities, every one of them a bumped reprice.

    Attributes:
        delta: Change in value for a one basis point fall in the curve, per
            basis point — the option's DV01, on the same sign convention as
            :func:`~rates_engine.risk.sensitivities.dv01`.
        gamma: Change in :attr:`delta` per basis point squared.
        vega: Change in value per basis point of *normal* volatility. Reported on the normal basis whatever model
            priced the option, because that is what the market quotes and
            what two desks can compare.
        theta: Change in value for one calendar day passing, per day.
            Negative for a long option, which is the whole of what a long
            option costs to hold.
        value: The unbumped price.
        rate_bump_bp: Curve bump used, in basis points.
        vol_bump_bp: Volatility bump used, in basis points of normal vol.
        currency: What every money figure above is in, taken from the
            discount curve rather than assumed.
    """

    delta: float
    gamma: float
    vega: float
    theta: float
    value: float
    rate_bump_bp: float
    vol_bump_bp: float
    currency: str

    def payload_fields(self) -> dict[str, Any]:
        """Every greek with its unit, and the bumps that produced them."""
        return {
            "value": self.value,
            "delta": self.delta,
            "delta_unit": f"{self.currency}_per_bp",
            "gamma": self.gamma,
            "gamma_unit": f"{self.currency}_per_bp_squared",
            "vega": self.vega,
            "vega_unit": f"{self.currency}_per_bp_normal_vol",
            "theta": self.theta,
            "theta_unit": f"{self.currency}_per_day",
            "rate_bump_bp": self.rate_bump_bp,
            "vol_bump_bp": self.vol_bump_bp,
            "method": "bump_and_reprice",
        }


def option_greeks(
    option: Swaption | CapFloor,
    curve_set: CurveSet,
    volatility: Volatility,
    *,
    rate_bump_bp: float = BUMP_BP,
    vol_bump_bp: float = 1.0,
    as_of: date | None = None,
    source_evidence: tuple[Evidence, ...] = (),
) -> GreeksResult:
    """Delta, gamma, vega and theta of a swaption or a cap, by bumping and repricing.

    No closed-form greeks. The pricers have them and they would be faster,
    but they would also be a second implementation that can disagree with the
    first, and the two would disagree exactly where it is hardest to notice —
    at a boundary, under a shift, in the degenerate case. Bumping the same
    price function that produces the value costs a handful of evaluations and
    cannot drift from it.

    Vega is always reported per basis point of *normal* volatility. When a
    lognormal quote priced the option, the bump is applied to its at-the-money
    normal equivalent and converted back, so two desks quoting different
    conventions still compare vegas.

    Args:
        option: A :class:`~rates_engine.instruments.swaption.Swaption` or
            :class:`~rates_engine.instruments.capfloor.CapFloor`.
        curve_set: Discount and projection curves.
        volatility: The quote used to price it.
        rate_bump_bp: Curve bump in basis points.
        vol_bump_bp: Volatility bump in basis points of normal volatility.
        as_of: Valuation date. Defaults to the curve's.
        source_evidence: Evidence of the curves and the surface.

    Returns:
        The :class:`GreeksResult`.

    Raises:
        ValueError: Either bump is not positive, or ``option`` is neither a
            swaption nor a cap.
    """

    if rate_bump_bp <= 0.0 or vol_bump_bp <= 0.0:
        raise ValueError(
            f"bumps must be positive, got rate {rate_bump_bp!r} and vol {vol_bump_bp!r}"
        )
    if not isinstance(option, (Swaption, CapFloor)):
        raise ValueError(
            f"option greeks are defined for Swaption and CapFloor, got {type(option).__name__}"
        )

    valuation = as_of or curve_set.as_of
    shift = rate_bump_bp * 1e-4

    def at(curves: CurveSet, vol: Volatility, day: date) -> float:
        if isinstance(option, Swaption):
            return swaption_pv(option, curves, vol, as_of=day).value
        return cap_floor_pv(option, curves, vol, as_of=day).value

    base = at(curve_set, volatility, valuation)
    up = at(shifted(curve_set, shift), volatility, valuation)
    down = at(shifted(curve_set, -shift), volatility, valuation)
    delta = (down - up) / 2.0 / rate_bump_bp
    gamma = (up + down - 2.0 * base) / (rate_bump_bp * rate_bump_bp)

    # Vega on the normal basis whatever priced it, so the number is comparable.
    if volatility.is_normal:
        bumped_vol = Volatility.unchecked(
            volatility.as_normal_bp() + vol_bump_bp, VolUnits.NORMAL_BP
        )
    else:
        forward = (
            forward_swap_rate(option, curve_set)
            if isinstance(option, Swaption)
            else option.caplets[-1].forward_rate(curve_set)
        )
        equivalent = volatility.atm_equivalent_normal(forward).as_normal_bp()
        bumped_vol = Volatility.unchecked(
            (equivalent + vol_bump_bp) * 1e-4 / forward, VolUnits.LOGNORMAL_DECIMAL
        )
    vega = (at(curve_set, bumped_vol, valuation) - base) / vol_bump_bp

    theta = at(curve_set, volatility, valuation + timedelta(days=1)) - base

    return GreeksResult(
        evidence=Evidence(
            produced_by="risk.option_greeks",
            fields={
                "instrument": option.describe(),
                "model": model_for(volatility),
                "volatility": volatility.to_dict(),
                "rate_bump_bp": rate_bump_bp,
                "rate_bump_basis": "zero_curve_parallel",
                "vol_bump_bp": vol_bump_bp,
                "vol_bump_basis": "normal_volatility",
                "theta_basis": "one_calendar_day",
                "difference": "central for delta and gamma, forward for vega and theta",
                "method": "bump_and_reprice",
                "method_note": (
                    "No closed-form greeks: a second implementation can disagree with "
                    "the first exactly where it is hardest to notice."
                ),
                "base_value": base,
            },
            sources=source_evidence,
            warnings=curve_warnings(curve_set),
        ),
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        value=base,
        rate_bump_bp=rate_bump_bp,
        vol_bump_bp=vol_bump_bp,
        currency=curve_set.discount.currency.value,
    )
