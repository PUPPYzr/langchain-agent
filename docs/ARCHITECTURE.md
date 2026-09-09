# Agent Architecture

## User Problem

用户希望查询当前天气、未来预报，或比较多个地点的天气，并获得基于外部天气数据的中文摘要。

## Agent Goal

在不臆造天气事实的前提下，根据用户问题选择当前天气、未来预报或多地点比较工具，并生成简洁中文说明。

## Architecture Decision

当前保留两条路径：

- 默认路径是确定性 Workflow：地点解析 -> 天气 API -> 本地格式化。
- `--agent` 路径是 Single Agent：模型决定调用当前天气、未来预报或多地点比较工具，工具返回结构化数据，模型生成摘要。

不引入 Multi-Agent、长期 Memory 或复杂 Planning。当前工具数量和任务范围不足以证明这些复杂度有收益。

## Contract

### Inputs

- `location`: 非空、最长 120 个字符的地点或天气问题。
- `--agent`: 可选，启用完整 Agent 流程。

### Outputs

- 成功：当前天气、每日预报或多个地点的结构化比较结果。
- 失败：用户可读错误；Agent 失败时同时记录 `run_id`。

### Constraints

- 实时天气必须来自 Open-Meteo。
- 不得使用模型记忆补充缺失天气字段。
- 天气工具是只读外部 API 操作。
- 模型请求有超时、有限重试、输出 token 和最大轮数预算。
- 相同工具和相同参数的重复调用会被运行时阻断。
- 预报天数限制为 1 到 7 天，对比地点限制为 2 到 5 个。

### Failure Conditions

- 输入为空或超过长度限制。
- 地点无法解析。
- 预报天数或对比地点数量无效。
- 天气服务超时、限流、网络失败或返回无效数据。
- 模型服务失败或超过运行预算。

## State / Memory / Knowledge

- State：当前 Agent invocation 的 messages、工具结果和运行状态。
- Memory：当前项目不保存长期用户记忆。
- Knowledge：Open-Meteo 地理编码和天气 API 的实时响应。

## Stop Conditions

- 模型生成最终回答。
- 工具返回不可恢复错误。
- `AGENT_MAX_TURNS` 转换得到的 LangGraph recursion limit。
- 模型请求超时或有限重试耗尽。
- 用户使用 Ctrl+C 取消。

## Success Criteria

- 正常地点得到结构化天气数据和最终摘要。
- 工具失败能区分输入错误、未找到、超时、限流和可重试服务错误。
- Agent 每次运行有唯一 `run_id`，模型和工具耗时可追踪。
- 终端使用单行累计进度；详细事件写入 `observability/traces/<run_id>.jsonl`。
- 模型服务不可用时在有限时间内失败，不无限等待。
