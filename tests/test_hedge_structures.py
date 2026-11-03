"""PRD-003 AC-4.1 to PRD-003 AC-4.5: seven structures, a table, and no recommendation.

The criterion that shapes this file is PRD-003 AC-4.5: there is no "best structure"
function, and the test for that is a scan of the public surface. Everything
else follows from it — the comparator returns numbers and labelled
trade-off axes, and the ordering tests assert what the costs *are* rather
than which cost is right.

One honest finding is recorded here rather than tuned away. PRD-003 AC-4.2's
premium ordering — seagull ≤ spread ≤ collar ≤ OTM ≤ ATM — holds exactly
for the PRD's own example, a payable, at coherent strikes. It does not hold
universally: for a receivable at the same offsets the collar and the spread
swap places, because the peso's interest differential puts the forward far
above spot and the two wings are not symmetric around it. The ordering is a
property of a case, not a theorem, and the tests say so.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date

import pytest

from rates_engine.conventions.currency_pair import USDMXN
from rates_engine.conventions.option_kind import OptionKind
from rates_engine.core.errors import ImplausibleInputError
from rates_engine.hedging.fx_structures import (
    TRADE_OFF_FRAME,
    Exposure,
    ExposureDirection,
    StructureQuote,
    compare_structures,
    zero_cost_collar_strike,
)
from rates_engine.models import garman_kohlhagen as gk

SETTLEMENT = date(2026, 12, 15)
QUOTE = StructureQuote(
    spot=18.50, expiry=0.25, r_domestic=0.0950, r_foreign=0.0420, volatility=0.115
)
ORDERED_BY_COST = [
    "seagull",
    "option_spread",
    "collar",
    "protective_option_otm",
    "protective_option_atm",
]


def _exposure(direction=ExposureDirection.PAYABLE, amount=1_000_000.0) -> Exposure:
    return Exposure(amount, direction, SETTLEMENT, USDMXN)


@pytest.fixture
def payable():
    return compare_structures(_exposure(), QUOTE)


@pytest.fixture
def receivable():
    return compare_structures(_exposure(ExposureDirection.RECEIVABLE), QUOTE)


class TestTheTableHasWhatWasAskedFor:
    """PRD-003 AC-4.1."""

    def test_every_structure_is_present(self, payable):
        assert [s.name for s in payable.structures] == [
            "unhedged",
            "forward",
            "protective_option_atm",
            "protective_option_otm",
            "collar",
            "collar_zero_cost",
            "option_spread",
            "seagull",
        ]

    def test_each_row_reports_the_five_columns(self, payable):
        """Every column is present on every row. The two bounds are `None`
        where the structure leaves that side open, which
        `TestTheBoundsAreTheStructuresNotTheGrids` pins row by row."""
        for structure in payable.structures:
            payload = structure.payload_fields()
            for column in (
                "upfront_cost",
                "worst_case_rate",
                "best_case_rate",
                "upside_participation",
                "payoff_grid",
            ):
                assert column in payload
            for column in ("upfront_cost", "upside_participation", "payoff_grid"):
                assert payload[column] is not None

    def test_the_payoff_grid_matches_the_spot_grid(self, payable):
        for structure in payable.structures:
            assert len(structure.payoff_grid) == len(payable.spot_grid)
            assert structure.spot_grid == payable.spot_grid

    def test_the_grid_straddles_the_spot(self, payable):
        assert payable.spot_grid[0] < QUOTE.spot < payable.spot_grid[-1]
        assert len(payable.spot_grid) == 21

    def test_the_total_cost_scales_with_the_exposure(self):
        small = compare_structures(_exposure(amount=1.0), QUOTE)
        big = compare_structures(_exposure(amount=5_000_000.0), QUOTE)
        for one, many in zip(small.structures, big.structures, strict=True):
            assert many.total_cost == pytest.approx(one.total_cost * 5_000_000.0)

    def test_a_row_can_be_fetched_by_name(self, payable):
        assert payable.by_name("forward").name == "forward"
        with pytest.raises(KeyError, match="no structure named"):
            payable.by_name("straddle")


class TestTheBaselineRows:
    """Doing nothing and doing the simplest thing."""

    def test_unhedged_costs_nothing_and_keeps_everything(self, payable):
        row = payable.by_name("unhedged")
        assert row.upfront_cost == 0.0
        assert row.upside_participation == pytest.approx(1.0)

    def test_unhedged_is_the_widest_spread_of_outcomes(self, payable):
        """Over the grid, since the unhedged row has no bound on either side."""
        row = payable.by_name("unhedged")
        span = max(row.payoff_grid) - min(row.payoff_grid)
        for other in payable.structures:
            assert max(other.payoff_grid) - min(other.payoff_grid) <= span + 1e-9

    def test_the_forward_costs_nothing_and_fixes_everything(self, payable):
        row = payable.by_name("forward")
        assert row.upfront_cost == 0.0
        assert row.upside_participation == pytest.approx(0.0)
        assert row.worst_case_rate == pytest.approx(row.best_case_rate)

    def test_the_forward_fixes_at_the_outright(self, payable):
        assert payable.by_name("forward").worst_case_rate == pytest.approx(QUOTE.forward)

    def test_protection_costs_something(self, payable):
        for name in ("protective_option_atm", "protective_option_otm"):
            assert payable.by_name(name).upfront_cost > 0.0


class TestThePremiumOrdering:
    """PRD-003 AC-4.2, for the case it holds in, and honesty about the case it does not."""

    def test_the_ordering_holds_for_the_prds_own_example(self, payable):
        costs = [payable.by_name(name).upfront_cost for name in ORDERED_BY_COST]
        assert costs == sorted(costs), dict(zip(ORDERED_BY_COST, costs, strict=True))

    def test_each_step_is_a_real_gap_not_a_tie(self, payable):
        costs = [payable.by_name(name).upfront_cost for name in ORDERED_BY_COST]
        assert all(b - a > 1e-3 for a, b in zip(costs, costs[1:], strict=False))

    def test_it_is_not_a_theorem_and_the_receivable_shows_why(self, receivable):
        """At the same offsets a receivable swaps the collar and the spread.
        The peso's interest differential puts the forward well above spot, so
        the two wings are not symmetric around it. Recorded rather than tuned
        away: an ordering that only holds after the offsets are adjusted to
        make it hold is not a property of the structures."""
        costs = [receivable.by_name(name).upfront_cost for name in ORDERED_BY_COST]
        assert costs != sorted(costs)
        assert receivable.by_name("collar").upfront_cost < receivable.by_name(
            "option_spread"
        ).upfront_cost

    def test_the_most_protection_is_always_the_most_expensive(self, payable, receivable):
        """What *is* universal: full at-the-money protection costs the most."""
        for comparison in (payable, receivable):
            atm = comparison.by_name("protective_option_atm").upfront_cost
            assert all(
                s.upfront_cost <= atm + 1e-12
                for s in comparison.structures
                if s.name != "protective_option_atm"
            )

    def test_the_seagull_is_zero_cost_by_construction(self, payable, receivable):
        """Its sold wing funds the spread's *net* cost — the bought
        protection less the far leg already sold. Funding the bought leg
        alone sells the same premium twice and reports a credit: a table
        saying a treasurer is paid to hedge, which was a pricing error
        rather than a bargain."""
        for comparison in (payable, receivable):
            assert comparison.by_name("seagull").upfront_cost == pytest.approx(
                0.0, abs=1e-6
            )

    def test_the_seagull_buys_a_better_best_case_by_selling_its_tail(self, payable):
        """Both are zero cost, and the difference between them is the whole
        point of the structure. The seagull has already sold its far call,
        so it has less left to fund and its sold put sits further out of the
        money — a lower strike, a better best case. It pays for that by
        capping its own protection: above the sold call the exposure
        re-opens, so its worst case is *worse* than the collar's. Better
        best, worse worst, same zero premium."""
        seagull = payable.by_name("seagull")
        collar = payable.by_name("collar_zero_cost")
        assert seagull.legs[-1][1] < collar.legs[-1][1]
        assert seagull.best_case_rate < collar.best_case_rate
        # Re-opened above the sold call: no bound at all, where the collar has one.
        assert seagull.worst_case_rate is None
        assert collar.worst_case_rate is not None
        assert max(seagull.payoff_grid) > max(collar.payoff_grid)

    def test_ordering_by_cost_sorts_rather_than_ranks(self, payable):
        ordered = payable.ordered_by_cost()
        assert [s.upfront_cost for s in ordered] == sorted(s.upfront_cost for s in ordered)
        assert ordered[0].name == "seagull"


class TestTheZeroCostCollar:
    """PRD-003 AC-4.2's second half: net premium zero, with the strike solved."""

    def test_its_net_premium_is_zero(self, payable, receivable):
        for comparison in (payable, receivable):
            assert comparison.by_name("collar_zero_cost").upfront_cost == pytest.approx(
                0.0, abs=1e-6
            )

    def test_the_solved_strike_is_reported(self, payable):
        assert "collar_zero_cost_sold_strike" in payable.evidence.fields

    def test_the_solver_actually_equalises_the_two_premiums(self):
        protective, strike = OptionKind.CALL, QUOTE.forward * 1.03
        sold = zero_cost_collar_strike(QUOTE, strike, protective)
        assert QUOTE.price(strike, protective) == pytest.approx(
            QUOTE.price(sold, protective.opposite), abs=1e-10
        )

    @pytest.mark.parametrize("kind", list(OptionKind))
    def test_it_works_from_either_side(self, kind):
        strike = QUOTE.forward * (1.03 if kind is OptionKind.CALL else 0.97)
        sold = zero_cost_collar_strike(QUOTE, strike, kind)
        assert QUOTE.price(strike, kind) == pytest.approx(
            QUOTE.price(sold, kind.opposite), abs=1e-10
        )

    def test_protection_too_dear_for_any_sold_wing_refuses(self):
        """A sold call's premium is bounded by the discounted spot, so a
        deep in-the-money put can cost more than the entire call wing is
        worth. No zero-cost collar exists then, and saying so beats
        returning whichever extreme strike the bisection ran into.

        The mirror case is *not* symmetric: a sold put's premium grows
        without bound in its strike, so a deep in-the-money call can always
        be paid for — at an absurd strike, but genuinely."""
        with pytest.raises(ImplausibleInputError, match="no call strike"):
            zero_cost_collar_strike(QUOTE, QUOTE.forward * 3.0, OptionKind.PUT)

    def test_the_mirror_case_is_solvable_because_a_sold_put_is_unbounded(self):
        strike = QUOTE.forward * 0.5
        sold = zero_cost_collar_strike(QUOTE, strike, OptionKind.CALL)
        assert QUOTE.price(strike, OptionKind.CALL) == pytest.approx(
            QUOTE.price(sold, OptionKind.PUT), abs=1e-9
        )
        assert sold > QUOTE.forward

    def test_it_bounds_the_outcome_on_both_sides(self, payable):
        row = payable.by_name("collar_zero_cost")
        unhedged = payable.by_name("unhedged")
        assert row.worst_case_rate is not None and row.best_case_rate is not None
        assert unhedged.worst_case_rate is None and unhedged.best_case_rate is None
        assert row.worst_case_rate < max(unhedged.payoff_grid)
        assert row.best_case_rate > min(unhedged.payoff_grid)


class TestTheTradeOff:
    """More cost, more protection, less participation."""

    def test_more_expensive_protection_gives_a_better_worst_case(self, payable):
        atm = payable.by_name("protective_option_atm")
        otm = payable.by_name("protective_option_otm")
        assert atm.upfront_cost > otm.upfront_cost
        assert atm.worst_case_rate < otm.worst_case_rate

    def test_giving_up_upside_is_what_pays_for_the_collar(self, payable):
        otm = payable.by_name("protective_option_otm")
        collar = payable.by_name("collar")
        assert collar.upfront_cost < otm.upfront_cost
        assert collar.upside_participation < otm.upside_participation

    @staticmethod
    def _kept_by_slope(structure, direction, forward, width):
        """The share of the favourable move a row keeps, from its legs alone.

        Integrates the row's slope in the spot over the favourable move --
        from the forward down to `(1 - width) F` for a payable, up to
        `(1 + width) F` for a receivable -- with the strikes as breakpoints.
        The slope is the open share of the exposure plus or minus each leg
        in the money; nothing here reads the payoff grid or calls the code
        under test.
        """
        payable = direction is ExposureDirection.PAYABLE
        edge = forward * (1.0 - width) if payable else forward * (1.0 + width)
        low, high = min(edge, forward), max(edge, forward)
        helped = -1.0 if payable else 1.0

        def slope(s):
            value = 1.0 - abs(structure.forward_amount) / 1_000_000.0
            for kind, strike, quantity in structure.legs:
                if kind == "call" and s > strike:
                    value += helped * quantity
                if kind == "put" and s < strike:
                    value -= helped * quantity
            return value

        points = sorted({low, high, *(k for _, k, _ in structure.legs if low < k < high)})
        moved = sum(
            slope(0.5 * (a + b)) * (b - a) for a, b in zip(points, points[1:], strict=False)
        )
        # For a payable the favourable move is downwards: what is kept is
        # the fall in the rate, which is the integral of the slope.
        return moved / (high - low)

    @pytest.mark.parametrize("direction", list(ExposureDirection))
    def test_participation_is_the_ratio_it_claims_to_be(self, direction):
        """The share of a favourable move from the forward the structure keeps,
        worked out from each row's legs rather than from what the code
        computed.

        Two earlier definitions are pinned out by this. The first read the
        two ends of the grid and gave an at-the-money call on a payable,
        which keeps every peso of a fall, 0.533. The second measured from
        today's spot, and with USD/MXN carry putting the forward above the
        spot, a receivable's at-the-money put kept nothing of the rise until
        the forward: 0.7279 at one year (0.9333 at three months) where the
        payable's mirror-image call read 1.0.
        """
        comparison = compare_structures(_exposure(direction), QUOTE)
        for structure in comparison.structures:
            expected = self._kept_by_slope(structure, direction, QUOTE.forward, 0.20)
            assert structure.upside_participation == pytest.approx(expected, abs=1e-12), (
                structure.name
            )

    def test_mirror_image_structures_keep_the_same_share(self):
        """Alan Vazquez's case on PR #13: one year, 9.5%/4.2%."""
        quote = StructureQuote(18.5, 1.0, 0.095, 0.042, 0.115)
        payable = compare_structures(_exposure(), quote)
        receivable = compare_structures(_exposure(ExposureDirection.RECEIVABLE), quote)
        for name in ("unhedged", "forward", "protective_option_atm",
                     "protective_option_otm", "collar", "option_spread"):
            assert payable.by_name(name).upside_participation == pytest.approx(
                receivable.by_name(name).upside_participation, abs=1e-12
            ), name
        assert receivable.by_name("protective_option_atm").upside_participation == pytest.approx(1.0)

    def test_the_closed_forms(self, payable):
        """The collar sells its put 8% under the forward, and the move is 20%
        of the forward, so it keeps exactly 0.08 / 0.20 of it; the seagull
        keeps the stretch between the forward and its sold put."""
        got = {s.name: s.upside_participation for s in payable.structures}
        assert got["protective_option_atm"] == pytest.approx(1.0)
        assert got["option_spread"] == pytest.approx(1.0)
        assert got["collar"] == pytest.approx(0.40, abs=1e-12)
        sold_put = payable.by_name("seagull").legs[-1][1]
        assert got["seagull"] == pytest.approx(
            (QUOTE.forward - sold_put) / (0.20 * QUOTE.forward), abs=1e-12
        )
        assert got["seagull"] < got["collar"]

    def test_the_clamp_never_binds_on_these_structures(self, payable, receivable):
        """So the assertions above test the ratio and not the guard. If a
        structure ever needs the clamp, this fails and says so rather than
        hiding it behind a range check that always passes."""
        for comparison in (payable, receivable):
            direction = comparison.exposure.direction
            for structure in comparison.structures:
                ratio = self._kept_by_slope(structure, direction, QUOTE.forward, 0.20)
                assert -1e-12 <= ratio <= 1.0 + 1e-12, structure.name

    def test_the_unhedged_and_forward_rows_bracket_participation(self, payable):
        assert payable.by_name("unhedged").upside_participation == pytest.approx(1.0)
        assert payable.by_name("forward").upside_participation == pytest.approx(0.0)


class TestDirectionMatters:
    """Protection is a call for a payable and a put for a receivable."""

    def test_a_payable_is_protected_by_calls(self, payable):
        assert payable.evidence.fields["protective_kind"] == "call"
        assert payable.by_name("protective_option_atm").legs[0][0] == "call"

    def test_a_receivable_is_protected_by_puts(self, receivable):
        assert receivable.evidence.fields["protective_kind"] == "put"
        assert receivable.by_name("protective_option_atm").legs[0][0] == "put"

    def test_the_protective_strike_sits_the_right_way_from_the_forward(self):
        payable = compare_structures(_exposure(), QUOTE)
        receivable = compare_structures(_exposure(ExposureDirection.RECEIVABLE), QUOTE)
        assert payable.evidence.fields["otm_strike"] > QUOTE.forward
        assert receivable.evidence.fields["otm_strike"] < QUOTE.forward

    def test_a_negative_amount_refuses_rather_than_meaning_the_other_direction(self):
        with pytest.raises(ValueError, match="positive size"):
            Exposure(-1.0, ExposureDirection.PAYABLE, SETTLEMENT, USDMXN)


class TestTheHedgeRatio:
    """PRD-003 AC-4.3: a fraction, applied linearly."""

    def test_half_a_hedge_is_halfway_between(self):
        full = compare_structures(_exposure(), QUOTE, hedge_ratio=1.0).by_name("forward")
        none = compare_structures(_exposure(), QUOTE, hedge_ratio=0.0).by_name("forward")
        half = compare_structures(_exposure(), QUOTE, hedge_ratio=0.5).by_name("forward")
        for a, b, c in zip(none.payoff_grid, half.payoff_grid, full.payoff_grid, strict=True):
            assert b == pytest.approx(0.5 * (a + c))

    def test_a_zero_ratio_is_the_unhedged_row(self, payable):
        none = compare_structures(_exposure(), QUOTE, hedge_ratio=0.0)
        assert none.by_name("forward").payoff_grid == pytest.approx(
            payable.by_name("unhedged").payoff_grid
        )

    def test_the_ratio_is_reported(self):
        result = compare_structures(_exposure(), QUOTE, hedge_ratio=0.6)
        assert result.by_name("forward").hedge_ratio == 0.6

    @pytest.mark.parametrize("bad", [-0.1, 1.1])
    def test_a_ratio_outside_zero_to_one_refuses(self, bad):
        with pytest.raises(ValueError, match="fraction in"):
            compare_structures(_exposure(), QUOTE, hedge_ratio=bad)


class TestTheVarianceDecomposition:
    """PRD-003 AC-4.3's second half."""

    @pytest.fixture
    def decomposed(self):
        return compare_structures(
            _exposure(), QUOTE, correlation=-0.30, foreign_asset_volatility=0.10
        ).residual_variance

    def test_the_identity_holds(self, decomposed):
        assert decomposed["total_variance_unhedged"] == pytest.approx(
            decomposed["asset_variance"]
            + decomposed["currency_variance"]
            + decomposed["cross_term"]
        )

    def test_the_cross_term_carries_the_correlation(self, decomposed):
        expected = 2.0 * -0.30 * 0.10 * QUOTE.volatility
        assert decomposed["cross_term"] == pytest.approx(expected)

    def test_a_negative_correlation_reduces_the_total(self, decomposed):
        assert decomposed["total_variance_unhedged"] < (
            decomposed["asset_variance"] + decomposed["currency_variance"]
        )

    def test_a_full_hedge_leaves_the_asset_variance(self, decomposed):
        assert decomposed["residual_variance_at_hedge_ratio"] == pytest.approx(
            decomposed["asset_variance"]
        )

    def test_no_hedge_leaves_the_whole_thing(self):
        result = compare_structures(
            _exposure(), QUOTE, hedge_ratio=0.0, correlation=-0.30,
            foreign_asset_volatility=0.10,
        )
        assert result.residual_variance["residual_variance_at_hedge_ratio"] == pytest.approx(
            result.residual_variance["total_variance_unhedged"]
        )

    def test_the_volatilities_are_the_square_roots(self, decomposed):
        assert decomposed["total_volatility_unhedged"] == pytest.approx(
            math.sqrt(decomposed["total_variance_unhedged"])
        )

    @pytest.mark.parametrize("hedge_ratio", [0.2, 0.5, 0.8])
    def test_a_partial_hedge_leaves_the_variance_of_what_is_left(self, hedge_ratio):
        """The hedged return is the asset's plus the unhedged share of the
        currency's, `r_a + (1 - h) r_x`, so its variance is
        `s_a^2 + (1 - h)^2 s_x^2 + 2 (1 - h) rho s_a s_x` -- quadratic in h.
        The engine subtracted `h (s_x^2 + 2 rho s_a s_x)`, linear in h, which
        is right only at h = 0 and h = 1, the two points tested. At h = 0.8
        it gave 0.011265 against 0.009149 (a two-million-path Monte Carlo in
        `REVISION-rates_engine-2026-10-15-repro/risk-app/r04_fx_cli.py`
        gives 0.009143)."""
        result = compare_structures(
            _exposure(), QUOTE, hedge_ratio=hedge_ratio, correlation=-0.30,
            foreign_asset_volatility=0.10,
        ).residual_variance
        sa, sx, rho, left = 0.10, 0.115, -0.30, 1.0 - hedge_ratio
        closed_form = sa**2 + left**2 * sx**2 + 2.0 * left * rho * sa * sx
        assert result["residual_variance_at_hedge_ratio"] == pytest.approx(closed_form, rel=1e-12)
        assert result["residual_volatility_at_hedge_ratio"] == pytest.approx(
            math.sqrt(closed_form), rel=1e-12
        )

    def test_the_partial_hedge_matches_a_simulation(self):
        """The same number without the algebra: simulate the two correlated
        returns and measure the variance of what the hedge leaves."""
        import numpy as np

        h, sa, sx, rho = 0.8, 0.10, 0.115, -0.30
        z1, z2 = np.random.default_rng(20261015).standard_normal((2, 400_000))
        asset = sa * z1
        currency = sx * (rho * z1 + math.sqrt(1.0 - rho * rho) * z2)
        simulated = float(np.var(asset + (1.0 - h) * currency))
        result = compare_structures(
            _exposure(), QUOTE, hedge_ratio=h, correlation=rho, foreign_asset_volatility=sa
        ).residual_variance
        assert result["residual_variance_at_hedge_ratio"] == pytest.approx(simulated, rel=0.01)
        assert result["residual_variance_at_hedge_ratio"] == pytest.approx(0.009149, abs=5e-7)

    def test_it_is_absent_rather_than_assumed_when_not_asked_for(self, payable):
        assert payable.residual_variance is None

    def test_half_the_inputs_refuses(self):
        with pytest.raises(ValueError, match="both a correlation"):
            compare_structures(_exposure(), QUOTE, correlation=-0.30)
        with pytest.raises(ValueError, match="both a correlation"):
            compare_structures(_exposure(), QUOTE, foreign_asset_volatility=0.10)


class TestItDoesNotRecommend:
    """PRD-003 AC-4.4 and PRD-003 AC-4.5, which are the reason this module exists in this shape."""

    def test_no_public_name_contains_recommend(self):
        import rates_engine
        import rates_engine.hedging.fx_structures as module

        for namespace in (module, rates_engine):
            offenders = [
                name for name in dir(namespace) if "recommend" in name.lower()
            ]
            assert offenders == [], offenders

    def test_no_source_line_defines_one(self):
        from pathlib import Path

        import rates_engine

        root = Path(rates_engine.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith(("def ", "class ")) and "recommend" in line.lower():
                    offenders.append(f"{path.name}:{number}")
        assert offenders == [], offenders

    def test_the_payload_holds_no_free_text_beyond_the_labelled_frame(self, payable):
        """Every string in a structure row is a name or an enum value. The
        only prose in the whole payload is the trade-off statement, which is
        a labelled field describing the axes rather than choosing on them."""
        for structure in payable.structures:
            payload = structure.payload_fields()
            strings = [v for v in payload.values() if isinstance(v, str)]
            assert strings == [structure.name]

    def test_the_trade_off_is_labelled_axes_not_advice(self):
        assert "upfront_cost" in TRADE_OFF_FRAME["axes"]
        assert TRADE_OFF_FRAME["monotone"] == "no"
        assert "depends on the treasury policy" in TRADE_OFF_FRAME["statement"]

    def test_the_frame_claims_no_monotonicity_the_table_lacks(self, payable):
        """The frame used to assert that paying more buys a better worst
        case. On this very market the option spread costs 0.16 and has a
        worse worst case than the zero-cost collar, because its sold far
        wing reopens the tail. Shipping that claim as a labelled field in
        every payload was the thing AC-4.4 exists to prevent, so this test
        pins the counterexample rather than the claim."""
        spread = payable.by_name("option_spread")
        collar = payable.by_name("collar_zero_cost")
        assert spread.upfront_cost > collar.upfront_cost
        # Its sold far call re-opens the tail: no worst case at all.
        assert spread.worst_case_rate is None
        assert collar.worst_case_rate is not None
        assert max(spread.payoff_grid) > collar.worst_case_rate
        assert TRADE_OFF_FRAME["monotone"] == "no"

    def test_participation_is_not_monotone_in_cost_either(self, payable):
        ordered = payable.ordered_by_cost()
        participations = [s.upside_participation for s in ordered]
        assert participations != sorted(participations)
        assert participations != sorted(participations, reverse=True)

    def test_the_statement_names_only_the_two_endpoints_it_can_defend(self):
        """It may name the cheapest and dearest, because those two *are*
        universal and tested in both directions. It may not rank the rest."""
        sentence = TRADE_OFF_FRAME["statement"].lower()
        assert "seagull" in sentence and "at-the-money" in sentence
        for name in ("collar", "forward", "put", "call"):
            assert f"{name} is best" not in sentence
        assert "recommend" not in sentence

    def test_the_comparison_payload_carries_the_frame(self, payable):
        assert payable.payload_fields()["trade_off"] == TRADE_OFF_FRAME


class TestPricingOnASmile:
    """Each leg on its own point of the smile, which is what a collar needs."""

    def test_a_callable_volatility_is_used_per_strike(self):
        seen: list[float] = []

        def smile(strike: float) -> float:
            seen.append(strike)
            return 0.115 + 0.30 * (strike / 18.75 - 1.0)

        quote = StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)
        compare_structures(_exposure(), quote)
        assert len(set(round(k, 6) for k in seen)) > 1

    def test_a_skew_changes_the_collar_strike(self):
        flat = zero_cost_collar_strike(QUOTE, QUOTE.forward * 1.03, OptionKind.CALL)
        skewed = StructureQuote(
            18.50, 0.25, 0.0950, 0.0420, lambda k: 0.115 + 0.30 * (k / 18.75 - 1.0)
        )
        assert zero_cost_collar_strike(skewed, QUOTE.forward * 1.03, OptionKind.CALL) != flat

    def test_a_bare_callable_is_named_as_such_because_it_has_no_evidence(self):
        """The three sources are distinguished because only one of them can
        say where its number came from. A lambda prices fine and carries
        nothing, and the payload says so rather than calling it a smile."""
        quote = StructureQuote(18.50, 0.25, 0.0950, 0.0420, lambda k: 0.115 + 0.0 * k)
        result = compare_structures(_exposure(), quote)
        assert result.evidence.fields["volatility_source"] == "callable"
        assert result.evidence.fields["volatility"] is None

    def test_a_smile_passed_directly_is_named_and_its_evidence_kept(self):
        from rates_engine.core.evidence import DataQuality
        from rates_engine.models.fx_delta import DeltaBasis, DeltaConvention, PremiumAdjustment
        from rates_engine.volatility.fx_smile import ATMConvention, SmileQuotes, VannaVolgaSmile

        smile = VannaVolgaSmile(
            spot=18.50,
            expiry=0.25,
            r_domestic=0.0950,
            r_foreign=0.0420,
            quotes=SmileQuotes(atm=0.1150, risk_reversal_25=0.0180, butterfly_25=0.0035),
            delta_convention=DeltaConvention(
                DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED
            ),
            atm_convention=ATMConvention.DELTA_NEUTRAL_STRADDLE,
        )
        result = compare_structures(
            _exposure(),
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile),
            otm_offset=0.12,
            spread_offset=0.25,
        )
        assert result.evidence.fields["volatility_source"] == "smile"
        # Those strikes are outside the quoted pillars, so every reading
        # carries the smile's flat-wing Degradation — and it survives the
        # float() in vol_at, which used to throw it away.
        assert result.evidence.worst_quality is DataQuality.ASSUMED


class TestSerialisationAndEvidence:
    def test_the_comparison_is_json_serialisable(self, payable):
        json.dumps(payable.to_dict())

    def test_every_row_chains_into_the_comparison(self, payable):
        assert len(payable.evidence.sources) == len(payable.structures)

    def test_the_evidence_records_the_strikes_it_chose(self, payable):
        fields = payable.evidence.fields
        for key in ("atm_strike", "otm_strike", "far_strike", "collar_sold_strike"):
            assert fields[key] > 0.0

    def test_the_premium_is_carried_to_settlement(self, payable):
        row = payable.by_name("protective_option_atm")
        carried = row.evidence.fields["premium_carried_to_settlement"]
        assert carried == pytest.approx(
            row.upfront_cost * math.exp(QUOTE.r_domestic * QUOTE.expiry)
        )
        assert carried > row.upfront_cost

    def test_the_exposure_travels_with_the_table(self, payable):
        stored = payable.payload_fields()["exposure"]
        assert stored["direction"] == "payable"
        assert stored["pair"] == "USD/MXN"


class TestArgumentRefusals:
    def test_a_sold_spread_leg_inside_the_bought_one_refuses(self):
        with pytest.raises(ValueError, match="further out"):
            compare_structures(_exposure(), QUOTE, otm_offset=0.08, spread_offset=0.03)

    def test_a_non_positive_offset_refuses(self):
        with pytest.raises(ValueError, match="further out"):
            compare_structures(_exposure(), QUOTE, otm_offset=0.0)

    def test_a_non_positive_collar_offset_refuses(self):
        with pytest.raises(ValueError, match="collar_offset"):
            compare_structures(_exposure(), QUOTE, collar_offset=0.0)


class TestTheGrid:
    """Alan Vazquez's round-3 finding 2 on PR #13: the grid was never checked.

    Upside participation is measured over a move of ``grid_width`` times the
    forward, so the width became an input to a reported number. Before:
    ``grid_width=0`` (or negative) read 0 on every row, the unhedged one
    included; 1.2 evaluated the structures at a spot of -3.70; one point
    divided by zero. And the reported grid ran from 14.80 to 22.20 around
    the 18.50 spot while participation measured from the 18.7468 forward,
    so the table and the number beside it described different moves.
    """

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.0, 1.2, float("nan"), float("inf")])
    def test_a_width_outside_zero_to_one_refuses(self, bad):
        with pytest.raises(ValueError, match="grid_width"):
            compare_structures(_exposure(), QUOTE, grid_width=bad)

    @pytest.mark.parametrize("bad", [1, 0, -3, 2.5, True])
    def test_fewer_than_two_points_refuses(self, bad):
        with pytest.raises(ValueError, match="grid_points"):
            compare_structures(_exposure(), QUOTE, grid_points=bad)

    def test_the_grid_is_centred_on_the_forward(self, payable):
        forward = QUOTE.forward
        grid = payable.spot_grid
        assert grid[0] == pytest.approx(forward * 0.80, rel=1e-14)
        assert grid[10] == pytest.approx(forward, rel=1e-14)
        assert grid[-1] == pytest.approx(forward * 1.20, rel=1e-14)
        assert payable.evidence.fields["grid_centre"] == "forward"

    def test_two_points_are_the_two_ends(self):
        grid = compare_structures(_exposure(), QUOTE, grid_points=2, grid_width=0.1).spot_grid
        assert grid == pytest.approx((QUOTE.forward * 0.9, QUOTE.forward * 1.1), rel=1e-14)

    @pytest.mark.parametrize("direction", list(ExposureDirection))
    @pytest.mark.parametrize("width", [0.05, 0.20, 0.50])
    def test_participation_is_the_slope_of_the_grids_favourable_half(self, direction, width):
        """Read off the reported grid, not recomputed: from the middle point
        (the forward) to the favourable end, the change in the effective
        rate over the change in spot."""
        table = compare_structures(_exposure(direction), QUOTE, grid_width=width)
        grid, middle = table.spot_grid, len(table.spot_grid) // 2
        for row in table.structures:
            rates = row.payoff_grid
            if direction is ExposureDirection.PAYABLE:
                slope = (rates[middle] - rates[0]) / (grid[middle] - grid[0])
            else:
                slope = (rates[-1] - rates[middle]) / (grid[-1] - grid[middle])
            assert row.upside_participation == pytest.approx(
                max(0.0, min(1.0, slope)), abs=1e-12
            ), row.name


class TestTheUnderlyingPricesAreRight:
    """The comparator is arithmetic over Garman-Kohlhagen, so spot-check it."""

    def test_the_atm_protection_costs_what_the_pricer_says(self, payable):
        row = payable.by_name("protective_option_atm")
        _, strike, _ = row.legs[0]
        assert row.upfront_cost == pytest.approx(
            gk.price(18.50, strike, 0.25, 0.0950, 0.0420, 0.115, OptionKind.CALL)
        )

    def test_a_spread_costs_the_difference_of_its_legs(self, payable):
        row = payable.by_name("option_spread")
        bought, sold = row.legs
        assert row.upfront_cost == pytest.approx(
            QUOTE.price(bought[1], OptionKind.CALL) - QUOTE.price(sold[1], OptionKind.CALL)
        )


class TestTheBasisReachesTheStructures:
    """A cross-currency basis moves the forward every structure is struck on.

    The basis is a spread on the quote leg (`pricing.fx_forward.BASIS_LEG`):
    the peso rate between spot and delivery is `r_d + b`. That moves the
    outright exactly as `forward_from_curves` does, and the peso premium is
    carried on the same shifted rate. Before, `StructureQuote` had no basis
    at all and the CLI dropped the config's, so hedge-structures' forward row
    sat 1,941 pips from fx-forward at one year on -100 bp.
    """

    BASIS = -25.0

    @pytest.fixture
    def quote(self):
        return StructureQuote(18.50, 0.25, 0.0950, 0.0420, 0.115, basis_bp=self.BASIS)

    def test_the_forward_carries_the_basis(self, quote):
        assert quote.forward == pytest.approx(
            18.50 * math.exp((0.0950 - 0.0025 - 0.0420) * 0.25), rel=1e-14
        )

    def test_the_forward_row_locks_there(self, quote):
        row = compare_structures(_exposure(), quote).by_name("forward")
        assert row.worst_case_rate == pytest.approx(quote.forward)
        assert row.best_case_rate == pytest.approx(quote.forward)
        assert quote.forward < QUOTE.forward  # a negative basis lowers it

    def test_an_option_is_priced_on_the_shifted_peso_rate(self, quote):
        strike = 18.80
        assert quote.price(strike, OptionKind.CALL) == pytest.approx(
            gk.price(18.50, strike, 0.25, 0.0925, 0.0420, 0.115, OptionKind.CALL), rel=1e-14
        )

    def test_the_payload_states_the_basis_and_its_leg(self, quote):
        stored = quote.to_dict()
        assert stored["basis_bp"] == self.BASIS
        assert stored["basis_leg"] == "quote"
        assert stored["r_domestic"] == 0.0950
        assert stored["r_domestic_effective"] == pytest.approx(0.0925)

    def test_no_basis_changes_nothing(self):
        assert QUOTE.basis_bp == 0.0
        assert QUOTE.forward == gk.forward(18.50, 0.25, 0.0950, 0.0420)

    def test_an_implausible_basis_refuses(self):
        with pytest.raises(ImplausibleInputError, match="500 bp"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, 0.115, basis_bp=-900.0)


class TestTheBoundsAreTheStructuresNotTheGrids:
    """The review's finding 3.3: a bound was the edge of the grid.

    `worst_case_rate` and `best_case_rate` were the largest and smallest
    rates on the reported grid, so a structure with no bound reported the
    grid's edge -- a number that moved with `grid_width` -- and the promised
    `None` never appeared (`REVISION-rates_engine-2026-10-15-repro/risk-app/
    r04_fx_cli.py`: the unhedged payable's worst case was 22.20 on the
    default grid and 27.75 on a 50% one). A bound is now worked out from the
    legs over every spot, and `None` means that side is open: the rate keeps
    moving with the spot past the outermost strike.
    """

    def _carried(self, row):
        return row.upfront_cost * math.exp(QUOTE.r_domestic * QUOTE.expiry)

    def test_which_rows_are_open_on_which_side_for_a_payable(self, payable):
        open_sides = {
            s.name: (s.worst_case_rate is None, s.best_case_rate is None)
            for s in payable.structures
        }
        assert open_sides == {
            "unhedged": (True, True),
            "forward": (False, False),
            "protective_option_atm": (False, True),
            "protective_option_otm": (False, True),
            "collar": (False, False),
            "collar_zero_cost": (False, False),
            "option_spread": (True, True),
            "seagull": (True, False),
        }

    def test_a_protective_call_caps_a_payable_at_strike_plus_premium(self, payable):
        """Above the strike the call pays the excess, so the payable never
        costs more than the strike plus the premium carried to settlement."""
        for name in ("protective_option_atm", "protective_option_otm"):
            row = payable.by_name(name)
            strike = row.legs[0][1]
            assert row.worst_case_rate == pytest.approx(strike + self._carried(row), rel=1e-14)

    def test_a_collar_is_bounded_by_its_two_strikes(self, payable):
        for name in ("collar", "collar_zero_cost"):
            row = payable.by_name(name)
            (_, call_strike, _), (_, put_strike, _) = row.legs
            assert row.worst_case_rate == pytest.approx(call_strike + self._carried(row), rel=1e-14)
            assert row.best_case_rate == pytest.approx(put_strike + self._carried(row), rel=1e-14)

    def test_a_seagulls_floor_is_its_sold_put(self, payable):
        row = payable.by_name("seagull")
        sold_put = row.legs[-1][1]
        assert row.best_case_rate == pytest.approx(sold_put + self._carried(row), rel=1e-14)

    def test_a_partial_forward_is_open_on_both_sides(self):
        row = compare_structures(_exposure(), QUOTE, hedge_ratio=0.8).by_name("forward")
        assert row.worst_case_rate is None and row.best_case_rate is None

    def test_a_receivable_is_floored_by_its_put(self, receivable):
        row = receivable.by_name("protective_option_atm")
        strike = row.legs[0][1]
        assert row.worst_case_rate == pytest.approx(strike - self._carried(row), rel=1e-14)
        assert row.best_case_rate is None
        assert receivable.by_name("unhedged").worst_case_rate is None

    def test_the_bounds_do_not_move_with_the_grid(self):
        narrow = compare_structures(_exposure(), QUOTE, grid_width=0.20)
        wide = compare_structures(_exposure(), QUOTE, grid_width=0.50)
        for a, b in zip(narrow.structures, wide.structures, strict=True):
            assert a.worst_case_rate == b.worst_case_rate, a.name
            assert a.best_case_rate == b.best_case_rate, a.name

    def test_a_bound_the_grid_does_not_reach_is_still_reported(self):
        """On a grid one percent wide the 8% collar's strikes are off the
        grid, and its bounds are still the strikes, not the grid's edge."""
        tight = compare_structures(_exposure(), QUOTE, grid_width=0.01).by_name("collar")
        (_, call_strike, _), (_, put_strike, _) = tight.legs
        assert max(tight.payoff_grid) < call_strike
        assert tight.worst_case_rate == pytest.approx(call_strike + self._carried(tight), rel=1e-14)
        assert tight.best_case_rate == pytest.approx(put_strike + self._carried(tight), rel=1e-14)

    def test_an_open_side_serialises_as_null(self, payable):
        payload = json.loads(json.dumps(payable.by_name("unhedged").to_dict()))
        assert payload["worst_case_rate"] is None
        assert payload["best_case_rate"] is None


class TestTheMarketIsCheckedBeforeItIsUsed:
    """The review's finding on FX inputs: every one of these was priced.

    `REVISION-rates_engine-2026-10-15-repro/risk-app/r14_fx_inputs.py`: a
    volatility typed in percent (11.5) priced an at-the-money call at about
    eight pesos a dollar; a negative one priced it as if it were zero; a
    settlement three months in the past gave an intrinsic value; a rate
    typed in percent (9.5) gave an absurd forward. The README's promise --
    a volatility is never a bare float, and the magnitude band catches what
    is left -- now holds here too.
    """

    @staticmethod
    def _quote(**kw):
        args = {"spot": 18.50, "expiry": 0.25, "r_domestic": 0.0950, "r_foreign": 0.0420,
                "volatility": 0.115}
        args.update(kw)
        return StructureQuote(**args)

    def test_a_volatility_in_percent_refuses(self):
        from rates_engine.core.errors import VolUnitsError

        with pytest.raises(VolUnitsError, match="lognormal"):
            self._quote(volatility=11.5)

    def test_a_percent_divided_twice_refuses(self):
        """11.5% arriving as 0.00115, below MIN_PLAUSIBLE_FX_VOLATILITY."""
        from rates_engine.core.errors import VolUnitsError

        with pytest.raises(VolUnitsError, match="0.2%"):
            self._quote(volatility=0.00115)

    @pytest.mark.parametrize("low", [0.03, 0.0115, 0.006])
    def test_a_low_but_real_fx_volatility_is_accepted(self, low):
        """A quiet G10 pair trades near 3% and a pegged one below 1%. The
        first version applied the 5% rates floor here and refused them
        (Alan Vazquez on PR #13, finding 5)."""
        from rates_engine.hedging.fx_structures import MIN_PLAUSIBLE_FX_VOLATILITY

        assert MIN_PLAUSIBLE_FX_VOLATILITY == 0.002
        assert self._quote(volatility=low).vol_at(18.8) == low

    @pytest.mark.parametrize("bad", [0.0, -0.115, float("nan"), float("inf")])
    def test_a_volatility_that_is_not_one_refuses(self, bad):
        with pytest.raises(ValueError, match="volatility"):
            self._quote(volatility=bad)

    @pytest.mark.parametrize("bad", [-0.25, 0.0, float("nan")])
    def test_a_settlement_that_is_not_ahead_refuses(self, bad):
        with pytest.raises(ValueError, match="expiry"):
            self._quote(expiry=bad)

    @pytest.mark.parametrize("bad", [0.0, -18.5, float("nan")])
    def test_a_spot_that_is_not_positive_refuses(self, bad):
        with pytest.raises(ValueError, match="spot"):
            self._quote(spot=bad)

    @pytest.mark.parametrize(
        ("key", "bad"),
        [("r_domestic", 9.5), ("r_foreign", 4.2), ("r_domestic", -1.5), ("r_foreign", float("nan"))],
    )
    def test_a_rate_outside_the_band_refuses(self, key, bad):
        with pytest.raises(ImplausibleInputError, match=key):
            self._quote(**{key: bad})

    def test_the_escape_is_an_unchecked_volatility(self):
        """Zero, or any market outside the band, is passed on purpose as a
        `Volatility` built with `unchecked`. At zero the call is worth its
        discounted intrinsic value on the forward."""
        from rates_engine.volatility.units import Volatility, VolUnits

        zero = self._quote(volatility=Volatility.unchecked(0.0, VolUnits.LOGNORMAL_DECIMAL))
        assert zero.price(18.0, OptionKind.CALL) == pytest.approx(
            math.exp(-0.095 * 0.25) * (zero.forward - 18.0)
        )
        huge = self._quote(volatility=Volatility.unchecked(6.0, VolUnits.LOGNORMAL_DECIMAL))
        assert huge.vol_at(18.8) == 6.0

    def test_a_volatility_in_percent_units_is_read_as_a_decimal(self):
        from rates_engine.volatility.units import Volatility

        quote = self._quote(volatility=Volatility.lognormal_percent(11.5))
        assert quote.vol_at(18.8) == pytest.approx(0.115)
        assert quote.to_dict()["volatility"] == pytest.approx(0.115)

    def test_a_normal_volatility_refuses(self):
        from rates_engine.core.errors import VolUnitsError
        from rates_engine.volatility.units import Volatility

        with pytest.raises(VolUnitsError):
            self._quote(volatility=Volatility.normal_bp(80.0))

    def test_a_callable_volatility_is_the_escape_hatch_and_is_not_banded(self):
        assert self._quote(volatility=lambda k: 0.115).price(18.8, OptionKind.CALL) > 0.0

    @pytest.mark.parametrize("bad", [1.5, -1.01, float("nan")])
    def test_a_correlation_outside_minus_one_to_one_refuses(self, bad):
        with pytest.raises(ValueError, match="correlation"):
            compare_structures(_exposure(), QUOTE, correlation=bad, foreign_asset_volatility=0.10)

    @pytest.mark.parametrize("bad", [-0.10, float("nan")])
    def test_a_foreign_asset_volatility_that_is_not_one_refuses(self, bad):
        with pytest.raises(ValueError, match="foreign_asset_volatility"):
            compare_structures(_exposure(), QUOTE, correlation=-0.3, foreign_asset_volatility=bad)

    def test_a_nan_amount_refuses(self):
        with pytest.raises(ValueError, match="positive size"):
            Exposure(float("nan"), ExposureDirection.PAYABLE, SETTLEMENT, USDMXN)


class TestTheSmileHasToSitOnTheLegsForward:
    """Alan Vazquez on PR #13, finding 6: a `VannaVolgaSmile` passed as the
    volatility keeps its own rates, so with a basis on the quote its
    at-the-money and delta pillars sat on a forward about one percent of
    strike from the legs' at -100 bp, and nothing said so. A smile that
    reports its forward is now held to the quote's; one that does not is
    marked."""

    @staticmethod
    def _smile(r_domestic):
        from rates_engine.models.fx_delta import DeltaBasis, DeltaConvention, PremiumAdjustment
        from rates_engine.volatility.fx_smile import ATMConvention, SmileQuotes, VannaVolgaSmile

        return VannaVolgaSmile(
            spot=18.50, expiry=0.25, r_domestic=r_domestic, r_foreign=0.0420,
            quotes=SmileQuotes(atm=0.1150, risk_reversal_25=0.0180, butterfly_25=0.0035),
            delta_convention=DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED),
            atm_convention=ATMConvention.DELTA_NEUTRAL_STRADDLE,
        )

    def test_a_smile_without_the_basis_refuses(self):
        with pytest.raises(ValueError, match="with the basis on it"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, self._smile(0.0950), basis_bp=-100.0)

    def test_a_smile_on_the_basis_adjusted_rate_is_accepted(self):
        quote = StructureQuote(18.50, 0.25, 0.0950, 0.0420, self._smile(0.0850), basis_bp=-100.0)
        assert quote.volatility.forward == pytest.approx(quote.forward, rel=1e-14)
        compare_structures(_exposure(), quote, otm_offset=0.02, spread_offset=0.04)

    def test_a_smile_on_another_expiry_refuses(self):
        from dataclasses import replace

        with pytest.raises(ValueError, match="expiry"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, replace(self._smile(0.0950), expiry=0.5))

    def test_a_smile_that_cannot_say_its_forward_is_marked(self):
        from rates_engine.core.evidence import DataQuality

        class StandIn:
            def volatility_at(self, strike):
                from rates_engine.core.evidence import Evidence

                class Reading:
                    volatility = 0.115
                    evidence = Evidence(produced_by="test.stand_in")

                return Reading()

        result = compare_structures(_exposure(), StructureQuote(18.50, 0.25, 0.0950, 0.0420, StandIn()))
        codes = set()

        def walk(evidence):
            codes.update(w.code for w in evidence.warnings)
            for source in evidence.sources:
                walk(source)

        walk(result.evidence)
        assert "smile_forward_unverified" in codes
        assert result.evidence.worst_quality is DataQuality.ASSUMED


class TestTheSmileCheckReadsWhatTheSmileReports:
    """Alan Vazquez's round-3 finding 3 on PR #13: the check read
    ``getattr(smile, "forward")`` and passed it to ``float``, so a smile that
    exposes its forward as a method raised a bare ``TypeError``; and it
    compared only the forward and the expiry. A smile on the same forward
    with another spot or another dollar rate places its spot-delta pillars
    elsewhere (a spot delta carries ``exp(-r_f T)``), and was accepted."""

    @staticmethod
    def _stand_in(**reported):
        from rates_engine.core.evidence import Evidence

        class Reading:
            volatility = 0.115
            evidence = Evidence(produced_by="test.stand_in")

        class StandIn:
            def volatility_at(self, strike):
                return Reading()

        smile = StandIn()
        for name, value in reported.items():
            setattr(smile, name, value)
        return smile

    @staticmethod
    def _codes(evidence):
        codes = {w.code for w in evidence.warnings}
        for source in evidence.sources:
            codes |= TestTheSmileCheckReadsWhatTheSmileReports._codes(source)
        return codes

    def test_a_forward_method_is_called(self):
        smile = self._stand_in(forward=lambda: QUOTE.forward, expiry=lambda: 0.25)
        quote = StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)
        result = compare_structures(_exposure(), quote)
        assert "smile_forward_unverified" not in self._codes(result.evidence)

    def test_a_forward_method_on_another_forward_refuses(self):
        smile = self._stand_in(forward=lambda: QUOTE.forward * 1.01)
        with pytest.raises(ValueError, match="forward"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)

    def test_a_forward_method_that_needs_arguments_refuses_by_name(self):
        smile = self._stand_in(forward=lambda expiry: QUOTE.forward)
        with pytest.raises(ValueError, match="smile's forward is a method that needs arguments"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)

    def test_a_forward_that_is_not_a_number_refuses_by_name(self):
        smile = self._stand_in(forward=object())
        with pytest.raises(ValueError, match="smile reports forward"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)

    def test_the_same_forward_on_another_dollar_rate_refuses(self):
        """Both rates up a point: the forward is the legs', the spot deltas
        are not. Accepted before."""
        smile = replace(
            TestTheSmileHasToSitOnTheLegsForward._smile(0.1050), r_foreign=0.0520
        )
        assert smile.forward == pytest.approx(QUOTE.forward, rel=1e-14)
        with pytest.raises(ValueError, match="r_foreign"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)

    def test_the_same_forward_from_another_spot_refuses(self):
        r_domestic = 0.0420 + math.log(QUOTE.forward / 18.40) / 0.25
        smile = replace(
            TestTheSmileHasToSitOnTheLegsForward._smile(r_domestic), spot=18.40
        )
        assert smile.forward == pytest.approx(QUOTE.forward, rel=1e-14)
        with pytest.raises(ValueError, match="spot"):
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)

    def test_the_refusal_names_every_input_to_rebuild_with(self):
        smile = self._stand_in(forward=QUOTE.forward, spot=18.40, r_foreign=0.05)
        with pytest.raises(ValueError) as caught:
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)
        message = str(caught.value)
        for piece in ("spot 18.4", "r_foreign 0.05", "spot=18.5", "r_foreign=0.042"):
            assert piece in message
        assert message.isascii()

    def test_a_smile_on_the_legs_inputs_is_accepted(self):
        smile = TestTheSmileHasToSitOnTheLegsForward._smile(0.0950)
        quote = StructureQuote(18.50, 0.25, 0.0950, 0.0420, smile)
        result = compare_structures(_exposure(), quote, otm_offset=0.02, spread_offset=0.04)
        assert "smile_forward_unverified" not in self._codes(result.evidence)
