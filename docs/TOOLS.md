# Tool Contract

## `get_current_weather`

### Purpose

查询一个城市、地区或地点的当前天气。该工具是只读工具，不执行写入、发布或其他外部副作用。

### Input Schema

```json
{
  "location": "合肥"
}
```

约束：`location` 必须是 1 到 120 个字符的字符串。

### Success Output

```json
{
  "status": "success",
  "error_type": null,
  "message": null,
  "retryable": false,
  "data": {
    "requested_location": "合肥",
    "resolved_location": {},
    "observation_time": "...",
    "weather_description": "晴朗",
    "current": {},
    "units": {}
  }
}
```

### Error Output

```json
{
  "status": "error",
  "error_type": "timeout",
  "message": "天气服务请求超时，请稍后重试。",
  "retryable": true,
  "data": null
}
```

支持的错误类型包括：

- `invalid_input`
- `not_found`
- `timeout`
- `rate_limit`
- `retryable`
- `service_error`

### Execution Policy

- Permission level：read-only。
- HTTP 请求超时：15 秒。
- 网络错误、HTTP 429 和 HTTP 5xx 最多重试 1 次，并使用短指数退避。
- 地点不存在、输入错误和不可解析响应不重试。
- 工具不接受模型生成的额外 URL、代码或命令。
- 工具结果必须先检查 `status`，不能把错误数据当作天气事实。

## Platform Registry

天气 Tool 通过 `weather_agent.tool_registry.WEATHER_TOOL_REGISTRY` 注册到平台层，使用稳定的 Tool ID 和版本解析实现：

```text
get-current-weather v1
get-weather-forecast v1
compare-current-weather v1
```

Agent 业务定义只引用 Tool ID，不直接依赖具体函数对象。当前 Registry 是进程内实现，后续可替换为持久化或多租户 Registry，而不改变 Tool 调用契约。

AgentSpec 通过 `ToolReference(tool_id, version)` 引用 Tool。Validator 会在运行前确认引用的 Tool、输入 Schema 和输出 Schema 都已注册，避免模型执行到一半才发现配置错误。

## `get_weather_forecast`

### Purpose

查询一个地点未来 1 到 7 天的每日预报，包括最高/最低温度、降水概率、天气状况、日出日落和最大风速。

### Input Schema

```json
{
  "location": "合肥",
  "days": 3
}
```

`days` 必须是 1 到 7 的整数。输出使用 `ForecastToolResult`，成功数据位于 `data.forecast`。

## `compare_current_weather`

### Purpose

查询并比较 2 到 5 个地点的当前天气。每个地点独立执行，单个地点失败不会丢弃其他成功结果。

### Input Schema

```json
{
  "locations": ["合肥", "上海", "北京"]
}
```

地点不得为空或重复。整体 `status` 为 `success`、`partial` 或 `error`，每个地点的结果位于 `results`。

### Permission

以上三个工具均为 read-only，不执行写入、发布或其他外部副作用。
