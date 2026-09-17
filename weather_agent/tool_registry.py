"""Platform Tool registrations for the weather domain."""

from agent_platform.tools import ToolRegistry, ToolSpec
from weather_agent.tools import (
    CompareWeatherInput,
    CurrentWeatherInput,
    ForecastData,
    ForecastInput,
    ForecastToolResult,
    WeatherComparisonResult,
    WeatherData,
    WeatherToolResult,
    compare_current_weather,
    get_current_weather,
    get_weather_forecast,
)


WEATHER_TOOL_REGISTRY = ToolRegistry(
    [
        ToolSpec(
            tool_id="get-current-weather",
            version=1,
            description="查询一个地点的当前天气",
            implementation=get_current_weather,
            input_schema=CurrentWeatherInput,
            output_schema=WeatherToolResult,
            timeout_seconds=15,
            max_retries=1,
        ),
        ToolSpec(
            tool_id="get-weather-forecast",
            version=1,
            description="查询一个地点未来 1 到 7 天的天气预报",
            implementation=get_weather_forecast,
            input_schema=ForecastInput,
            output_schema=ForecastToolResult,
            timeout_seconds=15,
            max_retries=1,
        ),
        ToolSpec(
            tool_id="compare-current-weather",
            version=1,
            description="比较 2 到 5 个地点的当前天气",
            implementation=compare_current_weather,
            input_schema=CompareWeatherInput,
            output_schema=WeatherComparisonResult,
            timeout_seconds=15,
            max_retries=1,
        ),
    ]
)

__all__ = ["WEATHER_TOOL_REGISTRY"]
