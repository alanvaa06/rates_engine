"""AC-8.3 and AC-8.4: the shock table, and the refusal when the strip has a hole."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rates_engine.curves import FuturesNode
from rates_engine.errors import IncompleteStripError
from rates_engine.hedging import SR3_DV01, shock_table, strip_hedge
from rates_engine.risk import money_convexity


class TestSizing:
    """The hedge is first-order neutral, in the basis it was sized in."""

    def test_the_contracts_offset_the_bucketed_deltas(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert hedge.hedge_ratio == pytest.approx(-1.0, abs=1e-6)

    def test_bucket_sum_equals_the_quote_parallel_dv01(self, par_swap, strip, as_of):
        # A sum of partial derivatives is the derivative along the diagonal.
        # Computed by two independent routes, so a sign slip in either fails.
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert sum(hedge.bucketed_delta_by_instrument.values()) == pytest.approx(
            hedge.quote_parallel_dv01, rel=1e-6
        )

    def test_a_payer_swap_is_hedged_long(self, par_swap, strip, as_of):
        # A payer gains when rates rise; a long futures position loses. The
        # signs have to come out this way round or the hedge doubles the risk.
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert hedge.swap_dv01 < 0
        assert hedge.total_contracts > 0
        assert all(count > 0 for count in hedge.contracts.values())

    def test_a_receiver_swap_is_hedged_short(self, par_swap, strip, as_of):
        from rates_engine.instruments import Side

        hedge = strip_hedge(replace(par_swap, side=Side.RECEIVER), strip, as_of=as_of)
        assert hedge.total_contracts < 0

    def test_the_contract_dv01_is_twenty_five(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert hedge.contract_dv01 == pytest.approx(25.0)
        assert SR3_DV01 == pytest.approx(25.0)

    def test_the_total_is_about_the_dv01_over_the_tick(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert hedge.total_contracts == pytest.approx(
            abs(hedge.quote_parallel_dv01) / 25.0, rel=1e-6
        )

    def test_the_bump_basis_is_named_as_the_quote_not_a_node(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        payload = hedge.to_dict()
        assert payload["bump_basis"] == "instrument_quote"
        assert "key-rate" in payload["evidence"]["fields"]["note"]

    def test_the_two_dv01_bases_are_reported_and_not_reconciled_away(
        self, par_swap, strip, as_of
    ):
        payload = strip_hedge(par_swap, strip, as_of=as_of).to_dict()
        assert payload["swap_dv01_bump_basis"] == "zero_curve_parallel"
        assert payload["quote_parallel_dv01"] != payload["swap_dv01"]
        assert "different derivatives" in payload["node_vs_instrument_note"]


class TestIncompleteStrip:
    """AC-8.4: a gap is named, never extrapolated over."""

    def test_a_strip_that_stops_short_refuses(self, par_swap, strip, as_of):
        short = tuple(i for i in strip if not isinstance(i, FuturesNode) or i.label != "SR3-9")
        with pytest.raises(IncompleteStripError, match="runs to"):
            strip_hedge(par_swap, short, as_of=as_of)

    def test_a_strip_that_starts_late_refuses(self, par_swap, strip, as_of):
        late = tuple(i for i in strip if not isinstance(i, FuturesNode) or i.label != "SR3-1")
        with pytest.raises(IncompleteStripError, match="starts at"):
            strip_hedge(par_swap, late, as_of=as_of)

    def test_the_refusal_names_the_period(self, par_swap, strip, as_of):
        short = tuple(i for i in strip if not isinstance(i, FuturesNode) or i.label != "SR3-9")
        with pytest.raises(IncompleteStripError) as caught:
            strip_hedge(par_swap, short, as_of=as_of)
        assert par_swap.maturity.isoformat() in str(caught.value)

    def test_the_check_can_be_waived_explicitly(self, par_swap, strip, as_of):
        short = tuple(i for i in strip if not isinstance(i, FuturesNode) or i.label != "SR3-9")
        hedge = strip_hedge(par_swap, short, as_of=as_of, require_full_coverage=False)
        assert len(hedge.contracts) == 8

    def test_no_futures_at_all_refuses(self, par_swap, strip, as_of):
        with pytest.raises(ValueError, match="FuturesNode"):
            strip_hedge(
                par_swap,
                tuple(i for i in strip if not isinstance(i, FuturesNode)),
                as_of=as_of,
            )


class TestShockTable:
    """AC-8.3: every column the table promises, and the residual behaving."""

    @pytest.fixture
    def table(self, par_swap, strip, as_of):
        return shock_table(strip_hedge(par_swap, strip, as_of=as_of))

    def test_every_column_is_present(self, table):
        assert list(table.table.columns) == [
            "shock_bp",
            "swap_pnl",
            "strip_pnl",
            "net_pnl",
            "swap_dv01_post_shock",
            "net_per_dv01_bp",
            "implied_position_money_convexity",
        ]

    def test_one_row_per_shock(self, table):
        assert len(table.table) == len(table.shocks_bp) == 8

    def test_net_is_swap_plus_strip(self, table):
        rows = table.table
        assert (rows.net_pnl - (rows.swap_pnl + rows.strip_pnl)).abs().max() < 1e-6

    def test_the_residual_is_second_order_not_first(self, table):
        # A first-order leak would make the +10 and -10 residuals differ in
        # sign or size; pure curvature makes them agree.
        rows = table.table.set_index("shock_bp")
        assert rows.loc[10.0, "net_pnl"] == pytest.approx(rows.loc[-10.0, "net_pnl"], rel=0.02)

    def test_the_residual_scales_quadratically(self, table):
        rows = table.table.set_index("shock_bp")
        ratio = rows.loc[100.0, "net_pnl"] / rows.loc[10.0, "net_pnl"]
        assert ratio == pytest.approx(100.0, rel=0.05)

    def test_the_implied_convexity_is_nearly_constant_across_the_grid(self, table):
        implied = table.table.implied_position_money_convexity
        assert abs(implied.max() - implied.min()) / abs(implied.mean()) < 0.05

    def test_post_shock_dv01_moves_the_right_way(self, table):
        rows = table.table.sort_values("shock_bp")
        # A payer's DV01 is negative; rates rising shrinks its magnitude.
        assert rows.swap_dv01_post_shock.is_monotonic_increasing

    def test_net_per_dv01_is_in_basis_points(self, table):
        rows = table.table.set_index("shock_bp")
        expected = rows.loc[100.0, "net_pnl"] / abs(rows.loc[100.0, "swap_dv01_post_shock"])
        assert rows.loc[100.0, "net_per_dv01_bp"] == pytest.approx(expected)

    def test_a_custom_shock_grid_is_honoured(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        table = shock_table(hedge, (-5.0, 5.0))
        assert list(table.table.shock_bp) == [-5.0, 5.0]

    def test_the_evidence_chains_back_to_the_bootstrap(self, table):
        chain = table.evidence.sources[0].sources[0]
        assert chain.produced_by == "curves.bootstrap_discount_curve"

    def test_the_strip_pnl_basis_is_declared(self, table):
        assert "own forward move" in table.evidence.fields["strip_pnl_basis"]


class TestConvexityReconciliation:
    """AC-10.10: DV01 and money convexity reproduce the full reprice."""

    def test_the_swap_pnl_is_dv01_plus_half_convexity(self, par_swap, strip, curve_set, as_of):
        from rates_engine.pricing import dv01

        table = shock_table(strip_hedge(par_swap, strip, as_of=as_of)).table
        first_order = dv01(par_swap, curve_set).value
        second_order = money_convexity(par_swap, curve_set).value
        for _, row in table.iterrows():
            predicted = -first_order * row.shock_bp + 0.5 * second_order * (
                row.shock_bp * 1e-4
            ) ** 2
            tolerance = 0.01 if abs(row.shock_bp) <= 10 else 0.10
            assert predicted == pytest.approx(row.swap_pnl, rel=tolerance)

    def test_it_is_far_tighter_than_the_tolerance_at_small_shocks(
        self, par_swap, strip, curve_set, as_of
    ):
        from rates_engine.pricing import dv01

        table = shock_table(strip_hedge(par_swap, strip, as_of=as_of), (10.0,)).table
        row = table.iloc[0]
        predicted = -dv01(par_swap, curve_set).value * 10.0 + 0.5 * money_convexity(
            par_swap, curve_set
        ).value * (10.0 * 1e-4) ** 2
        assert predicted == pytest.approx(row.swap_pnl, rel=1e-5)
