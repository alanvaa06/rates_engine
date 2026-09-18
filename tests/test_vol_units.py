"""PRD-002 AC-1.4: the units trap, closed at the type and backstopped by magnitude.

The mistake this guards against is worth four orders of magnitude, and it is
made by passing the right number to the wrong parameter. Two defences: a
volatility that knows what it is and refuses to convert, and a plausibility
band for the case where the units are right and the number is not.
"""

from __future__ import annotations

import pytest

from rates_engine.errors import VolUnitsError
from rates_engine.volatility.units import (
    LOGNORMAL_MAX_DECIMAL,
    LOGNORMAL_MIN_DECIMAL,
    NORMAL_MAX_BP,
    NORMAL_MIN_BP,
    Volatility,
    VolUnits,
)


class TestNoSilentConversion:
    """The first defence: the two kinds do not turn into each other."""

    def test_a_lognormal_vol_refuses_to_be_read_as_normal(self):
        with pytest.raises(VolUnitsError, match="at the money only"):
            Volatility.lognormal_percent(30.0).as_normal_decimal()

    def test_a_normal_vol_refuses_to_be_read_as_lognormal(self):
        with pytest.raises(VolUnitsError, match="cannot be converted"):
            Volatility.normal_bp(80.0).as_lognormal_decimal()

    def test_each_reads_in_its_own_units(self):
        assert Volatility.normal_bp(80.0).as_normal_decimal() == pytest.approx(0.008)
        assert Volatility.normal_bp(80.0).as_normal_bp() == pytest.approx(80.0)
        assert Volatility.lognormal_percent(30.0).as_lognormal_decimal() == pytest.approx(0.30)

    def test_the_decimal_and_quoted_spellings_agree(self):
        assert Volatility(0.008, VolUnits.NORMAL_DECIMAL).as_normal_bp() == pytest.approx(80.0)
        assert Volatility(0.30, VolUnits.LOGNORMAL_DECIMAL).as_lognormal_decimal() == 0.30

    def test_the_kind_is_readable_without_guessing(self):
        assert Volatility.normal_bp(80.0).is_normal
        assert not Volatility.lognormal_percent(30.0).is_normal
        assert VolUnits.NORMAL_DECIMAL.is_normal
        assert VolUnits.LOGNORMAL_PERCENT.is_lognormal


class TestTheTrapItself:
    """The second defence: the right enum with an implausible number in it."""

    def test_a_lognormal_decimal_passed_as_a_normal_one_is_caught(self):
        # 30% read as a normal decimal is 3,000 bp of absolute volatility.
        with pytest.raises(VolUnitsError, match="3,000 bp"):
            Volatility(0.30, VolUnits.NORMAL_DECIMAL)

    def test_a_normal_decimal_passed_as_a_lognormal_one_is_caught(self):
        # This is the case the PRD's original 0.5% floor let through: a
        # perfectly ordinary 80 bp normal vol, read as 0.8% lognormal.
        with pytest.raises(VolUnitsError, match="0.80000%"):
            Volatility(0.0080, VolUnits.LOGNORMAL_DECIMAL)

    @pytest.mark.parametrize("normal_bp", [60.0, 80.0, 100.0, 150.0])
    def test_the_whole_population_of_real_normal_vols_is_caught(self, normal_bp):
        with pytest.raises(VolUnitsError):
            Volatility(normal_bp * 1e-4, VolUnits.LOGNORMAL_DECIMAL)

    @pytest.mark.parametrize("lognormal_pct", [15.0, 25.0, 40.0, 60.0])
    def test_no_real_lognormal_vol_is_a_false_positive(self, lognormal_pct):
        assert Volatility.lognormal_percent(lognormal_pct).as_lognormal_decimal() > 0

    @pytest.mark.parametrize("normal_bp", [1.0, 60.0, 150.0, 400.0])
    def test_no_real_normal_vol_is_a_false_positive(self, normal_bp):
        assert Volatility.normal_bp(normal_bp).as_normal_bp() == pytest.approx(normal_bp)

    def test_basis_points_read_as_percent_are_caught(self):
        with pytest.raises(VolUnitsError, match="ceiling"):
            Volatility(800.0, VolUnits.LOGNORMAL_PERCENT)

    def test_the_bands_sit_between_the_two_populations(self):
        # The floor has to be above every plausible normal-vol-as-decimal and
        # below every plausible lognormal vol. That gap is the whole design.
        assert 150e-4 < LOGNORMAL_MIN_DECIMAL < 0.15
        assert NORMAL_MIN_BP < 60.0 < 150.0 < NORMAL_MAX_BP
        assert LOGNORMAL_MAX_DECIMAL > 1.0


class TestDegenerateAndEscape:
    """Zero is allowed, negatives are not, and the band has a door."""

    @pytest.mark.parametrize("units", list(VolUnits))
    def test_zero_is_allowed_in_every_unit(self, units):
        assert Volatility(0.0, units).value == 0.0

    @pytest.mark.parametrize("units", list(VolUnits))
    def test_a_negative_volatility_is_refused_in_every_unit(self, units):
        with pytest.raises(ValueError, match="non-negative"):
            Volatility(-0.01, units)

    def test_unchecked_skips_the_band_but_not_the_units(self):
        extreme = Volatility.unchecked(0.008, VolUnits.LOGNORMAL_DECIMAL)
        assert extreme.as_lognormal_decimal() == 0.008
        with pytest.raises(VolUnitsError):
            extreme.as_normal_decimal()

    def test_unchecked_still_refuses_a_negative(self):
        with pytest.raises(ValueError, match="non-negative"):
            Volatility.unchecked(-1.0, VolUnits.NORMAL_BP)


class TestAtMoneyEquivalence:
    """PRD-002 AC-1.3's cross-check, and the honesty about what it is."""

    def test_the_equivalence_round_trips(self):
        forward = 0.04
        lognormal = Volatility.lognormal_percent(20.0)
        normal = lognormal.atm_equivalent_normal(forward)
        assert normal.as_normal_bp() == pytest.approx(0.20 * forward * 1e4)
        back = normal.atm_equivalent_lognormal(forward)
        assert back.as_lognormal_decimal() == pytest.approx(0.20)

    def test_it_refuses_the_wrong_direction(self):
        with pytest.raises(VolUnitsError):
            Volatility.normal_bp(80.0).atm_equivalent_normal(0.04)
        with pytest.raises(VolUnitsError):
            Volatility.lognormal_percent(20.0).atm_equivalent_lognormal(0.04)

    def test_a_lognormal_equivalent_is_undefined_at_a_non_positive_forward(self):
        with pytest.raises(ValueError, match="undefined"):
            Volatility.normal_bp(80.0).atm_equivalent_lognormal(0.0)

    def test_it_is_named_for_what_it_is(self):
        # The method that could be mistaken for a unit conversion says so in
        # its own name, and the one that would be a conversion does not exist.
        assert not hasattr(Volatility, "to_normal")
        assert hasattr(Volatility, "atm_equivalent_normal")


class TestSerialisation:
    """A serialised volatility carries its units or it carries nothing."""

    def test_the_payload_has_both_fields(self):
        assert Volatility.normal_bp(80.0).to_dict() == {"value": 80.0, "units": "normal_bp"}
