"""Tests for the deterministic evaluation rules and dataset fixtures."""

import json
import tempfile
import unittest
from pathlib import Path

from evals.rules import (
    EvalCase,
    EvalDatasetError,
    ToolCallObservation,
    TrajectoryObservation,
    evaluate_case,
    evaluate_dataset,
    load_cases,
    load_observations,
)

DATASET_DIRECTORY = Path(__file__).resolve().parent.parent / "evals" / "datasets"


class EvalRuleTests(unittest.TestCase):
    def _case(self, **overrides) -> EvalCase:
        defaults = {
            "case_id": "case",
            "query": "合肥今天天气怎么样？",
            "expected_tools": ("get_current_weather",),
            "max_tool_calls": 2,
            "must_mention": ("合肥",),
        }
        defaults.update(overrides)
        return EvalCase(**defaults)

    def test_passing_observation_has_no_violations(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(
                ToolCallObservation(
                    name="get_current_weather",
                    arguments={"location": "合肥"},
                    status="success",
                ),
            ),
            final_answer="合肥当前天气：晴朗，气温 20℃。",
        )

        result = evaluate_case(self._case(), observation)

        self.assertTrue(result.passed, result.violations)

    def test_missing_expected_tool_is_reported(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            final_answer="合肥当前天气：晴朗。",
        )

        result = evaluate_case(self._case(), observation)

        self.assertFalse(result.passed)
        self.assertIn("tool_selection", {item.rule for item in result.violations})

    def test_forbidden_tool_is_reported(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(
                ToolCallObservation(name="get_current_weather", arguments={"location": "合肥"}),
                ToolCallObservation(
                    name="compare_current_weather",
                    arguments={"locations": ["合肥", "上海"]},
                ),
            ),
            final_answer="合肥当前天气：晴朗，气温 20℃。",
        )

        result = evaluate_case(
            self._case(forbidden_tools=("compare_current_weather",)),
            observation,
        )

        self.assertIn("forbidden_tool", {item.rule for item in result.violations})

    def test_out_of_range_forecast_days_is_reported(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(
                ToolCallObservation(
                    name="get_weather_forecast",
                    arguments={"location": "上海", "days": 10},
                ),
            ),
            final_answer="上海未来十天预报如下。",
        )

        result = evaluate_case(
            self._case(expected_tools=("get_weather_forecast",), must_mention=("上海",)),
            observation,
        )

        self.assertIn("argument_bounds", {item.rule for item in result.violations})

    def test_duplicate_identical_tool_call_is_reported(self) -> None:
        call = ToolCallObservation(
            name="get_current_weather",
            arguments={"location": "合肥"},
        )
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(call, call),
            final_answer="合肥当前天气：晴朗。",
        )

        result = evaluate_case(self._case(max_tool_calls=1), observation)

        rules = {item.rule for item in result.violations}
        self.assertIn("duplicate_tool_call", rules)
        self.assertIn("max_tool_calls", rules)

    def test_metrics_without_successful_tool_is_a_provenance_failure(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(
                ToolCallObservation(
                    name="get_current_weather",
                    arguments={"location": "火星"},
                    status="error",
                    error_type="not_found",
                ),
            ),
            final_answer="火星当前气温 20℃。",
        )

        result = evaluate_case(self._case(must_mention=()), observation)

        self.assertIn("fact_provenance", {item.rule for item in result.violations})

    def test_undisclosed_failure_is_reported(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(
                ToolCallObservation(
                    name="get_current_weather",
                    arguments={"location": "火星"},
                    status="error",
                    error_type="not_found",
                ),
            ),
            final_answer="火星的天气情况就是这样。",
        )

        result = evaluate_case(
            self._case(must_mention=(), require_failure_disclosure=True),
            observation,
        )

        self.assertIn("failure_disclosure", {item.rule for item in result.violations})

    def test_unknown_error_type_is_reported(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(
                ToolCallObservation(
                    name="get_current_weather",
                    arguments={"location": "火星"},
                    status="error",
                    error_type="mystery",
                ),
            ),
            final_answer="火星查询失败。",
        )

        result = evaluate_case(self._case(must_mention=()), observation)

        self.assertIn("error_classification", {item.rule for item in result.violations})

    def test_partial_failure_requires_naming_failed_location(self) -> None:
        observation = TrajectoryObservation(
            case_id="case",
            tool_calls=(
                ToolCallObservation(
                    name="compare_current_weather",
                    arguments={"locations": ["合肥", "火星"]},
                    status="partial",
                    failed_locations=("火星",),
                ),
            ),
            final_answer="合肥 20℃。",
        )

        result = evaluate_case(
            self._case(
                expected_tools=("compare_current_weather",),
                must_mention=(),
            ),
            observation,
        )

        self.assertIn("partial_disclosure", {item.rule for item in result.violations})

    def test_missing_observation_fails_the_case(self) -> None:
        report = evaluate_dataset([self._case()], {})

        self.assertEqual(report.failed, 1)
        self.assertIn(
            "observation",
            {item.rule for item in report.results[0].violations},
        )


class EvalDatasetTests(unittest.TestCase):
    def test_repository_dataset_passes_with_recorded_observations(self) -> None:
        cases = load_cases(DATASET_DIRECTORY / "weather_v1.jsonl")
        observations = load_observations(
            DATASET_DIRECTORY / "weather_v1_observations.jsonl"
        )

        report = evaluate_dataset(cases, observations)

        self.assertEqual(report.total, len(cases))
        self.assertEqual(report.failed, 0, report.summary())

    def test_malformed_jsonl_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.jsonl"
            path.write_text("{not json}\n", encoding="utf-8")

            with self.assertRaises(EvalDatasetError):
                load_cases(path)

    def test_case_id_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-id.jsonl"
            path.write_text(
                json.dumps({"query": "合肥天气"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(EvalDatasetError):
                load_cases(path)


if __name__ == "__main__":
    unittest.main()
