# Agent Architecture

## User Problem

用户希望查询当前天气、未来预报，或比较多个地点的天气，并获得基于外部天气数据的中文摘要。

## Agent Goal

在不臆造天气事实的前提下，根据用户问题选择当前天气、未来预报或多地点比较工具，并生成简洁中文说明。

## Architecture Decision

当前保留两条路径：

- 默认路径是确定性 Workflow：地点解析 -> 天气 API -> 本地格式化。
- `--agent` 路径是 Single Agent：模型决定调用当前天气、未来预报或多地点比较工具，工具返回结构化数据，模型生成摘要。
- CLI 通过 `agent_platform.AgentRuntime` 执行当前 Agent；`LegacyAgentRuntime` 内部适配现有 LangChain 实现。

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

## Phase 1 Runtime Boundary

当前平台抽象位于 `agent_platform/`：

- `AgentDefinition`：平台拥有的 Agent 标识、版本、目标和 Tool 引用。
- `AgentContext`：一次运行的用户问题、run/session 标识和 Runtime 配置。
- `AgentRunResult`：与框架无关的执行结果。
- `AgentRuntime`：平台定义的 Runtime 协议。
- `LegacyAgentRuntime`：当前 LangChain Agent 的兼容适配器。

当前不持久化 LangChain 对象，也不让 CLI 依赖 LangChain 的具体调用协议。`LangGraphAgentRuntime` 已通过 `runtime_type` 接入；现有 Agent 默认继续使用 `legacy`。

LangGraph Runtime 的 Checkpoint 由 `CheckpointProvider` 提供。默认的 langgraph 配置使用平台自有的 `SQLiteCheckpointProvider`，将线程、Checkpoint、父检查点关系和状态快照写入 SQLite；`InMemoryLangGraphCheckpointProvider` 仅用于测试或明确的临时运行。Run 记录由 `RunRepository` 保存，Checkpoint 查询由 `CheckpointRepository` 提供，两者共同组成运行账本和执行现场查询能力。

## Phase 3 AgentSpec Boundary

Phase 3 已建立平台拥有的版本化 Agent 定义：

- `AgentSpec`：Agent ID、版本、目标、Runtime、Model、Prompt、Tool 引用和执行预算。
- `PromptSpec`：Prompt ID、版本和系统提示词。
- `ToolReference`：稳定 Tool ID 与版本，不保存具体函数对象。
- `AgentSpecValidator`：在 Runtime 执行前校验 Runtime 类型、Tool 注册、Schema、模型超时和运行预算。
- `weather_agent.agent_spec.build_weather_agent_spec`：当前天气 Agent 的兼容 Spec 工厂。

现有 `build_weather_agent(settings)` 仍然是兼容入口，但内部先构造和校验 `WeatherAgentSpec`。当前 Spec 仍由 Python 工厂生成，尚未进入 YAML/数据库持久化；AgentCompiler 已统一负责校验、模型/工具绑定、State Schema 和 Checkpoint 注入。

## Phase 2 Model / Tool Boundary

Phase 2 已建立平台自己的 Model 和 Tool 边界：

- `agent_platform.models.ModelSpec`：模型 Provider、模型名称和运行预算。
- `agent_platform.models.ModelProvider`：平台模型创建协议。
- `LangChainModelProvider`：当前 OpenAI-compatible 模型的 LangChain 实现。
- `agent_platform.tools.ToolSpec`：Tool ID、版本、Schema、权限、超时和重试策略。
- `agent_platform.tools.ToolRegistry`：按稳定 Tool ID 和版本解析实现。

天气 Agent 通过 `WEATHER_TOOL_REGISTRY` 获取当前天气、未来预报和多地点比较 Tool。现有 `build_weather_agent(settings)` 调用保持兼容，但模型和 Tool 的具体实现已集中到平台边界之后。下一阶段可以在不改变天气业务代码的前提下加入 AgentCompiler。
