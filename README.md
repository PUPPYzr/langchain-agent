# LangGraph Weather Agent Platform

这是一个以天气查询为业务示例、逐步平台化的 LangChain/LangGraph Agent 项目。系统使用 Open-Meteo 提供实时天气事实，通过版本化 AgentSpec、Model/Tool Provider、Compiler、Runtime、State、Run 和 Checkpoint 等边界，将天气业务与通用 Agent 执行能力分离。

当前项目既可以直接执行确定性天气 Workflow，也可以让模型选择工具完成当前天气、未来预报和多地点对比。Agent 模式支持 `legacy` 与 `langgraph` 两种 Runtime，并使用 SQLite 保存运行记录；LangGraph Runtime 还会保存 Checkpoint，为后续中断恢复和长流程执行提供基础。

## 当前能力

- 当前天气查询：温度、体感温度、湿度、降水、风况和观测时间。
- 未来 1 到 7 天天气预报：最高/最低温度、降水概率、天气状况、日出日落和最大风速。
- 2 到 5 个地点的当前天气对比，并保留部分失败结果。
- 确定性 Workflow 与模型驱动 Agent 两种业务执行方式。
- `legacy` 与 `langgraph` Runtime，可通过环境变量选择。
- 版本化 `AgentSpec`、`PromptSpec`、`ModelSpec`、`ToolSpec` 和 Tool 引用。
- Compiler 统一完成配置校验、模型创建、工具解析、Prompt、State Schema 和 Checkpoint 绑定。
- LangGraph `CompiledStateGraph` 编译和执行。
- 同步运行、流式运行和 Resume 运行契约。
- Run 总超时、执行前取消检查和有限重试；LangGraph 中断 Run 会明确记录为 `interrupted`。
- SQLite Run 生命周期记录和 Run/Thread 查询。
- LangGraph 官方 SQLite Saver 持久化 Checkpoint 与 `pending_writes`，支持摘要查询、Thread 删除和跨 Saver 恢复。
- 旧版 `agent_checkpoints` 快照迁移到官方 Saver 表，原表不会自动删除。
- Runtime 事件、模型/工具回调和每次运行独立的 JSONL Trace。
- 结构化 Tool 输入、输出、权限和错误分类。

## 运行方式

项目有三条实际执行路径。

| 命令与配置 | 执行路径 | 模型 | Run 数据库 | Checkpoint |
| --- | --- | --- | --- | --- |
| `python main.py 合肥` | 确定性 Workflow | 不使用 | 不写入 | 不写入 |
| `python main.py` 或 `python main.py "问题" --agent`，配置 `legacy` | Agent + Legacy Runtime | 使用 | 写入 | 不写入 |
| `python main.py` 或 `python main.py "问题" --agent`，配置 `langgraph` | Agent + LangGraph Runtime | 使用 | 写入 | 写入 |

### 交互式 Agent

```powershell
python main.py
```

程序提示：

```text
请输入你要询问的问题？
```

可以输入：

```text
合肥今天的天气怎么样？
北京未来三天天气怎么样？
比较合肥、上海和北京今天的天气。
```

无参数运行固定进入 Agent 模式，具体使用 `legacy` 还是 `langgraph` 由 `AGENT_RUNTIME_TYPE` 决定。

### 命令行 Agent

```powershell
python main.py "北京未来三天天气怎么样" --agent
python main.py "比较合肥、上海和北京今天的天气" --agent
```

### 确定性 Workflow

```powershell
python main.py 合肥
```

该路径直接调用 Open-Meteo 当前天气服务并在本地格式化，不经过模型、Agent Runtime 或 Checkpoint，适合固定的单地点当前天气查询。

### 运行管理命令

查询 Run/Thread/Checkpoint 与显式恢复暂停的运行都不需要模型凭据（`--resume` 除外，它需要完整的 Agent 配置）：

```powershell
# 查询
python main.py --list-runs [--session <thread_id>] [--status interrupted] [--limit 20]
python main.py --list-threads [--limit 20]
python main.py --list-checkpoints <thread_id> [--limit 20]

# 恢复一个处于暂停（interrupted）状态的 Thread
python main.py --resume <thread_id> [--resume-value "approve"]

# 结构化指标（成功率/失败分类/延迟分位/调用数/Token/估算成本）
python main.py --metrics [--json] [--session <thread_id>] [--status <status>]

# 按 TRACE_RETENTION_DAYS 清理过期 Trace
python main.py --purge-traces
```

Resume 契约：Thread 必须存在持久化 Checkpoint；每次恢复都会创建新的 `run_id` 并沿用同一个 `thread_id`，新 Run 通过 `parent_run_id` 记录被恢复的 Run。已完成（`completed`）的 Thread 不允许再次恢复，避免重复执行副作用。未提供 `--resume-value` 时以 `None` 恢复。

运行暂停时 CLI 会以退出码 `3` 返回并打印 `thread_id`，便于脚本据此触发恢复。

### 离线评估

确定性评估不需要模型：数据集描述期望行为，观测文件描述实际轨迹，规则逐条比对。

```powershell
python -m evals --dataset evals/datasets/weather_v1.jsonl `
    --observations evals/datasets/weather_v1_observations.jsonl
python -m evals --dataset evals/datasets/weather_v1.jsonl `
    --observations evals/datasets/weather_v1_observations.jsonl --json
```

全部通过时退出码为 `0`，任一用例失败为 `1`，文件解析失败为 `2`，因此可以直接作为发布门禁。当前规则覆盖：工具选择、禁止工具、工具调用上限、重复调用、参数边界、错误分类、必需字段、失败披露与事实来源（无成功数据时不得给出具体数值）。

## Web 可视化对话界面

浏览器打开 `http://127.0.0.1:8080/` 即可使用；CLI 与 Web 走**同一套 Agent 核心**（`AgentApplicationService`），不存在第二套实现。

```text
浏览器 → service/（认证/限流/校验/审计）→ AgentService（会话 + 事件投影）
      → AgentApplicationService → RuntimeRegistry → LangGraph Agent → Open-Meteo
```

### 启动（一条命令）

```powershell
$env:SERVICE_API_KEYS="team-a:<至少16位密钥>"
$env:AGENT_RUNTIME_TYPE="langgraph"   # 多轮上下文必需
python -m service
```

前端是**免构建**的 ES 模块静态资源（`web/`），由同一个 Python 进程在同端口托管，因此**没有独立的 npm 开发服务器、没有 CORS 配置，也不需要 `npm install`**。浏览器首次打开会要求填写访问密钥（保存在 localStorage），模型密钥始终只存在于服务端。

### 界面能力

- 左侧会话栏：新建、切换、删除会话，会话标题取自首条用户消息，侧边栏可收起。
- 中间消息区：欢迎页 + 示例问题、用户/助手气泡、Markdown 渲染、代码块语法高亮与一键复制、回答复制、时间戳。
- 执行过程时间线：来自 Agent **真实事件**的工具调用名称、参数摘要、执行状态与结果摘要（可折叠）。
- 底部输入区：Enter 发送 / Shift+Enter 换行、自动增高、发送中禁用、**停止生成**按钮。
- 其它：自动滚动（用户上翻时不打断）、浅色/深色主题、响应式布局、键盘可达。

### 关于"流式"的诚实说明

后端按事件流式推送**执行过程**（状态、工具调用与结果），但 Agent 当前**不产生 token 级增量**——最终回答在 `run.completed` 事件中一次到达。因此界面在回答就绪后一次性渲染，**不使用逐字打字动画来伪装流式**。

### 多轮上下文

多轮记忆由 LangGraph Checkpointer 承担，因此需要 `AGENT_RUNTIME_TYPE=langgraph`。服务在 `/v1/health` 中返回 `context_mode`（`checkpointed` / `stateless`），若为 `stateless`，欢迎页会显式提示"多轮上下文不会保留"。会话记录（用户与助手消息）持久化在 `SERVICE_SESSION_DB_PATH`（默认与 Run 账本同一 SQLite 文件），**重启后仍在**。

## HTTP 服务（第三方调用）

`service/` 把平台暴露为 HTTP 服务，第三方系统可据此以编程方式提交问题、订阅流式事件、查询 Run/Thread/Checkpoint 并恢复暂停的运行。它复用 `AgentApplicationService`，**不复制** Runtime、Compiler 或 Tool Registry 逻辑。

### 启动

```powershell
$env:SERVICE_API_KEYS="team-a:<至少16位密钥>,team-b:<至少16位密钥>"
python -m service
```

默认只绑定 `127.0.0.1:8080`。**非回环地址必须显式设置 `SERVICE_ALLOW_PUBLIC_EXPOSURE=true`**，否则服务拒绝启动。

### 端点（前缀 `/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/health` | 存活探针（无需认证） |
| POST | `/v1/runs` | 创建并执行一次 Run；`Accept: text/event-stream` 时返回 SSE |
| GET | `/v1/runs` | 列出 Run，支持 `session_id` / `status` / `limit` |
| GET | `/v1/runs/{run_id}` | 查询单个 Run |
| GET | `/v1/threads` | 列出 Thread |
| GET | `/v1/threads/{thread_id}/checkpoints` | 列出 Checkpoint |
| POST | `/v1/threads/{thread_id}/resume` | 恢复暂停的 Run，可带 `resume_value` |
| DELETE | `/v1/threads/{thread_id}/checkpoints` | 删除该 Thread 的 Checkpoint |

### 边界与治理

- 认证：`Authorization: Bearer <api_key>` 或 `X-API-Key: <api_key>`；仅健康检查免认证。
- 租户隔离：Thread 命名空间为 `<tenant_id>:<session_id>`，跨租户访问一律返回 `404`（不区分"无权"与"不存在"）。
- 限流：`SERVICE_RATE_LIMIT_PER_MINUTE`，超限返回 `429` 并带 `Retry-After`。
- 幂等：`Idempotency-Key` 请求头，1 小时内重复请求复用同一个 `run_id`，不会重复执行。
- 审计：每个请求写入 `SERVICE_AUDIT_PATH`（JSONL），含 `request_id` / `tenant` / `status` / `run_id` / `duration_ms`，并按 `TracingPolicy` 脱敏；**审计先于响应写出**，保证有响应的请求不会漏审。
- 请求校验：JSON 体积上限、`Content-Type`、pydantic 字段校验；请求体在鉴权前统一消费，避免 keep-alive 连接错位。
- 错误响应：`{"error": {"code", "message", "request_id", "run_id"}}`，`code` 稳定可枚举（`unauthorized`、`invalid_request`、`not_found`、`method_not_allowed`、`conflict`、`payload_too_large`、`unsupported_media_type`、`rate_limited`、`run_cancelled`、`run_failed`、`run_timeout`、`internal_error`）。

### 示例

```powershell
curl.exe -s -X POST http://127.0.0.1:8080/v1/runs -H "Authorization: Bearer <api_key>" -H "Content-Type: application/json" -d "{\"question\":\"合肥今天天气怎么样？\"}"
```

当前实现为**本地/单实例**边界（stdlib `ThreadingHTTPServer` + pydantic，零新增依赖）。生产并发、Postgres Checkpoint、备份与容量规划属于 P2，见 `docs/ARCHITECTURE.md`。

## Agent 工具

| LangChain Tool | 平台 Tool ID | 用途 | 输入限制 |
| --- | --- | --- | --- |
| `get_current_weather` | `get-current-weather` v1 | 查询单个地点当前天气 | 地点长度 1 到 120 |
| `get_weather_forecast` | `get-weather-forecast` v1 | 查询未来每日预报 | 预报天数 1 到 7 |
| `compare_current_weather` | `compare-current-weather` v1 | 比较多个地点当前天气 | 地点数量 2 到 5，不允许重复 |

所有天气 Tool 都是只读操作。返回值使用 Pydantic Schema，错误会分类为 `invalid_input`、`not_found`、`timeout`、`rate_limit`、`retryable` 或 `service_error`。AgentSpec 只引用稳定的 Tool ID 和版本，Compiler 再通过 ToolRegistry 解析具体实现。

### 工具调用策略

Compiler 会用 `make_guarded_tool` 包装每个 Tool，Runtime 在每次运行时绑定一个 `ToolCallGuard`，因此重复调用与调用上限在**平台执行层**强制生效，而不是依赖 CLI 回调：

- 完全相同的工具 + 参数再次调用时，不再执行该 Tool，而是返回结构化错误 `error_type=duplicate_tool_call`，模型据此直接作答。
- 设置 `tool_call_policy.max_tool_calls` 后，超出上限的调用返回 `error_type=tool_call_budget_exhausted`。CLI 默认取 `AGENT_MAX_TURNS`。
- 每次运行的统计写入 `AgentRunResult.metadata.tool_call_policy`（`tool_call_count`、`blocked_duplicates`、`blocked_over_budget`）。

之所以不用回调抛异常：LangGraph `ToolNode` 会捕获工具及其回调的异常并转成 ToolMessage，回调层无法真正阻止执行。

### 取消语义

取消是协作式的（`agent_platform/cancellation.py` 的 `CancellationToken`）：检查点位于执行开始前、**每次工具调用前**与流式事件的间隙。取消或超时后，后续工具调用与模型轮次被阻止，Run 记录为 `cancelled`/`failed`，绝不记为 `completed`。

**已知边界**：已经进入阻塞状态的模型请求或 HTTP 读取**无法**被中断，其返回值会被丢弃，异常信息中会明确提示在途调用可能仍在排空。真正的强制终止需要异步 Runtime（在 `await` 点取消）或进程隔离，两者均已评估但未采纳，理由见 `docs/ARCHITECTURE.md`。

## 架构

一次 Agent 请求的主要调用链：

```text
main.py
  -> Settings
  -> Weather AgentSpec
  -> AgentSpecValidator
  -> LangChainAgentCompiler
       -> ModelProvider
       -> ToolRegistry
       -> LangGraphAgentState
       -> CheckpointProvider
  -> CompiledAgent / CompiledStateGraph
  -> AgentApplicationService
  -> RuntimeRegistry
  -> LegacyAgentRuntime 或 LangGraphAgentRuntime
  -> Open-Meteo Tool
  -> AgentRunResult
```

核心职责：

| 模块 | 作用 |
| --- | --- |
| `agent_platform/domain.py` | 定义框架无关的 Agent、运行上下文、结果和错误契约 |
| `agent_platform/spec/` | 描述 Agent、Prompt、Tool 引用及运行预算，并在执行前校验 |
| `agent_platform/models/` | 隔离平台模型配置与 LangChain/OpenAI-compatible 实现 |
| `agent_platform/tools/` | 管理版本化 Tool 契约、权限、Schema 和实现解析 |
| `agent_platform/compiler.py` | 将 AgentSpec 编译成可执行的 LangGraph-backed Agent |
| `agent_platform/runtime.py` | 定义 `run()`、`stream()`、`resume()` 和 Runtime 注册表 |
| `agent_platform/application.py` | 编排 Runtime 选择、Run 生命周期、超时、取消和重试 |
| `agent_platform/state.py` | 定义平台 State 和 LangGraph State Schema |
| `agent_platform/runs.py` | 保存和查询 Run/Thread 生命周期数据 |
| `agent_platform/checkpoints.py` | 提供内存或 SQLite Checkpoint，并查询 Checkpoint 摘要 |
| `agent_platform/events.py` | 将 Runtime 事件发布给 Trace、测试或未来的事件接口 |
| `weather_agent/` | 天气 AgentSpec、工具、Open-Meteo 服务和业务观测逻辑 |

`legacy` 与 `langgraph` 共用 Compiler、AgentSpec、模型、工具和大部分执行适配逻辑，不维护两套天气业务。区别主要在于 LangGraph Runtime 支持 Resume，并在编译时绑定 SQLite Checkpointer。

## 项目结构

```text
langchain-agent/
├── main.py                         # CLI、交互模式、进度和应用服务接入
├── requirements.txt                # Python 依赖
├── .env.example                    # 脱敏配置模板
├── agent_platform/
│   ├── application.py              # Run 生命周期与执行策略
│   ├── checkpoints.py              # Checkpoint Provider、Saver 和查询仓储
│   ├── compiler.py                 # AgentSpec 编译器
│   ├── domain.py                   # 平台领域对象
│   ├── events.py                   # Runtime 事件与 EventSink
│   ├── executable_runtime.py       # 两种 Runtime 的共用执行逻辑
│   ├── langgraph_runtime.py        # LangGraph Resume Runtime
│   ├── legacy_runtime.py           # 兼容 Runtime
│   ├── runs.py                     # Run/Thread Repository
│   ├── runtime.py                  # Runtime 协议和注册表
│   ├── state.py                    # 平台 State 与 LangGraph Schema
│   ├── models/                     # ModelSpec 与 ModelProvider
│   ├── spec/                       # AgentSpec、PromptSpec 和 Validator
│   └── tools/                      # ToolSpec、ToolProvider 和 ToolRegistry
├── weather_agent/
│   ├── agent.py                    # 天气 Agent 编译入口
│   ├── agent_spec.py               # 天气 AgentSpec 和系统 Prompt
│   ├── config.py                   # 环境变量与运行预算
│   ├── observability.py            # 进度与 JSONL Trace
│   ├── tool_registry.py            # 天气 Tool 注册
│   ├── tools.py                    # Tool 输入/输出契约
│   └── weather_service.py          # Open-Meteo API 客户端
├── observability/traces/           # 本地 JSONL Trace
├── tests/                          # 单元、回归和集成测试
└── docs/                           # 架构、Tool、Eval 和升级 TODO
```

## 安装

需要 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 配置

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
OPENAI_BASE_URL=https://your-provider.example/v1

MODEL_TEMPERATURE=0
MODEL_TIMEOUT_SECONDS=30
MODEL_MAX_RETRIES=0
MODEL_MAX_COMPLETION_TOKENS=400
AGENT_MAX_TURNS=4
AGENT_RUN_TIMEOUT_SECONDS=0

AGENT_RUNTIME_TYPE=legacy
AGENT_CHECKPOINT_DB_PATH=agent_runs.sqlite3
```

- `OPENAI_BASE_URL` 用于 OpenAI-compatible 模型服务。
- 模型必须支持 Tool/Function Calling。
- Open-Meteo 不需要天气 API Key。
- `AGENT_RUNTIME_TYPE` 只允许 `legacy` 或 `langgraph`。
- `AGENT_CHECKPOINT_DB_PATH` 同时用于 SQLite Run 记录和 LangGraph Checkpoint；相对路径以程序启动目录为基准。
- `AGENT_RUN_TIMEOUT_SECONDS` 是**整次运行**的预算，不是单次模型调用预算。为 `0` 时自动取 `MODEL_TIMEOUT_SECONDS * (AGENT_MAX_TURNS + 1)`；显式设置时必须不小于 `MODEL_TIMEOUT_SECONDS`。
- `TRACE_CAPTURE_CONTENT` 默认 `false`：问题、回答与工具参数在 Trace 中替换为 `<redacted>`；凭证键（`api_key`、`authorization` 等）**始终**被替换。
- `TRACE_MAX_STRING_LENGTH` 控制单个字符串字段的最大长度，`TRACE_RETENTION_DAYS` 控制保留期（`0` 表示不清理）。
- `MODEL_INPUT_PRICE_PER_MILLION` / `MODEL_OUTPUT_PRICE_PER_MILLION` 用于 `--metrics` 的估算成本；未配置时显示"未配置价格"。
- 外部请求和 Agent 执行都有有限预算，不要配置无限重试。

启用持久化 LangGraph Runtime：

```dotenv
AGENT_RUNTIME_TYPE=langgraph
AGENT_CHECKPOINT_DB_PATH=agent_runs.sqlite3
```

## SQLite 持久化

Agent 模式会打开或创建配置指定的 SQLite 文件。默认路径为项目根目录的：

```text
agent_runs.sqlite3
```

数据库文件会被复用，不会在每次运行时整体覆盖。Run Repository 与 LangGraph Saver 在同一个文件内使用各自的表。当前 SQLite 方案用于本地和单实例运行验证，不应视为多副本企业生产存储；扩容前需根据并发与恢复目标评估 PostgreSQL Checkpointer 和 Run Repository。

### `agent_runs`

保存一次运行的账本：`run_id`、`session_id`、Agent/Runtime 版本、`pending/running/interrupted/completed/failed/cancelled` 状态、时间、最终结果和错误类型。同一个 `run_id` 的记录会随生命周期更新，新 `run_id` 会新增记录。

### LangGraph Checkpoint 表

仅 LangGraph Runtime 写入，由 `langgraph-checkpoint-sqlite` 官方 `SqliteSaver` 管理 `checkpoints` 和 `writes` 表，包含 Thread、Checkpoint、父检查点、State、Metadata 和待处理节点写入。逻辑主键是：

```text
(thread_id, checkpoint_ns, checkpoint_id)
```

新的 Checkpoint 追加到已有数据库。当前 CLI 每次 Agent 启动都会生成新的 `session_id`，并将其作为 `thread_id`，因此每次独立运行通常形成新的 Thread。

Checkpoint 由 LangGraph Saver 使用受限的 JsonPlus/MsgPack 配置序列化，不应直接编辑数据库内容。可通过 `SQLiteCheckpointRepository` 查询摘要，通过 `SQLiteCheckpointProvider` 创建 Saver 读取完整 LangGraph State。升级前生成的 `agent_checkpoints` 表使用 Python `pickle`；首次打开 Checkpoint Provider/Repository 时会将快照迁移到官方表，但保留旧表不做自动清理。升级后首次运行 Agent 前应备份数据库；迁移只适用于本项目自己生成且可信的数据库，禁止对不可信 SQLite 文件执行迁移。

## Resume 与边界

平台已经提供 `AgentApplicationService.resume()`。遇到真实 LangGraph `interrupt()` 时，运行结果和 Run 账本状态为 `interrupted`，中断数据位于结果的 `metadata["interrupts"]`；使用相同 `thread_id` 并在上下文中传入 `resume_value`，Runtime 会发送 `Command(resume=...)`。Resume 也遵循 Run 超时、取消前检查和有限重试。

当前仍有这些限制：

- CLI 尚未提供 `resume`、Run 查询或 Checkpoint 查询子命令。
- CLI 每次运行生成新的 `session_id`，不会自动续接上一次对话。
- 天气业务尚未加入真正的 LangGraph `interrupt()` 人工确认节点。
- 已提供按 Thread 显式删除 Checkpoint 的 Repository 操作；自动保留、定期清理和分支回退策略仍需完善。
- 运行中的模型或 HTTP 请求不能被当前线程取消机制强制终止。
- 没有正式的历史 Checkpoint rollback API；当前仅具备读取指定 Checkpoint 的底层能力。

平台层已具备真实中断、跨 Saver 实例恢复和待处理写入恢复能力；用户可直接使用的 CLI/API 操作流程仍属于后续工作。

## 可观测性

每次 CLI 调用都会生成独立 Trace：

```text
observability/traces/<run_id>.jsonl
```

JSONL 每行一个事件，记录 `run_id`、`session_id`、模型、阶段、事件、耗时和详细信息。当前事件覆盖 Run、Runtime、模型、Tool 和 Open-Meteo HTTP 请求。Trace 不记录 API Key，目录已通过 `.gitignore` 排除。

注意：Trace 和 SQLite 结果可能包含用户问题、模型回答和工具参数。部署或提交仓库前应按实际数据安全要求保护或清理这些本地文件。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py agent_platform weather_agent tests
python -m pip check
git diff --check
```

当前测试覆盖：

- Weather Tool 输入、输出、错误分类、预报归一化和部分失败。
- HTTP 超时、有限重试、重复 Tool Call 阻断和 Agent 最大轮数。
- ModelSpec、ModelProvider、ToolSpec、ToolRegistry 和 AgentSpec 校验。
- Compiler 对 Model、Tool、State Schema 和 Checkpoint 的绑定。
- `legacy` / `langgraph` Runtime、事件、流式运行和 Resume。
- SQLite Checkpoint 的待处理写入持久化、旧版快照迁移和按 Thread 删除。
- 真实 LangGraph `interrupt()`、关闭并重建 Saver、`Command(resume=...)` 和 Run 状态恢复。
- Run 超时、取消前检查和有限重试。
- SQLite Run Repository、Run/Thread 查询。
- SQLite Checkpoint 跨 Saver 实例读取。
- 真实 `CompiledStateGraph` 编译和执行。

## 架构原则

- 固定流程优先使用确定性 Workflow，需要动态工具选择时才使用 Agent。
- 天气事实只来自 Open-Meteo，不由模型记忆补全。
- LangChain/LangGraph 属于能力适配与执行层，平台契约不暴露其具体返回格式。
- Agent 目标、Prompt、Model、Tool 和 Runtime 使用版本化 Spec 描述。
- Compiler 是唯一的 Agent 组装入口，避免重复构建逻辑。
- Runtime 选择、Run 生命周期和持久化分别由清晰边界负责。
- 所有外部请求必须有超时，重试和 Agent 轮数必须有上限。
- Tool 仅暴露完成任务所需的最小权限。
- 不在缺少明确收益时引入长期 Memory、Vector DB、MCP、Sandbox 或 Multi-Agent。

## 后续计划

按 P0/P1/P2 优先级、具体验收条件和测试要求执行根目录 [UPGRADE_TODO.md](UPGRADE_TODO.md)。后续优先补齐 CLI 的 Thread 查询与 Resume 操作、取消语义和离线 Eval，再按真实部署与负载需求建设 API、授权和多实例存储。

详细设计与进度见：

- [变更日志](CHANGELOG.md)：本仓库已完成的变更事实、接口契约与迁移注意
- [架构说明](docs/ARCHITECTURE.md)
- [Tool 契约](docs/TOOLS.md)
- [评估计划](docs/EVALS.md)
- [升级 TODO](UPGRADE_TODO.md)
