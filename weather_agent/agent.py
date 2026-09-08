"""Weather agent construction."""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from weather_agent.config import Settings
from weather_agent.tools import WEATHER_TOOLS

SYSTEM_PROMPT = """你是一名严谨的天气信息助手。

工作规则：
1. 用户询问某地天气时，必须调用 get_current_weather 获取实时数据，不得凭记忆猜测。
2. 明确说明解析后的地点和天气数据对应的观测时间。
3. 用简洁中文总结天气、温度、体感温度、湿度、降水和风况。
4. 数据中缺少的字段不要臆造。
5. 如果地点不明确或工具报错，应向用户说明，并请用户提供更具体的城市、地区或国家。
6. 这是天气信息，不要把一般性描述包装成灾害预警或专业安全保证。
"""


def build_weather_agent(settings: Settings | None = None) -> Any:
    """Build and return the configured LangChain weather agent."""
    resolved_settings = settings or Settings.from_environment()

    model_options: dict[str, Any] = {
        "model": resolved_settings.openai_model,
        "api_key": resolved_settings.openai_api_key,
        "temperature": resolved_settings.model_temperature,
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
