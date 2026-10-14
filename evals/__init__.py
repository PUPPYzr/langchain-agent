"""Versioned offline evaluation dataset and deterministic rule evaluation.

This package deliberately avoids a single LLM judge. Every check here is a
deterministic rule over an observed trajectory so that results are reproducible
and a regression can block a release.
"""

from evals.rules import (
    CaseResult,
    EvalCase,
    EvalReport,
    RuleViolation,
    ToolCallObservation,
    TrajectoryObservation,
    evaluate_case,
    evaluate_dataset,
    load_cases,
    load_observations,
)

__all__ = [
    "CaseResult",
    "EvalCase",
    "EvalReport",
    "RuleViolation",
    "ToolCallObservation",
    "TrajectoryObservation",
    "evaluate_case",
    "evaluate_dataset",
    "load_cases",
    "load_observations",
]
