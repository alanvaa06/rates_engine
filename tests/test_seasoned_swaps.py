"""Swaps and FRAs that started before the valuation date, and the ones that did not.

A trade struck last year is the ordinary case in a book, not an edge one. Its
coupons already paid are worth nothing today, the fixed coupon now accruing
is known in full, and the floating coupon now accruing is part history and
part forecast: the history is in published fixings, which no curve knows.
Until this file existed the pricer summed every flow -- paid ones included,
discounted at one because ``DiscountCurve.df`` is one on or before ``as_of``
-- and projected the period in progress as ``df(start) / df(end)``, which on
that same curve leaves the accrued part worth nothing. The answer came back
``observed`` and without a warning.

The references are independent of the code: QuantLib 1.44 numbers pasted as
constants with the script that produced them, closed forms on flat curves,
and, for the trades with no history on the valuation date, the outputs of
``main`` @ 3197ed6 before this change, which must not move by a bit.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import date, timedelta

import pytest

from rates_engine.conventions.calendar import BMV, SIFMA_US, HolidayCalendar
from rates_engine.conventions.daycount import DayCount
from rates_engine.conventions.indices import (
    TERM_SOFR_6M,
    TIIE_28,
    TIIE_FONDEO,
    IndexTenor,
    RateIndex,
)
from rates_engine.conventions.schedule import add_months
from rates_engine.conventions.side import Side
from rates_engine.core.errors import (
    CurrencyMismatchError,
    MissingFixingError,
    UnsupportedConventionError,
)
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.core.money import Currency
from rates_engine.curves.bootstrap import bootstrap_discount_curve
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.fra import FRA
from rates_engine.instruments.swaps import IRSwap, OISSwap
from rates_engine.market.snapshot import MarketSnapshot, Series
from rates_engine.pricing.calibration import SwapQuoteNode
from rates_engine.pricing.linear import annuity, discounted_value, par_rate, par_rate_value, pv
from rates_engine.pricing.projection import fra_fair_rate, is_paid

AS_OF = date(2026, 1, 15)

# ---------------------------------------------------------------------------
# Trades with no history on the valuation date: unchanged to the bit.
# ---------------------------------------------------------------------------

#: Zero rates, continuously compounded on ACT/365F, at 91 days and at each
#: whole year to ten. Hand-built rather than bootstrapped so that this pin
#: exercises the pricer and nothing that builds curves.
_ZEROS = (0.0402, 0.0400, 0.0385, 0.0372, 0.0366, 0.0363, 0.0365, 0.0370, 0.0376, 0.0380, 0.0383)


def _pinned_curve(spread: float = 0.0, currency: Currency = Currency.USD) -> DiscountCurve:
    days = (91,) + tuple(365 * k for k in range(1, 11))
    nodes = tuple(AS_OF + timedelta(days=d) for d in days)
    dfs = tuple(math.exp(-(z + spread) * d / 365.0) for z, d in zip(_ZEROS, days, strict=True))
    return DiscountCurve(AS_OF, nodes, dfs, currency=currency)


def _pinned_cases() -> dict[str, tuple[object, CurveSet]]:
    single = CurveSet(_pinned_curve())
    dual = CurveSet(_pinned_curve(), _pinned_curve(0.0015))
    peso = CurveSet(_pinned_curve(0.035, Currency.MXN))
    return {
        "ois_spot_payer": (
            OISSwap(effective=AS_OF, maturity=date(2031, 1, 15), fixed_rate=0.038,
                    notional=100_000_000.0),
            single,
        ),
        "ois_fwd_receiver": (
            OISSwap(effective=date(2026, 3, 18), maturity=date(2029, 3, 19), fixed_rate=0.036,
                    notional=50_000_000.0, side=Side.RECEIVER),
            single,
        ),
        "ois_quarterly_lag0": (
            OISSwap(effective=date(2026, 2, 17), maturity=date(2028, 2, 17), fixed_rate=0.039,
                    notional=10_000_000.0, frequency_months=3, payment_lag_days=0),
            dual,
        ),
        # Term SOFR fixes two SIFMA days before its period starts. A period
        # starting on or before 2026-01-16 fixed before as_of and is history,
        # tested below with its fixings; one starting at spot (the 20th, over
        # Martin Luther King Day) fixed on as_of itself and, with no fixing
        # supplied, is forecast exactly as before; from the 21st on it is
        # all forecast.
        "irs_after_spot_payer": (
            IRSwap(effective=date(2026, 1, 21), maturity=date(2030, 1, 21), fixed_rate=0.037,
                   notional=100_000_000.0),
            single,
        ),
        "irs_fwd_receiver_dual": (
            IRSwap(effective=date(2026, 4, 15), maturity=date(2033, 4, 15), fixed_rate=0.039,
                   notional=25_000_000.0, side=Side.RECEIVER),
            dual,
        ),
        "irs_6m_dual": (
            IRSwap(effective=date(2026, 7, 15), maturity=date(2031, 7, 15), fixed_rate=0.0375,
                   notional=40_000_000.0, index=TERM_SOFR_6M),
            dual,
        ),
        "ois_tiie_fondeo": (
            OISSwap(effective=AS_OF, maturity=date(2028, 1, 14), fixed_rate=0.075,
                    notional=500_000_000.0, frequency_months=3, index=TIIE_FONDEO),
            peso,
        ),
        "irs_spot_fixed_today_payer": (
            IRSwap(effective=date(2026, 1, 20), maturity=date(2030, 1, 20), fixed_rate=0.037,
                   notional=100_000_000.0),
            dual,
        ),
        # TIIE 28 fixes on its start, so this one fixed on as_of too.
        "fra_tiie28_fixed_today": (
            FRA(start=AS_OF, end=date(2026, 2, 12), rate=0.075, notional=1_000_000_000.0,
                index=TIIE_28),
            peso,
        ),
        "fra_fwd_payer_dual": (
            FRA(start=date(2026, 4, 15), end=date(2026, 7, 15), rate=0.037,
                notional=100_000_000.0),
            dual,
        ),
        "fra_after_spot_receiver": (
            FRA(start=date(2026, 1, 21), end=date(2026, 4, 21), rate=0.04, notional=100_000_000.0,
                side=Side.RECEIVER),
            single,
        ),
    }


#: What ``main`` @ 3197ed6 returned for :func:`_pinned_cases`, before this
#: branch, printed with ``repr`` so equality below is exact. Produced by
#: running the same constructions against that tree; ``flows`` is
#: ``(amount, rate)`` per cashflow in the order ``pv`` returns them.
PINNED: dict[str, dict[str, object]] = {
    "ois_spot_payer": {
        "pv": -657881.1908643087,
        "discounted_value": -657881.1908643087,
        "flows": (
            (-3852777.7777777775, 0.038),
            (-3884444.444444444, 0.038),
            (-3842222.222222222, 0.038),
            (-3842222.222222222, 0.038),
            (-3852777.7777777775, 0.038),
            (4081077.419238821, 0.04025172249112262),
            (3798816.56404919, 0.03716233595265512),
            (3510855.0957391625, 0.0347227427051126),
            (3531474.2427160707, 0.034926668334554545),
            (3573008.620640361, 0.03524063297069945),
        ),
        "par": 0.03655160873900526,
        "annuity": 4.542151064985605,
    },
    "ois_fwd_receiver": {
        "pv": -157895.83997027553,
        "discounted_value": -157895.83997027553,
        "flows": (
            (4999.999999999999, 0.036),
            (1824999.9999999998, 0.036),
            (1834999.9999999995, 0.036),
            (1819999.9999999998, 0.036),
            (-5507.152580097596, 0.03965149857670269),
            (-2011803.1594995034, 0.039684884242181986),
            (-1872996.073376798, 0.03674542705262383),
            (-1757185.8443160672, 0.03475752219526287),
        ),
        "par": 0.037125062287456274,
        "annuity": 2.806881747450121,
    },
    "ois_quarterly_lag0": {
        "pv": -20590.11752378769,
        "discounted_value": -20590.11752378769,
        "flows": (
            (-97500.0, 0.039),
            (-98583.33333333333, 0.039),
            (-99666.66666666666, 0.039),
            (-99666.66666666666, 0.039),
            (-96416.66666666667, 0.039),
            (-99666.66666666666, 0.039),
            (-99666.66666666666, 0.039),
            (-99666.66666666666, 0.039),
            (99380.28553514445, 0.03975211421405778),
            (100057.68674492321, 0.03958326069029929),
            (101162.76480569787, 0.03958542970657743),
            (98484.01414311647, 0.03853722292556732),
            (90627.37975069623, 0.036658265966573754),
            (93696.50294756227, 0.03666384897948089),
            (93696.50294756448, 0.03666384897948176),
            (91506.54668008462, 0.0358069095704679),
        ),
        "par": 0.03793546117924284,
        "annuity": 1.934181931396609,
    },
    "irs_after_spot_payer": {
        "pv": -63409.0715955965,
        "discounted_value": -63409.0715955965,
        "flows": (
            (-3751388.8888888885, 0.037),
            (-3751388.8888888885, 0.037),
            (-3771944.444444444, 0.037),
            (-3751388.8888888885, 0.037),
            (995793.263780298, 0.03983173055121192),
            (1000576.867449232, 0.03958326069029929),
            (1011627.6480569786, 0.03958542970657743),
            (1006756.6638370495, 0.039394825976232374),
            (916503.1711253712, 0.03666012684501485),
            (926733.5817465527, 0.036661987849314175),
            (936965.0294756227, 0.03666384897948089),
            (932982.9373518672, 0.03650802798333394),
            (866361.512358016, 0.0342736422471303),
            (866361.512358016, 0.0342736422471303),
            (895050.3266151877, 0.03427852314696463),
            (866803.6672406122, 0.034291134088639603),
            (871391.138518951, 0.03447261646888158),
            (871391.138518951, 0.03447261646888158),
            (871391.138518951, 0.03447261646888158),
            (881672.2535212706, 0.034500218616049724),
        ),
        "par": 0.03682853102652919,
        "annuity": 3.697990972483206,
    },
    "irs_fwd_receiver_dual": {
        "pv": 42758.46179660078,
        "discounted_value": 42758.46179660078,
        "flows": (
            (988541.6666666666, 0.039),
            (996666.6666666666, 0.039),
            (985833.3333333333, 0.039),
            (985833.3333333333, 0.039),
            (988541.6666666666, 0.039),
            (991250.0, 0.039),
            (996666.6666666666, 0.039),
            (-259607.2825834572, 0.041080712848371254),
            (-262456.3915386957, 0.04108013084953498),
            (-262456.3915386957, 0.04108013084953498),
            (-238458.83914797584, 0.03815341426367613),
            (-241121.1183624473, 0.03815542971889276),
            (-243783.6784074776, 0.03815744531595302),
            (-251274.93156821834, 0.038087989627182565),
            (-223527.62167203167, 0.035764419467525066),
            (-226022.45558477342, 0.035766190773854255),
            (-226022.45558477897, 0.035766190773855136),
            (-228545.18396465128, 0.035772289664032376),
            (-224771.55233634473, 0.035963448373815154),
            (-227280.3324485484, 0.03596523942042964),
            (-227280.3324485484, 0.03596523942042964),
            (-229810.09889819214, 0.03597027634928225),
            (-226637.563357579, 0.03626201013721264),
            (-229167.2653509824, 0.03626383100059502),
            (-231697.2210203716, 0.036265651985797294),
            (-231863.12861604439, 0.03629162013120695),
            (-241570.62178203903, 0.038651299485126245),
            (-244267.81040804423, 0.03865336780083337),
            (-246965.28724220124, 0.03865543626399672),
            (-247138.21225418322, 0.03868250278761129),
            (-260007.15586401155, 0.04114398949936007),
            (-260007.15586401155, 0.04114398949936007),
            (-262879.3473539115, 0.04114633262930789),
            (-272121.0219759629, 0.04124781806793543),
            (-268347.79520141793, 0.04293564723222687),
        ),
        "par": 0.0387192657244781,
        "annuity": 6.092374964490705,
    },
    "irs_6m_dual": {
        "pv": 46712.54915974382,
        "discounted_value": 46712.54915974382,
        "flows": (
            (-1520833.3333333333, 0.0375),
            (-1533333.3333333333, 0.0375),
            (-1516666.6666666665, 0.0375),
            (-1516666.6666666665, 0.0375),
            (-1520833.3333333333, 0.0375),
            (844268.9878012377, 0.04129576570766924),
            (771007.7695833207, 0.038337402907457935),
            (796014.206496114, 0.038310844162914574),
            (722513.5483751366, 0.035926088040752646),
            (730614.229275357, 0.035931847341411),
            (726552.5294567875, 0.03612692135420491),
            (734687.4943581572, 0.03613217185367986),
            (732611.7522134101, 0.03642820867359498),
            (745134.770141931, 0.03644680940911619),
            (781117.998821994, 0.03884012148838645),
        ),
        "par": 0.037761641303583136,
        "annuity": 4.463415038071334,
    },
    "ois_tiie_fondeo": {
        "pv": -1658157.1935208756,
        "discounted_value": -1658157.1935208756,
        "flows": (
            (-9270833.333333334, 0.075),
            (-9479166.666666666, 0.075),
            (-9583333.333333332, 0.075),
            (-9583333.333333332, 0.075),
            (-9375000.0, 0.075),
            (-9479166.666666666, 0.075),
            (-9583333.333333332, 0.075),
            (-9583333.333333332, 0.075),
            (9252791.548832407, 0.0748540439905543),
            (9429579.436624035, 0.07460766147658578),
            (9533430.931798814, 0.0746094594662516),
            (9533430.931798704, 0.07460945946625072),
            (8960067.322776211, 0.07168053858220969),
            (9056383.425464775, 0.07165490182785317),
            (9156809.93188117, 0.07166199077124395),
            (9156809.931881282, 0.07166199077124483),
        ),
        "par": 0.07322057983478619,
        "annuity": 1.863705071951486,
    },
    "irs_spot_fixed_today_payer": {
        "pv": 498055.7085923933,
        "discounted_value": 498055.7085923933,
        "flows": (
            (-3751388.8888888885, 0.037),
            (-3751388.8888888885, 0.037),
            (-3782222.222222222, 0.037),
            (-3751388.8888888885, 0.037),
            (1033228.5279987418, 0.041329141119949675),
            (1038355.3796825318, 0.04107779524018807),
            (1049825.566154783, 0.04108013084953498),
            (1045764.8613361849, 0.04092123370445941),
            (953835.3565919034, 0.03815341426367613),
            (964484.4734497892, 0.03815542971889276),
            (975134.7136299104, 0.03815744531595302),
            (971815.0377439061, 0.03802754495519633),
            (904089.8223391159, 0.035766190773855136),
            (904089.8223391159, 0.035766190773855136),
            (914070.1450361942, 0.03576796219706847),
            (934476.203487944, 0.035788450346346784),
            (879018.9622396749, 0.03595986663707761),
            (909121.3297941936, 0.03596523942042964),
            (939232.6799155581, 0.0359706132733618),
            (919821.0285091824, 0.03599299676775062),
        ),
        "par": 0.038345856015150705,
        "annuity": 3.7006611627516364,
    },
    "fra_tiie28_fixed_today": {
        "pv": -47619.33191668769,
        "discounted_value": -47619.33191668769,
        "flows": (
            (-47894.830633031364, 0.07438420932043245),
        ),
        "fair": 0.07438420932043245,
    },
    "fra_fwd_payer_dual": {
        "pv": 101122.05482570204,
        "discounted_value": 101122.05482570204,
        "flows": (
            (103151.35255605118, 0.041080712848371254),
        ),
        "fair": 0.041080712848371254,
    },
    "fra_after_spot_receiver": {
        "pv": 4162.507267539394,
        "discounted_value": 4162.507267539394,
        "flows": (
            (4206.736219702044, 0.03983173055121192),
        ),
        "fair": 0.03983173055121192,
    },
}


def _measure(instrument, curve_set, **fixings) -> dict[str, object]:
    result = pv(instrument, curve_set, **fixings)
    measured: dict[str, object] = {
        "pv": result.value,
        "discounted_value": discounted_value(instrument, curve_set, **fixings),
        "flows": tuple((flow.amount, flow.rate) for flow in result.cashflows or ()),
    }
    if isinstance(instrument, FRA):
        measured["fair"] = fra_fair_rate(instrument, curve_set, **fixings)
    else:
        measured["par"] = par_rate(instrument, curve_set, **fixings).value
        measured["annuity"] = annuity(instrument, curve_set).value
    return measured


class TestTradesWithNoHistoryDoNotMove:
    """Nothing here may change a trade with no history, not even in the last bit."""

    @pytest.mark.parametrize("name", sorted(PINNED))
    def test_every_measure_is_bit_identical_to_main(self, name):
        instrument, curve_set = _pinned_cases()[name]
        assert _measure(instrument, curve_set) == PINNED[name]

    @pytest.mark.parametrize("name", sorted(PINNED))
    def test_handing_them_fixings_changes_nothing(self, name):
        # A book is priced with one snapshot for every trade; the ones with
        # no history must not read it, and the two fixed today find their
        # index's series without that day's print and forecast as before.
        # The prints of the 14th are decoys: no period here fixed on them.
        instrument, curve_set = _pinned_cases()[name]
        snapshot = _fixings(
            _history(),
            _history("TIIE_FONDEO", 0.07, calendar=BMV),
            _term("TERM_SOFR_3M", {date(2026, 1, 14): 0.99}),
            _term("TIIE_28", {date(2026, 1, 14): 0.99}),
        )
        assert _measure(instrument, curve_set, fixings=snapshot) == PINNED[name]

    def test_the_battery_covers_both_starts_and_every_linear_product(self):
        # A pin that only held swaps struck later than today would miss the
        # boundary the change sits on: an overnight period starting exactly
        # on as_of has no history and must still be projected, not refused.
        cases = _pinned_cases()
        starts = {instrument.span[0] for instrument, _ in cases.values()}
        assert AS_OF in starts and any(start > AS_OF for start in starts)
        kinds = {type(instrument) for instrument, _ in cases.values()}
        assert kinds == {OISSwap, IRSwap, FRA}
        # And the term boundary: a period that fixed on as_of itself, which
        # is forecast without a snapshot exactly as main forecast it.
        assert {"irs_spot_fixed_today_payer", "fra_tiie28_fixed_today"} <= set(cases)


# ---------------------------------------------------------------------------
# Flows already paid are worth nothing today.
# ---------------------------------------------------------------------------

NOTIONAL = 100_000_000.0
FLAT = 0.04


def _flat_curve(rate: float = FLAT, currency: Currency = Currency.USD) -> DiscountCurve:
    """Flat continuous ``rate`` on ACT/365F, nodes every 365 days for ten years.

    Log-linear on a flat curve is exact everywhere, including before the
    first node, so ``df(d) == exp(-rate * days / 365)`` for every ``d`` after
    ``AS_OF`` and the expected values below are written with :func:`_df`
    rather than by asking the curve.
    """
    nodes = tuple(AS_OF + timedelta(days=365 * k) for k in range(1, 11))
    dfs = tuple(math.exp(-rate * k) for k in range(1, 11))
    return DiscountCurve(AS_OF, nodes, dfs, currency=currency)


def _df(day: date, rate: float = FLAT) -> float:
    return math.exp(-rate * (day - AS_OF).days / 365.0)


class TestFlowsAlreadyPaidAreWorthNothing:
    """A flow paid on or before ``as_of`` has left the trade; it is not discounted at one."""

    def test_a_coupon_paid_on_the_valuation_date_is_not_in_the_price(self):
        # Annual OIS with no payment lag: the first period ends and is paid
        # on as_of itself, the second starts there, so nothing is in
        # progress and no fixing is needed. Before this change the
        # 4,055,556 USD fixed coupon paid today entered the price at a
        # discount factor of one.
        swap = OISSwap(effective=date(2025, 1, 15), maturity=date(2027, 1, 15), fixed_rate=0.04,
                       notional=NOTIONAL, payment_lag_days=0)
        curve_set = CurveSet(_flat_curve())
        end = date(2027, 1, 15)
        expected = -NOTIONAL * 0.04 * 365 / 360 * _df(end) + NOTIONAL * (1.0 - _df(end))
        result = pv(swap, curve_set)
        assert result.value == pytest.approx(expected, rel=1e-12)
        assert all(flow.payment_date > AS_OF for flow in result.cashflows or ())
        assert discounted_value(swap, curve_set) == result.value

    def test_a_matured_swap_is_worth_exactly_nothing_and_needs_no_fixings(self):
        # Paid 2025-01-21, a year before as_of. Before this change its fixed
        # coupon came back at full value and its floating one at zero.
        swap = OISSwap(effective=date(2024, 1, 16), maturity=date(2025, 1, 16), fixed_rate=0.04,
                       notional=NOTIONAL)
        result = pv(swap, CurveSet(_flat_curve()))
        assert result.value == 0.0
        assert result.cashflows == ()

    def test_a_matured_fra_is_worth_exactly_nothing(self):
        fra = FRA(start=date(2025, 7, 15), end=date(2025, 10, 15), rate=0.04, notional=NOTIONAL)
        assert pv(fra, CurveSet(_flat_curve())).value == 0.0

    def test_the_annuity_counts_only_coupons_still_to_be_paid(self):
        # Coupons paid 2025-07-17 (gone), 2026-07-17 (accruing, counted in
        # full) and 2027-07-19. The annuity needs no fixing: it is the fixed
        # leg alone.
        swap = OISSwap(effective=date(2024, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                       notional=NOTIONAL)
        expected = 365 / 360 * _df(date(2026, 7, 17)) + 365 / 360 * _df(date(2027, 7, 19))
        assert annuity(swap, CurveSet(_flat_curve())).value == pytest.approx(expected, rel=1e-12)

    def test_one_rule_says_what_has_been_paid(self):
        # QuantLib's default, includeReferenceDateEvents false: a payment on
        # the valuation date has happened. Every cutoff in the pricer asks
        # this one function, so changing the convention is one edit.
        assert is_paid(AS_OF, AS_OF)
        assert is_paid(AS_OF - timedelta(days=1), AS_OF)
        assert not is_paid(AS_OF + timedelta(days=1), AS_OF)

    @pytest.mark.parametrize("measure", [pv, discounted_value, annuity, par_rate])
    def test_a_matured_trade_in_the_wrong_currency_is_still_refused(self, measure):
        # Paid 2025-01-20, so nothing is left to discount, but a peso swap on
        # the dollar curve is a mistake in the call, not a price of zero. Before
        # the check moved ahead of the cutoff, pv returned 0.0 here while
        # annuity raised; main raised for both.
        peso = OISSwap(effective=date(2023, 1, 16), maturity=date(2025, 1, 16), fixed_rate=0.04,
                       index=TIIE_FONDEO)
        with pytest.raises(CurrencyMismatchError, match="MXN"):
            measure(peso, CurveSet(_flat_curve()))

    def test_a_matured_peso_fra_on_the_dollar_curve_is_refused(self):
        fra = FRA(start=date(2025, 6, 2), end=date(2025, 6, 30), rate=0.075, index=TIIE_28)
        with pytest.raises(CurrencyMismatchError, match="MXN"):
            pv(fra, CurveSet(_flat_curve()))

    def test_the_pricer_drops_a_paid_flow_whatever_projected_it(self):
        # A product of the caller's own, registered the documented way, that
        # returns its whole history. The pricer is the one place a flow
        # meets the valuation date, so it is the one that drops it.
        from dataclasses import dataclass

        from rates_engine.instruments.cashflow import Cashflow
        from rates_engine.pricing.projection import project

        @dataclass(frozen=True)
        class TwoBullets:
            currency: Currency = Currency.USD

            @property
            def span(self) -> tuple[date, date]:
                return date(2025, 1, 15), date(2027, 1, 15)

            def describe(self) -> dict[str, object]:
                return {"kind": "two_bullets"}

        # The class is local, so it is named to ``register`` rather than read
        # from an annotation that ``from __future__ import annotations``
        # leaves as a string nothing can resolve.
        @project.register(TwoBullets)
        def _(instrument: TwoBullets, curve_set: CurveSet) -> tuple[Cashflow, ...]:
            def bullet(day: date) -> Cashflow:
                return Cashflow(payment_date=day, amount=1_000.0, leg="fixed",
                                accrual_start=day, accrual_end=day, year_fraction=0.0)

            return (bullet(date(2025, 7, 15)), bullet(AS_OF), bullet(date(2027, 1, 15)))

        result = pv(TwoBullets(), CurveSet(_flat_curve()))
        assert result.value == pytest.approx(1_000.0 * _df(date(2027, 1, 15)), rel=1e-12)
        assert [flow.payment_date for flow in result.cashflows or ()] == [date(2027, 1, 15)]


# ---------------------------------------------------------------------------
# The floating period in progress: part history, part forecast.
# ---------------------------------------------------------------------------


def _history(
    series_id: str = "SOFR",
    rate: float | Callable[[date], float] = 0.0430,
    *,
    start: date = date(2024, 1, 1),
    end: date = AS_OF,
    calendar: HolidayCalendar = SIFMA_US,
    quality: DataQuality = DataQuality.OBSERVED,
) -> Series:
    """One published rate per business day of ``calendar`` in ``[start, end)``."""
    days: list[date] = []
    day = start
    while day < end:
        if calendar.is_business_day(day):
            days.append(day)
        day += timedelta(days=1)
    values = tuple(rate(d) if callable(rate) else rate for d in days)
    return Series(
        series_id,
        tuple(days),
        values,
        Provenance(source="test", series_id=series_id, instrument_kind="fixing",
                   data_quality=quality),
    )


def _fixings(*series: Series) -> MarketSnapshot:
    return MarketSnapshot(as_of=AS_OF, series={s.series_id: s for s in series})


def _term(series_id: str, published: dict[date, float]) -> Series:
    days = tuple(sorted(published))
    return Series(
        series_id,
        days,
        tuple(published[d] for d in days),
        Provenance(source="test", series_id=series_id, instrument_kind="term_fixing"),
    )


class TestThePeriodInProgressNeedsItsFixings:
    """No curve knows what SOFR printed last month; without the fixings the price is refused."""

    SWAP = OISSwap(effective=date(2025, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                   notional=NOTIONAL)

    def test_pv_refuses_and_says_what_is_missing_and_how_to_pass_it(self):
        with pytest.raises(MissingFixingError) as caught:
            pv(self.SWAP, CurveSet(_flat_curve()))
        message = str(caught.value)
        for needed in ("2025-07-15", "2026-07-15", "2026-01-15", "'SOFR'", "fixings=",
                       "MarketSnapshot"):
            assert needed in message, (needed, message)

    @pytest.mark.parametrize("measure", [discounted_value, par_rate, par_rate_value])
    def test_every_measure_built_on_the_floating_leg_refuses_too(self, measure):
        with pytest.raises(MissingFixingError, match="SOFR"):
            measure(self.SWAP, CurveSet(_flat_curve()))

    def test_the_annuity_needs_no_fixing(self):
        expected = 365 / 360 * _df(date(2026, 7, 17)) + 365 / 360 * _df(date(2027, 7, 19))
        assert annuity(self.SWAP, CurveSet(_flat_curve())).value == pytest.approx(expected)

    def test_a_term_rate_period_names_its_fixing_date(self):
        # Period from Monday 2025-11-17; Term SOFR fixed two SIFMA days
        # earlier, on Thursday the 13th, and that is the date to supply.
        irs = IRSwap(effective=date(2025, 11, 17), maturity=date(2026, 11, 17), fixed_rate=0.04,
                     notional=NOTIONAL)
        with pytest.raises(MissingFixingError) as caught:
            pv(irs, CurveSet(_flat_curve()))
        message = str(caught.value)
        assert "TERM_SOFR_3M" in message
        assert "fixing of 2025-11-13" in message
        assert "2 business days before its accrual start" in message

    def test_a_fra_in_progress_refuses_and_so_does_its_fair_rate(self):
        fra = FRA(start=date(2025, 12, 15), end=date(2026, 3, 16), rate=0.04, notional=NOTIONAL)
        with pytest.raises(MissingFixingError, match="TERM_SOFR_3M"):
            pv(fra, CurveSet(_flat_curve()))
        with pytest.raises(MissingFixingError, match="TERM_SOFR_3M"):
            fra_fair_rate(fra, CurveSet(_flat_curve()))

    def test_a_snapshot_without_the_series_names_the_one_it_wanted(self):
        wrong = _fixings(_history("TERM_SOFR_3M"))
        with pytest.raises(MissingFixingError) as caught:
            pv(self.SWAP, CurveSet(_flat_curve()), fixings=wrong)
        assert "'SOFR'" in str(caught.value) and "TERM_SOFR_3M" in str(caught.value)
        assert "2025-07-15" in str(caught.value)

    def test_a_missing_business_day_is_named_never_interpolated(self):
        gap = date(2025, 10, 22)
        full = _history()
        kept = [(d, v) for d, v in zip(full.dates, full.values, strict=True) if d != gap]
        holed = Series("SOFR", tuple(d for d, _ in kept), tuple(v for _, v in kept),
                       full.provenance)
        with pytest.raises(MissingFixingError, match="2025-10-22"):
            pv(self.SWAP, CurveSet(_flat_curve()), fixings=_fixings(holed))

    def test_an_overnight_index_on_another_basis_is_refused_rather_than_compounded_on_360(self):
        # MarketSnapshot.compounded compounds 1 + r/360 a day. An overnight
        # index accruing on another basis would need another rule, and
        # applying this one would be a silent error the size of 365/360.
        odd = RateIndex(name="TEST_ON", label="Test ON", slug="test_on", currency=Currency.USD,
                        tenor=IndexTenor.OVERNIGHT, day_count=DayCount.ACT_365F,
                        calendar=SIFMA_US, administrator="nobody")
        swap = OISSwap(effective=date(2025, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                       index=odd)
        with pytest.raises(UnsupportedConventionError, match="ACT/360"):
            pv(swap, CurveSet(_flat_curve()), fixings=_fixings(_history("TEST_ON")))


class TestTheWrongCurrencyIsRefusedBeforeAnyFixingIsAskedFor:
    """A trade on a curve of another currency is refused as that, whatever it would need.

    The review of #12 (Alan, 2026-10-22): the currency was checked after the
    projection, which asks for fixings first, so both trades below raised
    ``MissingFixingError`` ("pass the published fixings", exit 1). Supplying
    them only reached ``CurrencyMismatchError`` (exit 2) on the second try.
    """

    PESO_OIS = OISSwap(effective=date(2025, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                       index=TIIE_FONDEO)
    PESO_FRA = FRA(start=date(2025, 12, 1), end=date(2026, 2, 1), rate=0.075, index=TIIE_28)

    @pytest.mark.parametrize(
        "measure", [pv, discounted_value, par_rate, par_rate_value, annuity]
    )
    def test_a_seasoned_peso_ois_on_the_dollar_curve(self, measure):
        with pytest.raises(CurrencyMismatchError, match="MXN") as caught:
            measure(self.PESO_OIS, CurveSet(_flat_curve()))
        assert caught.value.exit_code == 2

    @pytest.mark.parametrize("measure", [pv, discounted_value, fra_fair_rate])
    def test_a_seasoned_tiie_28_fra_on_the_dollar_curve(self, measure):
        with pytest.raises(CurrencyMismatchError, match="MXN") as caught:
            measure(self.PESO_FRA, CurveSet(_flat_curve()))
        assert caught.value.exit_code == 2

    def test_supplying_the_fixings_the_old_message_asked_for_changes_nothing(self):
        snapshot = _fixings(
            _history("TIIE_FONDEO", 0.07, calendar=BMV),
            _term("TIIE_28", {date(2025, 12, 1): 0.0745}),
        )
        for instrument in (self.PESO_OIS, self.PESO_FRA):
            with pytest.raises(CurrencyMismatchError, match="MXN"):
                pv(instrument, CurveSet(_flat_curve()), fixings=snapshot)

    def test_a_parametric_comparison_refuses_it_too(self):
        from dataclasses import dataclass
        from typing import Any

        from rates_engine.core.evidence import Evidence
        from rates_engine.core.results import EngineResult
        from rates_engine.pricing.linear import price_on_parametric

        @dataclass(frozen=True)
        class Fit(EngineResult):
            def payload_fields(self) -> dict[str, Any]:
                return {"curve_kind": "parametric", "model": "flat"}

        with pytest.raises(CurrencyMismatchError, match="MXN"):
            price_on_parametric(
                self.PESO_OIS,
                CurveSet(_flat_curve(0.0405)),
                CurveSet(_flat_curve()),
                fit=Fit(evidence=Evidence(produced_by="test.fit")),
            )

    def test_an_unseasoned_peso_fra_has_no_dollar_fair_rate(self):
        # main returned the dollar curve's forward for it, without a word.
        fra = FRA(start=date(2026, 4, 15), end=date(2026, 5, 13), rate=0.075, index=TIIE_28)
        with pytest.raises(CurrencyMismatchError, match="MXN"):
            fra_fair_rate(fra, CurveSet(_flat_curve()))


class TestClosedForms:
    """With the history and the curve both known exactly, so is the answer."""

    def test_an_ois_compounds_its_fixings_then_the_curve(self):
        # Monday 12 to Thursday 15 January 2026: three published fixings and
        # no weekend, so a daily and a business-day compounding rule agree
        # and the reference does not depend on which one is right.
        published = {date(2026, 1, 12): 0.0431, date(2026, 1, 13): 0.0429,
                     date(2026, 1, 14): 0.0433}
        swap = OISSwap(effective=date(2026, 1, 12), maturity=date(2027, 1, 12), fixed_rate=0.04,
                       notional=NOTIONAL)
        realised = math.prod(1.0 + r / 360.0 for r in published.values())
        pay = date(2027, 1, 14)
        growth = realised / _df(date(2027, 1, 12))
        expected = -NOTIONAL * 0.04 * 365 / 360 * _df(pay) + NOTIONAL * (growth - 1.0) * _df(pay)
        snapshot = _fixings(_term("SOFR", published))
        result = pv(swap, CurveSet(_flat_curve()), fixings=snapshot)
        assert result.value == pytest.approx(expected, rel=1e-12)
        floating = [flow for flow in result.cashflows or () if flow.leg == "float"]
        assert floating[0].rate == pytest.approx((growth - 1.0) / (365 / 360), rel=1e-12)

    def test_the_fixed_coupon_in_progress_is_counted_in_full(self):
        published = {date(2026, 1, 12): 0.0431, date(2026, 1, 13): 0.0429,
                     date(2026, 1, 14): 0.0433}
        swap = OISSwap(effective=date(2026, 1, 12), maturity=date(2027, 1, 12), fixed_rate=0.04,
                       notional=NOTIONAL)
        result = pv(swap, CurveSet(_flat_curve()), fixings=_fixings(_term("SOFR", published)))
        fixed = [flow for flow in result.cashflows or () if flow.leg == "fixed"]
        assert [flow.amount for flow in fixed] == [-NOTIONAL * 0.04 * 365 / 360]

    def test_fixings_on_and_after_the_valuation_date_are_never_read(self):
        # A period's coupon is history only up to the day before as_of; the
        # rest is the curve's. A snapshot that holds later prints -- a
        # backtest's look-ahead -- must not reach the price.
        published = {date(2026, 1, 12): 0.0431, date(2026, 1, 13): 0.0429,
                     date(2026, 1, 14): 0.0433}
        future = {**published, AS_OF: 0.99, date(2026, 1, 16): 0.99}
        swap = OISSwap(effective=date(2026, 1, 12), maturity=date(2027, 1, 12), fixed_rate=0.04,
                       notional=NOTIONAL)
        curve_set = CurveSet(_flat_curve())
        clean = pv(swap, curve_set, fixings=_fixings(_term("SOFR", published))).value
        assert pv(swap, curve_set, fixings=_fixings(_term("SOFR", future))).value == clean

    def test_a_tiie_de_fondeo_ois_compounds_on_the_bmv_calendar_in_pesos(self):
        published = {date(2026, 1, 12): 0.0702, date(2026, 1, 13): 0.0700,
                     date(2026, 1, 14): 0.0701}
        # Rolls on the 12th: 2026-04-12 is a Sunday, so the period ends on
        # Monday the 13th (91 days) and is paid two BMV days later.
        swap = OISSwap(effective=date(2026, 1, 12), maturity=date(2026, 4, 12), fixed_rate=0.075,
                       notional=500_000_000.0, frequency_months=3, index=TIIE_FONDEO)
        curve_set = CurveSet(_flat_curve(0.07, Currency.MXN))
        realised = math.prod(1.0 + r / 360.0 for r in published.values())
        growth = realised / _df(date(2026, 4, 13), 0.07)
        pay = date(2026, 4, 15)
        expected = (-500_000_000.0 * 0.075 * 91 / 360 + 500_000_000.0 * (growth - 1.0)) * _df(
            pay, 0.07
        )
        result = pv(swap, curve_set, fixings=_fixings(_term("TIIE_FONDEO", published)))
        assert result.value == pytest.approx(expected, rel=1e-12)
        assert result.unit == "MXN"
        assert result.evidence.fields["realised_fixings"][0]["series_id"] == "TIIE_FONDEO"

    def test_an_irs_pays_the_term_sofr_fixing_two_business_days_before_its_period(self):
        # Single curve, no payment lag: the projected periods telescope to
        # N (P(first projected start) - P(maturity)), and the period in
        # progress pays N * F * tau on the fixing of 2025-11-13, two SIFMA
        # days before it starts. The print of the 17th itself sets another
        # period and must not be read.
        irs = IRSwap(effective=date(2025, 11, 17), maturity=date(2026, 11, 17), fixed_rate=0.04,
                     notional=NOTIONAL)
        fixing = 0.0412
        snapshot = _fixings(
            _term("TERM_SOFR_3M", {date(2025, 11, 13): fixing, date(2025, 11, 17): 0.99})
        )
        first_end, end = date(2026, 2, 17), date(2026, 11, 17)
        expected = (
            -NOTIONAL * 0.04 * 365 / 360 * _df(end)
            + NOTIONAL * fixing * 92 / 360 * _df(first_end)
            + NOTIONAL * (_df(first_end) - _df(end))
        )
        assert pv(irs, CurveSet(_flat_curve()), fixings=snapshot).value == pytest.approx(
            expected, rel=1e-12
        )

    @pytest.mark.parametrize(("side", "sign"), [(Side.PAYER, 1.0), (Side.RECEIVER, -1.0)])
    def test_a_fra_in_progress_settles_on_its_fixing(self, side, sign):
        # Starts Monday 2025-12-15, fixed Thursday the 11th.
        fra = FRA(start=date(2025, 12, 15), end=date(2026, 3, 16), rate=0.04, notional=NOTIONAL,
                  side=side)
        snapshot = _fixings(_term("TERM_SOFR_3M", {date(2025, 12, 11): 0.0425}))
        curve_set = CurveSet(_flat_curve())
        expected = sign * NOTIONAL * (0.0425 - 0.04) * 91 / 360 * _df(date(2026, 3, 16))
        assert pv(fra, curve_set, fixings=snapshot).value == pytest.approx(expected, rel=1e-12)
        assert fra_fair_rate(fra, curve_set, fixings=snapshot) == 0.0425

    def test_a_tiie_28_fra_reads_its_peso_fixing_on_the_period_start(self):
        # TIIE 28 keeps the date the forward has always been projected from;
        # its real lag is unverified (docs/forge/research/003). The decoy on
        # the 23rd, two BMV days earlier, is what a Term SOFR rule would read.
        fra = FRA(start=date(2025, 12, 29), end=date(2026, 1, 26), rate=0.075,
                  notional=1_000_000_000.0, index=TIIE_28)
        snapshot = _fixings(
            _term("TIIE_28", {date(2025, 12, 23): 0.99, date(2025, 12, 29): 0.0745})
        )
        result = pv(fra, CurveSet(_flat_curve(0.07, Currency.MXN)), fixings=snapshot)
        expected = 1_000_000_000.0 * (0.0745 - 0.075) * 28 / 360 * _df(date(2026, 1, 26), 0.07)
        assert result.value == pytest.approx(expected, rel=1e-12)
        assert result.evidence.worst_quality is DataQuality.ASSUMED  # the MXN conventions

    def test_a_period_fully_accrued_and_not_yet_paid_is_all_history(self):
        # Accrued Monday 12 to Wednesday 14 January, paid Friday the 16th:
        # every fixing is published by as_of and the curve only discounts.
        published = {date(2026, 1, 12): 0.0431, date(2026, 1, 13): 0.0429}
        swap = OISSwap(effective=date(2026, 1, 12), maturity=date(2026, 1, 14), fixed_rate=0.04,
                       notional=NOTIONAL, frequency_months=1)
        realised = (1.0 + 0.0431 / 360.0) * (1.0 + 0.0429 / 360.0)
        expected = NOTIONAL * (realised - 1.0 - 0.04 * 2 / 360) * _df(date(2026, 1, 16))
        result = pv(swap, CurveSet(_flat_curve()), fixings=_fixings(_term("SOFR", published)))
        assert result.value == pytest.approx(expected, rel=1e-12)


class TestTermSOFRFixesTwoBusinessDaysBeforeItsPeriod:
    """A term coupon is history from its fixing date, which can precede the period itself.

    The ARRC convention, the one the cap pricer follows: Term SOFR is fixed
    two SIFMA business days before the period it sets. A fixing dated before
    ``as_of`` is history and must be supplied, even for a period that starts
    after ``as_of``. One dated ``as_of`` is read if supplied and forecast if
    not, QuantLib's default. One dated after is forecast.
    """

    def test_the_print_of_the_twentieth_sets_the_period_from_the_twenty_second(self):
        fra = FRA(start=date(2025, 10, 22), end=date(2026, 1, 22), rate=0.04, notional=NOTIONAL)
        snapshot = _fixings(
            _term("TERM_SOFR_3M", {date(2025, 10, 20): 0.0418, date(2025, 10, 22): 0.99})
        )
        result = pv(fra, CurveSet(_flat_curve()), fixings=snapshot)
        expected = NOTIONAL * (0.0418 - 0.04) * 92 / 360 * _df(date(2026, 1, 22))
        assert result.value == pytest.approx(expected, rel=1e-12)
        (record,) = result.evidence.fields["realised_fixings"]
        assert record["observed_from"] == record["observed_to"] == "2025-10-20"
        assert record["method"] == "term"

    def test_a_period_starting_after_as_of_that_fixed_before_it_needs_its_fixing(self):
        # Starts Friday 2026-01-16, after as_of, but fixed on Wednesday the
        # 14th, before it: history, refused without the print.
        irs = IRSwap(effective=date(2026, 1, 16), maturity=date(2027, 1, 16), fixed_rate=0.04,
                     notional=NOTIONAL)
        with pytest.raises(MissingFixingError) as caught:
            pv(irs, CurveSet(_flat_curve()))
        assert "fixed on 2026-01-14, before the valuation date 2026-01-15" in str(caught.value)

    def test_a_swap_starting_at_spot_reads_todays_fixing_when_given_and_forecasts_otherwise(
        self,
    ):
        # Spot is 2026-01-20 (Martin Luther King Day is the 19th), so the
        # first period fixed on as_of itself. QuantLib's default: the day's
        # print is used if it is known and forecast if it is not, so a swap
        # struck this morning prices before the print is out.
        irs = IRSwap(effective=date(2026, 1, 20), maturity=date(2027, 1, 20), fixed_rate=0.04,
                     notional=NOTIONAL)
        curve_set = CurveSet(_flat_curve())
        first_end, end = date(2026, 4, 20), date(2027, 1, 20)
        fixed_leg = -NOTIONAL * 0.04 * 365 / 360 * _df(end)
        rest = NOTIONAL * (_df(first_end) - _df(end))

        forecast = pv(irs, curve_set)
        first = NOTIONAL * (_df(date(2026, 1, 20)) / _df(first_end) - 1.0) * _df(first_end)
        assert forecast.value == pytest.approx(fixed_leg + first + rest, rel=1e-12)
        (entry,) = forecast.evidence.fields["fixings_dated_as_of"]
        assert entry == {
            "leg": "float",
            "accrual_start": "2026-01-20",
            "accrual_end": "2026-04-20",
            "payment_date": "2026-04-20",
            "series_id": "TERM_SOFR_3M",
            "fixing_date": "2026-01-15",
            "source": "curve",
        }
        assert "realised_fixings" not in forecast.evidence.fields
        # A snapshot without that day is the same as no snapshot.
        older = _fixings(_term("TERM_SOFR_3M", {date(2026, 1, 13): 0.0405}))
        assert pv(irs, curve_set, fixings=older).value == forecast.value

        fixing = 0.0405
        snapshot = _fixings(_term("TERM_SOFR_3M", {AS_OF: fixing}))
        read = pv(irs, curve_set, fixings=snapshot)
        expected = fixed_leg + NOTIONAL * fixing * 90 / 360 * _df(first_end) + rest
        assert read.value == pytest.approx(expected, rel=1e-12)
        assert read.evidence.fields["fixings_dated_as_of"][0]["source"] == "fixings"
        assert read.evidence.fields["realised_fixings"][0]["observed_from"] == "2026-01-15"

    def test_three_business_days_out_it_is_still_a_forecast(self):
        # Starts Wednesday 2026-01-21, fixes Friday the 16th: after as_of, so
        # projected off the curve and no fixing is asked for.
        fra = FRA(start=date(2026, 1, 21), end=date(2026, 4, 21), rate=0.04, notional=NOTIONAL)
        start, end = date(2026, 1, 21), date(2026, 4, 21)
        forward = (_df(start) / _df(end) - 1.0) / (90 / 360)
        expected = NOTIONAL * (forward - 0.04) * 90 / 360 * _df(end)
        assert pv(fra, CurveSet(_flat_curve())).value == pytest.approx(expected, rel=1e-12)

    def test_a_tiie_28_period_fixed_today_follows_the_same_rule(self):
        # TIIE 28 fixes on its start; a FRA starting on as_of fixed today, and
        # is forecast without the print (the pinned number from main) and
        # settled on it with.
        fra = FRA(start=AS_OF, end=date(2026, 2, 12), rate=0.075, notional=1_000_000_000.0,
                  index=TIIE_28)
        curve_set = CurveSet(_flat_curve(0.07, Currency.MXN))
        forecast = pv(fra, curve_set)
        assert forecast.evidence.fields["fixings_dated_as_of"][0]["source"] == "curve"
        snapshot = _fixings(_term("TIIE_28", {AS_OF: 0.0745}))
        expected = 1_000_000_000.0 * (0.0745 - 0.075) * 28 / 360 * _df(date(2026, 2, 12), 0.07)
        assert pv(fra, curve_set, fixings=snapshot).value == pytest.approx(expected, rel=1e-12)
        assert fra_fair_rate(fra, curve_set, fixings=snapshot) == 0.0745


class TestTodaysFixingIsForecastWithoutADegradation:
    """A fixing dated ``as_of`` that is not out yet is forecast and recorded, not degraded.

    With T+2 spot, the first period of every Term SOFR swap struck today
    fixes on ``as_of``: degrading the forecast marked the ordinary morning
    quote of a vanilla swap ``proxy``, and everything built on it, until the
    print came out. The review of #12 (Alan, 2026-10-22) asked for it to be
    flagged at most once the print is due; the set takes the lighter rule
    for every branch. The forecast is recorded in ``fixings_dated_as_of``
    (``"source": "curve"``) and adds no ``Degradation``, so ``pv``,
    ``par_rate`` and ``dv01`` report the same quality.
    """

    SPOT_IRS = IRSwap(effective=date(2026, 1, 20), maturity=date(2027, 1, 20), fixed_rate=0.04,
                      notional=NOTIONAL)

    def test_a_spot_swap_priced_before_todays_print_is_observed(self):
        # Before: worst_quality proxy, with curve_forward_for_todays_fixing.
        result = pv(self.SPOT_IRS, CurveSet(_flat_curve()))
        assert result.evidence.warnings == ()
        assert result.evidence.worst_quality is DataQuality.OBSERVED
        (entry,) = result.evidence.fields["fixings_dated_as_of"]
        assert (entry["series_id"], entry["source"]) == ("TERM_SOFR_3M", "curve")

    def test_a_series_without_that_day_is_forecast_and_observed_too(self):
        older = _fixings(_term("TERM_SOFR_3M", {date(2026, 1, 13): 0.0405}))
        result = pv(self.SPOT_IRS, CurveSet(_flat_curve()), fixings=older)
        assert result.evidence.warnings == ()
        assert result.evidence.fields["fixings_dated_as_of"][0]["source"] == "curve"

    def test_pv_par_rate_and_dv01_report_the_same_quality(self):
        # Alan's point 3: pv and par_rate were proxy, and dv01 -19,589.93
        # observed with no warnings, for one forecast. Now all three agree.
        from rates_engine.risk.sensitivities import dv01

        curve_set = CurveSet(_flat_curve())
        measures = (pv(self.SPOT_IRS, curve_set), par_rate(self.SPOT_IRS, curve_set),
                    dv01(self.SPOT_IRS, curve_set))
        assert {m.evidence.worst_quality for m in measures} == {DataQuality.OBSERVED}
        assert all(m.evidence.warnings == () for m in measures)

    def test_a_tiie_28_fra_keeps_only_the_peso_conventions(self):
        fra = FRA(start=AS_OF, end=date(2026, 2, 12), rate=0.075, notional=1_000_000_000.0,
                  index=TIIE_28)
        result = pv(fra, CurveSet(_flat_curve(0.07, Currency.MXN)))
        assert not [w for w in result.evidence.warnings if w.code.startswith("curve_forward")]
        assert result.evidence.worst_quality is DataQuality.ASSUMED  # the MXN conventions
        assert result.evidence.fields["fixings_dated_as_of"][0]["source"] == "curve"

    def test_with_the_print_it_is_read_and_still_observed(self):
        snapshot = _fixings(_term("TERM_SOFR_3M", {AS_OF: 0.0405}))
        result = pv(self.SPOT_IRS, CurveSet(_flat_curve()), fixings=snapshot)
        assert result.evidence.warnings == ()
        assert result.evidence.fields["fixings_dated_as_of"][0]["source"] == "fixings"


class TestASnapshotWithoutTheIndexIsRefusedEvenForTodaysFixing:
    """Only a day missing from a series that is there falls back to the curve.

    The rule the caps follow on #16. A snapshot that holds today's print
    under another name is a wrong snapshot, not a print still to come, and
    read as the second it priced Alan's two-year spot payer at 4% on a flat
    4% curve at the curve's 48,054.45 instead of the 180,487.14 its 4.50%
    print gives, flagged ``proxy`` and saying the fixing "was not among the
    fixings passed".
    """

    SPOT_IRS = IRSwap(effective=date(2026, 1, 20), maturity=date(2028, 1, 20), fixed_rate=0.04,
                      notional=NOTIONAL)

    def test_the_review_repro_is_refused_and_names_the_series_it_wanted(self):
        misnamed = _fixings(_term("TERM_SOFR", {AS_OF: 0.045}))
        with pytest.raises(MissingFixingError) as caught:
            pv(self.SPOT_IRS, CurveSet(_flat_curve()), fixings=misnamed)
        message = str(caught.value)
        for needed in (
            "IRSwap 2026-01-20 to 2028-01-20",
            "the period from 2026-01-20 to 2026-04-20 fixes on the valuation date 2026-01-15",
            "the fixings passed have no series named after its index",
            "has no series 'TERM_SOFR_3M'; it has ['TERM_SOFR']",
            "Name the series 'TERM_SOFR_3M'; an absent series is not a fixing that has yet "
            "to be published.",
        ):
            assert needed in message, (needed, message)

    def test_under_the_right_name_it_prices_on_the_print(self):
        # Closed form, single curve and no payment lag: the fixed leg, the
        # first period on the 4.50% print over its 90 days, and the rest
        # telescoping to N (P(2026-04-20) - P(2028-01-20)). Alan's number.
        first_end, middle, end = date(2026, 4, 20), date(2027, 1, 20), date(2028, 1, 20)
        expected = (
            -NOTIONAL * 0.04 * 365 / 360 * (_df(middle) + _df(end))
            + NOTIONAL * 0.045 * 90 / 360 * _df(first_end)
            + NOTIONAL * (_df(first_end) - _df(end))
        )
        assert expected == pytest.approx(180_487.14, abs=0.005)
        snapshot = _fixings(_term("TERM_SOFR_3M", {AS_OF: 0.045}))
        result = pv(self.SPOT_IRS, CurveSet(_flat_curve()), fixings=snapshot)
        assert result.value == pytest.approx(expected, rel=1e-12)

    @pytest.mark.parametrize("measure", [pv, discounted_value, par_rate, par_rate_value])
    def test_every_measure_on_the_floating_leg_refuses_it(self, measure):
        misnamed = _fixings(_term("TERM_SOFR", {AS_OF: 0.045}))
        with pytest.raises(MissingFixingError, match="Name the series 'TERM_SOFR_3M'"):
            measure(self.SPOT_IRS, CurveSet(_flat_curve()), fixings=misnamed)

    def test_a_tiie_28_fra_fixed_today_and_its_fair_rate_refuse_it(self):
        fra = FRA(start=AS_OF, end=date(2026, 2, 12), rate=0.075, notional=1_000_000_000.0,
                  index=TIIE_28)
        curve_set = CurveSet(_flat_curve(0.07, Currency.MXN))
        misnamed = _fixings(_term("TIIE28", {AS_OF: 0.0745}))
        with pytest.raises(MissingFixingError, match="Name the series 'TIIE_28'"):
            pv(fra, curve_set, fixings=misnamed)
        with pytest.raises(MissingFixingError, match="Name the series 'TIIE_28'"):
            fra_fair_rate(fra, curve_set, fixings=misnamed)

    def test_without_a_snapshot_it_is_still_forecast(self):
        # No snapshot at all is a valuation before the print, the ordinary
        # case; only a snapshot that lacks the series is a wrong one.
        result = pv(self.SPOT_IRS, CurveSet(_flat_curve()))
        assert result.value == pytest.approx(48_054.45, abs=0.005)

    def test_a_trade_that_has_not_fixed_ignores_the_snapshot(self):
        # Fixes 2026-01-16, after as_of: nothing is read, so no series is
        # needed and a book's snapshot without it changes nothing.
        fra = FRA(start=date(2026, 1, 21), end=date(2026, 4, 21), rate=0.04, notional=NOTIONAL)
        curve_set = CurveSet(_flat_curve())
        misnamed = _fixings(_term("TERM_SOFR", {AS_OF: 0.045}))
        assert pv(fra, curve_set, fixings=misnamed).value == pv(fra, curve_set).value
        assert pv(fra, curve_set).evidence.warnings == ()


class TestTheFixingDateIsLookedUpOnlyNearTheValuationDate:
    """A period starting weeks after as_of cannot have fixed, so its fixing date is not computed.

    Counting business days back for every period of every reprice doubled
    a thirty-year quarterly swap's price time (0.52 to 1.12 ms); only the
    few periods near the valuation date need the walk.
    """

    def test_a_thirty_year_swap_walks_the_calendar_for_its_first_periods_only(self, monkeypatch):
        calls: list[date] = []
        walk = SIFMA_US.add_business_days

        def counting(day: date, count: int) -> date:
            calls.append(day)
            return walk(day, count)

        monkeypatch.setattr(SIFMA_US, "add_business_days", counting)
        irs = IRSwap(effective=date(2026, 1, 20), maturity=date(2056, 1, 20), fixed_rate=0.04,
                     notional=NOTIONAL)
        pv(irs, CurveSet(_flat_curve()))
        lookups = [day for day in calls if day == date(2026, 1, 20)]
        assert lookups  # the spot period, which did fix on as_of, was looked up
        assert all(day <= AS_OF + timedelta(days=21) for day in calls)


#: The OIS strip of the review's repro scripts (``curves/common.py``), the
#: same quotes QuantLib was bootstrapped on below. Decimal, annual ACT/360,
#: payment lag two, spot 2026-01-20 (T+2 over Martin Luther King Day).
_SPOT = date(2026, 1, 20)
_OIS_QUOTES = (
    ("1M", 1, 0.0431), ("3M", 3, 0.0428), ("6M", 6, 0.0419), ("1Y", 12, 0.0402),
    ("2Y", 24, 0.0375), ("3Y", 36, 0.0362), ("5Y", 60, 0.0358), ("7Y", 84, 0.0363),
    ("10Y", 120, 0.0372), ("15Y", 180, 0.0384), ("20Y", 240, 0.0389), ("30Y", 360, 0.0382),
)


@pytest.fixture(scope="module")
def strip_curve() -> DiscountCurve:
    """The engine's own bootstrap of :data:`_OIS_QUOTES`, as the repro script builds it."""
    nodes = tuple(
        SwapQuoteNode(
            swap=OISSwap(effective=_SPOT, maturity=add_months(_SPOT, months), fixed_rate=quote),
            quoted_rate=quote,
            label=label,
        )
        for label, months, quote in _OIS_QUOTES
    )
    return bootstrap_discount_curve(AS_OF, nodes).curve


#: QuantLib 1.44, computed in the review's environment with its
#: ``curves/r04_seasoned_swap.py`` (the first two numbers are the ones that
#: script prints) extended to the cases below: ``OvernightIndexedSwap``
#: (annual, ACT/360, payment lag 2, UnitedStates(SOFR), Modified Following,
#: backward) on a ``PiecewiseLogLinearDiscount`` bootstrapped from
#: :data:`_OIS_QUOTES` with ``OISRateHelper`` (lag 2, annual), SOFR fixings at
#: a flat 4.30% on every UnitedStates(SOFR) business day from 2024-01-01 to
#: 2026-01-14. Payer at 4% on USD 100m, valued 2026-01-15. The 2024 and 2025
#: starts share every flow still to be paid, so QuantLib gives both the same
#: numbers. ``*_ANNUITY`` is ``-fixedLegBPS() / (notional * 1e-4)``.
QL_SEASONED_PV = -17348.132534572855
QL_SEASONED_FAIR = 0.039911027926253645
QL_SEASONED_ANNUITY = 1.9498401918820142
#: Same set-up, one annual period 2025-01-14 to 2026-01-14, paid 2026-01-16.
QL_ACCRUED_PV = 400007.9465766563
QL_ACCRUED_FAIR = 0.04394575534861574
QL_ACCRUED_ANNUITY = 1.0137677357948412
#: ``VanillaSwap``, payer at 3.9% on USD 100m, 2025-08-20 to 2027-08-20,
#: fixed annual ACT/360, floating quarterly on an IborIndex (3M, fixing days
#: 2, ACT/360, UnitedStates(SOFR)) projected on the QuantLib OIS curve
#: spread by 18 bp continuously compounded on ACT/365F and discounted on the
#: OIS curve. Fixings: 4.20% on 2025-08-18 and 4.12% on 2025-11-18, two
#: business days before the periods of the 20th they set.
QL_IRS_PV = -814681.7892547129
QL_IRS_FAIR = 0.034807974465136754
#: Same index and curves, payer at 3.9% from spot, 2026-01-20 to 2028-01-20,
#: whose first period fixed on 2026-01-15, the valuation date. With that
#: fixing in the index history at 4.05%, QuantLib reads it; with an index that
#: has no history it forecasts it (``enforcesTodaysHistoricFixings`` False,
#: the default).
QL_SPOT_IRS_PV = -40555.79445490334
QL_SPOT_IRS_FAIR = 0.03878812177284646
QL_SPOT_IRS_FORECAST_PV = 60668.38180689141
QL_SPOT_IRS_FORECAST_FAIR = 0.03931695370179005


def _compounding_rule_gap(start: date, end: date, rate: float = 0.0430) -> float:
    """How far apart the two compounding rules put a flat history's growth.

    ``MarketSnapshot.compounded`` multiplies ``1 + r/360`` once per calendar
    day, so a weekend is ``(1 + r/360)^3``. QuantLib, CME and the New York
    Fed multiply ``1 + r d/360`` once per business day, ``d`` the calendar
    days it covers. The review's finding 3.4; the rule lives in
    ``market/snapshot.py`` and is corrected on the branch that owns it, after
    which this gap is what the two prices may still differ by and no more.
    Written out here from the calendar alone, not by calling the snapshot.
    """
    days = [start + timedelta(days=k) for k in range((end - start).days)]
    daily = math.prod(1.0 + rate / 360.0 for _ in days)
    published = [day for day in days if SIFMA_US.is_business_day(day)]
    following = published[1:] + [end]
    business = math.prod(
        1.0 + rate * (after - day).days / 360.0
        for day, after in zip(published, following, strict=True)
    )
    return daily - business


def _pv_tolerance(start: date, end: date) -> float:
    """USD on USD 100m: the rule gap, grown by at most 5% of forecast and one dollar."""
    return NOTIONAL * abs(_compounding_rule_gap(start, end)) * 1.05 + 1.0


class TestAgainstQuantLib:
    """The seasoned trades of the review, against QuantLib with the same history."""

    @pytest.mark.parametrize("effective", [date(2025, 7, 15), date(2024, 7, 15)])
    def test_the_seasoned_payer_matches_quantlib(self, strip_curve, effective):
        # Before this change: -2,238,694.78 (2025 start) and -6,294,250.34
        # (2024 start) against QuantLib's -17,348.13; par 2.8519% and 1.8762%
        # against 3.9911%. Now 128 USD and 0.0066 bp apart, all of it the
        # compounding rule: 128.30 USD of coupon on this window.
        swap = OISSwap(effective=effective, maturity=date(2027, 7, 15), fixed_rate=0.04,
                       notional=NOTIONAL)
        curve_set = CurveSet(strip_curve)
        snapshot = _fixings(_history())
        tolerance = _pv_tolerance(date(2025, 7, 15), AS_OF)
        assert tolerance < 150.0  # the 222 bp error this guards against is 2.2m USD
        assert pv(swap, curve_set, fixings=snapshot).value == pytest.approx(
            QL_SEASONED_PV, abs=tolerance
        )
        assert par_rate(swap, curve_set, fixings=snapshot).value == pytest.approx(
            QL_SEASONED_FAIR, abs=tolerance / (NOTIONAL * QL_SEASONED_ANNUITY)
        )
        assert annuity(swap, curve_set).value == pytest.approx(QL_SEASONED_ANNUITY, abs=1e-9)

    def test_the_two_starts_value_the_same_remaining_flows_identically(self, strip_curve):
        curve_set = CurveSet(strip_curve)
        snapshot = _fixings(_history())
        young, old = (
            OISSwap(effective=effective, maturity=date(2027, 7, 15), fixed_rate=0.04,
                    notional=NOTIONAL)
            for effective in (date(2025, 7, 15), date(2024, 7, 15))
        )
        assert pv(old, curve_set, fixings=snapshot).value == pv(
            young, curve_set, fixings=snapshot
        ).value

    def test_a_period_accrued_but_unpaid_matches_quantlib(self, strip_curve):
        # A whole year of history, every day of it realised: 271 USD apart,
        # the rule gap to the cent.
        swap = OISSwap(effective=date(2025, 1, 14), maturity=date(2026, 1, 14), fixed_rate=0.04,
                       notional=NOTIONAL)
        curve_set = CurveSet(strip_curve)
        snapshot = _fixings(_history())
        tolerance = _pv_tolerance(date(2025, 1, 14), date(2026, 1, 14))
        assert pv(swap, curve_set, fixings=snapshot).value == pytest.approx(
            QL_ACCRUED_PV, abs=tolerance
        )
        assert par_rate(swap, curve_set, fixings=snapshot).value == pytest.approx(
            QL_ACCRUED_FAIR, abs=tolerance / (NOTIONAL * QL_ACCRUED_ANNUITY)
        )

    @pytest.mark.parametrize(("side", "expected"), [(Side.PAYER, 1.0), (Side.RECEIVER, -1.0)])
    def test_a_seasoned_term_sofr_irs_on_two_curves_matches_quantlib(
        self, strip_curve, side, expected
    ):
        # No overnight compounding is involved, so the tolerance is the
        # curves' agreement and nothing else.
        irs = IRSwap(effective=date(2025, 8, 20), maturity=date(2027, 8, 20), fixed_rate=0.039,
                     notional=NOTIONAL, side=side)
        curve_set = CurveSet(strip_curve, strip_curve.shifted(0.0018))
        snapshot = _fixings(
            _term("TERM_SOFR_3M", {date(2025, 8, 18): 0.0420, date(2025, 11, 18): 0.0412})
        )
        assert pv(irs, curve_set, fixings=snapshot).value == pytest.approx(
            expected * QL_IRS_PV, abs=1e-3
        )
        assert par_rate(irs, curve_set, fixings=snapshot).value == pytest.approx(
            QL_IRS_FAIR, abs=1e-10
        )

    def test_a_spot_starting_irs_that_fixed_today_matches_quantlib(self, strip_curve):
        irs = IRSwap(effective=date(2026, 1, 20), maturity=date(2028, 1, 20), fixed_rate=0.039,
                     notional=NOTIONAL)
        curve_set = CurveSet(strip_curve, strip_curve.shifted(0.0018))
        snapshot = _fixings(_term("TERM_SOFR_3M", {AS_OF: 0.0405}))
        assert pv(irs, curve_set, fixings=snapshot).value == pytest.approx(
            QL_SPOT_IRS_PV, abs=1e-3
        )
        assert par_rate(irs, curve_set, fixings=snapshot).value == pytest.approx(
            QL_SPOT_IRS_FAIR, abs=1e-10
        )
        assert pv(irs, curve_set).value == pytest.approx(QL_SPOT_IRS_FORECAST_PV, abs=1e-3)
        assert par_rate(irs, curve_set).value == pytest.approx(
            QL_SPOT_IRS_FORECAST_FAIR, abs=1e-10
        )

    def test_struck_at_its_par_rate_a_seasoned_swap_is_worth_nothing(self, strip_curve):
        # QuantLib's fairRate: the fixed rate that zeroes what is left. The
        # accrued floating coupon is in the numerator, the full fixed coupon
        # in progress in the annuity.
        curve_set = CurveSet(strip_curve)
        snapshot = _fixings(_history())
        swap = OISSwap(effective=date(2024, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                       notional=NOTIONAL)
        struck = swap.with_terms(fixed_rate=par_rate(swap, curve_set, fixings=snapshot).value)
        assert abs(pv(struck, curve_set, fixings=snapshot).value) < 1e-8 * NOTIONAL


class TestTheEvidenceSaysWhatHistoryWasUsed:
    """The fixings behind a number, and how far to trust them, travel with it."""

    SWAP = OISSwap(effective=date(2025, 7, 15), maturity=date(2027, 7, 15), fixed_rate=0.04,
                   notional=NOTIONAL)

    def test_the_price_records_each_window_and_its_quality(self):
        series = _history()
        result = pv(self.SWAP, CurveSet(_flat_curve()), fixings=_fixings(series))
        (record,) = result.evidence.fields["realised_fixings"]
        assert record["series_id"] == "SOFR"
        assert record["method"] == "compounded"
        assert record["observed_from"] == "2025-07-15"
        assert record["observed_to"] == "2026-01-15"
        assert record["accrual_start"] == "2025-07-15"
        assert record["accrual_end"] == "2026-07-15"
        assert record["data_quality"] == "observed"
        # Every SIFMA business day from 2025-07-15 to 2026-01-14 was read.
        assert record["business_days"] == sum(
            1 for d in series.dates if date(2025, 7, 15) <= d < AS_OF
        )
        assert series.provenance in result.evidence.inputs

    def test_assumed_fixings_make_the_price_assumed(self):
        series = _history(quality=DataQuality.ASSUMED)
        result = pv(self.SWAP, CurveSet(_flat_curve()), fixings=_fixings(series))
        assert result.evidence.worst_quality is DataQuality.ASSUMED

    def test_the_par_rate_records_them_too(self):
        result = par_rate(self.SWAP, CurveSet(_flat_curve()), fixings=_fixings(_history()))
        assert result.evidence.fields["realised_fixings"][0]["series_id"] == "SOFR"

    def test_the_payload_serialises(self):
        result = pv(self.SWAP, CurveSet(_flat_curve()), fixings=_fixings(_history()))
        stored = json.loads(json.dumps(result.to_dict()))
        assert stored["evidence"]["fields"]["realised_fixings"][0]["rate"] > 0.04

    def test_a_trade_with_no_history_records_none_even_when_handed_fixings(self):
        # Fixings passed for a book are not a reason for an unseasoned trade's
        # payload to change shape, or its numbers to move.
        instrument, curve_set = _pinned_cases()["ois_spot_payer"]
        result = pv(instrument, curve_set, fixings=_fixings(_history()))
        assert "realised_fixings" not in result.evidence.fields
        assert result.evidence.inputs == ()
        assert result.value == PINNED["ois_spot_payer"]["pv"]

    def test_a_parametric_comparison_carries_the_history_and_its_quality(self):
        # Both prices rest on the same fixings, so the comparison records them
        # once and an assumed history still marks it assumed.
        from dataclasses import dataclass
        from typing import Any

        from rates_engine.core.evidence import Evidence
        from rates_engine.core.results import EngineResult
        from rates_engine.pricing.linear import price_on_parametric

        @dataclass(frozen=True)
        class Fit(EngineResult):
            def payload_fields(self) -> dict[str, Any]:
                return {"curve_kind": "parametric", "model": "flat"}

        series = _history(quality=DataQuality.ASSUMED)
        result = price_on_parametric(
            self.SWAP,
            CurveSet(_flat_curve(0.0405)),
            CurveSet(_flat_curve()),
            fit=Fit(evidence=Evidence(produced_by="test.fit")),
            fixings=_fixings(series),
        )
        assert result.evidence.fields["realised_fixings"][0]["series_id"] == "SOFR"
        assert result.evidence.worst_quality is DataQuality.ASSUMED
        # The accrued half-year is identical on both curves and cancels out
        # of the difference; only the forecast moves.
        assert result.parametric_pv != result.bootstrap_pv
