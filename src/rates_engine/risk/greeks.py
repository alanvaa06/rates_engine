"""Option greeks by bump and full reprice: delta, gamma, vega, theta.

Split from :mod:`rates_engine.risk.sensitivities` because an option's greeks
bump two things -- the curve and the volatility -- and read the price from
:mod:`rates_engine.pricing.options` rather than from a cashflow sum. The rate
bump is the same zero-curve shift every other measure uses.

**Vega is per basis point of normal volatility, exactly.** A lognormal quote
is bumped through the normal volatility that reproduces its price: the price
is inverted for its Bachelier volatility, that is raised by the bump, and the
bumped price is inverted back to the lognormal volatility that is repriced.
For a cap this is done caplet by caplet, because one flat lognormal quote is
a different normal volatility on every forward. The shortcut it replaces,
``sigma_N = sigma_LN * F``, is the at-the-money leading term: it gave a
10y10y at 30% lognormal 0.893 of the vega of the same price quoted normal
(0.775 at 45%); and taken through the last caplet's forward for the whole
strip, it gave a ten-year cap at 25% 3,820.56 per bp where its caplets,
each converted exactly, add up to 5,959.07. Even caplet by caplet the
leading term is 8% short there (5,490.35).

**Theta is the decay of optionality and nothing else.** Every option's time
to expiry is one calendar day shorter; the curves are *not* rolled, so the
carry of the numeraire -- roughly ``r * V`` a day, 76 against a decay of 95
on a 10y10y at 100 bp normal on a flat 4% curve -- is not in it, and no
period is re-classified, so a fixing, a payment or an expiry that falls
tomorrow neither drops out of the price nor asks for a fixing that does not
exist yet. That is a choice, stated because the other one is as common: a
desk theta that rolls the curve forward a day answers a different question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from rates_engine.conventions.option_kind import OptionKind
from rates_engine.core.evidence import Evidence
from rates_engine.core.results import EngineResult
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.capfloor import CapFloor
from rates_engine.instruments.swaption import Swaption
from rates_engine.market.snapshot import MarketSnapshot
from rates_engine.models import bachelier, black
from rates_engine.pricing.collateral import valuation_warnings
from rates_engine.pricing.options import (
    PeriodStatus,
    cap_floor_periods,
    cap_floor_pv,
    forward_swap_rate,
    model_for,
    option_value,
    swaption_annuity,
    swaption_pv,
)
from rates_engine.risk.bumps import BUMP_BP, shifted
from rates_engine.volatility.units import Volatility, VolUnits

__all__ = ["GreeksResult", "option_greeks"]


def _lognormal_after_normal_bump(
    sigma: float,
    forward: float,
    strike: float,
    expiry: float,
    kind: OptionKind,
    bump_bp: float,
) -> float:
    """The lognormal volatility whose price is this one's after a normal-vol bump.

    Exact rather than the at-the-money shortcut: the Black price is inverted
    for the Bachelier volatility that reproduces it, that volatility is raised
    by ``bump_bp`` basis points, and the bumped Bachelier price is inverted
    back to a Black volatility. Repricing with the answer is therefore the
    price at the normal volatility plus one bump, to the inversions'
    tolerance, which is what "vega per basis point of normal vol" means.
    """
    if strike == 0.0:
        # The price is the forward, or nothing, whatever the volatility.
        return sigma
    unit = black.price(forward, strike, expiry, sigma, kind=kind)
    normal = bachelier.implied_normal_vol(unit, forward, strike, expiry, kind=kind)
    target = bachelier.price(forward, strike, expiry, normal + bump_bp * 1e-4, kind=kind)
    ceiling = forward if kind is OptionKind.CALL else strike
    if target >= ceiling:
        raise ValueError(
            f"a {bump_bp} bp rise in the normal volatility takes this option to "
            f"{target:.6g} per unit of numeraire, which no lognormal volatility reaches: "
            f"Black's prices stop short of {ceiling:.6g}. The quote is too extreme for a "
            "normal-vol vega; bump less, or quote the option in normal volatility."
        )
    return black.implied_lognormal_vol(target, forward, strike, expiry, kind=kind)


def _decayed_one_day(
    option: Swaption | CapFloor,
    curve_set: CurveSet,
    volatility: Volatility,
    valuation: date,
    fixings: MarketSnapshot | None,
) -> float:
    """The price with every expiry measured from tomorrow and nothing else moved.

    Away from any event this is exactly the price at ``as_of`` tomorrow, the
    same arithmetic in the same order. It differs where tomorrow is an event:
    a swaption expiring then decays to its intrinsic value rather than being
    refused as expired, and a caplet fixing then decays to its intrinsic value
    on the forward rather than asking for a fixing that is not published yet.
    """
    tomorrow = valuation + timedelta(days=1)
    if isinstance(option, Swaption):
        return option_value(
            volatility,
            forward_swap_rate(option, curve_set),
            option.strike,
            option.time_to_expiry(tomorrow),
            option.kind,
            swaption_annuity(option, curve_set),
        )
    values = []
    for period in cap_floor_periods(
        option, curve_set, volatility, as_of=valuation, fixings=fixings
    ):
        if period.status is not PeriodStatus.LIVE or period.forward is None:
            values.append(period.value)
            continue
        caplet = period.caplet
        values.append(
            option_value(
                volatility,
                period.forward,
                caplet.strike,
                caplet.time_to_expiry(tomorrow),
                caplet.kind,
                period.numeraire,
            )
        )
    return sum(values)


def _caplet_by_caplet(
    cap: CapFloor,
    curve_set: CurveSet,
    volatility: Volatility,
    valuation: date,
    fixings: MarketSnapshot | None,
    bump_bp: float,
) -> float:
    """A lognormal-quoted strip repriced with each live caplet's normal vol bumped."""
    sigma = volatility.as_lognormal_decimal()
    values = []
    for period in cap_floor_periods(
        cap, curve_set, volatility, as_of=valuation, fixings=fixings
    ):
        if period.status is not PeriodStatus.LIVE or period.forward is None:
            values.append(period.value)
            continue
        caplet = period.caplet
        bumped = _lognormal_after_normal_bump(
            sigma, period.forward, caplet.strike, period.expiry, caplet.kind, bump_bp
        )
        values.append(
            option_value(
                Volatility.unchecked(bumped, VolUnits.LOGNORMAL_DECIMAL),
                period.forward,
                caplet.strike,
                period.expiry,
                caplet.kind,
                period.numeraire,
            )
        )
    return sum(values)


@dataclass(frozen=True)
class GreeksResult(EngineResult):
    """An option's sensitivities, every one of them a bumped reprice.

    Attributes:
        delta: Change in value for a one basis point fall in the curve, per
            basis point — the option's DV01, on the same sign convention as
            :func:`~rates_engine.risk.sensitivities.dv01`.
        gamma: Change in :attr:`delta` per basis point squared.
        vega: Change in value per basis point of *normal* volatility,
            reported on that basis whatever model priced the option, because
            that is what the market quotes and what two desks can compare. A
            lognormal quote is converted exactly -- through the normal
            volatility that reproduces its price, caplet by caplet for a cap
            -- so the same price quoted either way has the same vega.
        theta: Change in value when every option's time to expiry is one
            calendar day shorter, per day. Only the decay of volatility time
            value: the curves are not rolled, so the numeraire's carry is
            not in it, and nothing that happens tomorrow -- a fixing, a
            payment, the expiry itself -- is re-classified. Negative for a
            long option.
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
    fixings: MarketSnapshot | None = None,
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
    lognormal quote priced the option, the bump is applied to the normal
    volatility that reproduces its price -- for a cap, each caplet's own --
    and converted back exactly, so the same price quoted in either
    convention has the same vega. A fixed caplet has no vega.

    Theta shortens every expiry by one calendar day and changes nothing else:
    it is the decay of volatility time value only, with the curves not rolled
    and every period's status -- paid, fixed, live -- held at the valuation
    date. See the module docstring for why.

    Args:
        option: A :class:`~rates_engine.instruments.swaption.Swaption` or
            :class:`~rates_engine.instruments.capfloor.CapFloor`.
        curve_set: Discount and projection curves.
        volatility: The quote used to price it.
        rate_bump_bp: Curve bump in basis points.
        vol_bump_bp: Volatility bump in basis points of normal volatility.
        as_of: Valuation date. Defaults to the curve's.
        fixings: Published fixings for a cap or floor with periods that have
            already fixed, as :func:`~rates_engine.pricing.options.cap_floor_pv`
            takes them. Ignored for a swaption.
        source_evidence: Evidence of the curves and the surface.

    Returns:
        The :class:`GreeksResult`.

    Raises:
        ValueError: Either bump is not positive, ``option`` is neither a
            swaption nor a cap, a swaption has expired, or every period of a
            cap has been paid.
        MissingFixingError: A period of a cap has fixed and ``fixings`` does
            not hold its fixing.
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

    def at(curves: CurveSet, vol: Volatility) -> float:
        if isinstance(option, Swaption):
            return swaption_pv(option, curves, vol, as_of=valuation).value
        return cap_floor_pv(option, curves, vol, as_of=valuation, fixings=fixings).value

    base = at(curve_set, volatility)
    up = at(shifted(curve_set, shift), volatility)
    down = at(shifted(curve_set, -shift), volatility)
    delta = (down - up) / 2.0 / rate_bump_bp
    gamma = (up + down - 2.0 * base) / (rate_bump_bp * rate_bump_bp)

    # Vega on the normal basis whatever priced it, so the number is comparable.
    if volatility.is_normal:
        bumped_vol = Volatility.unchecked(
            volatility.as_normal_bp() + vol_bump_bp, VolUnits.NORMAL_BP
        )
        bumped = at(curve_set, bumped_vol)
    elif isinstance(option, Swaption):
        sigma = _lognormal_after_normal_bump(
            volatility.as_lognormal_decimal(),
            forward_swap_rate(option, curve_set),
            option.strike,
            option.time_to_expiry(valuation),
            option.kind,
            vol_bump_bp,
        )
        bumped = at(curve_set, Volatility.unchecked(sigma, VolUnits.LOGNORMAL_DECIMAL))
    else:
        bumped = _caplet_by_caplet(option, curve_set, volatility, valuation, fixings, vol_bump_bp)
    vega = (bumped - base) / vol_bump_bp

    theta = _decayed_one_day(option, curve_set, volatility, valuation, fixings) - base

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
                "vol_bump_conversion": (
                    "none: the quote is normal"
                    if volatility.is_normal
                    else "exact: through the normal volatility reproducing each "
                    "option's price, caplet by caplet for a cap"
                ),
                "theta_basis": "one_calendar_day",
                "theta_note": (
                    "Volatility time decay only: every expiry one day shorter, the "
                    "curves not rolled, no period re-classified."
                ),
                "difference": "central for delta and gamma, forward for vega and theta",
                "method": "bump_and_reprice",
                "method_note": (
                    "No closed-form greeks: a second implementation can disagree with "
                    "the first exactly where it is hardest to notice."
                ),
                "base_value": base,
            },
            sources=source_evidence,
            warnings=valuation_warnings(option, curve_set),
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
