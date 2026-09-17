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
- Run 总超时、执行前取消检查和有限重试。
- SQLite Run 生命周期记录和 Run/Thread 查询。
- SQLite LangGraph Checkpoint 持久化和 Checkpoint 摘要查询。
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

## Agent 工具

| LangChain Tool | 平台 Tool ID | 用途 | 输入限制 |
| --- | --- | --- | --- |
| `get_current_weather` | `get-current-weather` v1 | 查询单个地点当前天气 | 地点长度 1 到 120 |
| `get_weather_forecast` | `get-weather-forecast` v1 | 查询未来每日预报 | 预报天数 1 到 7 |
| `compare_current_weather` | `compare-current-weather` v1 | 比较多个地点当前天气 | 地点数量 2 到 5，不允许重复 |

所有天气 Tool 都是只读操作。返回值使用 Pydantic Schema，错误会分类为 `invalid_input`、`not_found`、`timeout`、`rate_limit`、`retryable` 或 `service_error`。AgentSpec 只引用稳定的 Tool ID 和版本，Compiler 再通过 ToolRegistry 解析具体实现。

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

AGENT_RUNTIME_TYPE=legacy
AGENT_CHECKPOINT_DB_PATH=agent_runs.sqlite3
```

- `OPENAI_BASE_URL` 用于 OpenAI-compatible 模型服务。
- 模型必须支持 Tool/Function Calling。
- Open-Meteo 不需要天气 API Key。
- `AGENT_RUNTIME_TYPE` 只允许 `legacy` 或 `langgraph`。
- `AGENT_CHECKPOINT_DB_PATH` 同时用于 SQLite Run 记录和 LangGraph Checkpoint；相对路径以程序启动目录为基准。
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

数据库文件会被复用，不会在每次运行时整体覆盖。

### `agent_runs`

保存一次运行的账本：`run_id`、`session_id`、Agent/Runtime 版本、`pending/running/completed/failed/cancelled` 状态、时间、最终结果和错误类型。同一个 `run_id` 的记录会随生命周期更新，新 `run_id` 会新增记录。

### `agent_checkpoints`

仅 LangGraph Runtime 写入，保存 `thread_id`、`checkpoint_id`、命名空间、创建时间、父 Checkpoint、State 快照和 Metadata。主键是：

```text
(thread_id, checkpoint_ns, checkpoint_id)
```

新的 Checkpoint 通常追加到已有数据库；只有三个主键值完全相同时才替换同一行。当前 CLI 每次 Agent 启动都会生成新的 `session_id`，并将其作为 `thread_id`，因此每次独立运行通常形成新的 Thread。

Checkpoint Blob 使用 Python `pickle` 保存，不应被当作普通文本或跨语言数据格式直接编辑。可通过 `SQLiteCheckpointRepository` 查询摘要，通过 `SQLiteCheckpointProvider` 创建 Saver 读取完整 LangGraph State。

## Resume 与边界

平台已经提供 `AgentApplicationService.resume()`，LangGraph Runtime 可以按相同 `thread_id` 读取最新 Checkpoint；如果上下文包含 `resume_value`，Runtime 会发送 `Command(resume=...)`。

当前仍有这些限制：

- CLI 尚未提供 `resume`、Run 查询或 Checkpoint 查询子命令。
- CLI 每次运行生成新的 `session_id`，不会自动续接上一次对话。
- 天气业务尚未加入真正的 LangGraph `interrupt()` 人工确认节点。
- Checkpoint `pending_writes`、删除、保留和分支管理仍需完善。
- 运行中的模型或 HTTP 请求不能被当前线程取消机制强制终止。
- 没有正式的历史 Checkpoint rollback API；当前仅具备读取指定 Checkpoint 的底层能力。

因此，当前 Checkpoint 已能持久化和跨 Saver 实例读取，但完整的暂停、重启、人工确认和回退产品流程仍属于后续工作。

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

- 完整持久化 Checkpoint `pending_writes`，增加删除、保留和清理策略。
- 增加真实 `interrupt()` / `Command(resume=...)` 中断恢复集成测试和 CLI/API 入口。
- 使用自定义节点持续更新 `tool_results`、`errors` 和 `execution` 等业务 State。
- 完善真实 Tool trajectory、多轮 Tool Loop 和失败路径集成测试。
- 统一 Run、Node、Model、Tool、State、Retry、Checkpoint 和 Resume Trace。
- 建立离线 Eval Dataset、规则评估、轨迹评估和版本回归报告。
- 核心执行能力稳定后再增加 HTTP、SSE 或 WebSocket 接口。

详细设计与进度见：

- [架构说明](docs/ARCHITECTURE.md)
- [Tool 契约](docs/TOOLS.md)
- [评估计划](docs/EVALS.md)
- [升级 TODO](docs/UPGRADE_TODO.md)
