"""PRD-001 AC-7.1 to PRD-001 AC-7.5: the dual-curve solver, validated on inputs it was given.

Every input here is constructed. That is the decision, not an accident: there
is no free source of Term SOFR par rates or OIS-versus-term basis spreads, so
v1 proves the solver rather than a market fit. The tests are chosen to be the
ones that stay meaningful under that constraint — properties that must hold
for any basis, and an exact identity at zero basis.
"""

from __future__ import annotations

from datetime import date

import pytest

from rates_engine.conventions import DayCount, year_fraction
from rates_engine.curves import DiscountCurve, RealizedStubNode
from rates_engine.curves.dual import (
    SYNTHETIC,
    BasisSwapNode,
    TenorParSwapNode,
    solve_dual_curve,
)
from rates_engine.errors import NoTenorQuoteSourceError, UnderdeterminedCurveError
from rates_engine.evidence import DataQuality

AS_OF = date(2026, 1, 15)
MATURITIES = tuple(date(2026 + k, 1, 15) for k in range(1, 5))


def _ois_instruments(rate: float = 0.04):
    """A flat OIS curve pinned by one stub and three par swaps."""
    from rates_engine.curves import ParSwapNode

    instruments: list[object] = [
        RealizedStubNode(
            end=MATURITIES[0],
            accrual_factor=1.0 + rate * ((MATURITIES[0] - AS_OF).days / 360.0),
        )
    ]
    for index in range(1, len(MATURITIES)):
        payments = MATURITIES[: index + 1]
        fractions = tuple(
            year_fraction(AS_OF if k == 0 else payments[k - 1], payments[k], DayCount.ACT_360)
            for k in range(len(payments))
        )
        instruments.append(
            ParSwapNode(
                start=AS_OF,
                payment_dates=payments,
                year_fractions=fractions,
                quoted_rate=rate,
                label=f"ois_{index + 1}y",
            )
        )
    return tuple(instruments)


def _periods(upto: int):
    return tuple(
        (AS_OF if k == 0 else MATURITIES[k - 1], MATURITIES[k]) for k in range(upto + 1)
    )


def _dual_instruments(basis: float):
    """Tenor par swaps and basis swaps consistent with a flat basis."""
    tenor_nodes = []
    for index in (0, 1):
        payments = MATURITIES[: index + 1]
        fractions = tuple(
            year_fraction(AS_OF if k == 0 else payments[k - 1], payments[k], DayCount.ACT_360)
            for k in range(len(payments))
        )
        tenor_nodes.append(
            TenorParSwapNode(
                start=AS_OF,
                payment_dates=payments,
                year_fractions=fractions,
                float_periods=_periods(index),
                quoted_rate=0.04 + basis,
                label=f"term_{index + 1}y",
            )
        )
    basis_nodes = [
        BasisSwapNode(float_periods=_periods(index), quoted_spread=basis, label=f"basis_{index + 1}y")
        for index in (2, 3)
    ]
    return tuple(tenor_nodes), tuple(basis_nodes)


class TestSyntheticGate:
    """PRD-001 AC-7.5: asking for real quotes is refused, and the origin is declared."""

    def test_a_real_source_refuses(self):
        tenor, basis = _dual_instruments(0.0005)
        with pytest.raises(NoTenorQuoteSourceError, match="no free source"):
            solve_dual_curve(
                AS_OF, _ois_instruments(), tenor, basis, inputs_origin="ice_swap_rate"
            )

    def test_the_refusal_points_at_v1_1(self):
        tenor, basis = _dual_instruments(0.0005)
        with pytest.raises(NoTenorQuoteSourceError, match="v1.1"):
            solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis, inputs_origin="market")

    def test_the_result_declares_synthetic_inputs(self):
        tenor, basis = _dual_instruments(0.0005)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis)
        assert result.inputs_origin == SYNTHETIC
        assert result.to_dict()["inputs_origin"] == SYNTHETIC
        assert "no free source" in result.evidence.fields["inputs_origin_note"]

    def test_the_inputs_are_marked_synthetic_in_the_evidence(self):
        tenor, basis = _dual_instruments(0.0005)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis)
        assert DataQuality.SYNTHETIC in result.evidence.qualities
        assert result.evidence.worst_quality is DataQuality.SYNTHETIC

    def test_synthetic_ranks_better_than_proxy(self):
        # Deliberate and controlled beats a real number standing in for
        # another one: the rank in DataQuality says so, and it matters here.
        assert DataQuality.SYNTHETIC.rank < DataQuality.PROXY.rank


class TestRepricing:
    """PRD-001 AC-7.1: every instrument reprices under the simultaneous solve."""

    @pytest.mark.parametrize("basis", [0.0, 0.0005, 0.0025])
    def test_all_residuals_are_inside_tolerance(self, basis):
        tenor, basis_nodes = _dual_instruments(basis)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        assert max(abs(r) for r in result.residuals_bp.values()) < 0.01

    def test_the_tenor_curve_sits_above_the_ois_curve_with_a_positive_basis(self):
        tenor, basis_nodes = _dual_instruments(0.0025)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        for node in result.tenor.nodes:
            assert result.tenor.df(node) < result.ois.df(node)

    def test_zero_basis_makes_the_two_curves_coincide(self):
        tenor, basis_nodes = _dual_instruments(0.0)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        for node in result.tenor.nodes:
            assert result.tenor.df(node) == pytest.approx(result.ois.df(node), rel=1e-9)


class TestModes:
    """PRD-001 AC-7.2: the two modes are compared, and agree when there is nothing to feed back."""

    def test_zero_basis_makes_the_modes_agree(self):
        tenor, basis_nodes = _dual_instruments(0.0)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        assert result.sequential_vs_simultaneous_bp is not None
        assert max(abs(v) for v in result.sequential_vs_simultaneous_bp.values()) < 0.01

    def test_the_difference_is_reported_per_tenor(self):
        tenor, basis_nodes = _dual_instruments(0.0025)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        comparison = result.sequential_vs_simultaneous_bp
        assert comparison is not None
        assert set(comparison) == {n.isoformat() for n in result.tenor.nodes}

    def test_both_modes_reprice(self):
        tenor, basis_nodes = _dual_instruments(0.0015)
        for mode in ("sequential", "simultaneous"):
            result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes, mode=mode)
            assert result.mode == mode
            assert max(abs(r) for r in result.residuals_bp.values()) < 0.01

    def test_an_unknown_mode_refuses(self):
        tenor, basis_nodes = _dual_instruments(0.0)
        with pytest.raises(ValueError, match="simultaneous"):
            solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes, mode="clever")

    def test_the_comparison_can_be_skipped(self):
        tenor, basis_nodes = _dual_instruments(0.0)
        result = solve_dual_curve(
            AS_OF, _ois_instruments(), tenor, basis_nodes, compare_modes=False
        )
        assert result.sequential_vs_simultaneous_bp is None
        assert result.to_dict()["sequential_vs_simultaneous_bp"] is None


class TestForwardBasis:
    """PRD-001 AC-7.3: the forward basis reproduces the spread that was fed in."""

    def test_the_forward_basis_matches_the_quoted_spread(self):
        quoted = 0.0025
        tenor, basis_nodes = _dual_instruments(quoted)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        for node in basis_nodes:
            assert node.par_spread(result.ois, result.tenor) * 1e4 == pytest.approx(
                quoted * 1e4, abs=0.1
            )

    def test_it_is_reported_per_period(self):
        tenor, basis_nodes = _dual_instruments(0.0025)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        assert result.forward_basis_bp
        assert all(":" in key for key in result.forward_basis_bp)

    def test_zero_basis_gives_a_zero_forward_basis(self):
        tenor, basis_nodes = _dual_instruments(0.0)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        assert max(abs(v) for v in result.forward_basis_bp.values()) < 0.1


class TestUnderdetermined:
    """PRD-001 AC-7.4: a node with no instrument of its own is named, not guessed."""

    def test_two_instruments_on_one_node_refuses(self):
        tenor, basis_nodes = _dual_instruments(0.0005)
        clash = BasisSwapNode(
            float_periods=tenor[0].float_periods, quoted_spread=0.0005, label="clash"
        )
        with pytest.raises(UnderdeterminedCurveError, match="more than one instrument"):
            solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes + (clash,))

    def test_the_refusal_names_the_node(self):
        tenor, basis_nodes = _dual_instruments(0.0005)
        clash = BasisSwapNode(
            float_periods=tenor[0].float_periods, quoted_spread=0.0005, label="clash"
        )
        with pytest.raises(UnderdeterminedCurveError) as caught:
            solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes + (clash,))
        assert tenor[0].node_date.isoformat() in str(caught.value)

    def test_no_dual_instruments_at_all_refuses(self):
        with pytest.raises(UnderdeterminedCurveError, match="nothing to pin"):
            solve_dual_curve(AS_OF, _ois_instruments(), (), ())


class TestPayload:
    """Both curves and every diagnostic reach the payload."""

    def test_the_payload_carries_both_curves(self):
        tenor, basis_nodes = _dual_instruments(0.0015)
        payload = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes).to_dict()
        assert payload["ois_curve"]["nodes"]
        assert payload["tenor_curve"]["nodes"]
        assert payload["max_abs_residual_bp"] < 0.01
        assert payload["schema_version"] == "1.0"

    def test_a_synthetic_tenor_curve_is_a_real_discount_curve(self):
        tenor, basis_nodes = _dual_instruments(0.0015)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        assert isinstance(result.tenor, DiscountCurve)
        assert all(
            b <= a for a, b in zip(result.tenor.dfs, result.tenor.dfs[1:], strict=False)
        )
        assert all(df > 0 for df in result.tenor.dfs)

    def test_the_ois_bootstrap_evidence_chains_in(self):
        tenor, basis_nodes = _dual_instruments(0.0015)
        result = solve_dual_curve(AS_OF, _ois_instruments(), tenor, basis_nodes)
        assert result.evidence.sources[0].produced_by == "curves.bootstrap_discount_curve"
