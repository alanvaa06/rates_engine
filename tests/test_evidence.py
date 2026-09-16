"""AC-9.7 and AC-9.8: evidence composes, and degradation travels upward.

The failure this file exists to catch is the cheap one: a result that
flattens its inputs' evidence into a dictionary, so that a proxy recorded at
the bottom of the chain is gone by the time anyone reads the top.
"""

from __future__ import annotations

import json

import pytest

from rates_engine.diagnostics import all_warnings, quality_report, walk
from rates_engine.evidence import DataQuality, Degradation, Evidence, Provenance


def _chain() -> tuple[Evidence, Evidence, Evidence]:
    """Bootstrap -> price -> hedge, with a proxy seeded at the deepest level."""
    bootstrap = Evidence(
        produced_by="curves.bootstrap_discount_curve",
        inputs=(
            Provenance("file", "SOFR", instrument_kind="overnight_fixing"),
            Provenance(
                "file",
                "DGS2",
                instrument_kind="treasury_par_yield",
                data_quality=DataQuality.PROXY,
            ),
        ),
        fields={"interpolation": "log_linear_df"},
        warnings=(
            Degradation("treasury_par_proxy", "long end rests on Treasuries", DataQuality.PROXY),
        ),
    )
    price = Evidence(produced_by="pricing.pv", sources=(bootstrap,))
    hedge = Evidence(produced_by="hedging.strip_hedge", sources=(price,))
    return bootstrap, price, hedge


class TestComposition:
    """AC-9.7: the chain is nested, not flattened, and serialises whole."""

    def test_sources_are_evidence_not_dictionaries(self):
        _, price, hedge = _chain()
        assert hedge.sources == (price,)
        assert isinstance(hedge.sources[0].sources[0], Evidence)

    def test_serialisation_reaches_the_bottom_of_the_chain(self):
        _, _, hedge = _chain()
        payload = hedge.to_dict()
        deepest = payload["sources"][0]["sources"][0]
        assert deepest["produced_by"] == "curves.bootstrap_discount_curve"
        assert deepest["inputs"][1]["series_id"] == "DGS2"

    def test_the_whole_chain_is_json_serialisable(self):
        _, _, hedge = _chain()
        assert json.loads(json.dumps(hedge.to_dict()))["produced_by"] == "hedging.strip_hedge"

    def test_walk_visits_every_node_with_its_depth(self):
        bootstrap, price, hedge = _chain()
        assert walk(hedge) == [(0, hedge), (1, price), (2, bootstrap)]

    def test_with_sources_does_not_mutate(self):
        bootstrap, price, _ = _chain()
        extended = price.with_sources(bootstrap)
        assert len(price.sources) == 1
        assert len(extended.sources) == 2

    def test_evidence_fields_are_read_only(self):
        evidence = Evidence("x", fields={"a": 1})
        with pytest.raises(TypeError):
            evidence.fields["a"] = 2  # type: ignore[index]


class TestPropagation:
    """AC-9.8: a degradation at the bottom is visible at the top."""

    def test_worst_quality_climbs_the_chain(self):
        bootstrap, price, hedge = _chain()
        assert bootstrap.worst_quality is DataQuality.PROXY
        assert price.worst_quality is DataQuality.PROXY
        assert hedge.worst_quality is DataQuality.PROXY

    def test_quality_set_is_the_recursive_union(self):
        _, _, hedge = _chain()
        assert hedge.qualities == frozenset({DataQuality.OBSERVED, DataQuality.PROXY})

    def test_top_level_payload_states_the_quality(self):
        _, _, hedge = _chain()
        assert hedge.to_dict()["data_quality"] == "proxy"

    def test_warnings_surface_from_anywhere_in_the_chain(self):
        _, _, hedge = _chain()
        assert [w.code for w in all_warnings(hedge)] == ["treasury_par_proxy"]

    def test_clean_chain_reports_observed(self):
        clean = Evidence("b", inputs=(Provenance("file", "SOFR"),))
        assert Evidence("a", sources=(clean,)).worst_quality is DataQuality.OBSERVED

    def test_quality_report_names_the_step_that_degraded(self):
        _, _, hedge = _chain()
        report = quality_report(hedge)
        assert report["worst_quality"] == "proxy"
        assert report["depth"] == 3
        assert report["degraded_steps"] == ["curves.bootstrap_discount_curve"]

    @pytest.mark.parametrize(
        "worse,better",
        [
            (DataQuality.ASSUMED, DataQuality.PROXY),
            (DataQuality.PROXY, DataQuality.SYNTHETIC),
            (DataQuality.SYNTHETIC, DataQuality.OBSERVED),
        ],
    )
    def test_the_rank_runs_from_known_to_unchecked(self, worse, better):
        assert worse.rank > better.rank

    def test_assumed_beats_proxy_when_both_are_present(self):
        mixed = Evidence(
            "x",
            inputs=(
                Provenance("file", "DGS2", data_quality=DataQuality.PROXY),
                Provenance("assumed", "sr1_notional", data_quality=DataQuality.ASSUMED),
            ),
        )
        assert mixed.worst_quality is DataQuality.ASSUMED
