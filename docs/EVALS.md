# Evaluation Plan

当前 MVP 使用确定性检查，不依赖单一 LLM Judge。

## Regression Cases

| Case | Expected result |
| --- | --- |
| `合肥` | 返回 success，包含地点、观测时间和当前天气字段 |
| 空地点 | 返回 `invalid_input` |
| 不存在地点 | 返回 `not_found` |
| API 超时 | 返回 `timeout` 且 `retryable=true` |
| API 429 | 返回 `rate_limit` 且 `retryable=true` |
| Agent 模型超时 | 有限时间失败，错误包含 `run_id` |
| 默认模式 | 不发生模型调用 |
| `--agent` 模式 | 至少有模型和工具调用 trace |
| 未来三天预报 | 选择 `get_weather_forecast`，返回 3 个日期结果 |
| 预报天数为 0 或 8 | 输入校验失败，不调用天气 API |
| 两个地点比较 | 选择 `compare_current_weather`，返回两个地点结果 |
| 多地点部分失败 | 保留成功地点，并返回 `partial` 状态 |
| 对比地点重复 | 输入校验失败，不重复请求同一地点 |

## Metrics

- Tool argument validation success rate
- Weather response parsing success rate
- Tool error classification accuracy
- Agent task success rate
- End-to-end latency
- Model call count
- Model input/output token usage
- Failure stage coverage

## Production Feedback Loop

真实失败日志中的地点解析失败、超时、限流和模型异常，应脱敏后加入回归样例，并在修改 Prompt、工具或运行预算后重新执行。
