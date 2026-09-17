"""Platform AgentSpec factory for the weather Agent."""

from agent_platform.models import ModelSpec
from agent_platform.spec import AgentSpec, PromptSpec, ToolReference, validate_agent_spec
from agent_platform.tools import ToolRegistry
from weather_agent.config import Settings
from weather_agent.tool_registry import WEATHER_TOOL_REGISTRY


WEATHER_PROMPT_ID = "weather-assistant"
WEATHER_PROMPT_VERSION = 1
WEATHER_AGENT_ID = "weather-agent"
WEATHER_AGENT_VERSION = 1


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


def build_weather_agent_spec(
    settings: Settings,
    *,
    tool_registry: ToolRegistry | None = None,
) -> AgentSpec:
    """Build and validate the versioned platform definition for weather Agent."""
    resolved_registry = tool_registry or WEATHER_TOOL_REGISTRY
    spec = AgentSpec(
        agent_id=WEATHER_AGENT_ID,
        version=WEATHER_AGENT_VERSION,
        goal="根据 Open-Meteo 外部天气事实回答用户问题",
        model=ModelSpec(
            provider="openai-compatible",
            model=settings.openai_model,
            temperature=settings.model_temperature,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            max_completion_tokens=settings.model_max_completion_tokens,
        ),
        prompt=PromptSpec(
            prompt_id=WEATHER_PROMPT_ID,
            version=WEATHER_PROMPT_VERSION,
            system=SYSTEM_PROMPT,
        ),
        tools=(
            ToolReference("get-current-weather", 1),
            ToolReference("get-weather-forecast", 1),
            ToolReference("compare-current-weather", 1),
        ),
        runtime_type=settings.agent_runtime_type,
        max_turns=settings.agent_max_turns,
        timeout_seconds=settings.model_timeout_seconds,
    )
    return validate_agent_spec(spec, resolved_registry)
