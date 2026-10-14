"""Run the deterministic evaluation suite.

Usage:
    python -m evals --dataset evals/datasets/weather_v1.jsonl \
        --observations evals/datasets/weather_v1_observations.jsonl
"""

from __future__ import annotations

import argparse
import json

from evals.rules import EvalDatasetError, evaluate_dataset, load_cases, load_observations


def parse_arguments() -> argparse.Namespace:
    """Parse evaluation command-line arguments."""
    parser = argparse.ArgumentParser(description="运行确定性 Agent 评估")
    parser.add_argument("--dataset", required=True, help="JSONL Eval 数据集路径")
    parser.add_argument(
        "--observations",
        required=True,
        help="JSONL 观测轨迹路径（由运行记录或测试导出）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出报告，便于自动比较基线",
    )
    return parser.parse_args()


def main() -> int:
    """Evaluate the dataset and return a non-zero code on any failure."""
    arguments = parse_arguments()
    try:
        cases = load_cases(arguments.dataset)
        observations = load_observations(arguments.observations)
    except EvalDatasetError as exc:
        print(f"错误：{exc}")
        return 2
    report = evaluate_dataset(cases, observations)
    if arguments.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.summary())
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
