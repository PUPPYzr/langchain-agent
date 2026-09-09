"""Open-Meteo geocoding and current weather client."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from time import sleep
from time import perf_counter
from typing import Any, Literal

import httpx

from weather_agent.observability import report_progress, record_trace

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 15.0
REQUEST_MAX_RETRIES = 1
RETRY_BACKOFF_SECONDS = 0.25

CURRENT_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "weather_code",
    "cloud_cover",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "is_day",
)

DAILY_FIELDS = (
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "weather_code",
    "sunrise",
    "sunset",
    "wind_speed_10m_max",
)

WEATHER_CODE_DESCRIPTIONS = {
    0: "晴朗",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴天",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴并伴有小冰雹",
    99: "雷暴并伴有强冰雹",
}


class WeatherServiceError(RuntimeError):
    """Raised when a location or weather request cannot be completed."""

    def __init__(
        self,
        message: str,
        *,
        error_type: Literal[
            "retryable",
            "invalid_input",
            "timeout",
            "rate_limit",
            "not_found",
            "service_error",
        ] = "service_error",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """A place resolved by the Open-Meteo geocoding service."""

    name: str
    country: str
    admin1: str | None
    latitude: float
    longitude: float
    timezone: str | None

    @property
    def display_name(self) -> str:
        """Return a readable location name without duplicate components."""
        components = [self.name, self.admin1, self.country]
        unique_components: list[str] = []
        for component in components:
            if component and component not in unique_components:
                unique_components.append(component)
        return ", ".join(unique_components)


class OpenMeteoWeatherService:
    """Fetch current weather after resolving a human-readable place name."""

    def __init__(
        self,
        *,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = REQUEST_MAX_RETRIES,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._timeout = timeout
        self._max_retries = max_retries

    def get_current_weather(self, location_name: str) -> dict[str, Any]:
        """Resolve a place and return normalized current weather data."""
        location_name = location_name.strip()
        if not location_name:
            raise WeatherServiceError(
                "Location must not be empty.",
                error_type="invalid_input",
            )

        with httpx.Client(timeout=self._timeout) as client:
            report_progress("正在解析地点")
            location = self._resolve_location(client, location_name)
            report_progress("正在获取当前天气")
            weather = self._fetch_current_weather(client, location)

        return {
            "requested_location": location_name,
            "resolved_location": {
                **asdict(location),
                "display_name": location.display_name,
            },
            **weather,
        }

    def get_weather_forecast(
        self,
        location_name: str,
        *,
        days: int = 3,
    ) -> dict[str, Any]:
        """Resolve a place and return a normalized daily forecast."""
        location_name = location_name.strip()
        if not location_name:
            raise WeatherServiceError(
                "Location must not be empty.",
                error_type="invalid_input",
            )
        if not 1 <= days <= 7:
            raise WeatherServiceError(
                "Forecast days must be between 1 and 7.",
                error_type="invalid_input",
            )

        with httpx.Client(timeout=self._timeout) as client:
            report_progress("正在解析地点")
            location = self._resolve_location(client, location_name)
            report_progress("正在获取未来预报")
            forecast = self._fetch_daily_forecast(client, location, days)

        return {
            "requested_location": location_name,
            "resolved_location": {
                **asdict(location),
                "display_name": location.display_name,
            },
            **forecast,
        }

    def _resolve_location(
        self,
        client: httpx.Client,
        location_name: str,
    ) -> ResolvedLocation:
        response = self._get_json(
            client,
            GEOCODING_URL,
            params={
                "name": location_name,
                "count": 5,
                "language": "zh",
                "format": "json",
            },
            max_retries=self._max_retries,
        )
        results = response.get("results") or []
        if not results:
            raise WeatherServiceError(
                f"未找到地点：{location_name}",
                error_type="not_found",
            )

        result = results[0]
        try:
            return ResolvedLocation(
                name=str(result["name"]),
                country=str(result.get("country", "")),
                admin1=result.get("admin1"),
                latitude=float(result["latitude"]),
                longitude=float(result["longitude"]),
                timezone=result.get("timezone"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherServiceError(
                "地点服务返回了无法识别的数据。",
                error_type="service_error",
            ) from exc

    def _fetch_current_weather(
        self,
        client: httpx.Client,
        location: ResolvedLocation,
    ) -> dict[str, Any]:
        response = self._get_json(
            client,
            FORECAST_URL,
            params={
                "latitude": location.latitude,
                "longitude": location.longitude,
                "current": ",".join(CURRENT_FIELDS),
                "timezone": "auto",
                "forecast_days": 1,
            },
            max_retries=self._max_retries,
        )

        current = response.get("current")
        if not isinstance(current, dict):
            raise WeatherServiceError(
                "天气服务没有返回当前天气数据。",
                error_type="service_error",
            )

        units = response.get("current_units")
        weather_code = current.get("weather_code")
        try:
            normalized_code = int(weather_code) if weather_code is not None else None
        except (TypeError, ValueError):
            normalized_code = None

        return {
            "observation_time": current.get("time"),
            "timezone": response.get("timezone"),
            "timezone_abbreviation": response.get("timezone_abbreviation"),
            "weather_description": WEATHER_CODE_DESCRIPTIONS.get(
                normalized_code,
                "未知天气状况",
            ),
            "current": current,
            "units": units if isinstance(units, dict) else {},
        }

    def _fetch_daily_forecast(
        self,
        client: httpx.Client,
        location: ResolvedLocation,
        days: int,
    ) -> dict[str, Any]:
        response = self._get_json(
            client,
            FORECAST_URL,
            params={
                "latitude": location.latitude,
                "longitude": location.longitude,
                "daily": ",".join(DAILY_FIELDS),
                "timezone": "auto",
                "forecast_days": days,
            },
            max_retries=self._max_retries,
        )

        daily = response.get("daily")
        if not isinstance(daily, dict):
            raise WeatherServiceError(
                "天气服务没有返回未来预报数据。",
                error_type="service_error",
            )

        dates = daily.get("time")
        if not isinstance(dates, list):
            raise WeatherServiceError(
                "天气服务返回的预报日期无法识别。",
                error_type="service_error",
            )

        def series_value(field: str, index: int) -> Any:
            values = daily.get(field)
            if not isinstance(values, list) or index >= len(values):
                return None
            return values[index]

        forecast = []
        for index, date in enumerate(dates):
            weather_code = series_value("weather_code", index)
            try:
                normalized_code = int(weather_code) if weather_code is not None else None
            except (TypeError, ValueError):
                normalized_code = None
            forecast.append(
                {
                    "date": date,
                    "temperature_max": series_value("temperature_2m_max", index),
                    "temperature_min": series_value("temperature_2m_min", index),
                    "precipitation_probability_max": series_value(
                        "precipitation_probability_max", index
                    ),
                    "weather_code": normalized_code,
                    "weather_description": WEATHER_CODE_DESCRIPTIONS.get(
                        normalized_code,
                        "未知天气状况",
                    ),
                    "sunrise": series_value("sunrise", index),
                    "sunset": series_value("sunset", index),
                    "wind_speed_max": series_value("wind_speed_10m_max", index),
                }
            )

        return {
            "timezone": response.get("timezone"),
            "timezone_abbreviation": response.get("timezone_abbreviation"),
            "forecast": forecast,
            "units": daily.get("units")
            if isinstance(daily.get("units"), dict)
            else response.get("daily_units", {}),
        }

    @staticmethod
    def _get_json(
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, Any],
        max_retries: int = 0,
    ) -> dict[str, Any]:
        started_at = perf_counter()
        for attempt in range(max_retries + 1):
            try:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.TimeoutException as exc:
                if attempt < max_retries:
                    sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                    continue
                raise WeatherServiceError(
                    "天气服务请求超时，请稍后重试。",
                    error_type="timeout",
                    retryable=True,
                ) from exc
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                is_retryable = status_code == 429 or status_code >= 500
                if is_retryable and attempt < max_retries:
                    sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                    continue
                if status_code == 429:
                    error_type: Literal[
                        "retryable", "invalid_input", "timeout", "rate_limit", "not_found", "service_error"
                    ] = "rate_limit"
                    retryable = True
                elif status_code >= 500:
                    error_type = "retryable"
                    retryable = True
                elif status_code == 404:
                    error_type = "not_found"
                    retryable = False
                else:
                    error_type = "service_error"
                    retryable = False
                raise WeatherServiceError(
                    f"天气服务返回 HTTP {status_code}。",
                    error_type=error_type,
                    retryable=retryable,
                ) from exc
            except httpx.RequestError as exc:
                if attempt < max_retries:
                    sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                    continue
                raise WeatherServiceError(
                    "无法连接天气服务，请稍后重试。",
                    error_type="retryable",
                    retryable=True,
                ) from exc
            except ValueError as exc:
                raise WeatherServiceError(
                    "天气服务返回了无法解析的数据。",
                    error_type="service_error",
                ) from exc
        else:
            raise WeatherServiceError(
                "天气服务请求未完成。",
                error_type="retryable",
                retryable=True,
            )

        elapsed = perf_counter() - started_at
        record_trace(
            "http",
            "request_completed",
            url=url,
            elapsed_seconds=round(elapsed, 3),
        )

        if not isinstance(payload, dict):
            raise WeatherServiceError(
                "天气服务返回了非预期数据。",
                error_type="service_error",
            )
        return payload
