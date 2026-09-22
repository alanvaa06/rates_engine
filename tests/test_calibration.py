"""A curve calibrated to real swaps reprices them with the pricer that values them.

ARCHITECTURE.md 8.1, option a: the curve layer still knows no products, and
the calibration node that wraps a swap lives in the pricing layer, so the
residual the bootstrap drives to zero is computed by
:func:`rates_engine.pricing.linear.par_rate_value` itself.
"""

from __future__ import annotations

from datetime import date

import pytest

from rates_engine.conventions.daycount import DayCount, year_fraction
from rates_engine.conventions.indices import TIIE_FONDEO
from rates_engine.core.errors import CurrencyMismatchError, ProxySourceNotDeclaredError
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.core.money import Currency
from rates_engine.curves.bootstrap import ParSwapNode, bootstrap_discount_curve
from rates_engine.curves.discount import CurveSet
from rates_engine.instruments.swaps import OISSwap
from rates_engine.pricing.calibration import SwapQuoteNode
from rates_engine.pricing.linear import par_rate, pv

AS_OF = date(2026, 1, 15)
QUOTES = {1: 0.0400, 2: 0.0410, 3: 0.0418, 4: 0.0425, 5: 0.0430}


def _swap(years: int, *, lag: int = 2, index=None, as_of: date = AS_OF) -> OISSwap:
    kwargs = {"index": index} if index is not None else {}
    return OISSwap(
        effective=as_of,
        maturity=date(as_of.year + years, as_of.month, as_of.day),
        fixed_rate=QUOTES[years],
        notional=100_000_000.0,
        payment_lag_days=lag,
        **kwargs,
    )


def _nodes(
    *, lag: int = 2, index=None, provenance=None, as_of: date = AS_OF, years=tuple(QUOTES)
) -> tuple[SwapQuoteNode, ...]:
    extra = {"provenance": provenance} if provenance is not None else {}
    return tuple(
        SwapQuoteNode(
            _swap(tenor, lag=lag, index=index, as_of=as_of),
            QUOTES[tenor],
            label=f"ois_{tenor}y",
            **extra,
        )
        for tenor in years
    )


def _date_nodes(as_of: date, years=tuple(QUOTES)) -> tuple[ParSwapNode, ...]:
    """The same quotes as ``ParSwapNode``s, built the way a caller would:
    the swap's payment dates and accruals, float leg assumed to telescope."""
    nodes = []
    for tenor in years:
        quote = QUOTES[tenor]
        schedule = _swap(tenor, lag=0, as_of=as_of).schedule
        nodes.append(
            ParSwapNode(
                start=as_of,
                payment_dates=schedule.payment,
                year_fractions=tuple(
                    year_fraction(s, e, DayCount.ACT_360)
                    for s, e in zip(schedule.accrual_start, schedule.accrual_end, strict=True)
                ),
                quoted_rate=quote,
                label=f"par_{tenor}y",
            )
        )
    return tuple(nodes)


def _date_with_no_rolls() -> date:
    """The first date from AS_OF whose next three anniversaries are all
    business days, so that no payment date is rolled away from its accrual end.

    Three, not five: an anniversary's weekday moves by one a year (two in a
    leap year), so six consecutive ones always include a weekend.
    """
    from datetime import timedelta

    from rates_engine.conventions.calendar import SIFMA_US

    day = AS_OF
    while day.day > 28 or not all(
        SIFMA_US.is_business_day(date(day.year + k, day.month, day.day)) for k in range(4)
    ):
        day += timedelta(days=1)
    return day


class TestTheCurveRepricesItsSwaps:
    def test_every_quote_is_reproduced_by_the_pricer(self):
        curve = CurveSet(bootstrap_discount_curve(AS_OF, _nodes()).curve)
        for years, quote in QUOTES.items():
            assert par_rate(_swap(years), curve).value == pytest.approx(quote, abs=1e-12)

    def test_a_swap_struck_at_its_quote_prices_at_zero(self):
        curve = CurveSet(bootstrap_discount_curve(AS_OF, _nodes()).curve)
        for years in QUOTES:
            assert abs(pv(_swap(years), curve).value) < 1e-6  # on 100m notional

    def test_the_payment_lag_is_calibrated_rather_than_ignored(self):
        """A lagged swap pays after its accrual ends. The node pins the last
        payment date, and the pricer reads the lag, so the fit holds."""
        node = _nodes()[0]
        assert node.node_date > node.swap.maturity

    def test_the_evidence_carries_the_swap(self):
        result = bootstrap_discount_curve(AS_OF, _nodes())
        used = result.evidence.fields["instruments_used"]
        assert used[0]["kind"] == "swap_quote"
        assert used[0]["swap"]["kind"] == "ois_swap"


class TestAgainstTheDateNode:
    def test_where_no_date_rolls_the_two_nodes_are_one_calculation(self):
        """With no payment lag and no rolled date, an OIS floating leg
        telescopes to P(start) - P(end), which is what ParSwapNode assumes.
        The two node types then describe one calculation and the curves agree."""
        as_of, years = _date_with_no_rolls(), (1, 2, 3)
        from_swaps = bootstrap_discount_curve(
            as_of, _nodes(lag=0, as_of=as_of, years=years)
        ).curve
        from_dates = bootstrap_discount_curve(as_of, _date_nodes(as_of, years)).curve
        assert from_swaps.nodes == from_dates.nodes
        for a, b in zip(from_swaps.dfs, from_dates.dfs, strict=True):
            assert a == pytest.approx(b, rel=1e-13)

    def test_where_a_date_rolls_only_the_swap_node_reprices_the_swap(self):
        """The duplication was not harmless. When an anniversary falls on a
        weekend the payment rolls past the accrual end, the floating leg no
        longer telescopes, and a curve fitted to ParSwapNodes misprices the
        very swaps whose quotes it was fitted to. SwapQuoteNode fits the
        pricer's own formula, so it does not."""
        from_swaps = CurveSet(bootstrap_discount_curve(AS_OF, _nodes(lag=0)).curve)
        from_dates = CurveSet(bootstrap_discount_curve(AS_OF, _date_nodes(AS_OF)).curve)
        worst_swap = max(
            abs(par_rate(_swap(y, lag=0), from_swaps).value - q) for y, q in QUOTES.items()
        )
        worst_date = max(
            abs(par_rate(_swap(y, lag=0), from_dates).value - q) for y, q in QUOTES.items()
        )
        assert worst_swap < 1e-12
        assert worst_date > 1e-6  # more than a hundredth of a basis point


class TestCurrencyAndProvenance:
    def test_a_peso_curve_calibrates_to_peso_swaps(self):
        result = bootstrap_discount_curve(
            AS_OF, _nodes(index=TIIE_FONDEO), currency=Currency.MXN
        )
        assert result.curve.currency is Currency.MXN

    def test_a_dollar_swap_cannot_calibrate_a_peso_curve(self):
        with pytest.raises(CurrencyMismatchError):
            bootstrap_discount_curve(AS_OF, _nodes(), currency=Currency.MXN)

    def test_a_proxy_quote_still_needs_declaring(self):
        proxy = Provenance(
            source="fred", instrument_kind="treasury_par_yield", data_quality=DataQuality.PROXY
        )
        with pytest.raises(ProxySourceNotDeclaredError):
            bootstrap_discount_curve(AS_OF, _nodes(provenance=proxy))
