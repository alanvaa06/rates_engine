"""Regressions for the independent review of the index-bearing-instruments work.

Each test reproduces a defect the review found and confirmed, on the case it
was found with, and fails on the code before its fix.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from rates_engine.conventions.indices import SOFR, TERM_SOFR_3M, TIIE_FONDEO
from rates_engine.core.errors import (
    ConfigurationError,
    CurrencyMismatchError,
    UnsupportedConventionError,
)
from rates_engine.core.evidence import DataQuality
from rates_engine.core.money import Currency
from rates_engine.curves.bootstrap import FuturesNode, RealizedStubNode, bootstrap_discount_curve
from rates_engine.curves.discount import CurveSet, DiscountCurve
from rates_engine.instruments.capfloor import CapFloor
from rates_engine.instruments.fra import FRA
from rates_engine.instruments.swaps import IRSwap, OISSwap
from rates_engine.pricing.calibration import SwapQuoteNode
from rates_engine.pricing.linear import par_rate

MONTH_END = date(2026, 3, 31)
QUOTES = {1: 0.0400, 2: 0.0410, 3: 0.0418, 4: 0.0425, 5: 0.0430}


def _month_end_nodes(**swap_terms) -> tuple[SwapQuoteNode, ...]:
    return tuple(
        SwapQuoteNode(
            OISSwap(
                effective=MONTH_END,
                maturity=date(MONTH_END.year + years, 3, 31),
                fixed_rate=quote,
                payment_lag_days=0,
                **swap_terms,
            ),
            quote,
        )
        for years, quote in QUOTES.items()
    )


class TestTheNodeDate:
    def test_a_payment_rolled_back_before_maturity_still_fits(self):
        """Modified following rolls 2029-03-31 (a Saturday) back to the 29th,
        while the floating leg still reads the discount factor at the 31st.
        The node pinned the 29th and the three-year fit came undone."""
        nodes = _month_end_nodes()
        three_year = nodes[2]
        assert three_year.swap.schedule.payment[-1] < three_year.swap.maturity
        assert three_year.node_date == three_year.swap.maturity
        curve = CurveSet(bootstrap_discount_curve(MONTH_END, nodes).curve)
        for node in nodes:
            assert par_rate(node.swap, curve).value == pytest.approx(node.quoted_rate, abs=1e-12)


class TestResidualsAreNotLostToSharedLabels:
    def test_unlabelled_swap_quotes_get_distinct_labels(self):
        labels = [node.label for node in _month_end_nodes()]
        assert len(set(labels)) == len(labels)

    def test_unlabelled_futures_each_keep_their_residual(self):
        """Two FuturesNodes without a label are both "future". The residual
        dict kept only the last, and the strict check read that one for all."""
        as_of = date(2026, 1, 15)
        start = date(2026, 3, 18)
        nodes = [RealizedStubNode(end=start, accrual_factor=1.0 + 0.04 * 62 / 360)]
        period = start
        for _ in range(3):
            following = period + timedelta(days=91)
            nodes.append(FuturesNode(start=period, end=following, forward_rate=0.04))
            period = following
        result = bootstrap_discount_curve(as_of, tuple(nodes))
        assert len(result.residuals_bp) == len(nodes)
        assert sum(key.startswith("future@") for key in result.residuals_bp) == 3


class TestCurrencyEverywhere:
    def test_a_dollar_cap_on_a_peso_curve_refuses(self):
        nodes = tuple(date(2026, 1, 15) + timedelta(days=365 * k) for k in range(1, 6))
        peso = DiscountCurve(
            date(2026, 1, 15), nodes, tuple(math.exp(-0.09 * k) for k in range(1, 6)),
            currency=Currency.MXN,
        )
        cap = CapFloor(effective=date(2026, 4, 15), maturity=date(2028, 4, 15), strike=0.05)
        from rates_engine.pricing.options import cap_floor_pv
        from rates_engine.volatility.units import Volatility, VolUnits

        with pytest.raises(CurrencyMismatchError):
            cap_floor_pv(cap, CurveSet(peso), Volatility(90.0, VolUnits.NORMAL_BP))

    def test_the_cli_will_not_build_a_peso_curve_from_dollar_quotes(self):
        from rates_engine.app.commands import price

        config = {
            "as_of": "2026-01-15",
            "curve": {"stub": {"end": "2026-03-18", "accrual_factor": 1.0069}},
            "swap": {
                "effective": "2026-03-18", "maturity": "2027-03-18",
                "fixed_rate": 0.09, "index": "TIIE_FONDEO",
            },
        }
        with pytest.raises(ConfigurationError, match="SOFR instruments"):
            price(config)


class TestAPesoCurveSaysWhatItAssumes:
    def test_a_curve_fitted_to_tiie_swaps_carries_their_assumptions(self):
        result = bootstrap_discount_curve(
            MONTH_END, _month_end_nodes(index=TIIE_FONDEO), currency=Currency.MXN
        )
        codes = {w.code for w in result.evidence.warnings}
        for name in TIIE_FONDEO.unresolved:
            assert f"unresolved_convention:{name}" in codes
        assert result.evidence.worst_quality is DataQuality.ASSUMED
        assert {d.code for d in result.curve.provenance} == codes

    def test_a_dollar_curve_carries_nothing_new(self):
        result = bootstrap_discount_curve(MONTH_END, _month_end_nodes())
        assert result.evidence.warnings == ()
        assert result.curve.provenance == ()


class TestTermsAreChecked:
    def test_a_swap_quote_node_takes_only_an_ois(self):
        irs = IRSwap(effective=MONTH_END, maturity=date(2028, 3, 31), fixed_rate=0.04)
        with pytest.raises(UnsupportedConventionError, match="dual"):
            SwapQuoteNode(irs, 0.04)  # type: ignore[arg-type]

    def test_a_fra_on_an_overnight_rate_is_refused(self):
        with pytest.raises(UnsupportedConventionError, match="overnight"):
            FRA(start=date(2026, 3, 18), end=date(2026, 6, 18), rate=0.04, index=SOFR)

    def test_a_tenor_no_term_sofr_is_published_for_is_refused(self):
        """Documented break: v0.3 accepted a two-month IRS and labelled it
        term_sofr_2m, an index that does not exist."""
        with pytest.raises(UnsupportedConventionError, match="2 months"):
            IRSwap(effective=MONTH_END, maturity=date(2028, 3, 31), fixed_rate=0.04,
                   float_frequency_months=2)


class TestOneTradeOneValue:
    def test_three_spellings_of_one_swap_are_equal(self):
        terms = {"effective": MONTH_END, "maturity": date(2028, 3, 31), "fixed_rate": 0.04}
        spellings = [
            IRSwap(**terms),
            IRSwap(**terms, float_frequency_months=3),
            IRSwap(**terms, index=TERM_SOFR_3M),
        ]
        assert spellings[0] == spellings[1] == spellings[2]
        assert len({hash(s) for s in spellings}) == 1

    def test_the_same_holds_for_a_cap(self):
        terms = {"effective": MONTH_END, "maturity": date(2028, 3, 31), "strike": 0.04}
        assert CapFloor(**terms) == CapFloor(**terms, frequency_months=3)
        assert CapFloor(**terms) == CapFloor(**terms, index=TERM_SOFR_3M)
