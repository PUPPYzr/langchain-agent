"""Deterministic rule evaluation for the weather Agent.

The dataset describes what the Agent is expected to do; an observation captures
what it actually did (tool calls, arguments, statuses, final answer). Rules
compare the two without any model in the loop, so a regression is reproducible
and cheap to run in CI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

KNOWN_ERROR_TYPES = frozenset(
    {
        "invalid_input",
        "not_found",
        "timeout",
        "rate_limit",
        "retryable",
        "service_error",
    }
)

FAILURE_KEYWORDS = ("失败", "无法", "错误", "未找到", "超时", "限流")

# Matches values that look like a concrete weather metric, e.g. "23℃" or
# "23.5 度". Used to detect fabricated numbers when no tool succeeded.
METRIC_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:°C|℃|度|%)")


class EvalDatasetError(ValueError):
    """Raised when a dataset or observation file cannot be parsed."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One expected behaviour for a single user query."""

    case_id: str
    query: str
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    max_tool_calls: int = 4
    days_bounds: tuple[int, int] = (1, 7)
    locations_bounds: tuple[int, int] = (2, 5)
    must_mention: tuple[str, ...] = ()
    require_failure_disclosure: bool = False

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise EvalDatasetError("case_id must not be empty")
        if not self.query.strip():
            raise EvalDatasetError(f"query must not be empty: {self.case_id}")
        if self.max_tool_calls < 1:
            raise EvalDatasetError(f"max_tool_calls must be >= 1: {self.case_id}")


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    """One observed tool call and its outcome."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    status: str = "success"
    error_type: str | None = None
    failed_locations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrajectoryObservation:
    """What the Agent actually did for one case."""

    case_id: str
    query: str = ""
    tool_calls: tuple[ToolCallObservation, ...] = ()
    final_answer: str = ""
    turn_count: int = 0
    status: str = "completed"


@dataclass(frozen=True, slots=True)
class RuleViolation:
    """One failed deterministic rule."""

    rule: str
    detail: str


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Evaluation outcome for one case."""

    case_id: str
    passed: bool
    violations: tuple[RuleViolation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "violations": [
                {"rule": violation.rule, "detail": violation.detail}
                for violation in self.violations
            ],
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate evaluation outcome for a dataset run."""

    results: tuple[CaseResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "results": [result.to_dict() for result in self.results],
        }

    def summary(self) -> str:
        lines = [f"Eval: {self.passed}/{self.total} passed, {self.failed} failed"]
        for result in self.results:
            marker = "PASS" if result.passed else "FAIL"
            lines.append(f"[{marker}] {result.case_id}")
            for violation in result.violations:
                lines.append(f"    - {violation.rule}: {violation.detail}")
        return "\n".join(lines)


def _normalize_arguments(arguments: Mapping[str, Any]) -> str:
    return json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False, default=str)


def _successful_calls(observation: TrajectoryObservation) -> list[ToolCallObservation]:
    # ``partial`` still carries successful data for the locations that resolved,
    # so it counts as a source of facts.
    return [
        call
        for call in observation.tool_calls
        if call.status in {"success", "partial"}
    ]


def _check_tool_selection(
    case: EvalCase,
    observation: TrajectoryObservation,
    violations: list[RuleViolation],
) -> None:
    called = {call.name for call in observation.tool_calls}
    for tool in case.expected_tools:
        if tool not in called:
            violations.append(
                RuleViolation("tool_selection", f"expected tool not called: {tool}")
            )
    for tool in case.forbidden_tools:
        if tool in called:
            violations.append(
                RuleViolation("forbidden_tool", f"forbidden tool was called: {tool}")
            )


def _check_tool_call_budget(
    case: EvalCase,
    observation: TrajectoryObservation,
    violations: list[RuleViolation],
) -> None:
    if len(observation.tool_calls) > case.max_tool_calls:
        violations.append(
            RuleViolation(
                "max_tool_calls",
                f"tool calls {len(observation.tool_calls)} exceed "
                f"{case.max_tool_calls}",
            )
        )
    seen: set[str] = set()
    for call in observation.tool_calls:
        key = f"{call.name}|{_normalize_arguments(call.arguments)}"
        if key in seen:
            violations.append(
                RuleViolation(
                    "duplicate_tool_call",
                    f"repeated identical call: {call.name}",
                )
            )
        seen.add(key)


def _check_argument_bounds(
    case: EvalCase,
    observation: TrajectoryObservation,
    violations: list[RuleViolation],
) -> None:
    for call in observation.tool_calls:
        arguments = dict(call.arguments)
        if call.name == "get_current_weather":
            location = arguments.get("location")
            if not isinstance(location, str) or not location.strip():
                violations.append(
                    RuleViolation(
                        "argument_bounds",
                        "get_current_weather requires a non-empty location",
                    )
                )
            elif len(location) > 120:
                violations.append(
                    RuleViolation(
                        "argument_bounds",
                        "get_current_weather location exceeds 120 characters",
                    )
                )
        elif call.name == "get_weather_forecast":
            days = arguments.get("days", 3)
            low, high = case.days_bounds
            if not isinstance(days, int) or not low <= days <= high:
                violations.append(
                    RuleViolation(
                        "argument_bounds",
                        f"forecast days {days!r} outside {low}..{high}",
                    )
                )
        elif call.name == "compare_current_weather":
            locations = arguments.get("locations")
            low, high = case.locations_bounds
            if not isinstance(locations, list) or not all(
                isinstance(item, str) and item.strip() for item in locations
            ):
                violations.append(
                    RuleViolation(
                        "argument_bounds",
                        "compare_current_weather requires a list of places",
                    )
                )
            elif not low <= len(locations) <= high:
                violations.append(
                    RuleViolation(
                        "argument_bounds",
                        f"locations count {len(locations)} outside {low}..{high}",
                    )
                )
            elif len({item.casefold() for item in locations}) != len(locations):
                violations.append(
                    RuleViolation(
                        "argument_bounds",
                        "compare_current_weather locations must be unique",
                    )
                )


def _check_error_classification(
    observation: TrajectoryObservation,
    violations: list[RuleViolation],
) -> None:
    for call in observation.tool_calls:
        if call.status == "error":
            if call.error_type not in KNOWN_ERROR_TYPES:
                violations.append(
                    RuleViolation(
                        "error_classification",
                        f"unknown error_type {call.error_type!r} for {call.name}",
                    )
                )


def _check_answer(
    case: EvalCase,
    observation: TrajectoryObservation,
    violations: list[RuleViolation],
) -> None:
    answer = observation.final_answer.strip()
    if not answer:
        violations.append(RuleViolation("answer_present", "final answer is empty"))
        return
    for keyword in case.must_mention:
        if keyword not in answer:
            violations.append(
                RuleViolation("must_mention", f"answer missing {keyword!r}")
            )


def _check_fact_provenance(
    observation: TrajectoryObservation,
    violations: list[RuleViolation],
) -> None:
    if _successful_calls(observation):
        return
    if observation.tool_calls and METRIC_PATTERN.search(observation.final_answer):
        violations.append(
            RuleViolation(
                "fact_provenance",
                "answer states concrete metrics although no tool succeeded",
            )
        )


def _check_failure_disclosure(
    case: EvalCase,
    observation: TrajectoryObservation,
    violations: list[RuleViolation],
) -> None:
    has_error = any(call.status == "error" for call in observation.tool_calls)
    if case.require_failure_disclosure and has_error and not _successful_calls(
        observation
    ):
        answer = observation.final_answer
        if not any(keyword in answer for keyword in FAILURE_KEYWORDS):
            violations.append(
                RuleViolation(
                    "failure_disclosure",
                    "failed run must explain the failure to the user",
                )
            )
    for call in observation.tool_calls:
        for location in call.failed_locations:
            if location not in observation.final_answer:
                violations.append(
                    RuleViolation(
                        "partial_disclosure",
                        f"answer does not mention failed location: {location}",
                    )
                )


def evaluate_case(
    case: EvalCase,
    observation: TrajectoryObservation,
) -> CaseResult:
    """Apply every deterministic rule for one case."""
    violations: list[RuleViolation] = []
    _check_tool_selection(case, observation, violations)
    _check_tool_call_budget(case, observation, violations)
    _check_argument_bounds(case, observation, violations)
    _check_error_classification(observation, violations)
    _check_answer(case, observation, violations)
    _check_fact_provenance(observation, violations)
    _check_failure_disclosure(case, observation, violations)
    return CaseResult(
        case_id=case.case_id,
        passed=not violations,
        violations=tuple(violations),
    )


def evaluate_dataset(
    cases: Iterable[EvalCase],
    observations: Mapping[str, TrajectoryObservation],
) -> EvalReport:
    """Evaluate every case, recording a violation for missing observations."""
    results: list[CaseResult] = []
    for case in cases:
        observation = observations.get(case.case_id)
        if observation is None:
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=False,
                    violations=(
                        RuleViolation("observation", "no observation recorded"),
                    ),
                )
            )
            continue
        results.append(evaluate_case(case, observation))
    return EvalReport(results=tuple(results))


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        raise EvalDatasetError(f"file not found: {file_path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        file_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(
                f"{file_path}:{line_number} is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise EvalDatasetError(
                f"{file_path}:{line_number} must be a JSON object"
            )
        records.append(payload)
    return records


def _tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EvalDatasetError(f"expected a list of strings, got {value!r}")
    return tuple(str(item) for item in value)


def load_cases(path: str | Path) -> tuple[EvalCase, ...]:
    """Load a versioned dataset of expected behaviours from JSONL."""
    cases: list[EvalCase] = []
    for payload in _read_jsonl(path):
        days_bounds = payload.get("days_bounds") or (1, 7)
        locations_bounds = payload.get("locations_bounds") or (2, 5)
        cases.append(
            EvalCase(
                case_id=str(payload.get("case_id", "")),
                query=str(payload.get("query", "")),
                expected_tools=_tuple_of_strings(payload.get("expected_tools")),
                forbidden_tools=_tuple_of_strings(payload.get("forbidden_tools")),
                max_tool_calls=int(payload.get("max_tool_calls", 4)),
                days_bounds=(int(days_bounds[0]), int(days_bounds[1])),
                locations_bounds=(
                    int(locations_bounds[0]),
                    int(locations_bounds[1]),
                ),
                must_mention=_tuple_of_strings(payload.get("must_mention")),
                require_failure_disclosure=bool(
                    payload.get("require_failure_disclosure", False)
                ),
            )
        )
    return tuple(cases)


def load_observations(path: str | Path) -> dict[str, TrajectoryObservation]:
    """Load observed trajectories from JSONL, keyed by case id."""
    observations: dict[str, TrajectoryObservation] = {}
    for payload in _read_jsonl(path):
        calls = tuple(
            ToolCallObservation(
                name=str(call.get("name", "")),
                arguments=call.get("arguments") or {},
                status=str(call.get("status", "success")),
                error_type=call.get("error_type"),
                failed_locations=_tuple_of_strings(call.get("failed_locations")),
            )
            for call in payload.get("tool_calls", [])
        )
        case_id = str(payload.get("case_id", ""))
        observations[case_id] = TrajectoryObservation(
            case_id=case_id,
            query=str(payload.get("query", "")),
            tool_calls=calls,
            final_answer=str(payload.get("final_answer", "")),
            turn_count=int(payload.get("turn_count", 0)),
            status=str(payload.get("status", "completed")),
        )
    return observations
