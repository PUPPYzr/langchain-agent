import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from main import TimingCallbackHandler
from weather_agent.config import ConfigurationError, Settings
from weather_agent.tools import (
    compare_current_weather,
    get_current_weather,
    get_weather_forecast,
)
from weather_agent.weather_service import (
    OpenMeteoWeatherService,
    ResolvedLocation,
    WeatherServiceError,
)


class WeatherServiceErrorTests(unittest.TestCase):
    def test_empty_location_is_invalid_input(self) -> None:
        with self.assertRaises(WeatherServiceError) as context:
            OpenMeteoWeatherService().get_current_weather("   ")

        self.assertEqual(context.exception.error_type, "invalid_input")
        self.assertFalse(context.exception.retryable)

    def test_timeout_is_retryable(self) -> None:
        request = httpx.Request("GET", "https://example.test")
        client = Mock()
        client.get.side_effect = httpx.ReadTimeout("timed out", request=request)

        with self.assertRaises(WeatherServiceError) as context:
            OpenMeteoWeatherService._get_json(client, str(request.url), params={})

        self.assertEqual(context.exception.error_type, "timeout")
        self.assertTrue(context.exception.retryable)

    def test_server_error_is_retried_once(self) -> None:
        request = httpx.Request("GET", "https://example.test")
        failed_response = httpx.Response(503, request=request)
        successful_response = httpx.Response(
            200,
            request=request,
            json={"results": []},
        )
        client = Mock()
        client.get.side_effect = [failed_response, successful_response]

        result = OpenMeteoWeatherService._get_json(
            client,
            str(request.url),
            params={},
            max_retries=1,
        )

        self.assertEqual(result, {"results": []})
        self.assertEqual(client.get.call_count, 2)

    def test_not_found_tool_result_is_structured(self) -> None:
        with patch.object(
            OpenMeteoWeatherService,
            "get_current_weather",
            side_effect=WeatherServiceError(
                "未找到地点：不存在",
                error_type="not_found",
            ),
        ):
            result = get_current_weather.invoke({"location": "不存在"})

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_type, "not_found")
        self.assertFalse(result.retryable)
        self.assertIsNone(result.data)

    def test_duplicate_tool_call_is_blocked(self) -> None:
        callback = TimingCallbackHandler(run_id=str(uuid4()), model_name="test")
        callback.on_tool_start(
            {"name": "get_current_weather"},
            '{"location":"合肥"}',
            run_id=str(uuid4()),
        )

        with self.assertRaisesRegex(RuntimeError, "Duplicate tool call blocked"):
            callback.on_tool_start(
                {"name": "get_current_weather"},
                '{"location":"合肥"}',
                run_id=str(uuid4()),
            )

    def test_agent_turn_budget_rejects_unbounded_value(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_MODEL": "test-model",
                "AGENT_MAX_TURNS": "21",
            },
            clear=False,
        ):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_unknown_runtime_type_is_rejected(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_MODEL": "test-model",
                "AGENT_RUNTIME_TYPE": "unknown",
            },
            clear=False,
        ):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_daily_forecast_is_normalized(self) -> None:
        request = httpx.Request("GET", "https://example.test")
        response = httpx.Response(
            200,
            request=request,
            json={
                "timezone": "Asia/Shanghai",
                "daily": {
                    "time": ["2026-09-09", "2026-09-10"],
                    "temperature_2m_max": [30.0, 28.0],
                    "temperature_2m_min": [22.0, 21.0],
                    "precipitation_probability_max": [10, 60],
                    "weather_code": [0, 61],
                    "sunrise": ["05:40", "05:41"],
                    "sunset": ["18:20", "18:19"],
                    "wind_speed_10m_max": [15.0, 22.0],
                    "units": {"temperature_2m_max": "°C"},
                },
            },
        )
        client = Mock()
        client.get.return_value = response
        location = ResolvedLocation(
            name="合肥",
            country="中国",
            admin1="安徽",
            latitude=31.8,
            longitude=117.2,
            timezone="Asia/Shanghai",
        )

        result = OpenMeteoWeatherService._fetch_daily_forecast(
            OpenMeteoWeatherService(), client, location, 2
        )

        self.assertEqual(len(result["forecast"]), 2)
        self.assertEqual(result["forecast"][1]["weather_description"], "小雨")
        self.assertEqual(result["forecast"][0]["temperature_max"], 30.0)

    def test_forecast_tool_rejects_invalid_days(self) -> None:
        with self.assertRaises(Exception):
            get_weather_forecast.invoke({"location": "合肥", "days": 8})

    def test_comparison_preserves_partial_failure(self) -> None:
        success = {
            "requested_location": "合肥",
            "resolved_location": {"display_name": "合肥, 安徽, 中国"},
            "observation_time": "2026-09-09T14:00",
            "weather_description": "晴朗",
            "current": {},
            "units": {},
        }
        with patch.object(
            OpenMeteoWeatherService,
            "get_current_weather",
            side_effect=[
                success,
                WeatherServiceError("未找到地点：不存在", error_type="not_found"),
            ],
        ):
            result = compare_current_weather.invoke(
                {"locations": ["合肥", "不存在"]}
            )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.results[0].status, "success")
        self.assertEqual(result.results[1].error_type, "not_found")


if __name__ == "__main__":
    unittest.main()
