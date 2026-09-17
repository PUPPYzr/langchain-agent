# LangChain Weather Agent

这是一个使用 LangChain 构建的命令行天气 Agent。用户输入地点后，Agent 会调用 Open-Meteo 查询地点和当前天气，再由模型生成中文摘要。

## 功能

- 交互式自然语言输入：运行 `python main.py` 后直接输入问题。
- 当前天气查询：温度、体感温度、湿度、降水、风况和观测时间。
- 未来 1 到 7 天天气预报：最高/最低温度、降水概率、天气状况、日出日落和最大风速。
- 多地点天气对比：支持比较 2 到 5 个地点，并保留部分失败结果。
- 双执行模式：
  - Agent 模式：模型选择 Tool，工具获取数据，模型生成摘要。
  - Workflow 模式：直接调用天气服务并本地格式化，速度更快且不依赖模型。
- 结构化 Tool 输入输出和错误分类。
- 模型超时、有限重试、最大 Token 和 Agent 轮数限制。
- 统一终端进度显示和按运行保存的 JSONL Trace。
- 平台自有 Agent 抽象和 Legacy Runtime 适配层，为后续 LangGraph Runtime 留出替换边界。
- 平台自有 ModelSpec、ModelProvider、ToolSpec 和 ToolRegistry，LangChain 仅作为适配实现。
- 版本化 AgentSpec、PromptSpec 和运行前配置校验。
- 单元测试、回归测试和架构/Tool/Eval 文档。

## 示例

### 交互模式

```powershell
python main.py
```

程序会提示：

```text
请输入你要询问的问题？
```

可以输入：

```text
合肥今天的天气怎么样？
北京未来三天天气怎么样？
比较合肥和上海今天的天气。
```

无参数交互模式会自动使用完整 Agent，由模型根据问题选择对应工具。

### 命令行模式

完整 Agent 模式：

```powershell
python main.py "北京未来三天天气怎么样" --agent
python main.py "比较合肥、上海和北京今天的天气" --agent
```

快速 Workflow 模式：

```powershell
python main.py 合肥
```

快速模式适合固定的当前天气查询，不经过模型判断和总结，通常延迟更低。

## Agent 工具

| Tool | 用途 | 输入限制 |
| --- | --- | --- |
| `get_current_weather` | 查询单个地点当前天气 | 地点长度 1 到 120 |
| `get_weather_forecast` | 查询未来每日预报 | 预报天数 1 到 7 |
| `compare_current_weather` | 比较多个地点当前天气 | 地点数量 2 到 5，不允许重复 |

所有 Tool 都是只读操作，不执行文件修改、命令执行、发布或其他外部副作用。Tool 返回统一的结构化成功/失败结果，支持 `invalid_input`、`not_found`、`timeout`、`rate_limit`、`retryable` 和 `service_error` 等错误类型。

## 项目结构

```text
langchain-agent/
├── main.py                         # CLI 入口、交互模式和进度显示
├── requirements.txt                # Python 依赖
├── .env.example                    # 脱敏配置模板
├── agent_platform/                 # 平台领域抽象与 Runtime 适配器
│   ├── domain.py                   # AgentDefinition、AgentContext、AgentRunResult
│   ├── runtime.py                  # AgentRuntime 接口
│   ├── legacy_runtime.py            # 现有 LangChain Agent 兼容运行时
│   ├── models/                     # ModelSpec、ModelProvider 和 LangChain Adapter
│   ├── spec/                       # AgentSpec、PromptSpec 和 Validator
│   └── tools/                      # ToolSpec、ToolProvider 和 ToolRegistry
├── weather_agent/
│   ├── agent.py                    # 模型、系统提示词和 Agent 组装
│   ├── config.py                   # 环境变量和运行预算
│   ├── tools.py                    # Tool 输入/输出契约
│   ├── weather_service.py          # Open-Meteo API 客户端
│   └── observability.py            # 进度显示和 JSONL Trace
├── observability/traces/           # 本地运行 Trace，不提交 Git
├── tests/                          # 回归测试
├── docs/
│   ├── ARCHITECTURE.md             # Agent 架构与契约
│   ├── TOOLS.md                    # Tool 设计说明
│   └── EVALS.md                    # Eval 场景与指标
└── AGENT_DEVELOPMENT_*.md          # Agent 开发约束、指令和提示词
```

## 安装

Python 需要 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 配置

复制配置模板：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
OPENAI_BASE_URL=https://your-provider.example/v1
```

`OPENAI_BASE_URL` 用于配置 OpenAI 兼容服务。所选模型必须支持 Function Calling / Tool Calling，否则普通聊天可用但 Agent 可能无法正常选择工具。

天气数据由 Open-Meteo 提供，不需要天气 API Key。

### Agent 运行预算

```dotenv
MODEL_TEMPERATURE=0
MODEL_TIMEOUT_SECONDS=30
MODEL_MAX_RETRIES=0
MODEL_MAX_COMPLETION_TOKENS=400
AGENT_MAX_TURNS=4
```

默认不自动重试模型请求，避免第三方服务异常时重复等待。模型服务稳定后可以设置有限的 `MODEL_MAX_RETRIES`，但不要配置为无限重试。

## 可观测性

终端只显示统一进度，例如：

```text
[进度] 正在调用工具：get_current_weather | 已用时 6.3 秒
[进度] 工具调用完成，正在生成答案 | 已用时 10.2 秒
[进度] 完成 | 已用时 18.4 秒
```

每次运行会生成一个独立 Trace：

```text
observability/traces/<run_id>.jsonl
```

Trace 包含：

- `run_id` 和 `session_id`
- 模型名称、输入/输出 Token 和模型耗时
- Tool 名称、参数和耗时
- Open-Meteo HTTP 请求耗时
- 成功、失败和错误类型

Trace 不记录 API Key，并已通过 `.gitignore` 排除，不会提交到 GitHub。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py weather_agent tests
python -m pip check
```

当前测试覆盖：

- 当前天气 Tool 错误处理
- ModelSpec 和 ModelProvider 参数映射
- ToolSpec 注册、解析和重复保护
- Legacy Runtime 与 Model/Tool Registry 兼容
- 未来预报数据归一化
- 预报天数校验
- 多地点部分失败
- HTTP 超时和有限重试
- 重复 Tool Call 阻断
- Agent 轮数配置上限
- AgentSpec、PromptSpec 和 ToolReference 版本校验
- AgentSpec 的 Runtime、Tool Registry 和模型超时校验

## 架构原则

- 固定流程优先使用确定性 Workflow。
- 只有需要动态工具选择时才使用 Single Agent。
- 不引入没有证据证明必要的 Multi-Agent、长期记忆或复杂规划。
- Tool 只暴露完成任务所需的最小能力。
- 外部事实必须来自 Open-Meteo，不由模型记忆补全。
- 模型、Tool、HTTP 和错误阶段必须可追踪。
- 所有外部请求必须有超时，重试必须有上限。
- LangChain 属于能力适配层，Agent Runtime 由平台抽象定义。
- Agent 的目标、Prompt、Model、Tool 和 Runtime 通过版本化 Spec 描述。
- 非法 Agent 配置必须在运行前校验失败。
- 当前使用 Legacy Runtime，LangGraph 仅在后续复杂多步骤任务中按需引入。

更多设计细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、[docs/TOOLS.md](docs/TOOLS.md) 和 [docs/EVALS.md](docs/EVALS.md)。

## 后续方向

- 地点消歧和候选地点选择。
- 空气质量查询。
- 连续对话和有限会话 State。
- 穿衣、出行和户外活动建议。
- 缓存、指标收集和更完整的 Eval 数据集。
