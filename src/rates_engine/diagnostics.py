"""Reading an evidence chain: flattening it, and asking what it rests on."""

from __future__ import annotations

from typing import Any

from rates_engine.evidence import DataQuality, Degradation, Evidence
from rates_engine.results import EngineResult

__all__ = ["walk", "all_warnings", "quality_report", "summarise"]


def walk(evidence: Evidence, _depth: int = 0) -> list[tuple[int, Evidence]]:
    """Every node of an evidence chain, depth first, with its depth.

    Args:
        evidence: Root of the chain.
        _depth: Internal recursion depth.

    Returns:
        Pairs of depth and evidence, the root first.
    """
    found = [(_depth, evidence)]
    for source in evidence.sources:
        found.extend(walk(source, _depth + 1))
    return found


def all_warnings(evidence: Evidence) -> tuple[Degradation, ...]:
    """Every degradation anywhere in the chain, deduplicated by code and message.

    Args:
        evidence: Root of the chain. Nested ``sources`` are walked, so a
            degradation three results deep still surfaces here.

    Returns:
        The degradations, in the order first encountered. Deduplicated on
        ``(code, message)`` rather than ``code`` alone: two curves can carry
        the same proxy code about different tenors, and collapsing those
        would under-report.
    """
    seen: dict[tuple[str, str], Degradation] = {}
    for _, node in walk(evidence):
        for warning in node.warnings:
            seen.setdefault((warning.code, warning.message), warning)
    return tuple(seen.values())


def quality_report(evidence: Evidence) -> dict[str, Any]:
    """What the chain's worst input is, and which step introduced it.

    Args:
        evidence: Root of the chain.

    Returns:
        A mapping with the worst quality, every quality present, the chain
        depth, and the ``produced_by`` of each step that contributed
        something less than observed.
    """
    contributors = [
        node.produced_by
        for _, node in walk(evidence)
        if any(
            p.data_quality is not DataQuality.OBSERVED for p in node.inputs
        )
        or node.warnings
    ]
    return {
        "worst_quality": evidence.worst_quality.value,
        "qualities": sorted(q.value for q in evidence.qualities),
        "depth": max(depth for depth, _ in walk(evidence)) + 1,
        "degraded_steps": contributors,
        "warnings": [w.to_dict() for w in all_warnings(evidence)],
    }


def summarise(result: EngineResult) -> dict[str, Any]:
    """A short human-oriented view of a result: what it is and what it rests on.

    Args:
        result: Any engine result.

    Returns:
        A mapping with the result type, its payload fields and the quality
        report of its evidence chain.
    """
    return {
        "result_type": type(result).__name__,
        "produced_by": result.evidence.produced_by,
        "fields": {k: v for k, v in result.payload_fields().items() if not isinstance(v, dict)},
        "quality": quality_report(result.evidence),
    }
