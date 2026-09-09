"""Weather agent construction."""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from weather_agent.config import Settings
from weather_agent.tools import WEATHER_TOOLS

SYSTEM_PROMPT = """你是一名严谨的天气信息助手。

工作规则：
1. 当前天气问题调用 get_current_weather；未来 1 到 7 天问题调用 get_weather_forecast。
2. 用户要求比较 2 到 5 个地点时调用 compare_current_weather，不要手动猜测或合并地点数据。
3. 先检查工具返回的 status；只有 status 为 success 时才使用 data 中的数据。
4. 对比结果为 partial 时，只总结成功地点，并明确说明失败地点和原因。
5. 如果工具返回 error，不要重复调用相同工具和相同参数；根据 error_type 说明问题。
6. 明确说明解析后的地点和天气数据对应的观测时间或预报日期。
7. 用简洁中文总结用户请求的字段；数据中缺少的字段不要臆造。
8. 这是天气信息，不要把一般性描述包装成灾害预警或专业安全保证。
"""


def build_weather_agent(settings: Settings | None = None) -> Any:
    """Build and return the configured LangChain weather agent."""
    resolved_settings = settings or Settings.from_environment()

    model_options: dict[str, Any] = {
        "model": resolved_settings.openai_model,
        "api_key": resolved_settings.openai_api_key,
        "temperature": resolved_settings.model_temperature,
        "timeout": resolved_settings.model_timeout_seconds,
        "max_retries": resolved_settings.model_max_retries,
        "max_completion_tokens": resolved_settings.model_max_completion_tokens,
    }
    if resolved_settings.openai_base_url:
        model_options["base_url"] = resolved_settings.openai_base_url

    model = ChatOpenAI(**model_options)
    return create_agent(
        model=model,
        tools=WEATHER_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        name="weather-agent",
    )
