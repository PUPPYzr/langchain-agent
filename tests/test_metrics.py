"""Tests for structured Run and trace metrics."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_platform.metrics import (
    Pricing,
    summarize_runs,
    summarize_trace_directory,
    summarize_trace_records,
)
from agent_platform.runs import RunRecord


def _record(
    run_id: str,
    *,
    status: str,
    created_at: str,
    updated_at: str,
    error_type: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        session_id="thread-1",
        agent_id="weather-agent",
        agent_version=1,
        runtime_type="langgraph",
        status=status,
        created_at=created_at,
        updated_at=updated_at,
        error_type=error_type,
    )


class RunMetricsTests(unittest.TestCase):
    def test_status_success_rate_and_error_categories(self) -> None:
        records = [
            _record(
                "r1",
                status="completed",
                created_at="2026-10-14T00:00:00+00:00",
                updated_at="2026-10-14T00:00:01+00:00",
            ),
            _record(
                "r2",
                status="failed",
                created_at="2026-10-14T00:00:00+00:00",
                updated_at="2026-10-14T00:00:02+00:00",
                error_type="model_timeout",
            ),
            _record(
                "r3",
                status="cancelled",
                created_at="2026-10-14T00:00:00+00:00",
                updated_at="2026-10-14T00:00:03+00:00",
                error_type="cancelled",
            ),
            _record(
                "r4",
                status="completed",
                created_at="2026-10-14T00:00:00+00:00",
                updated_at="2026-10-14T00:00:04+00:00",
            ),
        ]

        metrics = summarize_runs(records)

        self.assertEqual(metrics.total, 4)
        self.assertEqual(metrics.by_status["completed"], 2)
        self.assertAlmostEqual(metrics.success_rate, 0.5)
        self.assertEqual(metrics.by_error_category["model_timeout"], 1)
        self.assertEqual(metrics.by_error_category["cancelled"], 1)

    def test_latency_uses_nearest_rank_percentiles(self) -> None:
        records = [
            _record(
                f"r{index}",
                status="completed",
                created_at="2026-10-14T00:00:00+00:00",
                updated_at=f"2026-10-14T00:00:0{index}+00:00",
            )
            for index in range(1, 5)
        ]

        latency = summarize_runs(records).latency

        self.assertEqual(latency.count, 4)
        self.assertEqual(latency.p50_ms, 3000.0)
        self.assertEqual(latency.p95_ms, 4000.0)
        self.assertEqual(latency.max_ms, 4000.0)

    def test_empty_ledger_is_reported_as_zero(self) -> None:
        metrics = summarize_runs([])

        self.assertEqual(metrics.total, 0)
        self.assertEqual(metrics.success_rate, 0.0)
        self.assertEqual(metrics.latency.count, 0)

    def test_unparsable_timestamps_do_not_break_latency(self) -> None:
        records = [
            _record(
                "r1",
                status="completed",
                created_at="not-a-timestamp",
                updated_at="also-not",
            )
        ]

        metrics = summarize_runs(records)

        self.assertEqual(metrics.latency.count, 0)
        self.assertEqual(metrics.total, 1)


class TraceMetricsTests(unittest.TestCase):
    def test_call_counts_tokens_and_cost(self) -> None:
        records = [
            {
                "run_id": "r1",
                "stage": "model",
                "event": "request_completed",
                "details": {"input_tokens": 1000, "output_tokens": 500},
            },
            {"run_id": "r1", "stage": "tool", "event": "call_started", "details": {}},
            {"run_id": "r1", "stage": "http", "event": "request_completed"},
            {
                "run_id": "r1",
                "stage": "runtime",
                "event": "run.retry_scheduled",
                "error_category": "model_timeout",
            },
            {
                "run_id": "r2",
                "stage": "runtime",
                "event": "run.cancelled",
                "error_category": "cancelled",
            },
            {"run_id": "r2", "stage": "runtime", "event": "run.interrupted"},
            {"run_id": "r2", "stage": "runtime", "event": "run.resumed"},
            {"run_id": "r2", "stage": "runtime", "event": "run.timeout"},
        ]

        metrics = summarize_trace_records(records, pricing=Pricing(2.0, 4.0))

        self.assertEqual(metrics.runs, 2)
        self.assertEqual(metrics.model_calls, 1)
        self.assertEqual(metrics.tool_calls, 1)
        self.assertEqual(metrics.http_calls, 1)
        self.assertEqual(metrics.input_tokens, 1000)
        self.assertEqual(metrics.output_tokens, 500)
        self.assertAlmostEqual(metrics.estimated_cost_usd, 0.004)
        self.assertTrue(metrics.pricing_configured)
        self.assertEqual(metrics.retries, 1)
        self.assertEqual(metrics.cancellations, 1)
        self.assertEqual(metrics.interrupts, 1)
        self.assertEqual(metrics.resumes, 1)
        self.assertEqual(metrics.timeouts, 1)
        self.assertEqual(metrics.by_error_category["model_timeout"], 1)

    def test_unknown_tokens_are_ignored(self) -> None:
        records = [
            {
                "run_id": "r1",
                "stage": "model",
                "event": "request_completed",
                "details": {"input_tokens": "unknown", "output_tokens": None},
            }
        ]

        metrics = summarize_trace_records(records)

        self.assertEqual(metrics.input_tokens, 0)
        self.assertEqual(metrics.output_tokens, 0)
        self.assertFalse(metrics.pricing_configured)

    def test_directory_summary_reads_jsonl_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-1.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"run_id": "r1", "stage": "model", "event": "request_completed",
                             "details": {"input_tokens": 10, "output_tokens": 20}}
                        ),
                        "not json",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            metrics = summarize_trace_directory(directory, pricing=Pricing(1.0, 1.0))

        self.assertEqual(metrics.runs, 1)
        self.assertEqual(metrics.model_calls, 1)
        self.assertEqual(metrics.input_tokens, 10)
        self.assertAlmostEqual(metrics.estimated_cost_usd, 0.00003)

    def test_missing_directory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metrics = summarize_trace_directory(Path(directory) / "absent")

        self.assertEqual(metrics.runs, 0)
        self.assertEqual(metrics.model_calls, 0)


class PricingTests(unittest.TestCase):
    def test_prices_are_read_from_the_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MODEL_INPUT_PRICE_PER_MILLION": "0.5",
                "MODEL_OUTPUT_PRICE_PER_MILLION": "1.5",
            },
        ):
            pricing = Pricing.from_environment()

        self.assertEqual(pricing.input_per_million, 0.5)
        self.assertEqual(pricing.output_per_million, 1.5)
        self.assertTrue(pricing.configured)

    def test_default_pricing_is_unconfigured(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            pricing = Pricing.from_environment()

        self.assertFalse(pricing.configured)

    def test_invalid_price_is_rejected(self) -> None:
        with patch.dict("os.environ", {"MODEL_INPUT_PRICE_PER_MILLION": "free"}):
            with self.assertRaises(ValueError):
                Pricing.from_environment()


if __name__ == "__main__":
    unittest.main()
