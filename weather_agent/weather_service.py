"""Open-Meteo geocoding and current weather client."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import sys
from time import perf_counter
from typing import Any

import httpx

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 15.0

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

    def __init__(self, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def get_current_weather(self, location_name: str) -> dict[str, Any]:
        """Resolve a place and return normalized current weather data."""
        location_name = location_name.strip()
        if not location_name:
            raise WeatherServiceError("Location must not be empty.")

        with httpx.Client(timeout=self._timeout) as client:
            location = self._resolve_location(client, location_name)
            weather = self._fetch_current_weather(client, location)

        return {
            "requested_location": location_name,
            "resolved_location": {
                **asdict(location),
                "display_name": location.display_name,
            },
            **weather,
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
        )
        results = response.get("results") or []
        if not results:
            raise WeatherServiceError(f"未找到地点：{location_name}")

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
            raise WeatherServiceError("地点服务返回了无法识别的数据。") from exc

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
        )

        current = response.get("current")
        if not isinstance(current, dict):
            raise WeatherServiceError("天气服务没有返回当前天气数据。")

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

    @staticmethod
    def _get_json(
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = perf_counter()
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise WeatherServiceError("天气服务请求超时，请稍后重试。") from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise WeatherServiceError(
                f"天气服务返回 HTTP {status_code}。"
            ) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise WeatherServiceError("无法连接天气服务或解析其响应。") from exc

        elapsed = perf_counter() - started_at
        print(f"[耗时] HTTP {url}: {elapsed:.3f} 秒", file=sys.stderr)

        if not isinstance(payload, dict):
            raise WeatherServiceError("天气服务返回了非预期数据。")
        return payload
