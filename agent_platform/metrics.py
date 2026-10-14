"""Structured metrics derived from the Run ledger and trace files.

Metrics are computed from persisted artifacts rather than from live run state so
that two versions can be compared after the fact: the Run ledger provides
success rate, failure categories and latency, while trace files provide model and
tool call counts, token usage and estimated cost.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_platform.runs import RunRecord
from agent_platform.telemetry import (
    STAGE_MODEL,
    STAGE_TOOL,
    iter_trace_records,
)


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """Run duration distribution in milliseconds."""

    count: int
    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Aggregate health of the Run ledger."""

    total: int
    by_status: Mapping[str, int]
    success_rate: float
    by_error_category: Mapping[str, int]
    latency: LatencySummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_status": dict(self.by_status),
            "success_rate": round(self.success_rate, 4),
            "by_error_category": dict(self.by_error_category),
            "latency_ms": {
                "count": self.latency.count,
                "p50": self.latency.p50_ms,
                "p95": self.latency.p95_ms,
                "max": self.latency.max_ms,
            },
        }


@dataclass(frozen=True, slots=True)
class TraceMetrics:
    """Aggregate behaviour and cost derived from trace files."""

    runs: int
    model_calls: int
    tool_calls: int
    http_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    pricing_configured: bool
    by_error_category: Mapping[str, int]
    retries: int
    cancellations: int
    timeouts: int
    interrupts: int
    resumes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "http_calls": self.http_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "pricing_configured": self.pricing_configured,
            "by_error_category": dict(self.by_error_category),
            "retries": self.retries,
            "cancellations": self.cancellations,
            "timeouts": self.timeouts,
            "interrupts": self.interrupts,
            "resumes": self.resumes,
        }


@dataclass(frozen=True, slots=True)
class Pricing:
    """Token pricing used for cost estimation."""

    input_per_million: float = 0.0
    output_per_million: float = 0.0

    @property
    def configured(self) -> bool:
        return self.input_per_million > 0 or self.output_per_million > 0

    @classmethod
    def from_environment(cls) -> "Pricing":
        try:
            return cls(
                input_per_million=float(
                    os.getenv("MODEL_INPUT_PRICE_PER_MILLION", "0").strip()
                ),
                output_per_million=float(
                    os.getenv("MODEL_OUTPUT_PRICE_PER_MILLION", "0").strip()
                ),
            )
        except ValueError as exc:
            raise ValueError(
                "MODEL_INPUT_PRICE_PER_MILLION and MODEL_OUTPUT_PRICE_PER_MILLION "
                "must be numbers."
            ) from exc


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def _duration_ms(record: RunRecord) -> float | None:
    try:
        created = datetime.fromisoformat(record.created_at)
        updated = datetime.fromisoformat(record.updated_at)
    except ValueError:
        return None
    return max(0.0, (updated - created).total_seconds() * 1000)


def summarize_runs(records: Iterable[RunRecord]) -> RunMetrics:
    """Aggregate status, failure categories and latency across Runs."""
    by_status: dict[str, int] = {}
    by_error: dict[str, int] = {}
    durations: list[float] = []
    total = 0
    for record in records:
        total += 1
        by_status[record.status] = by_status.get(record.status, 0) + 1
        if record.error_type:
            by_error[record.error_type] = by_error.get(record.error_type, 0) + 1
        duration = _duration_ms(record)
        if duration is not None:
            durations.append(duration)
    success_rate = (by_status.get("completed", 0) / total) if total else 0.0
    return RunMetrics(
        total=total,
        by_status=by_status,
        success_rate=success_rate,
        by_error_category=by_error,
        latency=LatencySummary(
            count=len(durations),
            p50_ms=_percentile(durations, 0.5),
            p95_ms=_percentile(durations, 0.95),
            max_ms=round(max(durations), 3) if durations else 0.0,
        ),
    )


def _token_value(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def summarize_trace_records(
    records: Iterable[Mapping[str, Any]],
    *,
    pricing: Pricing | None = None,
) -> TraceMetrics:
    """Aggregate call counts, tokens, estimated cost and failure events."""
    resolved_pricing = pricing or Pricing()
    run_ids: set[str] = set()
    model_calls = tool_calls = http_calls = 0
    input_tokens = output_tokens = 0
    by_error: dict[str, int] = {}
    counters = {"retries": 0, "cancellations": 0, "timeouts": 0, "interrupts": 0, "resumes": 0}
    for record in records:
        run_id = record.get("run_id")
        if isinstance(run_id, str):
            run_ids.add(run_id)
        stage = record.get("stage")
        event = record.get("event")
        details = record.get("details")
        detail_map = details if isinstance(details, Mapping) else {}
        if stage == STAGE_MODEL and event == "request_completed":
            model_calls += 1
            input_tokens += _token_value(detail_map.get("input_tokens"))
            output_tokens += _token_value(detail_map.get("output_tokens"))
        elif stage == STAGE_TOOL and event == "call_started":
            tool_calls += 1
        elif stage == "http" and event == "request_completed":
            http_calls += 1
        category = record.get("error_category")
        if isinstance(category, str) and category:
            by_error[category] = by_error.get(category, 0) + 1
        if event == "run.retry_scheduled":
            counters["retries"] += 1
        elif event == "run.cancelled":
            counters["cancellations"] += 1
        elif event == "run.timeout":
            counters["timeouts"] += 1
        elif event == "run.interrupted":
            counters["interrupts"] += 1
        elif event == "run.resumed":
            counters["resumes"] += 1
    estimated_cost = (
        input_tokens / 1_000_000 * resolved_pricing.input_per_million
        + output_tokens / 1_000_000 * resolved_pricing.output_per_million
    )
    return TraceMetrics(
        runs=len(run_ids),
        model_calls=model_calls,
        tool_calls=tool_calls,
        http_calls=http_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        pricing_configured=resolved_pricing.configured,
        by_error_category=by_error,
        **counters,
    )


def summarize_trace_directory(
    directory: str | Path,
    *,
    pricing: Pricing | None = None,
) -> TraceMetrics:
    """Aggregate every JSONL trace in a directory."""
    trace_directory = Path(directory)

    def records() -> Iterable[Mapping[str, Any]]:
        if not trace_directory.exists():
            return
        for path in sorted(trace_directory.glob("*.jsonl")):
            yield from iter_trace_records(path)

    return summarize_trace_records(records(), pricing=pricing)
