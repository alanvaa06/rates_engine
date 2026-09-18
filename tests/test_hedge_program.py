"""PRD-003 AC-5.1 and PRD-003 AC-5.2: the policy is data, and a typo in it is loud.

The failure this guards against is specific: a policy file with
`target_hedge_ration: 0.8` in it loads, ignores the line, and produces a
hedge that looks compliant because the rule never existed. Strict loading
turns that into a refusal naming the key.

The audit is the other half, and the choice there is to report rather than
enforce by default. A treasurer outside the band at month end usually
knows; a library that refuses to compute is not more careful than one that
computes and says so, with a severity and what would bring it back.
"""

from __future__ import annotations

import json

import pytest

from rates_engine.errors import (
    ConfigurationError,
    HedgeError,
    PolicyBreachError,
    RatesEngineError,
)
from rates_engine.evidence import DataQuality
from rates_engine.hedge_program import (
    HedgeProgram,
    ProgramAudit,
    RebalanceFrequency,
    Severity,
    audit_hedge,
    load_program,
)

VALID = {
    "name": "treasury_2026",
    "target_hedge_ratio": 0.80,
    "discretion_band": 0.10,
    "rebalance_frequency": "monthly",
    "allowed_instruments": ["forward", "collar", "collar_zero_cost"],
}


@pytest.fixture
def program() -> HedgeProgram:
    return load_program(dict(VALID))


class TestStrictLoading:
    """PRD-003 AC-5.1: an unknown key is a refusal naming it."""

    def test_a_valid_programme_loads(self, program):
        assert program.name == "treasury_2026"
        assert program.target_hedge_ratio == 0.80
        assert program.rebalance_frequency is RebalanceFrequency.MONTHLY
        assert "collar" in program.allowed_instruments

    def test_a_misspelt_key_refuses_and_names_it(self):
        broken = dict(VALID)
        broken["target_hedge_ration"] = broken.pop("target_hedge_ratio")
        with pytest.raises(ConfigurationError) as excinfo:
            load_program(broken)
        assert "target_hedge_ration" in str(excinfo.value)

    def test_the_refusal_lists_what_is_accepted(self):
        with pytest.raises(ConfigurationError) as excinfo:
            load_program({**VALID, "hedge_everything": True})
        message = str(excinfo.value)
        for key in ("target_hedge_ratio", "discretion_band", "rebalance_frequency"):
            assert key in message

    def test_it_says_why_silence_would_be_worse(self):
        with pytest.raises(ConfigurationError) as excinfo:
            load_program({**VALID, "unknown": 1})
        assert "quietly ignored" in str(excinfo.value)

    @pytest.mark.parametrize(
        "missing", ["target_hedge_ratio", "discretion_band", "rebalance_frequency"]
    )
    def test_a_missing_required_key_refuses(self, missing):
        incomplete = {k: v for k, v in VALID.items() if k != missing}
        with pytest.raises(ConfigurationError) as excinfo:
            load_program(incomplete)
        assert missing in str(excinfo.value)

    def test_there_is_no_default_target(self):
        with pytest.raises(ConfigurationError, match="no default target"):
            load_program({"discretion_band": 0.1, "rebalance_frequency": "monthly"})

    def test_an_unknown_frequency_refuses_and_lists_the_real_ones(self):
        with pytest.raises(ConfigurationError) as excinfo:
            load_program({**VALID, "rebalance_frequency": "fortnightly"})
        message = str(excinfo.value)
        assert "fortnightly" in message
        assert "quarterly" in message

    @pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
    def test_a_ratio_outside_zero_to_one_refuses(self, bad):
        with pytest.raises(ConfigurationError, match="fraction in"):
            load_program({**VALID, "target_hedge_ratio": bad})

    def test_a_band_outside_zero_to_one_refuses(self):
        with pytest.raises(ConfigurationError, match="discretion_band"):
            load_program({**VALID, "discretion_band": 1.5})

    def test_an_overlay_is_named_as_out_of_scope(self):
        with pytest.raises(ConfigurationError) as excinfo:
            load_program({**VALID, "target_hedge_ratio": 1.5})
        assert "overlay" in str(excinfo.value)

    def test_allowed_instruments_may_be_omitted(self):
        program = load_program({k: v for k, v in VALID.items() if k != "allowed_instruments"})
        assert program.allowed_instruments == frozenset()

    def test_it_is_catchable_as_an_engine_error(self):
        with pytest.raises(RatesEngineError):
            load_program({"nonsense": 1})


class TestTheBand:
    """Bounds resolve from the target and the band, and clamp."""

    def test_the_bounds_are_the_target_plus_and_minus_the_band(self, program):
        assert program.lower == pytest.approx(0.70)
        assert program.upper == pytest.approx(0.90)

    def test_the_upper_bound_is_capped_at_one(self):
        program = HedgeProgram(0.95, 0.20, RebalanceFrequency.MONTHLY)
        assert program.upper == 1.0

    def test_the_lower_bound_is_floored_at_zero(self):
        program = HedgeProgram(0.05, 0.20, RebalanceFrequency.MONTHLY)
        assert program.lower == 0.0

    def test_a_zero_band_means_the_target_exactly(self):
        program = HedgeProgram(0.80, 0.0, RebalanceFrequency.DAILY)
        assert program.lower == program.upper == 0.80


class TestTheAudit:
    """PRD-003 AC-5.2: severity and suggestion, reported."""

    def test_inside_the_band_is_compliant(self, program):
        audit = audit_hedge(program, 0.85, proposed_instrument="collar")
        assert audit.compliant
        assert [v.severity for v in audit.violations] == [Severity.INFO]

    def test_the_compliant_finding_still_says_something(self, program):
        audit = audit_hedge(program, 0.85, proposed_instrument="collar")
        finding = next(v for v in audit.violations if v.code == "hedge_ratio_within_band")
        assert "inside the programme" in finding.message
        assert "No change is implied" in finding.suggestion

    def test_outside_the_band_is_a_warning_with_a_suggestion(self, program):
        audit = audit_hedge(program, 0.65)
        assert not audit.compliant
        finding = audit.violations[0]
        assert finding.severity is Severity.WARNING
        assert finding.code == "hedge_ratio_outside_band"
        assert "0.7000" in finding.suggestion

    def test_far_outside_is_a_breach(self, program):
        audit = audit_hedge(program, 0.30)
        assert audit.violations[0].severity is Severity.BREACH

    def test_exactly_twice_the_band_is_a_warning_not_a_breach(self, program):
        """0.8 - 0.6 evaluates to 0.20000000000000007, so without a
        tolerance a proposal sitting precisely on the boundary is reported
        one severity worse than the policy says."""
        assert audit_hedge(program, 0.60).violations[0].severity is Severity.WARNING
        assert audit_hedge(program, 0.59).violations[0].severity is Severity.BREACH

    def test_the_suggestion_points_the_right_way(self, program):
        assert "Moving to" in audit_hedge(program, 0.60).violations[0].suggestion
        assert "Reducing to" in audit_hedge(program, 0.99).violations[0].suggestion

    def test_a_forbidden_instrument_is_a_breach(self, program):
        audit = audit_hedge(program, 0.85, proposed_instrument="seagull")
        codes = {v.code for v in audit.violations}
        assert "instrument_not_permitted" in codes
        assert not audit.compliant

    def test_the_instrument_finding_lists_what_is_allowed(self, program):
        audit = audit_hedge(program, 0.85, proposed_instrument="seagull")
        finding = next(v for v in audit.violations if v.code == "instrument_not_permitted")
        assert "collar" in finding.message
        assert "policy change, not a" in finding.suggestion

    def test_an_unstated_instrument_under_a_restriction_is_a_warning(self, program):
        audit = audit_hedge(program, 0.85)
        codes = {v.code for v in audit.violations}
        assert "instrument_not_stated" in codes

    def test_an_unrestricted_programme_does_not_ask(self):
        program = HedgeProgram(0.80, 0.10, RebalanceFrequency.MONTHLY)
        audit = audit_hedge(program, 0.85)
        assert audit.compliant
        assert {v.code for v in audit.violations} == {"hedge_ratio_within_band"}

    def test_findings_come_worst_first(self, program):
        audit = audit_hedge(program, 0.30, proposed_instrument="seagull")
        severities = [v.severity for v in audit.violations]
        assert severities == sorted(severities, key=lambda s: {"breach": 0, "warning": 1, "info": 2}[s.value])

    def test_a_ratio_outside_zero_to_one_refuses(self, program):
        with pytest.raises(ConfigurationError, match="fraction in"):
            audit_hedge(program, 1.5)


class TestStrictAuditing:
    """The report becomes a refusal when the caller asks."""

    def test_strict_refuses_on_a_violation(self, program):
        with pytest.raises(PolicyBreachError) as excinfo:
            audit_hedge(program, 0.30, strict=True)
        assert "departs from programme 'treasury_2026'" in str(excinfo.value)

    def test_a_breach_is_not_a_malformed_programme(self, program):
        """Deep review: this used to raise `ConfigurationError`, the same
        refusal as an unreadable policy file. The two need different
        handling — one is fixed by editing the file, the other by changing
        the trade — so catching one must not catch the other."""
        with pytest.raises(PolicyBreachError) as excinfo:
            audit_hedge(program, 0.30, strict=True)
        assert not isinstance(excinfo.value, ConfigurationError)
        assert isinstance(excinfo.value, HedgeError)
        assert excinfo.value.exit_code == 2

    def test_a_malformed_ratio_is_still_a_configuration_error(self, program):
        """The other half: the argument being out of range is a caller
        mistake, and it keeps the exit code that says so."""
        with pytest.raises(ConfigurationError) as excinfo:
            audit_hedge(program, 1.4, strict=True)
        assert not isinstance(excinfo.value, PolicyBreachError)
        assert excinfo.value.exit_code == 1

    def test_strict_passes_when_compliant(self, program):
        audit = audit_hedge(program, 0.85, proposed_instrument="collar", strict=True)
        assert audit.compliant

    def test_the_refusal_carries_the_messages(self, program):
        with pytest.raises(PolicyBreachError) as excinfo:
            audit_hedge(program, 0.30, proposed_instrument="seagull", strict=True)
        message = str(excinfo.value)
        assert "outside the programme" in message
        assert "does not permit" in message

    def test_the_default_reports_rather_than_refusing(self, program):
        assert isinstance(audit_hedge(program, 0.30), ProgramAudit)


class TestEvidence:
    def test_violations_become_degradations(self, program):
        audit = audit_hedge(program, 0.30, proposed_instrument="seagull")
        codes = {w.code for w in audit.evidence.warnings}
        assert "hedge_ratio_outside_band" in codes
        assert "instrument_not_permitted" in codes

    def test_a_compliant_audit_carries_no_degradation(self, program):
        audit = audit_hedge(program, 0.85, proposed_instrument="collar")
        assert audit.evidence.warnings == ()
        assert audit.evidence.worst_quality is not DataQuality.ASSUMED

    def test_a_violating_audit_reaches_assumed(self, program):
        assert audit_hedge(program, 0.30).evidence.worst_quality is DataQuality.ASSUMED


class TestSerialisation:
    def test_the_policy_serialises_with_its_bounds_resolved(self, program):
        stored = program.to_dict()
        assert stored["lower_bound"] == pytest.approx(0.70)
        assert stored["upper_bound"] == pytest.approx(0.90)
        assert stored["rebalance_frequency"] == "monthly"

    def test_the_audit_payload_holds_every_finding(self, program):
        payload = audit_hedge(program, 0.30, proposed_instrument="seagull").payload_fields()
        assert payload["compliant"] is False
        assert len(payload["violations"]) == 2
        assert all({"code", "severity", "message", "suggestion"} == set(v) for v in payload["violations"])

    def test_it_is_json_serialisable(self, program):
        json.dumps(audit_hedge(program, 0.30).to_dict())

    def test_a_loaded_programme_round_trips_through_its_own_payload(self, program):
        stored = program.to_dict()
        reloaded = load_program(
            {
                "name": stored["name"],
                "target_hedge_ratio": stored["target_hedge_ratio"],
                "discretion_band": stored["discretion_band"],
                "rebalance_frequency": stored["rebalance_frequency"],
                "allowed_instruments": stored["allowed_instruments"],
            }
        )
        assert reloaded == program


class TestItIntegratesWithTheComparator:
    """The structures the comparator produces are the names a policy names."""

    def test_every_permitted_name_is_a_real_structure(self, program):
        from datetime import date

        from rates_engine.fx.quote import USDMXN
        from rates_engine.hedging_structures import (
            Exposure,
            ExposureDirection,
            StructureQuote,
            compare_structures,
        )

        comparison = compare_structures(
            Exposure(1e6, ExposureDirection.PAYABLE, date(2026, 12, 15), USDMXN),
            StructureQuote(18.50, 0.25, 0.0950, 0.0420, 0.115),
        )
        available = {s.name for s in comparison.structures}
        assert program.allowed_instruments <= available
