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
from datetime import date

import pytest

from rates_engine.errors import ImplausibleInputError
from rates_engine.fx import garman_kohlhagen as gk
from rates_engine.fx.quote import USDMXN
from rates_engine.hedging_structures import (
    TRADE_OFF_FRAME,
    Exposure,
    ExposureDirection,
    StructureQuote,
    compare_structures,
    zero_cost_collar_strike,
)
from rates_engine.volatility.kinds import OptionKind

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
        row = payable.by_name("unhedged")
        span = row.worst_case_rate - row.best_case_rate
        for other in payable.structures:
            assert abs(other.worst_case_rate - other.best_case_rate) <= span + 1e-9

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

    def test_the_seagull_is_the_cheapest_and_can_be_a_credit(self, payable, receivable):
        for comparison in (payable, receivable):
            seagull = comparison.by_name("seagull")
            assert seagull.upfront_cost < 0.0
            assert seagull.upfront_cost == min(s.upfront_cost for s in comparison.structures)

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
        assert row.worst_case_rate < unhedged.worst_case_rate
        assert row.best_case_rate > unhedged.best_case_rate


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

    def test_participation_is_a_fraction(self, payable, receivable):
        for comparison in (payable, receivable):
            for structure in comparison.structures:
                assert 0.0 <= structure.upside_participation <= 1.0

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
        import rates_engine.hedging_structures as module

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
        assert TRADE_OFF_FRAME["upfront_cost"] == "higher"
        assert TRADE_OFF_FRAME["protection"] == "better"
        assert TRADE_OFF_FRAME["upside_participation"] == "worse"
        assert "depends on the treasury policy" in TRADE_OFF_FRAME["statement"]

    def test_the_statement_names_no_structure(self):
        sentence = TRADE_OFF_FRAME["statement"].lower()
        for name in ("collar", "seagull", "forward", "put", "call", "spread"):
            assert name not in sentence

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

    def test_the_payload_says_a_smile_was_used(self):
        quote = StructureQuote(18.50, 0.25, 0.0950, 0.0420, lambda k: 0.115 + 0.0 * k)
        result = compare_structures(_exposure(), quote)
        assert result.evidence.fields["volatility_source"] == "smile"
        assert result.evidence.fields["volatility"] is None


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
