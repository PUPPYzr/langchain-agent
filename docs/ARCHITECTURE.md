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
- 整次运行超过 `AGENT_RUN_TIMEOUT_SECONDS`。该预算是**整次运行**的，不是单次模型调用预算；未显式配置时按 `MODEL_TIMEOUT_SECONDS * (AGENT_MAX_TURNS + 1)` 推导。
- 用户使用 Ctrl+C 取消。

## Resume Contract

平台对恢复行为定义如下边界，由 `AgentApplicationService.verify_resumable()` 与 CLI `--resume` 共同保证：

- Thread 必须存在持久化 Checkpoint，否则拒绝恢复。
- 每次恢复创建新的 `run_id`，沿用同一个 `thread_id`。
- 新 Run 通过 `parent_run_id` 记录被恢复的 Run，形成可追溯的恢复链。
- 已完成（`completed`）的 Thread 不允许再次恢复，避免重复执行副作用；`interrupted` 可恢复，`failed`/`cancelled`/`running` 允许按崩溃恢复语义重试。
- 未提供 `resume_value` 时以 `None` 恢复；提供时通过 `Command(resume=...)` 注入。
- 恢复复用 Run 级超时、执行前取消检查与有限重试策略。

运行暂停时 `AgentRunResult.status` 为 `interrupted`，CLI 以退出码 `3` 返回并输出 `thread_id`。

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

LangGraph Runtime 的 Checkpoint 由 `CheckpointProvider` 提供。默认的 langgraph 配置使用 `langgraph-checkpoint-sqlite` 官方 `SqliteSaver`，将线程、Checkpoint、父检查点关系、状态快照和 `pending_writes` 写入 SQLite，并仅允许安全 MsgPack 类型反序列化；`InMemoryLangGraphCheckpointProvider` 仅用于测试或明确的临时运行。升级前的 `agent_checkpoints` 快照会迁移到官方表，源表保留。Run 记录由 `RunRepository` 保存，Checkpoint 查询与按 Thread 删除由 `CheckpointRepository` 提供。

LangGraph Runtime 能将真实 `interrupt()` 表达为 `interrupted` Run，并通过相同 `thread_id` 和 `Command(resume=...)` 恢复。该能力已有跨 Saver 实例的并行节点待处理写入集成测试；天气业务与 CLI 尚未暴露人工确认和 Resume 流程。

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

## 上下文传播与可观测性边界

`AgentApplicationService._execute_with_policy()` 在独立线程执行 Runtime。由于 `ContextVar` 不会被新线程继承，执行前会复制调用方上下文（`copy_context()`）再提交任务，使 Trace Recorder 与进度上报在回调线程内仍可见。Trace 与进度由此覆盖 `stage=model`、`stage=tool`、`stage=http` 事件，而不仅是 Run 级事件。

## Tool Call Policy

工具调用策略由平台执行层强制，不放在 CLI：

- `LangChainAgentCompiler` 用 `make_guarded_tool` 包装每个注册 Tool。
- `ExecutableAgentRuntime` 在 `run`/`stream`/`resume` 中为每次运行绑定一个 `ToolCallGuard`（经 `ContextVar` 传播，与 S1 的上下文复制配合，保证并发与顺序运行互不共享状态）。
- 守卫在调用前判定：相同工具 + 相同参数 → 返回 `error_type=duplicate_tool_call`；超出 `max_tool_calls` → 返回 `error_type=tool_call_budget_exhausted`。两种情况都**不执行原 Tool**，而是把结构化错误交给模型。
- 统计写入 `AgentRunResult.metadata.tool_call_policy`。

选择"返回结构化错误"而不是"抛异常"的原因：LangGraph `ToolNode` 默认 `handle_tool_errors=True`，会捕获工具及其回调抛出的异常并转为 ToolMessage，回调层 raise 无法阻止执行。因此可靠的控制点必须在 Tool 本身。

## Offline Evaluation

`evals/` 提供版本化数据集与确定性规则评估，不依赖 LLM Judge：

- `EvalCase` 描述期望（工具选择、禁止工具、调用上限、参数边界、必需字段、失败披露）。
- `TrajectoryObservation` 描述实际（工具调用、参数、状态、错误类型、最终回答）。
- 规则包括工具选择、禁止工具、调用上限、重复调用、参数边界、错误分类、必需字段、失败/部分失败披露、事实来源（无成功调用时不得出现具体数值）。
- `python -m evals --dataset ... --observations ...` 返回 0/1/2 作为发布门禁。

## Cancellation Semantics

取消是**协作式**的，契约由 `agent_platform/cancellation.py` 定义：

- `CancellationToken` 是线程安全的取消信号，可通过 `cancel_event`（`threading.Event`）或既有令牌构造，因此旧调用方不受影响。
- `cancellation_scope(token)` 把令牌绑定到当前执行上下文；`AgentApplicationService` 在提交任务前复制上下文（`copy_context()`），因此工作线程内的守卫与 Tool 包装器观察到同一个令牌。
- 强制检查点：
  1. 执行开始前（`raise_if_cancelled("execution")`）；
  2. **每次工具调用前**（`ToolCallGuard.check` 首先查询令牌）；
  3. 流式运行的两个事件之间。
- CLI 在 Ctrl+C 时设置共享的 `cancel_event`；`AgentApplicationService.run/stream/resume` 捕获 `KeyboardInterrupt` 并把 Run 记为 `cancelled`。

### 诚实边界（不欺骗用户）

同步设计中**无法**中止已经进入阻塞状态的调用：

- 一次正在进行的模型请求或 `httpx` 阻塞读取不会被另一个线程打断；
- `future.cancel()` 只对尚未开始的任务有效；
- 超时路径会同时 `token.request_cancel("run timeout")` 以阻止后续工具与轮次，并在异常信息中明确说明"在途调用可能仍在排空（draining）"；
- 被取消或超时的 Run 记录为 `cancelled` / `failed`，**绝不记为 `completed`**；在途调用的结果被丢弃。

### 异步 Runtime 与进程隔离评估

| 方案 | 能否强制终止在途调用 | 代价 | 结论 |
| --- | --- | --- | --- |
| 当前：同步 + 协作令牌 | 否（仅在检查点生效） | 已实现，改动最小 | 保留为默认 |
| 异步 Runtime（`ainvoke` + `httpx.AsyncClient`） | 可以，取消发生在 `await` 点 | 需要与同步 Runtime 并行的异步实现、异步 Tool 与账本写入；会引入两套执行语义 | 未采纳，待证据（长耗时工具或硬取消需求） |
| 进程隔离（子进程 + 终止） | 可以，硬杀 | 序列化、进程管理、可观测性复杂度显著上升 | 未采纳，同上 |

`RuntimeRegistry` 已按 `runtime_type` 装配，因此新增一个 `async` Runtime 不需要修改业务代码；但按项目约束，只有在 Eval 或负载数据证明收益后才立项。

## Observability Contract

`agent_platform/telemetry.py` 是唯一的事实来源，所有写入 Trace 的生产者（Runtime、Application Service、Tool 守卫、CLI 回调、天气 Tool）都遵守同一套约定。

### 规范阶段与事件

| 阶段 | 生产者 | 代表事件 |
| --- | --- | --- |
| `run` | CLI | `question_received`、`answer_ready`、`finished`、`failed`、`interrupted` |
| `runtime` | `ExecutableAgentRuntime` / `AgentApplicationService` | `run.started`、`state.updated`、`run.completed`、`run.failed`、`run.interrupted`、`run.resumed`、`run.cancelled`、`run.timeout`、`run.retry_scheduled` |
| `model` | CLI 回调 | `request_completed`（含 token） |
| `tool` | CLI 回调 | `call_started`、`call_completed` |
| `http` | `weather_service` | `request_completed` |

事件统一通过 `emit_event(context, event_type, **data)` 发布，因此始终携带同一个 `run_id` 与 `session_id`（Thread），异常路径（取消、超时、重试、中断、恢复）与正常路径落入同一 Trace 文件。

### 统一错误分类

`classify_error(error)` 沿 `__cause__`/`__context__` 链检索（有环保护），平台异常优先于供应商启发式，输出固定类别：`cancelled`、`run_timeout`、`model_timeout`、`rate_limit`、`authentication_error`、`agent_turn_limit`、`resume_not_allowed`、`agent_error`。该类别同时写入 Run 账本 `error_type` 与 Trace 顶层 `error_category`。

### 脱敏与保留

`TracingPolicy` 控制 Trace 的可持久化范围：

- 凭证键（`api_key`、`authorization`、`token`、`secret`、`password` 等）**始终**替换为 `<redacted>`；`input_tokens`/`output_tokens` 不受影响。
- 用户内容键（`question`、`content`、`message`、`arguments` 等）默认替换为 `<redacted>`，仅在 `TRACE_CAPTURE_CONTENT=true` 时保留。
- 长字符串按 `TRACE_MAX_STRING_LENGTH` 截断。
- `purge_expired_traces` 按 `TRACE_RETENTION_DAYS` 清理，CLI 入口为 `python main.py --purge-traces`。
- `TraceRecorder.record` 在文件关闭后为 no-op，避免被取消的运行在线程排空阶段写入已关闭的文件。

### 结构化指标

`agent_platform/metrics.py` 从已落盘产物计算，可跨版本比较：

- `summarize_runs`：状态分布、成功率、失败分类、延迟 p50/p95/max（最近秩）。
- `summarize_trace_directory`：Run 数、模型/工具/HTTP 调用数、输入输出 Token、估算成本（需配置 `MODEL_INPUT_PRICE_PER_MILLION` / `MODEL_OUTPUT_PRICE_PER_MILLION`）、重试/取消/超时/中断/恢复计数。
- CLI：`python main.py --metrics [--json] [--session ID] [--status S]`。

### 已知边界

- Node 与 Checkpoint 事件仍由 LangGraph 内部产生，未新增独立事件；节点级信息通过 `state.updated` 与 `CheckpointRepository` 查询间接覆盖。
- **Run 账本仍保存最终回答正文**（`result_json`）。本阶段的数据保护仅覆盖 Trace；是否对账本正文做脱敏/加密需要单独决策。

## HTTP Service Boundary

`service/` 是平台的接入层，唯一职责是把 HTTP 请求翻译成 `AgentApplicationService` 调用：

```text
HTTP 请求
  → ServiceRequestHandler（认证 / 限流 / 校验 / 审计）
  → AgentService（租户命名空间 / 幂等 / 序列化）
  → AgentApplicationService（Run 生命周期、超时、取消、重试）
  → RuntimeRegistry → Runtime → Tool
```

### 分层职责

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| `service/security.py` | 认证、令牌桶限流、审计写入 | 业务语义 |
| `service/api.py` | 请求模型、错误码到状态码映射、响应序列化 | 执行业务逻辑 |
| `service/app.py` | 路由、租户隔离、幂等、HTTP 语义 | Runtime/Compiler/Tool 细节 |
| `service/config.py` | 环境配置与**启动安全门禁** | 运行时策略 |

### 关键设计决策

- **租户隔离用命名空间而非新表**：Thread 实际标识为 `<tenant_id>:<session_id>`，因此"越权读取"天然不存在，跨租户查询统一返回 `404`（不泄露资源是否存在）。
- **幂等靠 `Idempotency-Key` → `run_id` 映射**：重复请求复用既有 Run，不重复执行。映射为进程内、TTL 1 小时；多实例部署需改为外部存储（P2）。
- **审计先于响应**：若审计写在响应之后，客户端可能在审计落盘前就收到响应，进程崩溃会丢审计。因此先审计、再写出响应。
- **请求体先消费再早返回**：HTTP/1.1 keep-alive 下，未读取的请求体会被解析成下一个请求行，导致状态码错乱。超限请求体无法安全排空，故直接关闭连接。
- **启动安全门禁**：非回环绑定在 `SERVICE_ALLOW_PUBLIC_EXPOSURE` 为假时直接拒绝启动，把 TODO 的"未完成边界前不暴露公网"变成代码约束而非文档约定。

### 为什么不用 ASGI 框架

当前边界定位为本地/单实例，`ThreadingHTTPServer` + 已存在的 pydantic 足以覆盖认证、校验、SSE 与审计，且**不引入新依赖**。若后续需要多实例、连接池或 OpenAPI 文档，只需替换 `create_server`：`AgentService` 与 `service/api.py` 的契约不依赖传输层，可直接被 FastAPI 复用。

### 已知边界

- 幂等表与限流桶为进程内状态，多实例下需外部存储。
- 编译后的 Agent 在进程内复用一份（与 CLI 每次重建不同），假设 LangGraph 在不同 `thread_id` 上并发调用是安全的。
- 未实现 OpenAPI/Schema 导出，接口契约以本文件与 README 为准。
