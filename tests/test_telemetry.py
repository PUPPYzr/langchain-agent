"""Tests for unified error classification, redaction and retention."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_platform.domain import (
    AgentRunCancelledError,
    AgentRunTimeoutError,
    AgentRuntimeError,
    ResumeNotAllowedError,
)
from agent_platform.telemetry import (
    CATEGORY_AGENT_ERROR,
    CATEGORY_CANCELLED,
    CATEGORY_MODEL_TIMEOUT,
    CATEGORY_RATE_LIMIT,
    CATEGORY_RESUME_NOT_ALLOWED,
    CATEGORY_RUN_TIMEOUT,
    CATEGORY_TURN_LIMIT,
    REDACTED,
    TracingPolicy,
    classify_error,
    purge_expired_traces,
    redact,
)
from weather_agent.observability import TraceRecorder


class ClassifyErrorTests(unittest.TestCase):
    def test_platform_errors_win_over_provider_heuristics(self) -> None:
        self.assertEqual(classify_error(AgentRunCancelledError("x")), CATEGORY_CANCELLED)
        self.assertEqual(classify_error(AgentRunTimeoutError("x")), CATEGORY_RUN_TIMEOUT)
        self.assertEqual(
            classify_error(ResumeNotAllowedError("x")),
            CATEGORY_RESUME_NOT_ALLOWED,
        )

    def test_wrapped_platform_error_is_found_in_the_cause_chain(self) -> None:
        error = AgentRuntimeError("wrapped")
        error.__cause__ = AgentRunCancelledError("inner")

        self.assertEqual(classify_error(error), CATEGORY_CANCELLED)

    def test_provider_markers_are_classified(self) -> None:
        self.assertEqual(classify_error(ValueError("Rate limit reached (429)")), CATEGORY_RATE_LIMIT)
        self.assertEqual(classify_error(ValueError("request timed out")), CATEGORY_MODEL_TIMEOUT)
        self.assertEqual(classify_error(ValueError("Recursion limit of 9 reached")), CATEGORY_TURN_LIMIT)

    def test_unknown_error_falls_back_to_agent_error(self) -> None:
        self.assertEqual(classify_error(ValueError("something odd")), CATEGORY_AGENT_ERROR)

    def test_cyclic_cause_chain_does_not_loop_forever(self) -> None:
        first = AgentRuntimeError("first")
        second = AgentRuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first

        self.assertEqual(classify_error(first), CATEGORY_AGENT_ERROR)


class RedactionTests(unittest.TestCase):
    def test_credentials_are_always_removed(self) -> None:
        policy = TracingPolicy(capture_content=True)
        result = redact(
            {"api_key": "sk-123", "authorization": "Bearer x", "key": "k"},
            policy=policy,
        )

        self.assertEqual(result["api_key"], REDACTED)
        self.assertEqual(result["authorization"], REDACTED)
        self.assertEqual(result["key"], REDACTED)

    def test_token_counters_are_not_treated_as_credentials(self) -> None:
        policy = TracingPolicy()
        result = redact({"input_tokens": 12, "output_tokens": 34}, policy=policy)

        self.assertEqual(result["input_tokens"], 12)
        self.assertEqual(result["output_tokens"], 34)

    def test_content_is_redacted_by_default(self) -> None:
        result = redact({"question": "合肥天气", "content": "晴朗"}, policy=TracingPolicy())

        self.assertEqual(result["question"], REDACTED)
        self.assertEqual(result["content"], REDACTED)

    def test_content_is_preserved_when_capture_is_enabled(self) -> None:
        policy = TracingPolicy(capture_content=True)
        result = redact({"question": "合肥天气"}, policy=policy)

        self.assertEqual(result["question"], "合肥天气")

    def test_nested_structures_are_redacted_recursively(self) -> None:
        policy = TracingPolicy()
        result = redact(
            {"tool": "x", "arguments": {"location": "合肥"}, "items": [{"api_key": "k"}]},
            policy=policy,
        )

        self.assertEqual(result["arguments"], REDACTED)
        self.assertEqual(result["items"][0]["api_key"], REDACTED)

    def test_long_strings_are_truncated(self) -> None:
        policy = TracingPolicy(max_string_length=16)
        result = redact({"note": "a" * 50}, policy=policy)

        self.assertTrue(result["note"].endswith("…"))
        self.assertEqual(len(result["note"]), 17)

    def test_invalid_policy_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TracingPolicy(max_string_length=4)
        with self.assertRaises(ValueError):
            TracingPolicy(retention_days=-1)

    def test_policy_reads_the_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TRACE_CAPTURE_CONTENT": "true",
                "TRACE_MAX_STRING_LENGTH": "64",
                "TRACE_RETENTION_DAYS": "3",
            },
        ):
            policy = TracingPolicy.from_environment()

        self.assertTrue(policy.capture_content)
        self.assertEqual(policy.max_string_length, 64)
        self.assertEqual(policy.retention_days, 3)


class RetentionTests(unittest.TestCase):
    def test_expired_traces_are_purged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            old = directory_path / "old.jsonl"
            fresh = directory_path / "fresh.jsonl"
            old.write_text("{}\n", encoding="utf-8")
            fresh.write_text("{}\n", encoding="utf-8")
            stale_time = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            import os

            os.utime(old, (stale_time, stale_time))

            removed = purge_expired_traces(
                directory_path,
                TracingPolicy(retention_days=7),
            )

            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_zero_retention_disables_purging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            path.write_text("{}\n", encoding="utf-8")

            removed = purge_expired_traces(directory, TracingPolicy(retention_days=0))

            self.assertEqual(removed, 0)
            self.assertTrue(path.exists())


class TraceRecorderPolicyTests(unittest.TestCase):
    def _read_records(self, directory: Path) -> list[dict]:
        path = directory / "run-1.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_recorder_redacts_details_and_promotes_error_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            recorder = TraceRecorder(
                run_id="run-1",
                session_id="thread-1",
                directory=directory_path,
            )
            recorder.record(
                "run",
                "failed",
                error_category=CATEGORY_RUN_TIMEOUT,
                error_type="AgentRunTimeoutError",
                question="合肥天气",
                api_key="sk-secret",
            )
            recorder.close()

            payload = self._read_records(directory_path)[-1]

        self.assertEqual(payload["error_category"], CATEGORY_RUN_TIMEOUT)
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["session_id"], "thread-1")
        self.assertEqual(payload["details"]["question"], REDACTED)
        self.assertEqual(payload["details"]["api_key"], REDACTED)

    def test_recording_after_close_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = TraceRecorder(
                run_id="run-1",
                session_id="thread-1",
                directory=Path(directory),
            )
            recorder.close()

            recorder.record("run", "failed", error_category=CATEGORY_AGENT_ERROR)

            self.assertEqual(self._read_records(Path(directory)), [])


if __name__ == "__main__":
    unittest.main()
