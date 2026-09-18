"""The hedging policy as data, kept apart from the calculation.

A treasury's hedging programme is a set of decisions somebody made in a
committee: how much of the exposure to cover, how far a treasurer may
deviate before it needs explaining, how often to rebalance, which
instruments are allowed at all. None of that is a calculation, and putting
it in code means changing code to change policy.

So it is a configuration, and this module does two things with it: load it
strictly, and audit a proposed hedge against it.

**Strictly** means an unknown key is a refusal naming the key, not a
silently ignored line. A policy file with ``target_hedge_ration: 0.8`` in
it is a policy file that does nothing, and the failure mode is a hedge that
looks compliant because the rule never loaded.

**Audit, not enforce.** :func:`audit_hedge` reports violations with a
severity and a suggestion and returns them; ``strict=True`` turns the
report into a refusal. The default is to report, because a treasurer
outside the band at month end usually knows, and a library that refuses to
compute is not more careful than one that computes and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rates_engine.errors import ConfigurationError
from rates_engine.evidence import DataQuality, Degradation, Evidence
from rates_engine.results import EngineResult

__all__ = [
    "RebalanceFrequency",
    "Severity",
    "HedgeProgram",
    "Violation",
    "ProgramAudit",
    "load_program",
    "audit_hedge",
]

_KEYS = frozenset(
    {
        "target_hedge_ratio",
        "discretion_band",
        "rebalance_frequency",
        "allowed_instruments",
        "name",
    }
)


class RebalanceFrequency(StrEnum):
    """How often the programme says to bring the hedge back to target."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    AT_INCEPTION = "at_inception"


class Severity(StrEnum):
    """How far outside the policy a finding is.

    ``INFO``
        Inside the band, reported for the record.
    ``WARNING``
        Outside the band but inside twice it, or a policy field that could
        not be checked.
    ``BREACH``
        Outside twice the band, or an instrument the programme forbids.
    """

    INFO = "info"
    WARNING = "warning"
    BREACH = "breach"


@dataclass(frozen=True)
class HedgeProgram:
    """A hedging policy.

    Attributes:
        target_hedge_ratio: The fraction of exposure the policy targets, in
            ``[0, 1]``.
        discretion_band: How far either side of the target a treasurer may
            go without it being a violation, as a fraction.
        rebalance_frequency: How often the hedge is brought back.
        allowed_instruments: Structure names the programme permits. Empty
            means every structure is allowed, which is a policy too and is
            recorded as one.
        name: A label for the payload.
    """

    target_hedge_ratio: float
    discretion_band: float
    rebalance_frequency: RebalanceFrequency
    allowed_instruments: frozenset[str] = frozenset()
    name: str = "unnamed_program"

    def __post_init__(self) -> None:
        if not 0.0 <= self.target_hedge_ratio <= 1.0:
            raise ConfigurationError(
                f"target_hedge_ratio is a fraction in [0, 1]; got "
                f"{self.target_hedge_ratio!r}. A ratio above one is an overlay, which "
                "PRD-003 puts out of scope."
            )
        if not 0.0 <= self.discretion_band <= 1.0:
            raise ConfigurationError(
                f"discretion_band is a fraction in [0, 1]; got {self.discretion_band!r}"
            )

    @property
    def lower(self) -> float:
        """The bottom of the discretion band, floored at zero."""
        return max(0.0, self.target_hedge_ratio - self.discretion_band)

    @property
    def upper(self) -> float:
        """The top of the discretion band, capped at one."""
        return min(1.0, self.target_hedge_ratio + self.discretion_band)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the policy, with the band resolved into bounds."""
        return {
            "name": self.name,
            "target_hedge_ratio": self.target_hedge_ratio,
            "discretion_band": self.discretion_band,
            "lower_bound": self.lower,
            "upper_bound": self.upper,
            "rebalance_frequency": self.rebalance_frequency.value,
            "allowed_instruments": sorted(self.allowed_instruments),
        }


def load_program(config: dict[str, Any]) -> HedgeProgram:
    """Build a programme from a configuration mapping, strictly.

    Args:
        config: The mapping, typically from a YAML or JSON file.

    Returns:
        The :class:`HedgeProgram`.

    Raises:
        ConfigurationError: An unknown key, a missing required one, or a
            value outside its range. The message names the key, because a
            policy file whose typo is silently ignored is a policy that does
            nothing while looking like it does something.
    """
    unknown = sorted(set(config) - _KEYS)
    if unknown:
        raise ConfigurationError(
            f"unknown key(s) in the hedge programme: {', '.join(unknown)}. "
            f"Accepted: {', '.join(sorted(_KEYS))}. A key that is quietly ignored is a "
            "policy line that does nothing, which is worse than a policy that fails "
            "to load."
        )
    missing = sorted({"target_hedge_ratio", "discretion_band", "rebalance_frequency"} - set(config))
    if missing:
        raise ConfigurationError(
            f"the hedge programme is missing: {', '.join(missing)}. There is no default "
            "target hedge ratio — a programme that does not state one is not a programme."
        )
    frequency = config["rebalance_frequency"]
    try:
        parsed = RebalanceFrequency(frequency)
    except ValueError as exc:
        raise ConfigurationError(
            f"rebalance_frequency {frequency!r} is not one of "
            f"{', '.join(f.value for f in RebalanceFrequency)}"
        ) from exc
    return HedgeProgram(
        target_hedge_ratio=float(config["target_hedge_ratio"]),
        discretion_band=float(config["discretion_band"]),
        rebalance_frequency=parsed,
        allowed_instruments=frozenset(config.get("allowed_instruments") or ()),
        name=str(config.get("name", "unnamed_program")),
    )


@dataclass(frozen=True)
class Violation:
    """One way a proposed hedge departs from the programme.

    Attributes:
        code: A stable identifier.
        severity: How far outside.
        message: What is wrong, in a sentence.
        suggestion: What would bring it back, as a sentence. Not advice
            about whether to: a statement of what the policy implies.
    """

    code: str
    severity: Severity
    message: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise the finding."""
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class ProgramAudit(EngineResult):
    """A proposed hedge, read against the programme.

    Attributes:
        program: The policy audited against.
        proposed_ratio: The hedge ratio proposed.
        proposed_instrument: The structure proposed, or ``None``.
        violations: Every finding, worst first.
        compliant: True when nothing above ``INFO`` was found.
    """

    program: HedgeProgram
    proposed_ratio: float
    proposed_instrument: str | None
    violations: tuple[Violation, ...]
    compliant: bool

    def payload_fields(self) -> dict[str, Any]:
        """The policy, the proposal, and every finding with its severity."""
        return {
            "program": self.program.to_dict(),
            "proposed_ratio": self.proposed_ratio,
            "proposed_instrument": self.proposed_instrument,
            "violations": [v.to_dict() for v in self.violations],
            "compliant": self.compliant,
        }


_RANK = {Severity.BREACH: 0, Severity.WARNING: 1, Severity.INFO: 2}


def audit_hedge(
    program: HedgeProgram,
    proposed_ratio: float,
    *,
    proposed_instrument: str | None = None,
    strict: bool = False,
) -> ProgramAudit:
    """Read a proposed hedge against a programme and report the departures.

    Args:
        program: The policy.
        proposed_ratio: The hedge ratio being proposed, in ``[0, 1]``.
        proposed_instrument: The structure's name, checked against
            ``allowed_instruments`` when the programme names any.
        strict: Refuse instead of reporting, when anything above ``INFO``
            is found.

    Returns:
        The :class:`ProgramAudit`.

    Raises:
        ConfigurationError: ``strict`` is set and the proposal departs from
            the programme, or ``proposed_ratio`` is not a fraction.
    """
    if not 0.0 <= proposed_ratio <= 1.0:
        raise ConfigurationError(
            f"a proposed hedge ratio is a fraction in [0, 1]; got {proposed_ratio!r}"
        )

    findings: list[Violation] = []
    distance = abs(proposed_ratio - program.target_hedge_ratio)
    # Exactly twice the band is a warning, not a breach. Without the
    # tolerance, 0.8 - 0.6 evaluates to 0.20000000000000007 and a proposal
    # sitting precisely on the boundary is reported one severity worse than
    # the policy says — a distinction a treasurer would have to debug.
    breach_at = 2.0 * program.discretion_band
    if proposed_ratio < program.lower or proposed_ratio > program.upper:
        severity = (
            Severity.BREACH
            if distance > breach_at + 1e-12 * max(1.0, breach_at)
            else Severity.WARNING
        )
        findings.append(
            Violation(
                code="hedge_ratio_outside_band",
                severity=severity,
                message=(
                    f"the proposed ratio {proposed_ratio:.4f} is outside the programme's "
                    f"[{program.lower:.4f}, {program.upper:.4f}] band around a target of "
                    f"{program.target_hedge_ratio:.4f}, by "
                    f"{distance - program.discretion_band:.4f}."
                ),
                suggestion=(
                    f"Moving to {program.lower:.4f} would re-enter the band; "
                    f"{program.target_hedge_ratio:.4f} would return to target."
                    if proposed_ratio < program.lower
                    else f"Reducing to {program.upper:.4f} would re-enter the band; "
                    f"{program.target_hedge_ratio:.4f} would return to target."
                ),
            )
        )
    else:
        findings.append(
            Violation(
                code="hedge_ratio_within_band",
                severity=Severity.INFO,
                message=(
                    f"the proposed ratio {proposed_ratio:.4f} is inside the programme's "
                    f"[{program.lower:.4f}, {program.upper:.4f}] band."
                ),
                suggestion="No change is implied by the programme.",
            )
        )

    if program.allowed_instruments and proposed_instrument is not None:
        if proposed_instrument not in program.allowed_instruments:
            findings.append(
                Violation(
                    code="instrument_not_permitted",
                    severity=Severity.BREACH,
                    message=(
                        f"the programme does not permit {proposed_instrument!r}; it "
                        f"allows {', '.join(sorted(program.allowed_instruments))}."
                    ),
                    suggestion=(
                        "Use one of the permitted structures, or amend the programme's "
                        "allowed_instruments. The second is a policy change, not a "
                        "calculation."
                    ),
                )
            )
    elif program.allowed_instruments and proposed_instrument is None:
        findings.append(
            Violation(
                code="instrument_not_stated",
                severity=Severity.WARNING,
                message=(
                    "the programme restricts instruments but the proposal does not name "
                    "one, so the restriction could not be checked."
                ),
                suggestion="Name the structure being proposed.",
            )
        )

    ordered = tuple(sorted(findings, key=lambda v: _RANK[v.severity]))
    compliant = all(v.severity is Severity.INFO for v in ordered)

    if strict and not compliant:
        raise ConfigurationError(
            f"the proposed hedge departs from programme {program.name!r}: "
            + "; ".join(v.message for v in ordered if v.severity is not Severity.INFO)
        )

    evidence = Evidence(
        produced_by="hedge_program.audit_hedge",
        fields={
            "program": program.to_dict(),
            "proposed_ratio": proposed_ratio,
            "proposed_instrument": proposed_instrument,
            "violations": [v.to_dict() for v in ordered],
            "compliant": compliant,
        },
        warnings=tuple(
            Degradation(
                code=v.code,
                message=v.message,
                data_quality=DataQuality.ASSUMED,
            )
            for v in ordered
            if v.severity is not Severity.INFO
        ),
    )
    return ProgramAudit(
        evidence=evidence,
        program=program,
        proposed_ratio=proposed_ratio,
        proposed_instrument=proposed_instrument,
        violations=ordered,
        compliant=compliant,
    )
