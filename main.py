"""Command-line entry point for the LangChain weather agent."""

from __future__ import annotations

import argparse
import json
import sys
from time import perf_counter
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

from weather_agent import build_weather_agent
from weather_agent.config import ConfigurationError, Settings
from weather_agent.observability import (
    ProgressReporter,
    TraceRecorder,
    progress_context,
    record_trace,
)
from weather_agent.weather_service import OpenMeteoWeatherService, WeatherServiceError


class TimingCallbackHandler(BaseCallbackHandler):
    """Log individual model and tool run durations for Agent mode."""

    def __init__(
        self,
        *,
        run_id: str,
        model_name: str,
        progress: ProgressReporter | None = None,
    ) -> None:
        self.run_id = run_id
        self.model_name = model_name
        self.progress = progress
        self._started_at: dict[Any, float] = {}
        self._tool_calls: set[tuple[str, str]] = set()

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> Any:
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._started_at[run_id] = perf_counter()
        return run_id

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        started_at = self._started_at.pop(run_id, None)
        if started_at is not None:
            usage = self._extract_usage(response)
            elapsed = round(perf_counter() - started_at, 3)
            record_trace(
                "model",
                "request_completed",
                model=self.model_name,
                elapsed_seconds=elapsed,
                input_tokens=usage.get("input_tokens", "unknown"),
                output_tokens=usage.get("output_tokens", "unknown"),
            )
            if self.progress is not None:
                self.progress.update("模型响应完成")

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, Any]:
        """Extract token usage from LangChain's possible response shapes."""
        usage = getattr(response, "usage_metadata", None) or {}
        if usage:
            return usage

        generations = getattr(response, "generations", None) or []
        if generations and generations[0]:
            message = getattr(generations[0][0], "message", None)
            usage = getattr(message, "usage_metadata", None) or {}
            if usage:
                return usage

        llm_output = getattr(response, "llm_output", None) or {}
        return llm_output.get("token_usage", {}) or {}

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> Any:
        tool_name = serialized.get("name", "unknown_tool")
        try:
            arguments = json.loads(input_str)
        except (TypeError, json.JSONDecodeError):
            arguments = input_str
        normalized_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        call_key = (tool_name, normalized_arguments)
        if call_key in self._tool_calls:
            raise RuntimeError(
                f"Duplicate tool call blocked: {tool_name}({normalized_arguments})"
            )
        self._tool_calls.add(call_key)
        record_trace(
            "tool",
            "call_started",
            tool=tool_name,
            arguments=arguments,
        )
        if self.progress is not None:
            self.progress.update(f"正在调用工具：{tool_name}")
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._started_at[run_id] = perf_counter()
        return run_id

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        started_at = self._started_at.pop(run_id, None)
        if started_at is not None:
            record_trace(
                "tool",
                "call_completed",
                elapsed_seconds=round(perf_counter() - started_at, 3),
            )
            if self.progress is not None:
                self.progress.update("工具调用完成，正在生成答案")


def classify_agent_error(error: Exception) -> str:
    """Map provider and runtime failures to stable user-facing categories."""
    error_name = type(error).__name__.lower()
    if "timeout" in error_name:
        return "model_timeout"
    if "ratelimit" in error_name or "rate_limit" in error_name:
        return "rate_limit"
    if "authentication" in error_name or "permission" in error_name:
        return "authentication_error"
    if "recursion" in error_name:
        return "agent_turn_limit"
    if "duplicate tool call" in str(error).lower():
        return "duplicate_tool_call"
    return "agent_error"


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="查询某地当前天气的 LangChain Agent")
    parser.add_argument(
        "location",
        nargs="?",
        help="地点或天气问题，例如：北京、北京未来三天天气、比较北京和上海",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="使用大模型 Agent 处理查询；默认直接调用天气服务",
    )
    return parser.parse_args()


def extract_text(result: dict[str, Any]) -> str:
    """Extract readable content from the agent's final message."""
    messages = result.get("messages") or []
    if not messages:
        return "Agent 没有返回消息。"

    content = messages[-1].content
    if isinstance(content, str):
        return clean_terminal_text(content)
    return clean_terminal_text(str(content))


def clean_terminal_text(text: str) -> str:
    """Remove Markdown emphasis markers from terminal-facing Agent output."""
    return text.replace("**", "")


def format_weather(weather: dict[str, Any]) -> str:
    """Format the structured weather response without another model request."""
    location = weather["resolved_location"]["display_name"]
    current = weather.get("current", {})
    units = weather.get("units", {})

    def value(field: str) -> str:
        raw_value = current.get(field)
        if raw_value is None:
            return "暂无数据"
        return f"{raw_value}{units.get(field, '')}"

    return "\n".join(
        (
            f"{location} 当前天气",
            f"观测时间：{weather.get('observation_time', '未知')}",
            f"天气：{weather.get('weather_description', '未知')}",
            f"温度：{value('temperature_2m')}，体感：{value('apparent_temperature')}",
            f"湿度：{value('relative_humidity_2m')}，降水：{value('precipitation')}",
            f"风速：{value('wind_speed_10m')}，风向：{value('wind_direction_10m')}",
        )
    )


def main() -> int:
    """Run a single weather query."""
    arguments = parse_arguments()
    interactive_mode = arguments.location is None
    try:
        question = arguments.location or input("请输入你要询问的问题？").strip()
    except EOFError:
        print("未检测到输入，请重新运行程序并输入问题。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    if not question:
        print("问题不能为空。", file=sys.stderr)
        return 2

    try:
        use_agent = arguments.agent or interactive_mode
        if use_agent:
            settings = Settings.from_environment()
            run_id = str(uuid4())
            session_id = str(uuid4())
            progress = ProgressReporter()
            with progress_context(progress), TraceRecorder(
                run_id=run_id,
                session_id=session_id,
                model_name=settings.openai_model,
            ) as trace:
                trace.record("run", "question_received", question=question)
                progress.update("正在初始化 Agent")
                agent = build_weather_agent(settings)
                trace.record("agent", "initialized")
                progress.update("正在请求模型分析问题")
                try:
                    result = agent.invoke(
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": question,
                                }
                            ]
                        },
                        config={
                            "callbacks": [
                                TimingCallbackHandler(
                                    run_id=run_id,
                                    model_name=settings.openai_model,
                                    progress=progress,
                                )
                            ],
                            "metadata": {
                                "run_id": run_id,
                                "session_id": session_id,
                            },
                            "recursion_limit": max(2, settings.agent_max_turns * 2 + 1),
                        },
                    )
                except Exception as exc:
                    trace.record(
                        "run",
                        "failed",
                        error_type=classify_agent_error(exc),
                        exception=type(exc).__name__,
                    )
                    progress.finish("Agent 失败")
                    print(
                        f"错误：Agent 执行失败，run_id={run_id}，"
                        f"error_type={classify_agent_error(exc)}。"
                        "可检查模型服务，或暂时使用确定性天气查询模式。",
                        file=sys.stderr,
                    )
                    return 1
                trace.record("run", "answer_ready")
                progress.finish("完成")
                output = extract_text(result)
        else:
            run_id = str(uuid4())
            session_id = str(uuid4())
            progress = ProgressReporter()
            with progress_context(progress), TraceRecorder(
                run_id=run_id,
                session_id=session_id,
            ) as trace:
                trace.record("run", "question_received", question=question)
                try:
                    weather = OpenMeteoWeatherService().get_current_weather(question)
                except WeatherServiceError:
                    progress.finish("查询失败")
                    raise
                trace.record("run", "answer_ready")
                progress.finish("完成")
                output = format_weather(weather)
    except (ConfigurationError, WeatherServiceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
