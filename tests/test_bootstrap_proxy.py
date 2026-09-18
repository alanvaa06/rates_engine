"""PRD-001 AC-3.6, 3.7 and 3.8: the Treasury proxy is opt-in, labelled, and barred from goldens.

Decision B2 accepted the swap spread a Treasury par yield carries. These are
the three things that keep that decision from quietly spreading: it cannot be
used by accident, it marks everything it touches, and it may not appear under
a number this package claims to have validated against someone else's.
"""

from __future__ import annotations

from datetime import date

import pytest

from rates_engine.curves import ParSwapNode, bootstrap_discount_curve
from rates_engine.curves.bootstrap import TREASURY_PROXY
from rates_engine.errors import ProxySourceNotDeclaredError
from rates_engine.evidence import DataQuality, Provenance
from rates_engine.market.classify import TREASURY_PAR_YIELD, TREASURY_PROXY_NOTE


def _treasury_node(as_of: date, label: str = "DGS2", rate: float = 0.0405) -> ParSwapNode:
    return ParSwapNode(
        start=as_of,
        payment_dates=(date(2027, 1, 15), date(2028, 1, 17)),
        year_fractions=(1.0139, 1.0167),
        quoted_rate=rate,
        label=label,
        provenance=Provenance(
            source="file",
            series_id=label,
            instrument_kind=TREASURY_PAR_YIELD,
            data_quality=DataQuality.PROXY,
            notes=TREASURY_PROXY_NOTE,
        ),
    )


class TestOptIn:
    """PRD-001 AC-3.6: no default admits a proxy."""

    def test_undeclared_proxy_refuses_and_names_the_instrument(self, as_of):
        with pytest.raises(ProxySourceNotDeclaredError, match="DGS2"):
            bootstrap_discount_curve(as_of, (_treasury_node(as_of),))

    def test_the_refusal_names_the_flag_that_would_allow_it(self, as_of):
        with pytest.raises(ProxySourceNotDeclaredError, match="treasury_proxy"):
            bootstrap_discount_curve(as_of, (_treasury_node(as_of),))

    def test_the_refusal_says_there_is_no_default(self, as_of):
        with pytest.raises(ProxySourceNotDeclaredError, match="no default"):
            bootstrap_discount_curve(as_of, (_treasury_node(as_of),))

    def test_declaring_it_lets_it_through(self, as_of):
        result = bootstrap_discount_curve(
            as_of, (_treasury_node(as_of),), long_end_source=TREASURY_PROXY
        )
        assert result.long_end_source == TREASURY_PROXY

    def test_an_unrecognised_source_refuses(self, as_of, strip):
        with pytest.raises(ProxySourceNotDeclaredError, match="not recognised"):
            bootstrap_discount_curve(as_of, strip, long_end_source="vibes")

    def test_a_clean_curve_needs_no_declaration(self, flat_curve):
        assert flat_curve.long_end_source is None
        assert not flat_curve.uses_proxy


class TestLabelling:
    """PRD-001 AC-3.7: the nodes, the warning and the payload all say so."""

    @pytest.fixture
    def proxied(self, as_of, strip):
        futures_only = tuple(i for i in strip if i.provenance.data_quality is DataQuality.OBSERVED)
        return bootstrap_discount_curve(
            as_of,
            futures_only + (_treasury_node(as_of),),
            long_end_source=TREASURY_PROXY,
        )

    def test_the_affected_node_is_marked(self, proxied):
        assert DataQuality.PROXY in proxied.node_quality
        assert proxied.node_quality.count(DataQuality.PROXY) == 1
        assert proxied.uses_proxy

    def test_the_clean_nodes_are_not_marked(self, proxied):
        assert proxied.node_quality.count(DataQuality.OBSERVED) == len(proxied.node_quality) - 1

    def test_a_warning_names_the_swap_spread_and_the_dates(self, proxied):
        warnings = proxied.evidence.warnings
        assert len(warnings) == 1
        assert warnings[0].code == "treasury_par_proxy"
        assert "swap spread" in warnings[0].message
        assert "not quantified" in warnings[0].message
        assert "2028-01-17" in warnings[0].message

    def test_the_payload_exposes_the_source_at_the_top_level(self, proxied):
        payload = proxied.to_dict()
        assert payload["long_end_source"] == TREASURY_PROXY
        assert payload["node_quality"].count("proxy") == 1

    def test_the_evidence_quality_is_proxy(self, proxied):
        assert proxied.evidence.worst_quality is DataQuality.PROXY

    def test_it_propagates_into_a_price_built_on_the_curve(self, proxied, strip_span):
        from rates_engine.curves import CurveSet
        from rates_engine.instruments import OISSwap
        from rates_engine.pricing import pv

        swap = OISSwap(
            effective=strip_span[0], maturity=date(2028, 1, 17), fixed_rate=0.04,
            notional=1_000_000.0,
        )
        price = pv(swap, CurveSet(proxied.curve), source_evidence=(proxied.evidence,))
        assert price.evidence.worst_quality is DataQuality.PROXY
        assert price.to_dict()["evidence"]["data_quality"] == "proxy"

    def test_a_clean_curve_carries_none_of_the_three_marks(self, flat_curve):
        payload = flat_curve.to_dict()
        assert payload["long_end_source"] is None
        assert "proxy" not in payload["node_quality"]
        assert flat_curve.evidence.warnings == ()


class TestGoldensStayClean:
    """PRD-001 AC-3.8: no published number is validated against a proxied curve."""

    def test_the_suite_s_shared_curve_has_no_proxy_node(self, flat_curve):
        assert DataQuality.PROXY not in flat_curve.node_quality

    def test_golden_tests_assert_their_curve_is_clean(self):
        # Read the golden module rather than trusting a convention: the point
        # is that a future edit which introduces a proxy there gets caught.
        from pathlib import Path

        source = (Path(__file__).parent / "test_golden_cme.py").read_text(encoding="utf-8")
        assert "assert_no_proxy" in source, (
            "test_golden_cme.py must assert its curve is proxy-free (PRD-001 AC-3.8)"
        )
