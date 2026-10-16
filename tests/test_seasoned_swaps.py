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
and, for the swaps that start on or after the valuation date, the outputs of
``main`` @ 3197ed6 before this change, which must not move by a bit.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from rates_engine.conventions.indices import TERM_SOFR_6M, TIIE_FONDEO
from rates_engine.conventions.side import Side
from rates_engine.core.money import Currency
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.fra import FRA
from rates_engine.instruments.swaps import IRSwap, OISSwap
from rates_engine.pricing.linear import annuity, discounted_value, par_rate, pv
from rates_engine.pricing.projection import fra_fair_rate

AS_OF = date(2026, 1, 15)

# ---------------------------------------------------------------------------
# Swaps that start on or after the valuation date: unchanged to the bit.
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
        "irs_spot_payer": (
            IRSwap(effective=AS_OF, maturity=date(2030, 1, 15), fixed_rate=0.037,
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
        "fra_fwd_payer_dual": (
            FRA(start=date(2026, 4, 15), end=date(2026, 7, 15), rate=0.037,
                notional=100_000_000.0),
            dual,
        ),
        "fra_spot_receiver": (
            FRA(start=AS_OF, end=date(2026, 4, 15), rate=0.04, notional=100_000_000.0,
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
    "irs_spot_payer": {
        "pv": -54850.192308265716,
        "discounted_value": -54850.192308265716,
        "flows": (
            (-3751388.8888888885, 0.037),
            (-3782222.222222222, 0.037),
            (-3741111.111111111, 0.037),
            (-3741111.111111111, 0.037),
            (996161.8622385426, 0.039846474489541706),
            (1000650.590524943, 0.03958617720758016),
            (1011627.6480569786, 0.03958542970657743),
            (1011627.6480569786, 0.03958542970657743),
            (916503.1711253712, 0.03666012684501485),
            (926733.5817465527, 0.036661987849314175),
            (936965.0294756448, 0.03666384897948176),
            (965673.9250677227, 0.03659395926572422),
            (856800.3871573193, 0.03427201548629277),
            (866361.512358016, 0.0342736422471303),
            (866361.512358016, 0.0342736422471303),
            (876034.0929618194, 0.03427959494198424),
            (861774.2698191045, 0.03447097079276418),
            (871391.138518951, 0.03447261646888158),
            (871391.138518951, 0.03447261646888158),
            (881091.8400903088, 0.034477506786142524),
        ),
        "par": 0.036851668968366905,
        "annuity": 3.6978231530094314,
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
    "fra_fwd_payer_dual": {
        "pv": 101122.05482570204,
        "discounted_value": 101122.05482570204,
        "flows": (
            (103151.35255605118, 0.041080712848371254),
        ),
        "fair": 0.041080712848371254,
    },
    "fra_spot_receiver": {
        "pv": 3800.28081333695,
        "discounted_value": 3800.28081333695,
        "flows": (
            (3838.1377614573817, 0.039846474489541706),
        ),
        "fair": 0.039846474489541706,
    },
}


def _measure(instrument, curve_set) -> dict[str, object]:
    result = pv(instrument, curve_set)
    measured: dict[str, object] = {
        "pv": result.value,
        "discounted_value": discounted_value(instrument, curve_set),
        "flows": tuple((flow.amount, flow.rate) for flow in result.cashflows or ()),
    }
    if isinstance(instrument, FRA):
        measured["fair"] = fra_fair_rate(instrument, curve_set)
    else:
        measured["par"] = par_rate(instrument, curve_set).value
        measured["annuity"] = annuity(instrument, curve_set).value
    return measured


class TestSwapsThatStartOnOrAfterTheValuationDateDoNotMove:
    """Nothing here may change a trade with no history, not even in the last bit."""

    @pytest.mark.parametrize("name", sorted(PINNED))
    def test_every_measure_is_bit_identical_to_main(self, name):
        instrument, curve_set = _pinned_cases()[name]
        assert _measure(instrument, curve_set) == PINNED[name]

    def test_the_battery_covers_both_starts_and_every_linear_product(self):
        # A pin that only held swaps struck later than today would miss the
        # boundary the change sits on: a period starting exactly on as_of
        # has no history and must still be projected, not refused.
        cases = _pinned_cases()
        starts = {instrument.span[0] for instrument, _ in cases.values()}
        assert AS_OF in starts and any(start > AS_OF for start in starts)
        kinds = {type(instrument) for instrument, _ in cases.values()}
        assert kinds == {OISSwap, IRSwap, FRA}
