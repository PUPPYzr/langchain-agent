"""LangChain tools exposed to the weather agent."""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

from weather_agent.weather_service import OpenMeteoWeatherService


@tool
def get_current_weather(location: str) -> dict[str, Any]:
    """查询某个城市、地区或地点的当前天气。

    Args:
        location: 用户提供的地点名称，例如“北京市”“上海浦东”或“Tokyo”。

    Returns:
        包含解析后的地点、观测时间、天气描述、温度、湿度、降水和风况的数据。
    """
    service = OpenMeteoWeatherService()
    return service.get_current_weather(location)


WEATHER_TOOLS = [get_current_weather]
