"""PRD-001 AC-8.3 and PRD-001 AC-8.4: the shock table, and the refusal when the strip has a hole."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from rates_engine.app import commands
from rates_engine.app.builders import calibration_nodes
from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
from rates_engine.core.errors import (
    ConfigurationError,
    IncompleteStripError,
    UnderdeterminedCurveError,
)
from rates_engine.core.evidence import DataQuality
from rates_engine.curves.bootstrap import (
    FuturesContract,
    FuturesNode,
    RealizedStubNode,
    bootstrap_discount_curve,
)
from rates_engine.curves.discount import CurveSet
from rates_engine.hedging.futures_strip import SR1_DV01, SR3_DV01, shock_table, strip_hedge
from rates_engine.instruments.swaps import OISSwap
from rates_engine.pricing.linear import par_rate
from rates_engine.risk.sensitivities import money_convexity


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
        from rates_engine.conventions.side import Side

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


def _relabelled(strip, label: str | None) -> tuple:
    """The suite's strip with every futures label replaced, or dropped for the default."""
    return tuple(
        (replace(i, label=label) if label is not None else FuturesNode(i.start, i.end, i.forward_rate))
        if isinstance(i, FuturesNode)
        else i
        for i in strip
    )


class TestContractsThatShareALabel:
    """A label names a contract for the report; it must not decide how many there are.

    ``FuturesNode.label`` defaults to ``"future"``, so the README strip built
    without labels gives nine contracts the same name. Keying the hedge's
    dicts by label let each overwrite the last: the sizing kept one
    contract's delta for the whole strip and reported 101.6 contracts against
    the 878 the swap needs, a hedge ratio of -0.116, and no error.
    """

    def test_unlabelled_futures_are_sized_one_by_one(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, _relabelled(strip, None), as_of=as_of)
        assert len(hedge.contracts) == 9
        # The two identities the sizing has to satisfy whatever the labels
        # are, each against a number computed by a separate route: the
        # bucketed deltas sum to the all-quotes-at-once DV01, and the strip
        # offsets it.
        assert sum(hedge.bucketed_delta_by_instrument.values()) == pytest.approx(
            hedge.quote_parallel_dv01, rel=1e-6
        )
        assert hedge.hedge_ratio == pytest.approx(-1.0, abs=1e-6)
        assert hedge.total_contracts == pytest.approx(
            abs(hedge.quote_parallel_dv01) / SR3_DV01, rel=1e-6
        )

    def test_a_shared_label_is_made_unique_with_the_period_end(self, par_swap, strip, as_of):
        # The same rule the bootstrap applies to its residuals.
        hedge = strip_hedge(par_swap, _relabelled(strip, "SR3"), as_of=as_of)
        ends = [i.end.isoformat() for i in strip if isinstance(i, FuturesNode)]
        assert list(hedge.contracts) == [f"SR3@{end}" for end in ends]
        assert set(hedge.periods) == set(hedge.contracts) == set(hedge.base_forwards)

    def test_the_labels_do_not_change_a_single_number(self, par_swap, strip, as_of):
        labelled = strip_hedge(par_swap, strip, as_of=as_of)
        for label in (None, "SR3"):
            other = strip_hedge(par_swap, _relabelled(strip, label), as_of=as_of)
            assert list(other.contracts.values()) == list(labelled.contracts.values())
            assert other.total_contracts == labelled.total_contracts
            assert other.strip_dv01 == labelled.strip_dv01

    def test_the_shock_table_nets_every_contract(self, par_swap, strip, as_of):
        # README strip at +10 bp: the labelled hedge nets to -310 USD of
        # convexity; the merged one netted to +193,078.
        labelled = shock_table(strip_hedge(par_swap, strip, as_of=as_of), (10.0,))
        merged = shock_table(strip_hedge(par_swap, _relabelled(strip, None), as_of=as_of), (10.0,))
        assert merged.table.net_pnl.iloc[0] == labelled.table.net_pnl.iloc[0]
        assert abs(merged.table.net_pnl.iloc[0]) < 1_000.0

    def test_a_label_that_collides_after_disambiguation_refuses(self, par_swap, strip, as_of):
        futures = [i for i in strip if isinstance(i, FuturesNode)]
        clash = f"X@{futures[1].end.isoformat()}"
        relabelled = []
        for i in strip:
            if i is futures[0]:
                i = replace(i, label=clash)
            elif i is futures[1] or i is futures[2]:
                i = replace(i, label="X")
            relabelled.append(i)
        with pytest.raises(ConfigurationError, match="label"):
            strip_hedge(par_swap, tuple(relabelled), as_of=as_of)

    def test_the_keys_index_the_bootstraps_residuals(self, par_swap, strip, as_of):
        # A future that shares its label with an instrument that is not a
        # future -- here the realised stub's default label -- is keyed the way
        # the bootstrap keys it, so the hedge's keys look up its residuals.
        # Counting repeats among the futures alone keyed it "realized_stub".
        first = next(i for i in strip if isinstance(i, FuturesNode))
        relabelled = tuple(
            replace(i, label="realized_stub") if i is first else i for i in strip
        )
        hedge = strip_hedge(par_swap, relabelled, as_of=as_of)
        residuals = hedge.evidence.sources[0].fields["fit_residuals_bp"]
        assert f"realized_stub@{first.end.isoformat()}" in hedge.contracts
        assert set(hedge.contracts) <= set(residuals)

    def test_a_contract_passed_twice_is_refused_by_the_bootstrap(self, par_swap, strip, as_of):
        # Same label and same period end: that is one node pinned twice, and
        # the bootstrap's refusal names it, rather than a label clash.
        twice = strip + (next(i for i in strip if isinstance(i, FuturesNode)),)
        with pytest.raises(UnderdeterminedCurveError, match="both pin the node"):
            strip_hedge(par_swap, twice, as_of=as_of)


#: One SR1 contract, in USD per basis point: USD 5,000,000 x 30/360 x 1bp,
#: the figure CME publishes for the one-month SOFR future (41.67). Written
#: out here rather than imported, so the test does not lean on the constant
#: it checks.
SR1_TICK = 5_000_000 * (30 / 360) * 1e-4
#: One SR3 contract: USD 1,000,000 x 0.25 x 1bp.
SR3_TICK = 1_000_000 * 0.25 * 1e-4

MIX_AS_OF = date(2026, 1, 15)


def _sr1_sr3_mix(*, declare: bool = True) -> tuple:
    """Three SR1 months under three SR3 quarters, as PRD-001 US-3 builds a front end.

    The first SR3 quarter (18 March to 17 June) overlaps the SR1 months of
    March and April, which is how the two contracts are traded together.
    """
    rate = 0.04
    sr1 = {"contract": "SR1"} if declare else {}
    sr3 = {"contract": "SR3"} if declare else {}
    return (
        RealizedStubNode(end=date(2026, 2, 1), accrual_factor=1 + rate * 17 / 360),
        FuturesNode(date(2026, 2, 1), date(2026, 3, 1), rate, label="SR1-G6", **sr1),
        FuturesNode(date(2026, 3, 1), date(2026, 4, 1), rate, label="SR1-H6", **sr1),
        FuturesNode(date(2026, 4, 1), date(2026, 5, 1), rate, label="SR1-J6", **sr1),
        FuturesNode(date(2026, 3, 18), date(2026, 6, 17), rate, label="SR3-H6", **sr3),
        FuturesNode(date(2026, 6, 17), date(2026, 9, 16), rate, label="SR3-M6", **sr3),
        FuturesNode(date(2026, 9, 16), date(2026, 12, 16), rate, label="SR3-U6", **sr3),
    )


def _mix_swap(instruments) -> OISSwap:
    curve_set = CurveSet(bootstrap_discount_curve(MIX_AS_OF, instruments).curve)
    provisional = OISSwap(
        effective=date(2026, 2, 3), maturity=date(2026, 12, 16), fixed_rate=0.04,
        notional=100_000_000.0,
    )
    return replace(provisional, fixed_rate=par_rate(provisional, curve_set).value)


def _mix_hedge(*, declare: bool = True, **kwargs):
    instruments = _sr1_sr3_mix(declare=declare)
    return strip_hedge(
        _mix_swap(instruments), instruments, as_of=MIX_AS_OF, require_full_coverage=False,
        **kwargs,
    )


class TestContractSize:
    """An SR1 is worth USD 41.67 a basis point and an SR3 USD 25.00; each is sized at its own.

    Every contract used to be divided by one ``contract_dv01``, 25.00 by
    default, so an SR1 month was bought 1.67 times over: the February
    contract of this strip carries -718.3 USD/bp of the swap and came out at
    28.7 contracts where 17.2 offset it. Nothing in the result was
    inconsistent, because the strip DV01 was multiplied back by the same
    wrong tick; the error was in the number of contracts a trader would buy.
    """

    def test_an_sr1_contract_is_sized_at_its_own_tick(self):
        hedge = _mix_hedge()
        for label in ("SR1-G6", "SR1-H6", "SR1-J6"):
            delta = hedge.bucketed_delta_by_instrument[label]
            assert hedge.contracts[label] == pytest.approx(-delta / SR1_TICK, rel=1e-12)
        # The review's figures for the February contract.
        assert hedge.bucketed_delta_by_instrument["SR1-G6"] == pytest.approx(-718.3, abs=0.05)
        assert hedge.contracts["SR1-G6"] == pytest.approx(17.2, abs=0.05)

    def test_the_sr3_contracts_beside_them_stay_at_twenty_five(self):
        hedge = _mix_hedge()
        for label in ("SR3-H6", "SR3-M6", "SR3-U6"):
            delta = hedge.bucketed_delta_by_instrument[label]
            assert hedge.contracts[label] == pytest.approx(-delta / SR3_TICK, rel=1e-12)

    def test_each_contract_reports_its_tick(self):
        hedge = _mix_hedge()
        assert hedge.contract_dv01_by_label["SR1-G6"] == pytest.approx(SR1_TICK, rel=1e-12)
        assert hedge.contract_dv01_by_label["SR3-H6"] == pytest.approx(SR3_TICK, rel=1e-12)
        assert SR1_DV01 == pytest.approx(41.67, abs=0.005)
        # A mixed strip has no single contract DV01, and says so rather than
        # reporting one of the two.
        assert hedge.contract_dv01 is None
        assert hedge.to_dict()["contract_dv01_by_label"]["SR1-H6"] == pytest.approx(SR1_TICK)

    def test_the_strip_dv01_adds_each_contract_at_its_own_tick(self):
        hedge = _mix_hedge()
        expected = sum(
            count * (SR1_TICK if label.startswith("SR1") else SR3_TICK)
            for label, count in hedge.contracts.items()
        )
        assert hedge.strip_dv01 == pytest.approx(expected, rel=1e-12)
        assert hedge.hedge_ratio == pytest.approx(-1.0, abs=1e-6)

    def test_a_calendar_month_is_read_as_sr1_when_undeclared(self):
        # The SR1 reference period is the calendar month and the SR3 one runs
        # IMM date to IMM date; no SR3 can start on the first of a month, so
        # the period says which contract an undeclared node is.
        declared, undeclared = _mix_hedge(declare=True), _mix_hedge(declare=False)
        assert undeclared.contracts == declared.contracts
        sizing = undeclared.evidence.fields["contract_sizing"]
        assert sizing["SR1-G6"]["contract"] == "SR1"
        assert sizing["SR1-G6"]["basis"] == "calendar_month"
        assert sizing["SR3-M6"]["basis"] == "imm_quarter"
        assert declared.evidence.fields["contract_sizing"]["SR1-G6"]["basis"] == "declared"

    def test_a_period_that_is_neither_is_sized_as_sr3_and_says_so(self, par_swap, strip, as_of):
        shifted = tuple(
            replace(i, start=i.start + timedelta(days=1)) if isinstance(i, FuturesNode) else i
            for i in strip
        )
        hedge = strip_hedge(par_swap, shifted, as_of=as_of, require_full_coverage=False)
        assert hedge.contract_dv01 == pytest.approx(SR3_TICK)
        assert {s["basis"] for s in hedge.evidence.fields["contract_sizing"].values()} == {
            "assumed"
        }
        # A field alone left worst_quality at "observed".
        codes = {w.code for w in hedge.evidence.warnings}
        assert "futures_contract_assumed" in codes
        assert hedge.evidence.worst_quality is DataQuality.ASSUMED

    def test_an_sr1_month_with_rolled_dates_is_flagged_not_read_as_sr1(self):
        # 2026-02-02 to 2026-03-02: February rolled to business days. It is no
        # calendar month, so it is sized as SR3 -- 1.67 times the contracts an
        # SR1 would need -- and the degradation says which declaration fixes it.
        rate = 0.04
        instruments = (
            RealizedStubNode(end=date(2026, 2, 2), accrual_factor=1 + rate * 18 / 360),
            FuturesNode(date(2026, 2, 2), date(2026, 3, 2), rate, label="G6"),
            FuturesNode(date(2026, 3, 2), date(2026, 4, 1), rate, label="H6"),
        )
        swap = OISSwap(effective=date(2026, 2, 3), maturity=date(2026, 3, 31), fixed_rate=rate)
        hedge = strip_hedge(swap, instruments, as_of=MIX_AS_OF, require_full_coverage=False)
        assumed = next(
            w for w in hedge.evidence.warnings if w.code == "futures_contract_assumed"
        )
        assert "G6" in assumed.message
        assert "contract='SR1'" in assumed.message
        assert hedge.evidence.worst_quality is DataQuality.ASSUMED

    def test_a_standard_strip_carries_no_contract_degradation(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert hedge.evidence.warnings == ()
        assert hedge.evidence.worst_quality is DataQuality.OBSERVED

    def test_the_shock_table_prices_each_contract_at_its_own_tick(self):
        # Sized at 41.67 but priced at 25 the SR1 leg would leave a
        # first-order residual, and +10 and -10 would net to opposite signs.
        rows = shock_table(_mix_hedge(), (-10.0, 10.0)).table.set_index("shock_bp")
        assert rows.loc[10.0, "net_pnl"] == pytest.approx(rows.loc[-10.0, "net_pnl"], rel=0.05)
        assert abs(rows.loc[10.0, "net_pnl"]) < 100.0

    def test_the_evidence_derives_each_tick_it_used(self):
        derivation = _mix_hedge().evidence.fields["contract_dv01_derivation"]
        assert derivation["SR1"].startswith("5,000,000 x 30/360 x 1bp = 41.67 USD/bp")
        assert derivation["SR3"] == "1,000,000 x 0.25 x 1bp = 25.00 USD/bp"

    def test_an_sr3_strip_derives_only_the_sr3_tick(self, par_swap, strip, as_of):
        derivation = strip_hedge(par_swap, strip, as_of=as_of).evidence.fields[
            "contract_dv01_derivation"
        ]
        assert derivation == {"SR3": "1,000,000 x 0.25 x 1bp = 25.00 USD/bp"}

    def test_the_assumed_sr1_notional_is_carried_into_the_hedge(self, par_swap, strip, as_of):
        hedge = _mix_hedge()
        assert "sr1_notional_assumed" in {w.code for w in hedge.evidence.warnings}
        assert hedge.evidence.worst_quality is DataQuality.ASSUMED
        sr3_only = strip_hedge(par_swap, strip, as_of=as_of)
        assert sr3_only.evidence.warnings == ()

    def test_a_caller_contract_dv01_sizes_a_contract_the_period_does_not_name(
        self, par_swap, strip, as_of
    ):
        # The case the override is for: a period that is neither a calendar
        # month nor an IMM quarter, i.e. a contract this build does not know.
        shifted = tuple(
            replace(i, start=i.start + timedelta(days=1)) if isinstance(i, FuturesNode) else i
            for i in strip
        )
        hedge = strip_hedge(
            par_swap, shifted, as_of=as_of, contract_dv01=30.0, require_full_coverage=False
        )
        assert hedge.contract_dv01 == 30.0
        for label, delta in hedge.bucketed_delta_by_instrument.items():
            assert hedge.contracts[label] == pytest.approx(-delta / 30.0, rel=1e-12)
        assert "given by the caller" in hedge.evidence.fields["contract_dv01_derivation"]["caller"]
        # Sized at the caller's tick, but still flagged: nothing here can say
        # what a contract on such a period is worth, so the count rests on
        # the caller's word (round-2 review, point 1).
        assert "futures_contract_assumed" in {w.code for w in hedge.evidence.warnings}

    def test_a_caller_contract_dv01_that_contradicts_a_declared_contract_refuses(self):
        with pytest.raises(ConfigurationError, match="SR1-G6"):
            _mix_hedge(contract_dv01=25.0)

    def test_a_caller_contract_dv01_that_contradicts_an_inferred_sr1_refuses(self):
        # The review's case: 25.00, the old documented default, on undeclared
        # calendar months sized February's SR1 at 28.72 contracts instead of
        # 17.23, silently.
        with pytest.raises(ConfigurationError, match="SR1-G6"):
            _mix_hedge(declare=False, contract_dv01=25.0)

    def test_a_caller_contract_dv01_equal_to_an_inferred_sr3_tick_is_accepted(
        self, par_swap, strip, as_of
    ):
        hedge = strip_hedge(par_swap, strip, as_of=as_of, contract_dv01=25.0)
        assert hedge.contract_dv01 == 25.0
        assert hedge.contracts == strip_hedge(par_swap, strip, as_of=as_of).contracts

    def test_a_caller_contract_dv01_equal_to_the_declared_tick_is_accepted(
        self, par_swap, strip, as_of
    ):
        declared = tuple(
            replace(i, contract="SR3") if isinstance(i, FuturesNode) else i for i in strip
        )
        hedge = strip_hedge(par_swap, declared, as_of=as_of, contract_dv01=25.0)
        assert hedge.contract_dv01 == 25.0

    def test_a_declared_contract_that_contradicts_its_period_refuses(self, par_swap, strip, as_of):
        wrong = tuple(
            replace(i, contract="SR1") if isinstance(i, FuturesNode) else i for i in strip
        )
        with pytest.raises(ConfigurationError, match="IMM"):
            strip_hedge(par_swap, wrong, as_of=as_of)

    def test_an_unknown_contract_is_refused_when_the_node_is_built(self):
        with pytest.raises(ConfigurationError, match="SR1"):
            FuturesNode(date(2026, 3, 18), date(2026, 6, 17), 0.04, contract="SR2")

    def test_the_contract_names_are_one_vocabulary(self):
        # One definition, read by the node, the hedge and the CLI.
        assert [c.value for c in FuturesContract] == ["SR1", "SR3"]
        node = FuturesNode(date(2026, 3, 18), date(2026, 6, 17), 0.04, contract="SR3")
        assert node.contract is FuturesContract.SR3

    @pytest.mark.parametrize(
        ("contract", "start", "end", "tick"),
        [
            ("SR1", date(2026, 2, 1), date(2026, 3, 1), SR1_TICK),
            ("SR3", date(2026, 3, 18), date(2026, 6, 17), SR3_TICK),
        ],
    )
    def test_every_contract_name_has_a_tick(self, contract, start, end, tick):
        rate = 0.04
        instruments = (
            RealizedStubNode(end=start, accrual_factor=1 + rate * (start - MIX_AS_OF).days / 360),
            FuturesNode(start, end, rate, label=contract, contract=contract),
        )
        swap = OISSwap(effective=start, maturity=end - timedelta(days=7), fixed_rate=rate)
        hedge = strip_hedge(swap, instruments, as_of=MIX_AS_OF)
        assert hedge.contract_dv01 == pytest.approx(tick, rel=1e-12)

    def test_a_mixed_strip_reports_a_total_per_contract_and_none_overall(self):
        # 28.5 SR1 plus 299.5 SR3 is not 328 of anything.
        hedge = _mix_hedge()
        by_contract = hedge.total_contracts_by_contract
        assert by_contract["SR1"] == pytest.approx(
            sum(hedge.contracts[k] for k in ("SR1-G6", "SR1-H6", "SR1-J6")), rel=1e-12
        )
        assert by_contract["SR3"] == pytest.approx(
            sum(hedge.contracts[k] for k in ("SR3-H6", "SR3-M6", "SR3-U6")), rel=1e-12
        )
        assert hedge.total_contracts is None
        assert hedge.to_dict()["total_contracts_by_contract"] == by_contract

    def test_a_single_contract_strip_keeps_its_total(self, par_swap, strip, as_of):
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        assert hedge.total_contracts_by_contract == {"SR3": hedge.total_contracts}

    def test_an_undeclared_node_describes_itself_as_before(self, strip):
        node = next(i for i in strip if isinstance(i, FuturesNode))
        assert "contract" not in node.describe()
        assert replace(node, contract="SR3").describe()["contract"] == "SR3"


def _rolled_sr1(**declared) -> tuple:
    """The round-2 review's two SR1 months, with their dates rolled to business days.

    1 February and 1 March 2026 are Sundays, so a schedule that rolls them
    gives 2 February to 2 March and 2 March to 1 April: neither period is a
    calendar month, and neither runs from one IMM date to the next.
    """
    rate = 0.04
    return (
        RealizedStubNode(end=date(2026, 2, 2), accrual_factor=1 + rate * 18 / 360),
        FuturesNode(date(2026, 2, 2), date(2026, 3, 2), rate, label="SR1-2", **declared),
        FuturesNode(date(2026, 3, 2), date(2026, 4, 1), rate, label="SR1-3", **declared),
    )


def _rolled_sr1_hedge(*, contract_dv01=None, **declared):
    """The review's repro: a USD 100m payer from 2 February to 1 April 2026 on those months."""
    swap = OISSwap(
        effective=date(2026, 2, 2), maturity=date(2026, 4, 1), fixed_rate=0.04,
        notional=100_000_000.0,
    )
    return strip_hedge(
        swap, _rolled_sr1(**declared), as_of=MIX_AS_OF, contract_dv01=contract_dv01,
        require_full_coverage=False,
    )


class TestAPeriodThatNamesNoContract:
    """A caller's tick on a period that names no contract is the caller's word, and says so.

    Round-2 review, points 1, 2 and 5. The two rolled SR1 months above are
    neither a calendar month nor an IMM quarter, so a ``contract_dv01``
    passed for them was taken unchecked, and the degradation the default
    path attaches was dropped: 25.00, the old documented default, bought
    them 1.67 times over -- 30.94 and 33.14 contracts where an SR1 hedge
    needs 18.56 and 19.88 -- and the result read ``observed``.
    """

    def test_a_caller_tick_keeps_the_assumed_contract_degradation(self):
        hedge = _rolled_sr1_hedge(contract_dv01=SR3_TICK)
        assert hedge.contracts["SR1-2"] == pytest.approx(30.94, abs=0.005)
        assert hedge.contracts["SR1-3"] == pytest.approx(33.14, abs=0.005)
        assert "futures_contract_assumed" in {w.code for w in hedge.evidence.warnings}
        assert hedge.evidence.worst_quality is DataQuality.ASSUMED

    def test_the_degradation_says_which_declaration_fixes_it(self):
        hedge = _rolled_sr1_hedge(contract_dv01=SR3_TICK)
        message = next(
            w.message for w in hedge.evidence.warnings if w.code == "futures_contract_assumed"
        )
        assert "SR1-2, SR1-3" in message
        assert "contract='SR1'" in message
        # 41.67 / 25.00: what the caller's tick does to an SR1 month.
        assert "1.67 times" in message

    def test_the_caller_tick_still_sizes_the_contracts(self):
        # Flagged, not refused: the caller may know a contract this build
        # does not. 25.00 is the tick the default path assumes, so the two
        # agree to the bit.
        assert (
            _rolled_sr1_hedge(contract_dv01=SR3_TICK).contracts == _rolled_sr1_hedge().contracts
        )

    @pytest.mark.parametrize("tick", [41.67, SR1_TICK])
    def test_an_sr1_tick_from_the_caller_carries_the_sr1_notional_flag(self, tick):
        # Keyed on the tick used, not on what the period was read as: 41.67
        # on these months sizes them as SR1s, on the same assumed notional.
        hedge = _rolled_sr1_hedge(contract_dv01=tick)
        assert hedge.contracts["SR1-2"] == pytest.approx(18.56, abs=0.005)
        codes = {w.code for w in hedge.evidence.warnings}
        assert {"sr1_notional_assumed", "futures_contract_assumed"} <= codes

    def test_a_tick_that_is_not_the_sr1_one_carries_no_sr1_flag(self):
        codes = {w.code for w in _rolled_sr1_hedge(contract_dv01=30.0).evidence.warnings}
        assert "sr1_notional_assumed" not in codes

    def test_declaring_the_contract_clears_the_contract_degradation(self):
        hedge = _rolled_sr1_hedge(contract="SR1")
        for label, delta in hedge.bucketed_delta_by_instrument.items():
            assert hedge.contracts[label] == pytest.approx(-delta / SR1_TICK, rel=1e-12)
        assert {w.code for w in hedge.evidence.warnings} == {"sr1_notional_assumed"}
        assert hedge.evidence.fields["contract_sizing"]["SR1-2"]["basis"] == "declared"

    def test_the_caller_derivation_names_the_contracts_it_could_not_check(self):
        derivation = _rolled_sr1_hedge(contract_dv01=SR3_TICK).evidence.fields[
            "contract_dv01_derivation"
        ]["caller"]
        assert "not derived from a contract spec" not in derivation
        assert "unchecked for SR1-2, SR1-3" in derivation

    def test_the_caller_derivation_says_when_every_contract_was_checked(
        self, par_swap, strip, as_of
    ):
        derivation = strip_hedge(par_swap, strip, as_of=as_of, contract_dv01=25.0).evidence.fields[
            "contract_dv01_derivation"
        ]["caller"]
        assert "checked against the tick of every contract" in derivation
        assert "unchecked" not in derivation


class TestADeclaredContractOnAPeriodNotItsOwn:
    """A declared contract on a period that is not exactly its own is held to its length.

    Round-2 review, point 3: ``FuturesNode(2026-02-02, 2026-05-04,
    contract="SR1")``, ninety-one days, was sized as an SR1 -- 59.93
    contracts at 41.67 -- with basis ``declared`` and nothing to say the
    declaration and the dates disagree.

    The reference lengths come from the contracts' definitions, not from the
    code: an SR1 references a calendar month, 28 to 31 days; an SR3 runs
    from one IMM date to the next, and as the third Wednesday falls between
    the 15th and the 21st that is 12, 13 or 14 weeks, 84, 91 or 98 days. A
    business-day roll moves a date by at most three days on SIFMA's calendar
    (a Saturday before a Monday holiday rolls to the Tuesday), and one
    convention moves both ends the same way, so a rolled period is within
    three days of its reference length.
    """

    @staticmethod
    def _hedge(start: date, end: date, contract: str):
        rate = 0.04
        instruments = (
            RealizedStubNode(end=start, accrual_factor=1 + rate * (start - MIX_AS_OF).days / 360),
            FuturesNode(start, end, rate, label="X", contract=contract),
        )
        swap = OISSwap(effective=start, maturity=end, fixed_rate=rate, notional=100_000_000.0)
        return strip_hedge(swap, instruments, as_of=MIX_AS_OF)

    def test_an_sr1_declared_on_a_quarter_long_period_is_refused(self):
        with pytest.raises(ConfigurationError, match="runs 91 days") as caught:
            self._hedge(date(2026, 2, 2), date(2026, 5, 4), "SR1")
        assert "X is declared SR1" in str(caught.value)

    @pytest.mark.parametrize(
        ("contract", "days", "accepted"),
        [
            ("SR1", 24, False),
            ("SR1", 25, True),
            ("SR1", 34, True),
            ("SR1", 35, False),
            ("SR1", 91, False),
            ("SR3", 30, False),
            ("SR3", 80, False),
            ("SR3", 81, True),
            ("SR3", 101, True),
            ("SR3", 102, False),
        ],
    )
    def test_the_tolerance_is_three_days_either_side(self, contract, days, accepted):
        # 2 February is neither the first of a month nor an IMM date, so no
        # period starting there is recognised exactly and only the length
        # is left to check.
        start = date(2026, 2, 2)
        end = start + timedelta(days=days)
        if not accepted:
            with pytest.raises(ConfigurationError, match=f"runs {days} days"):
                self._hedge(start, end, contract)
            return
        hedge = self._hedge(start, end, contract)
        tick = SR1_TICK if contract == "SR1" else SR3_TICK
        assert hedge.contract_dv01 == pytest.approx(tick, rel=1e-12)
        assert "futures_contract_assumed" not in {w.code for w in hedge.evidence.warnings}


class TestTheNotesSayWhatIsMeasured:
    """The evidence used to say the bucketed deltas and the key rates "sum to
    the same parallel DV01". They do not: each sums to its own parallel, and
    the two parallels differ by about 0.37% on the README strip."""

    def test_the_hedge_note_gives_each_basis_its_own_parallel(self, par_swap, strip, as_of):
        note = strip_hedge(par_swap, strip, as_of=as_of).evidence.fields["note"]
        assert "same parallel" not in note
        assert "quote_parallel_dv01" in note
        assert "key-rate" in note

    def test_the_two_parallels_differ_on_the_readme_strip(self, par_swap, strip, curve_set, as_of):
        # The README strip (flat 4%, nine IMM quarters, USD 100m two-year
        # payer). The figures quoted in the module docstring.
        from rates_engine.risk.sensitivities import key_rate_dv01

        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        key_rates = key_rate_dv01(par_swap, curve_set, (0.5, 1.0, 1.5, 2.0)).total
        buckets = sum(hedge.bucketed_delta_by_instrument.values())
        assert key_rates == pytest.approx(-21_870.07, abs=0.01)
        assert buckets == pytest.approx(-21_949.91, abs=0.01)
        assert buckets / key_rates == pytest.approx(1.00365, abs=1e-5)

    def test_a_zero_rate_basis_point_moves_a_quarterly_forward_by_less_than_one(
        self, curve_set, strip
    ):
        # Closed form for the factor the shock table's comment quotes: with
        # P(s)/P(e) = 1 + F tau and a parallel shift dy of the ACT/365F zero
        # rate, dF = (1 + F tau) x (days/365) / tau x dy = (1 + F tau) x
        # 360/365 x dy. At 4% over a 91-day quarter that is 0.99627, not one
        # and not 365/360.
        from rates_engine.risk.bumps import shifted

        moved = shifted(curve_set, 1e-4)
        for contract in (i for i in strip if isinstance(i, FuturesNode)):
            base = curve_set.discount.forward(contract.start, contract.end)
            tau = (contract.end - contract.start).days / 360.0
            move_bp = (moved.discount.forward(contract.start, contract.end) - base) * 1e4
            assert move_bp == pytest.approx((1.0 + base * tau) * 360.0 / 365.0, rel=1e-4)


class TestIncompleteStrip:
    """PRD-001 AC-8.4: a gap is named, never extrapolated over."""

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


class TestCoverageWithOverlappingContracts:
    """SR1 months traded under SR3 quarters overlap, and overlap is not a hole.

    The check compared each contract only with the one starting next, so a
    short contract inside a long one hid the long one's reach: the SR1/SR3
    mix below was refused with a gap from 1 May to 17 June that the March
    SR3 quarter covers to the day.
    """

    def test_an_overlapping_strip_with_no_hole_is_accepted(self):
        instruments = _sr1_sr3_mix()
        hedge = strip_hedge(_mix_swap(instruments), instruments, as_of=MIX_AS_OF)
        assert hedge.evidence.fields["coverage_checked"] is True
        assert len(hedge.contracts) == 6

    def test_a_real_hole_in_an_overlapping_strip_is_still_named(self):
        instruments = tuple(
            i for i in _sr1_sr3_mix() if not isinstance(i, FuturesNode) or i.label != "SR3-H6"
        )
        with pytest.raises(IncompleteStripError, match="2026-05-01 to 2026-06-17"):
            strip_hedge(_mix_swap(_sr1_sr3_mix()), instruments, as_of=MIX_AS_OF)

    def test_the_hole_is_measured_from_the_furthest_reach_so_far(self, as_of):
        # A short contract inside a long one: the hole after them opens where
        # the long one ends, not where the short one does.
        long = FuturesNode(date(2026, 3, 18), date(2026, 9, 16), 0.04, label="LONG")
        short = FuturesNode(date(2026, 6, 17), date(2026, 7, 15), 0.04, label="SHORT")
        late = FuturesNode(date(2026, 10, 21), date(2026, 12, 16), 0.04, label="LATE")
        swap = OISSwap(
            effective=date(2026, 3, 18), maturity=date(2026, 12, 16), fixed_rate=0.04
        )
        with pytest.raises(IncompleteStripError, match="2026-09-16 to 2026-10-21"):
            strip_hedge(swap, (long, short, late), as_of=as_of)



class TestShockTable:
    """PRD-001 AC-8.3: every column the table promises, and the residual behaving."""

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

    @pytest.mark.parametrize("grid", [(0.0, 10.0), (-10.0, -0.0)])
    def test_a_zero_shock_is_refused_by_name(self, par_swap, strip, as_of, grid):
        # The implied convexity column is 2 x net / shock^2, which is 0/0 at
        # a zero shock; it used to escape as a bare ZeroDivisionError.
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        with pytest.raises(ConfigurationError, match="0 bp"):
            shock_table(hedge, grid)

    def test_a_one_pass_grid_is_read_once(self, par_swap, strip, as_of):
        # The zero check consumed a generator before the rows were built, so
        # a grid passed as one came back with no rows.
        hedge = strip_hedge(par_swap, strip, as_of=as_of)
        table = shock_table(hedge, (s for s in (10.0, -10.0)))
        assert list(table.table.shock_bp) == [10.0, -10.0]
        assert table.shocks_bp == (10.0, -10.0)


class TestConvexityReconciliation:
    """PRD-001 AC-10.10: DV01 and money convexity reproduce the full reprice."""

    def test_the_swap_pnl_is_dv01_plus_half_convexity(self, par_swap, strip, curve_set, as_of):
        from rates_engine.risk.sensitivities import dv01

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
        from rates_engine.risk.sensitivities import dv01

        table = shock_table(strip_hedge(par_swap, strip, as_of=as_of), (10.0,)).table
        row = table.iloc[0]
        predicted = -dv01(par_swap, curve_set).value * 10.0 + 0.5 * money_convexity(
            par_swap, curve_set
        ).value * (10.0 * 1e-4) ** 2
        assert predicted == pytest.approx(row.swap_pnl, rel=1e-5)

    @pytest.mark.parametrize("side", ["payer", "receiver"])
    def test_the_hedged_residual_has_the_sign_of_the_swaps_convexity(
        self, par_swap, strip, curve_set, as_of, side
    ):
        # The guard test_golden_cme.py's -100 bp golden points to. A linear
        # strip leaves 0.5 x C x dy^2, so a payer (short convexity) nets
        # negative on both sides of the grid and a receiver positive. The
        # convexity comes from risk.sensitivities, a separate route from the
        # shock table's full reprice.
        from rates_engine.conventions.side import Side

        swap = replace(par_swap, side=Side(side))
        convexity = money_convexity(swap, curve_set).value
        rows = shock_table(strip_hedge(swap, strip, as_of=as_of), (-100.0, 100.0)).table
        assert (convexity < 0) == (side == "payer")
        for net in rows.net_pnl:
            assert (net < 0) == (convexity < 0)


def _hedge_config(as_of, *, label=None) -> dict:
    """A ``hedge`` command config over the suite's two years of IMM quarters at 96.00."""
    start = imm_date(2026, 3)
    futures = []
    period = start
    for index in range(9):
        following = next_imm_on_or_after(period + timedelta(days=1))
        futures.append(
            {
                "label": label if label is not None else f"SR3-{index + 1}",
                "start": period.isoformat(),
                "end": following.isoformat(),
                "price": 96.0,
            }
        )
        period = following
    return {
        "as_of": as_of.isoformat(),
        "curve": {
            "stub": {
                "end": start.isoformat(),
                "accrual_factor": 1.0 + 0.04 * ((start - as_of).days / 360.0),
            },
            "futures": futures,
        },
        "swap": {
            "effective": start.isoformat(),
            "maturity": period.isoformat(),
            "fixed_rate": 0.04,
            "notional": 100_000_000.0,
            "side": "payer",
        },
        "hedge": {"shocks_bp": [-10, 10]},
    }


class TestTheHedgeCommand:
    """The CLI's ``hedge`` body: what the config can say, and what it refuses."""

    def test_futures_rows_that_share_a_label_are_sized_one_by_one(self, as_of):
        # Every row labelled "SR3" used to come back as one contract of 101.6
        # with a hedge ratio of -0.116, and exit code 0.
        payload = commands.hedge(_hedge_config(as_of, label="SR3"))
        assert len(payload["contracts"]) == 9
        assert payload["hedge_ratio"] == pytest.approx(-1.0, abs=1e-6)
        labelled = commands.hedge(_hedge_config(as_of))
        assert payload["total_contracts"] == labelled["total_contracts"]

    def test_a_futures_row_can_name_its_contract(self, as_of):
        config = _hedge_config(as_of)
        config["curve"]["futures"][0]["contract"] = "SR3"
        nodes = [n for n in calibration_nodes(config, as_of) if isinstance(n, FuturesNode)]
        assert nodes[0].contract == "SR3"
        assert nodes[1].contract is None

    @pytest.mark.parametrize("value", ["SR2", "sr3", 3])
    def test_a_contract_the_engine_does_not_size_is_refused_by_its_path(self, as_of, value):
        config = _hedge_config(as_of)
        config["curve"]["futures"][2]["contract"] = value
        with pytest.raises(ConfigurationError, match=r"curve\.futures\[2\]\.contract"):
            commands.hedge(config)

    def test_sr1_rows_are_sized_at_the_sr1_tick(self, as_of):
        # Eleven calendar months, read as SR1 from their periods. The command
        # used to pass contract_dv01=25.00 for every row whatever it was.
        payload = commands.hedge(_sr1_config(as_of))
        assert payload["contract_dv01"] == pytest.approx(SR1_TICK, rel=1e-12)
        for label, delta in payload["bucketed_delta_by_instrument"].items():
            assert payload["contracts"][label] == pytest.approx(-delta / SR1_TICK, rel=1e-12)
        declared = _sr1_config(as_of)
        for row in declared["curve"]["futures"]:
            row["contract"] = "SR1"
        assert commands.hedge(declared)["contracts"] == payload["contracts"]

    def test_a_contract_dv01_that_agrees_with_the_contracts_is_accepted(self, as_of):
        config = _hedge_config(as_of)
        config["hedge"]["contract_dv01"] = 25
        payload = commands.hedge(config)
        assert payload["contract_dv01"] == 25.0

    def test_a_contract_dv01_that_contradicts_the_contracts_is_refused(self, as_of):
        # The old documented default, 25, on SR1 months: refused, not obeyed.
        config = _sr1_config(as_of)
        config["hedge"]["contract_dv01"] = 25
        with pytest.raises(ConfigurationError, match="SR1-02") as caught:
            commands.hedge(config)
        # By the keys in the file, not only the label (round-2 review, point 4).
        assert str(caught.value).startswith("hedge.contract_dv01 contradicts curve.futures[0]:")

    def test_a_contract_dv01_that_contradicts_a_declared_row_names_its_contract(self, as_of):
        config = _hedge_config(as_of)
        config["curve"]["futures"][0]["contract"] = "SR3"
        config["hedge"]["contract_dv01"] = 41.67
        with pytest.raises(ConfigurationError) as caught:
            commands.hedge(config)
        assert str(caught.value).startswith(
            "hedge.contract_dv01 contradicts curve.futures[0].contract:"
        )

    def test_a_declared_contract_that_contradicts_its_period_names_the_row(self, as_of):
        config = _hedge_config(as_of)
        config["curve"]["futures"][3]["contract"] = "SR1"
        with pytest.raises(ConfigurationError) as caught:
            commands.hedge(config)
        assert str(caught.value).startswith("curve.futures[3].contract: SR3-4 is declared SR1")

    def test_rolled_sr1_rows_sized_at_a_caller_tick_are_flagged(self, as_of):
        # The review's CLI case: `hedge.contract_dv01: 25` on SR1 rows whose
        # dates were rolled to business days exited 0 reading "observed".
        config = _sr1_config(as_of)
        config["curve"]["stub"] = {"end": "2026-02-02", "accrual_factor": 1.0 + 0.04 * 18 / 360}
        config["curve"]["futures"] = [
            {"label": "SR1-2", "start": "2026-02-02", "end": "2026-03-02", "price": 96.0},
            {"label": "SR1-3", "start": "2026-03-02", "end": "2026-04-01", "price": 96.0},
        ]
        config["swap"].update(effective="2026-02-02", maturity="2026-04-01")
        config["hedge"].update(contract_dv01=25, require_full_coverage=False)
        warnings = commands.hedge(config)["evidence"]["warnings"]
        assert "futures_contract_assumed" in {w["code"] for w in warnings}

    @pytest.mark.parametrize("tick", [41.67, SR1_TICK, None])
    def test_the_assumed_sr1_notional_is_flagged_whoever_gave_the_tick(self, as_of, tick):
        # 41.67 is inside the tolerance and is the same contract as 41.666...;
        # the warning used to appear for one and not the other.
        config = _sr1_config(as_of)
        if tick is not None:
            config["hedge"]["contract_dv01"] = tick
        codes = {w["code"] for w in commands.hedge(config)["evidence"]["warnings"]}
        assert "sr1_notional_assumed" in codes


def _sr1_config(as_of) -> dict:
    """A ``hedge`` config on SR1 months, February to December 2026, at 96.00."""
    futures = []
    for month in range(2, 13):
        start = date(2026, month, 1)
        end = date(2026 + month // 12, month % 12 + 1, 1)
        futures.append(
            {"label": f"SR1-{month:02d}", "start": start.isoformat(), "end": end.isoformat(),
             "price": 96.0}
        )
    return {
        "as_of": as_of.isoformat(),
        "curve": {
            "stub": {"end": "2026-02-01", "accrual_factor": 1.0 + 0.04 * 17 / 360.0},
            "futures": futures,
        },
        "swap": {
            "effective": "2026-03-02",
            "maturity": "2026-12-01",
            "fixed_rate": 0.04,
            "notional": 100_000_000.0,
            "side": "payer",
        },
        "hedge": {"shocks_bp": [-10, 10]},
    }


class TestTheHedgeCommandShocks:
    """``hedge.shocks_bp`` through the command."""

    def test_a_zero_shock_in_the_config_is_refused_by_name(self, as_of):
        # It used to reach the caller as ZeroDivisionError, exit code 1.
        config = _hedge_config(as_of)
        config["hedge"]["shocks_bp"] = [10, 0]
        with pytest.raises(ConfigurationError, match=r"hedge\.shocks_bp\[1\]"):
            commands.hedge(config)


class TestTheHedgeCommandCoverage:
    """``hedge.require_full_coverage`` through the command, which never exposed it."""

    @staticmethod
    def _short(as_of) -> dict:
        config = _hedge_config(as_of)
        config["curve"]["futures"] = config["curve"]["futures"][:-1]
        return config

    def test_a_short_strip_is_refused_by_default(self, as_of):
        with pytest.raises(IncompleteStripError, match="runs to"):
            commands.hedge(self._short(as_of))

    def test_the_check_can_be_waived_from_the_config(self, as_of):
        config = self._short(as_of)
        config["hedge"]["require_full_coverage"] = False
        payload = commands.hedge(config)
        assert len(payload["contracts"]) == 8
        assert payload["evidence"]["fields"]["coverage_checked"] is False

    @pytest.mark.parametrize("value", ["false", 0, None])
    def test_a_flag_that_is_not_a_boolean_is_refused(self, as_of, value):
        # bool("false") is True: coercing would turn a request to waive the
        # check into one to enforce it, or the other way round for 0.
        config = self._short(as_of)
        config["hedge"]["require_full_coverage"] = value
        with pytest.raises(ConfigurationError, match="hedge.require_full_coverage"):
            commands.hedge(config)
