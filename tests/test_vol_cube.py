"""PRD-002 AC-3.1 to PRD-002 AC-3.4: what the cube reads off the grid, and what it refuses.

The cube's job is narrow on purpose. Inside a quoted ``(expiry, tenor)``
smile it fills strikes with SABR and says so in the evidence; across expiries
or tenors it refuses, because interpolating a surface is a modelling decision
that v2 has not made. These tests pin both halves — the filling and the
refusing — and pin the serialisation that makes a stored cube unambiguous.

The fixture is ``tests/fixtures/swaption_vol_cube.csv``, constructed rather
than observed; its provenance sibling says so and says how. Its smile is
quadratic in moneyness, which SABR is not, so PRD-002 AC-3.2's residual measures the
expansion against a shape it did not generate.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import date
from pathlib import Path

import pytest

from rates_engine.core.errors import (
    CalibrationError,
    ShiftRequiredError,
    SliceNotQuotedError,
    VolatilityError,
)
from rates_engine.core.evidence import DataQuality
from rates_engine.volatility.cube import CubePoint, StrikeConvention, VolCube
from rates_engine.volatility.units import Volatility, VolUnits

AS_OF = date(2026, 9, 16)
FIXTURE = Path(__file__).parent / "fixtures" / "swaption_vol_cube.csv"
HOLE = (5.0, 10.0, -0.005)
"""The strike the fixture deliberately does not quote: 5y10y, 50 bp below."""


def _rows() -> list[dict[str, float]]:
    with FIXTURE.open(encoding="utf-8") as handle:
        return [{k: float(v) for k, v in row.items()} for row in csv.DictReader(handle)]


@pytest.fixture(scope="module")
def rows() -> list[dict[str, float]]:
    """The fixture rows, parsed once."""
    return _rows()


@pytest.fixture(scope="module")
def forwards(rows) -> dict[tuple[float, float], float]:
    """The forward per slice, as the fixture records it."""
    return {(r["expiry"], r["tenor"]): r["forward"] for r in rows}


@pytest.fixture(scope="module")
def cube(rows) -> VolCube:
    """The fixture cube, absolute strikes, normal basis points."""
    return VolCube(
        as_of=AS_OF,
        points=tuple(
            CubePoint(
                expiry=r["expiry"],
                tenor=r["tenor"],
                strike=r["strike"],
                volatility=Volatility(r["normal_vol_bp"], VolUnits.NORMAL_BP),
            )
            for r in rows
        ),
    )


class TestFixtureIsWhatItClaims:
    """Before trusting the fixture, check it says what the tests assume."""

    def test_provenance_marks_it_constructed(self):
        record = json.loads(FIXTURE.with_suffix(".csv.provenance.json").read_text())
        assert record["data_quality"] == "synthetic"
        assert "no market quote was copied" in record["source"]

    def test_the_hole_is_actually_missing(self, rows, forwards):
        expiry, tenor, offset = HOLE
        strike = forwards[(expiry, tenor)] + offset
        present = [
            r
            for r in rows
            if r["expiry"] == expiry
            and r["tenor"] == tenor
            and math.isclose(r["strike"], strike)
        ]
        assert present == [], "PRD-002 AC-3.1 needs a gap; the fixture has stopped having one"

    def test_every_other_slice_is_complete(self, cube):
        sizes = {key: len(points) for key, points in cube.slices.items()}
        assert sizes == {(1.0, 5.0): 7, (5.0, 5.0): 7, (5.0, 10.0): 6, (10.0, 10.0): 7}


class TestQuotedPointsComeBackUntouched:
    """A strike the grid holds is read, never refitted."""

    def test_source_is_quoted(self, cube, forwards):
        forward = forwards[(5.0, 5.0)]
        quote = cube.volatility_at(5.0, 5.0, forward, forward)
        assert quote.source == "quoted"
        assert quote.calibration is None

    def test_value_is_the_quote_itself(self, cube, forwards, rows):
        forward = forwards[(1.0, 5.0)]
        strike = forward + 0.005
        expected = next(
            r["normal_vol_bp"]
            for r in rows
            if r["expiry"] == 1.0 and r["tenor"] == 5.0 and math.isclose(r["strike"], strike)
        )
        quote = cube.volatility_at(1.0, 5.0, strike, forward)
        assert quote.volatility.value == expected
        assert quote.volatility.units is VolUnits.NORMAL_BP

    def test_a_quoted_read_claims_no_synthetic_input(self, cube, forwards):
        forward = forwards[(5.0, 5.0)]
        quote = cube.volatility_at(5.0, 5.0, forward, forward)
        assert quote.evidence.worst_quality is DataQuality.OBSERVED
        assert quote.evidence.fields["source"] == "quoted"


class TestAQuotedStrikeIsFoundWhicheverWayItWasComputed:
    """A strike is matched to a quote within a tolerance, not by float equality.

    ``forward + offset`` and the strike the fixture writes down differ by a
    rounding error -- 6.9e-18 at most here -- and exact equality called 8 of
    the 27 quoted points unquoted, refitted them and reported ``sabr``.
    """

    def test_the_tolerance_is_far_below_any_real_strike_spacing(self):
        from rates_engine.volatility.cube import STRIKE_TOLERANCE

        # A millionth of a basis point: well above float noise on a rate,
        # and nothing a market would ever quote as two different strikes.
        assert 1e-15 < STRIKE_TOLERANCE <= 1e-10

    def test_an_absolute_cube_reads_forward_plus_offset_as_the_quote(self, cube, rows):
        for r in rows:
            strike = r["forward"] + r["strike_offset"]
            quote = cube.volatility_at(r["expiry"], r["tenor"], strike, r["forward"])
            assert quote.source == "quoted", (r, strike)
            assert quote.volatility.value == r["normal_vol_bp"]

    def test_a_relative_cube_reads_the_written_strike_as_the_quote(self, rows):
        relative = VolCube(
            as_of=AS_OF,
            points=tuple(
                CubePoint(
                    r["expiry"], r["tenor"], r["strike_offset"],
                    Volatility(r["normal_vol_bp"], VolUnits.NORMAL_BP),
                )
                for r in rows
            ),
            strike_convention=StrikeConvention.RELATIVE_TO_FORWARD,
        )
        for r in rows:
            quote = relative.volatility_at(r["expiry"], r["tenor"], r["strike"], r["forward"])
            assert quote.source == "quoted", r
            assert quote.volatility.value == r["normal_vol_bp"]

    def test_a_strike_a_tenth_of_a_basis_point_away_is_not_the_quote(self, cube, forwards):
        forward = forwards[(1.0, 5.0)]
        quote = cube.volatility_at(1.0, 5.0, forward + 1e-5, forward)
        assert quote.source == "sabr"


class TestExtrapolationSaysSo:
    """Outside the quoted strikes SABR extrapolates, and the answer is marked.

    The 1y5y smile is quoted from 3.25% to 5.25%. A strike of 20% used to come
    back as 248.5 bp, ``source="sabr"`` and an evidence note saying it was
    interpolated.
    """

    @pytest.fixture
    def far(self, cube, forwards):
        return cube.volatility_at(1.0, 5.0, 0.20, forwards[(1.0, 5.0)])

    def test_it_is_labelled_an_extrapolation(self, far):
        assert far.source == "sabr_extrapolated"
        assert far.reliable is False
        assert far.evidence.fields["extrapolated"] is True
        assert far.evidence.fields["quoted_strike_range"] == pytest.approx([0.0325, 0.0525])

    def test_it_carries_a_warning_that_marks_it_assumed(self, far):
        codes = [w.code for w in far.evidence.warnings]
        assert codes == ["outside_quoted_strikes"]
        assert far.evidence.worst_quality is DataQuality.ASSUMED
        assert "extrapolat" in far.evidence.warnings[0].message

    def test_the_provenance_no_longer_calls_it_interpolated(self, far):
        notes = far.evidence.inputs[0].notes or ""
        assert "Interpolated" not in notes
        assert "Extrapolated" in notes

    @pytest.mark.parametrize("strike", [0.005, 0.01, 0.10])
    def test_both_wings_are_caught(self, cube, forwards, strike):
        quote = cube.volatility_at(1.0, 5.0, strike, forwards[(1.0, 5.0)])
        assert quote.source == "sabr_extrapolated"
        assert not quote.reliable

    def test_a_fill_inside_the_smile_is_reliable_and_unwarned(self, cube, forwards):
        expiry, tenor, offset = HOLE
        forward = forwards[(expiry, tenor)]
        inside = cube.volatility_at(expiry, tenor, forward + offset, forward)
        assert inside.source == "sabr"
        assert inside.reliable is True
        assert inside.evidence.warnings == ()
        assert inside.evidence.fields["extrapolated"] is False

    def test_a_quote_is_reliable(self, cube, forwards):
        forward = forwards[(5.0, 5.0)]
        assert cube.volatility_at(5.0, 5.0, forward, forward).reliable is True

    def test_the_payload_carries_the_flag(self, far):
        payload = far.payload_fields()
        assert payload["reliable"] is False
        assert payload["source"] == "sabr_extrapolated"
        json.dumps(far.to_dict())


class TestSABRFillsTheGap:
    """PRD-002 AC-3.1: an unquoted strike comes from the smile of its own slice."""

    @pytest.fixture
    def filled(self, cube, forwards):
        expiry, tenor, offset = HOLE
        forward = forwards[(expiry, tenor)]
        return cube.volatility_at(expiry, tenor, forward + offset, forward)

    def test_source_is_sabr(self, filled):
        assert filled.source == "sabr"
        assert filled.calibration is not None

    def test_evidence_names_the_quoted_strikes_used(self, filled, rows):
        used = filled.evidence.fields["quoted_strikes_used"]
        quoted = sorted(
            r["strike"] for r in rows if r["expiry"] == 5.0 and r["tenor"] == 10.0
        )
        assert sorted(used) == pytest.approx(quoted)
        assert filled.strike not in used, "the gap is not one of its own inputs"

    def test_evidence_carries_all_four_parameters(self, filled):
        parameters = filled.evidence.fields["parameters"]
        assert set(parameters) >= {"alpha", "beta", "rho", "nu"}
        assert parameters["beta"] == pytest.approx(0.5)

    def test_a_filled_read_is_marked_synthetic(self, filled):
        assert filled.evidence.worst_quality is DataQuality.SYNTHETIC
        assert filled.evidence.sources, "the fit's own evidence should be nested"

    def test_the_filled_value_sits_between_its_neighbours(self, filled, rows):
        """A 50 bp-below read should land between the -100 and -25 quotes.

        Not a tolerance on the model, a sanity check on monotonicity: this
        smile falls as the strike falls on that wing, so the interpolated
        point cannot be outside the two quotes that bracket it.
        """
        neighbours = {
            round(r["strike"], 6): r["normal_vol_bp"]
            for r in rows
            if r["expiry"] == 5.0 and r["tenor"] == 10.0
        }
        low = neighbours[round(0.0398 - 0.010, 6)]
        high = neighbours[round(0.0398 - 0.0025, 6)]
        assert min(low, high) < filled.volatility.value < max(low, high)

    def test_reading_twice_gives_the_same_number(self, cube, forwards):
        expiry, tenor, offset = HOLE
        forward = forwards[(expiry, tenor)]
        first = cube.volatility_at(expiry, tenor, forward + offset, forward)
        second = cube.volatility_at(expiry, tenor, forward + offset, forward)
        assert first.volatility.value == second.volatility.value


class TestExpansionQuality:
    """PRD-002 AC-3.2: how closely Hagan's formula tracks the quoted smile."""

    @pytest.mark.parametrize(
        "expiry,tenor", [(1.0, 5.0), (5.0, 5.0), (5.0, 10.0), (10.0, 10.0)]
    )
    def test_rmse_under_half_a_basis_point(self, cube, forwards, expiry, tenor):
        fit = cube.calibrate_slice(expiry, tenor, forwards[(expiry, tenor)])
        assert fit.rmse_bp < 0.5, f"{expiry}x{tenor} fitted to {fit.rmse_bp:.4f} bp"

    def test_the_worst_single_strike_is_also_small(self, cube, forwards):
        worst = max(
            cube.calibrate_slice(e, t, forwards[(e, t)]).max_error_bp
            for e, t in cube.slices
        )
        assert worst < 1.0, f"worst single-strike miss {worst:.4f} bp"

    def test_the_fit_is_not_trivially_flat(self, cube, forwards):
        """A fit that collapsed to a flat smile would also have small RMSE here
        only if the smile were flat. It is not, so nu must be away from zero."""
        fit = cube.calibrate_slice(5.0, 5.0, forwards[(5.0, 5.0)])
        assert fit.parameters.nu > 0.1
        assert fit.parameters.rho < 0.0, "the fixture skew is negative"


class TestItRefusesAcrossSlices:
    """v2 fills strikes. It does not invent expiries or tenors."""

    def test_an_unquoted_expiry_refuses(self, cube, forwards):
        with pytest.raises(SliceNotQuotedError) as excinfo:
            cube.volatility_at(3.0, 5.0, 0.04, 0.04)
        assert "3.0" in str(excinfo.value)

    def test_an_unquoted_tenor_refuses(self, cube):
        with pytest.raises(SliceNotQuotedError):
            cube.volatility_at(5.0, 7.0, 0.04, 0.04)

    def test_the_refusal_lists_what_it_does_have(self, cube):
        with pytest.raises(SliceNotQuotedError) as excinfo:
            cube.calibrate_slice(2.0, 2.0, 0.04)
        message = str(excinfo.value)
        assert "(5.0, 10.0)" in message
        assert "does not interpolate across expiry or tenor" in message

    def test_calibrating_an_unquoted_slice_refuses(self, cube):
        with pytest.raises(SliceNotQuotedError):
            cube.calibrate_slice(30.0, 30.0, 0.04)


class TestThinAndBrokenSlices:
    """A slice that cannot support a fit says so instead of producing one."""

    def test_two_strikes_cannot_pin_three_parameters(self):
        cube = VolCube(
            as_of=AS_OF,
            points=tuple(
                CubePoint(1.0, 5.0, k, Volatility(v, VolUnits.NORMAL_BP))
                for k, v in ((0.039, 96.0), (0.041, 95.0))
            ),
        )
        with pytest.raises(CalibrationError):
            cube.calibrate_slice(1.0, 5.0, 0.04)

    def test_a_negative_forward_needs_a_shift(self, cube):
        """PRD-002 AC-3.3, through the cube rather than the model directly."""
        with pytest.raises(ShiftRequiredError):
            cube.calibrate_slice(5.0, 5.0, -0.002)


class TestUnitsAreEnforcedNotAssumed:
    """A cube with mixed or lognormal quotes is refused at construction."""

    def test_mixed_units_refuse(self):
        with pytest.raises(VolatilityError) as excinfo:
            VolCube(
                as_of=AS_OF,
                points=(
                    CubePoint(1.0, 5.0, 0.04, Volatility(95.0, VolUnits.NORMAL_BP)),
                    CubePoint(1.0, 5.0, 0.045, Volatility(0.25, VolUnits.LOGNORMAL_DECIMAL)),
                ),
            )
        assert "mixed units" in str(excinfo.value)

    def test_a_lognormal_cube_refuses(self):
        with pytest.raises(VolatilityError) as excinfo:
            VolCube(
                as_of=AS_OF,
                points=(
                    CubePoint(1.0, 5.0, 0.04, Volatility(0.25, VolUnits.LOGNORMAL_DECIMAL)),
                ),
                units=VolUnits.LOGNORMAL_DECIMAL,
            )
        assert "normal volatility" in str(excinfo.value)


class TestRelativeStrikes:
    """The same smile written as offsets reads identically."""

    @pytest.fixture
    def relative(self, rows) -> VolCube:
        return VolCube(
            as_of=AS_OF,
            points=tuple(
                CubePoint(
                    r["expiry"],
                    r["tenor"],
                    r["strike_offset"],
                    Volatility(r["normal_vol_bp"], VolUnits.NORMAL_BP),
                )
                for r in rows
            ),
            strike_convention=StrikeConvention.RELATIVE_TO_FORWARD,
        )

    def test_a_quoted_offset_reads_as_quoted(self, relative, forwards):
        forward = forwards[(5.0, 5.0)]
        quote = relative.volatility_at(5.0, 5.0, forward + 0.005, forward)
        assert quote.source == "quoted"

    def test_the_gap_fills_to_the_same_number(self, relative, cube, forwards):
        expiry, tenor, offset = HOLE
        forward = forwards[(expiry, tenor)]
        absolute = cube.volatility_at(expiry, tenor, forward + offset, forward)
        shifted = relative.volatility_at(expiry, tenor, forward + offset, forward)
        assert shifted.volatility.value == pytest.approx(absolute.volatility.value)

    def test_the_convention_reaches_the_evidence(self, relative, forwards):
        forward = forwards[(5.0, 5.0)]
        quote = relative.volatility_at(5.0, 5.0, forward, forward)
        assert quote.evidence.fields["strike_convention"] == "relative_to_forward"


class TestSerialisation:
    """PRD-002 AC-3.4: a stored cube is unambiguous on its own."""

    def test_it_states_units_convention_and_date(self, cube):
        stored = cube.to_dict()
        assert stored["units"] == "normal_bp"
        assert stored["strike_convention"] == "absolute"
        assert stored["as_of"] == "2026-09-16"

    def test_it_states_the_backbone_and_shift_a_fit_would_use(self, cube):
        stored = cube.to_dict()
        assert stored["beta"] == pytest.approx(0.5)
        assert stored["shift"] == 0.0

    def test_every_point_survives_the_round_trip(self, cube):
        stored = cube.to_dict()
        assert stored["points"] == len(cube.points)
        assert sum(len(s["strikes"]) for s in stored["slices"]) == len(cube.points)

    def test_it_is_json_serialisable(self, cube):
        json.dumps(cube.to_dict())

    def test_a_point_serialises_its_own_units(self, cube):
        point = cube.points[0].to_dict()
        assert point["volatility"]["units"] == "normal_bp"

    def test_a_quote_payload_carries_the_fit_when_there_was_one(self, cube, forwards):
        expiry, tenor, offset = HOLE
        forward = forwards[(expiry, tenor)]
        payload = cube.volatility_at(expiry, tenor, forward + offset, forward).payload_fields()
        assert payload["source"] == "sabr"
        assert payload["calibration"]["parameters"]["beta"] == pytest.approx(0.5)
        json.dumps(payload)

    def test_a_quoted_payload_has_no_calibration(self, cube, forwards):
        forward = forwards[(5.0, 5.0)]
        payload = cube.volatility_at(5.0, 5.0, forward, forward).payload_fields()
        assert payload["calibration"] is None
