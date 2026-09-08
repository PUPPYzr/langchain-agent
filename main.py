"""Command-line entry point for the LangChain weather agent."""

from __future__ import annotations

import argparse
import sys
from time import perf_counter
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from weather_agent import build_weather_agent
from weather_agent.config import ConfigurationError
from weather_agent.weather_service import OpenMeteoWeatherService, WeatherServiceError


def log_timing(stage: str, started_at: float) -> None:
    """Write an elapsed-time record without mixing it into the answer."""
    elapsed = perf_counter() - started_at
    print(f"[耗时] {stage}: {elapsed:.3f} 秒", file=sys.stderr)


class TimingCallbackHandler(BaseCallbackHandler):
    """Log individual model and tool run durations for Agent mode."""

    def __init__(self) -> None:
        self._started_at: dict[Any, float] = {}

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> Any:
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._started_at[run_id] = perf_counter()
        return run_id

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        started_at = self._started_at.pop(run_id, None)
        if started_at is not None:
            log_timing("模型请求", started_at)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> Any:
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._started_at[run_id] = perf_counter()
        return run_id

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        started_at = self._started_at.pop(run_id, None)
        if started_at is not None:
            log_timing("天气工具调用", started_at)


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="查询某地当前天气的 LangChain Agent")
    parser.add_argument(
        "location",
        nargs="?",
        help="要查询的地点，例如：北京、上海浦东、Tokyo",
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
        return content
    return str(content)


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
    location = arguments.location or input("请输入要查询天气的地点：").strip()
    if not location:
        print("地点不能为空。", file=sys.stderr)
        return 2

    try:
        if arguments.agent:
            started_at = perf_counter()
            agent = build_weather_agent()
            log_timing("Agent 初始化", started_at)

            started_at = perf_counter()
            result = agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": f"请查询并说明 {location} 当前的天气状况。",
                        }
                    ]
                },
                config={"callbacks": [TimingCallbackHandler()]},
            )
            log_timing("Agent 完整调用（包含模型和工具）", started_at)
            output = extract_text(result)
        else:
            started_at = perf_counter()
            weather = OpenMeteoWeatherService().get_current_weather(location)
            log_timing("直接天气查询（包含地点解析和天气请求）", started_at)
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
