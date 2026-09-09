"""LangChain tools exposed to the weather agent."""

from __future__ import annotations

from typing import Any, Literal

from langchain.tools import tool
from pydantic import BaseModel, Field, field_validator

from weather_agent.weather_service import OpenMeteoWeatherService, WeatherServiceError


class CurrentWeatherInput(BaseModel):
    """Validated input contract for the current-weather tool."""

    location: str = Field(min_length=1, max_length=120)


class WeatherData(BaseModel):
    """Structured weather payload returned by the weather service."""

    requested_location: str
    resolved_location: dict[str, Any]
    observation_time: str | None = None
    timezone: str | None = None
    timezone_abbreviation: str | None = None
    weather_description: str
    current: dict[str, Any]
    units: dict[str, Any]


class WeatherToolResult(BaseModel):
    """Stable success and failure contract exposed to the Agent."""

    status: Literal["success", "error"]
    error_type: str | None = None
    message: str | None = None
    retryable: bool = False
    data: WeatherData | None = None


class ForecastInput(BaseModel):
    """Validated input contract for the forecast tool."""

    location: str = Field(min_length=1, max_length=120)
    days: int = Field(default=3, ge=1, le=7)


class ForecastDay(BaseModel):
    """Normalized forecast values for one local calendar day."""

    date: str
    temperature_max: float | None = None
    temperature_min: float | None = None
    precipitation_probability_max: float | None = None
    weather_code: int | None = None
    weather_description: str
    sunrise: str | None = None
    sunset: str | None = None
    wind_speed_max: float | None = None


class ForecastData(BaseModel):
    """Structured daily forecast payload."""

    requested_location: str
    resolved_location: dict[str, Any]
    timezone: str | None = None
    timezone_abbreviation: str | None = None
    forecast: list[ForecastDay]
    units: dict[str, Any]


class ForecastToolResult(BaseModel):
    """Stable success and failure contract for forecast queries."""

    status: Literal["success", "error"]
    error_type: str | None = None
    message: str | None = None
    retryable: bool = False
    data: ForecastData | None = None


class CompareWeatherInput(BaseModel):
    """Validated input contract for multi-location comparison."""

    locations: list[str] = Field(min_length=2, max_length=5)

    @field_validator("locations")
    @classmethod
    def normalize_locations(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("locations must not contain empty values")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("locations must not contain duplicates")
        return normalized


class WeatherComparisonItem(BaseModel):
    """Result for one location in a comparison."""

    requested_location: str
    status: Literal["success", "error"]
    error_type: str | None = None
    message: str | None = None
    retryable: bool = False
    data: WeatherData | None = None


class WeatherComparisonResult(BaseModel):
    """Structured result that preserves partial comparison failures."""

    status: Literal["success", "partial", "error"]
    results: list[WeatherComparisonItem]


@tool(args_schema=CurrentWeatherInput)
def get_current_weather(location: str) -> WeatherToolResult:
    """查询某个城市、地区或地点的当前天气。

    Args:
        location: 用户提供的地点名称，例如“北京市”“上海浦东”或“Tokyo”。

    Returns:
        结构化成功或失败结果。失败结果包含 error_type 和 retryable 字段。
    """
    try:
        weather = OpenMeteoWeatherService().get_current_weather(location)
    except WeatherServiceError as exc:
        return WeatherToolResult(
            status="error",
            error_type=exc.error_type,
            message=str(exc),
            retryable=exc.retryable,
        )

    return WeatherToolResult(
        status="success",
        data=WeatherData.model_validate(weather),
    )


@tool(args_schema=ForecastInput)
def get_weather_forecast(location: str, days: int = 3) -> ForecastToolResult:
    """查询某地未来 1 到 7 天的每日天气预报。"""
    try:
        forecast = OpenMeteoWeatherService().get_weather_forecast(
            location,
            days=days,
        )
    except WeatherServiceError as exc:
        return ForecastToolResult(
            status="error",
            error_type=exc.error_type,
            message=str(exc),
            retryable=exc.retryable,
        )

    return ForecastToolResult(
        status="success",
        data=ForecastData.model_validate(forecast),
    )


@tool(args_schema=CompareWeatherInput)
def compare_current_weather(locations: list[str]) -> WeatherComparisonResult:
    """查询并比较 2 到 5 个地点的当前天气。"""
    service = OpenMeteoWeatherService()
    results: list[WeatherComparisonItem] = []
    for location in locations:
        try:
            weather = service.get_current_weather(location)
        except WeatherServiceError as exc:
            results.append(
                WeatherComparisonItem(
                    requested_location=location,
                    status="error",
                    error_type=exc.error_type,
                    message=str(exc),
                    retryable=exc.retryable,
                )
            )
        else:
            results.append(
                WeatherComparisonItem(
                    requested_location=location,
                    status="success",
                    data=WeatherData.model_validate(weather),
                )
            )

    successful = sum(result.status == "success" for result in results)
    status: Literal["success", "partial", "error"]
    if successful == len(results):
        status = "success"
    elif successful == 0:
        status = "error"
    else:
        status = "partial"
    return WeatherComparisonResult(status=status, results=results)


WEATHER_TOOLS = [
    get_current_weather,
    get_weather_forecast,
    compare_current_weather,
]
