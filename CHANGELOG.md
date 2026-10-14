# 变更日志 · Agent 平台化 + Web 对话界面

> 记录日期：2026-10-14
> 维护入口：待办与验收标准见 [`UPGRADE_TODO.md`](UPGRADE_TODO.md)；本文件记录**已完成的变更事实**。
> 推送前请先阅读文末 [第 12 节：推送前检查清单](#12-推送前检查清单必读)。

---

## 1. 一句话总览

项目从「CLI 单点天气 Agent」升级为：

```text
平台化 Agent 执行层（Spec / Model / Tool / Compiler / Runtime / State / Run / Checkpoint / Event）
  + 协作式取消与重试策略
  + 统一遥测（事件分类 · 脱敏 · 保留期 · 结构化指标）
  + 离线确定性评估
  + HTTP 服务边界（认证 / 限流 / 租户隔离 / 幂等 / 审计）
  + 浏览器可视化对话界面（真实执行过程可见）
```

**原有能力完整保留**：`python main.py 合肥`、`python main.py --agent "问题"` 行为不变。

---

## 2. 架构变化

```text
改造前
  CLI ──> LangChain Agent ──> 工具 ──> Open-Meteo

改造后
  浏览器 ──┐
           ├─> service/（认证 · 限流 · 校验 · 审计 · 会话）
  CLI ─────┘        │
                    ▼
            AgentService（会话 + 真实事件投影）
                    │
                    ▼
            AgentApplicationService（生命周期 · 超时 · 取消 · 重试）
                    │
                    ▼
            RuntimeRegistry ──> LangGraph Runtime ──> Tool 守卫 ──> Open-Meteo
                    │
                    ▼
       Run 账本(SQLite) · Checkpoint(SQLite) · Trace(JSONL) · 审计(JSONL)
```

**核心原则未变**：Agent 只有一套实现。CLI 与 Web 共用 `AgentApplicationService`，HTTP 层不复制 Runtime / Compiler / Tool Registry 逻辑。

---

## 3. 能力清单（已完成）

### 3.1 Agent 运行时与生命周期

| 能力 | 说明 |
| --- | --- |
| 官方 Checkpoint | 改用 LangGraph 官方 `SqliteSaver`（`langgraph-checkpoint-sqlite`），完整保留 `pending_writes`，支持真正的 `interrupt()` / `Command(resume=...)` |
| 旧快照迁移 | 自建 `agent_checkpoints` 快照一次性迁移到官方表，**不删除旧表与 Run 记录**，迁移幂等（`agent_checkpoint_migrations`） |
| 反序列化收紧 | 新 Checkpoint 使用 `JsonPlusSerializer(allowed_msgpack_modules=None)`（遗留 pickle 路径仅用于一次性迁移，要求数据库可信） |
| `interrupted` 状态 | 纳入领域模型、同步运行、流式事件与 SQLite 账本 |
| Resume 契约 | Thread 须有 Checkpoint；`completed` 拒绝重复恢复；每次恢复新 `run_id`、沿用同 `thread_id`、记录 `parent_run_id`；无 `resume_value` 时以 `None` 恢复 |
| 整次运行预算 | 新增 `AGENT_RUN_TIMEOUT_SECONDS`，与单次模型超时分离 |
| 取消语义 | 协作式取消令牌，检查点覆盖「执行前 / 每次工具调用前 / 流式事件之间」 |

### 3.2 工具调用治理

- Compiler 用 `make_guarded_tool` 包装每个 Tool，Runtime 为每次运行绑定 `ToolCallGuard`。
- **重复调用**：返回结构化错误 `error_type=duplicate_tool_call`，**不再执行原 Tool**。
- **调用上限**：超出 `tool_call_policy.max_tool_calls` 返回 `tool_call_budget_exhausted`。
- 统计写入 `AgentRunResult.metadata.tool_call_policy`（`tool_call_count` / `blocked_duplicates` / `blocked_over_budget` / `blocked_cancelled`）。
- **重要结论**：原先在回调层 `raise` 阻止重复调用**是无效的**——LangGraph `ToolNode` 会捕获工具及其回调异常并转为 ToolMessage。可靠控制点必须在 Tool 本身。

### 3.3 取消语义（含诚实边界）

- `CancellationToken` + `cancellation_scope`：CLI 的 Ctrl+C、服务端取消、超时都会请求取消，Run 记为 `cancelled` / `failed`，**绝不记为 `completed`**。
- **诚实边界**：同步阻塞中的模型/HTTP 调用**无法被中断**，其返回值被丢弃；超时异常信息明确提示"在途调用可能仍在排空"。异步 Runtime（`await` 点可取消）与进程隔离方案已评估，因无证据证明收益而**未采纳**。

### 3.4 可观测性与数据保护

- 统一错误分类 `classify_error()`：`cancelled` / `run_timeout` / `model_timeout` / `rate_limit` / `authentication_error` / `agent_turn_limit` / `resume_not_allowed` / `agent_error`，同时写入 Run 账本 `error_type` 与 Trace 顶层 `error_category`。
- 统一事件发布 `emit_event()`，新增 `run.retry_scheduled` / `run.cancelled` / `run.timeout`，异常路径与正常路径**关联同一 `run_id` / `session_id`**。
- Trace 脱敏：凭证键**始终**替换为 `<redacted>`；用户内容（问题/回答/工具参数）默认替换，需 `TRACE_CAPTURE_CONTENT=true` 才保留；长字符串按上限截断；`purge_expired_traces` 按保留期清理。
- 结构化指标：`--metrics` 输出成功率、失败分类、延迟 p50/p95/max、模型/工具/HTTP 调用数、输入输出 Token、估算成本（需配置单价）。
- 修复了一个真实缺陷：Trace/进度因 `ContextVar` 不跨线程而**静默丢失**，现由执行前 `copy_context()` 传播。

### 3.5 离线确定性评估

- `evals/` 提供版本化数据集与规则评估，**不依赖 LLM Judge**、不需要模型密钥。
- 规则覆盖：工具选择、禁止工具、调用上限、重复调用、参数边界、错误分类、必需字段、失败/部分失败披露、事实来源（无成功数据时不得给出具体数值）。
- `python -m evals ...` 退出码 0/1/2，可直接作为发布门禁。

### 3.6 HTTP 服务边界

- 端点：创建/查询 Run、流式事件、Thread、Checkpoint、Resume、**取消**、会话、聊天。
- 认证：`Authorization: Bearer <key>` 或 `X-API-Key`（健康检查除外）。
- **租户隔离**：Thread 命名空间为 `<tenant_id>:<session_id>`，跨租户一律 `404`（不泄露资源是否存在）。
- 限流（令牌桶）、幂等（`Idempotency-Key`，1 小时内复用同一 `run_id`）、请求体积/媒体类型校验、稳定错误码。
- 审计：每个请求写入 JSONL，**审计先于响应写出**（避免"有响应的请求漏审"）。
- 启动安全门禁：非回环绑定在 `SERVICE_ALLOW_PUBLIC_EXPOSURE` 为假时**拒绝启动**。

### 3.7 Web 可视化对话界面

- 左侧会话栏（新建/切换/删除/收起）、中间消息区（Markdown、代码高亮与复制、时间戳）、底部输入区（Enter 发送、Shift+Enter 换行、停止生成）。
- **执行过程时间线**：从真实 LangGraph 状态派生工具名、参数摘要、执行状态与结果摘要，可折叠；**不伪造**工具调用。
- 会话持久化在 SQLite，重启后仍在；多轮上下文由 LangGraph Checkpointer 承担（`/v1/health` 返回 `context_mode`，`stateless` 时界面显式提示）。
- 免构建 ES 模块前端（`web/`），由同一个 Python 进程同端口托管：**无 npm、无 Vite、无 CORS 配置**，一条命令启动。

### 3.8 命令行入口（保留并扩展）

```powershell
python main.py 合肥                                   # 确定性 Workflow（不变）
python main.py --agent "北京未来三天天气"              # Agent（不变）
python main.py --list-runs [--session S] [--status T] [--limit N]
python main.py --list-threads | --list-checkpoints <thread_id>
python main.py --resume <thread_id> [--resume-value "approve"]
python main.py --metrics [--json]
python main.py --purge-traces
python -m service                                    # HTTP + Web UI
python -m evals --dataset ... --observations ...
```

---

## 4. 新增文件

**平台层（`agent_platform/`）**

| 文件 | 作用 |
| --- | --- |
| `cancellation.py` | 协作式取消契约（`CancellationToken` / `cancellation_scope` / `resolve_cancellation_token`） |
| `guardrails.py` | 工具调用策略（`ToolCallGuard` / `active_tool_guard` / `make_guarded_tool`） |
| `telemetry.py` | 阶段与错误类别常量、`classify_error`、`TracingPolicy`、`redact`、`purge_expired_traces` |
| `metrics.py` | `RunMetrics` / `TraceMetrics` / `Pricing` / `summarize_runs` / `summarize_trace_directory` |

**评估（`evals/`）**：`__init__.py`、`__main__.py`、`rules.py`、`datasets/weather_v1.jsonl`、`datasets/weather_v1_observations.jsonl`

**服务层（`service/`）**：`__init__.py`、`__main__.py`、`config.py`、`security.py`、`api.py`、`app.py`、`sessions.py`、`chat.py`

**前端（`web/`）**：`index.html`、`styles.css`、`api.js`、`markdown.js`、`app.js`

**测试（`tests/`，新增 8 个文件）**：`test_cancellation.py`、`test_chat_api.py`、`test_cli.py`、`test_evals.py`、`test_metrics.py`、`test_service_api.py`、`test_telemetry.py`、`test_tool_loop.py`

**文档**：根目录 `UPGRADE_TODO.md`（唯一维护入口）、`CHANGELOG.md`（本文件）

---

## 5. 修改文件

| 文件 | 改动要点 |
| --- | --- |
| `agent_platform/application.py` | `copy_context` 传播、取消令牌、重试/取消/超时事件、`verify_resumable`、`delete_checkpoints`、错误分类落账本 |
| `agent_platform/runs.py` | 新增 `parent_run_id` 字段与向后兼容列迁移（`_ensure_column`） |
| `agent_platform/domain.py` | `RunStatus` 增加 `interrupted`、新增 `RUN_STATUSES` 与 `ResumeNotAllowedError` |
| `agent_platform/compiler.py` | 编译时用 `make_guarded_tool` 包装每个 Tool |
| `agent_platform/executable_runtime.py` | 守卫调用上下文、守卫统计合并进结果、`run.interrupted` 事件 |
| `agent_platform/langgraph_runtime.py` | `resume` 复用超时/取消/重试策略 |
| `agent_platform/checkpoints.py` | 切换到官方 `SqliteSaver`、一次性旧快照迁移、按 Thread 删除 |
| `agent_platform/events.py` | 新增 `emit_event` 统一事件发布 |
| `agent_platform/__init__.py` | 导出新增能力 |
| `weather_agent/config.py` | 新增整次运行预算与 `checkpoint_db_path_from_environment()` |
| `weather_agent/agent_spec.py` | 使用整次运行预算作为 Agent 超时 |
| `weather_agent/observability.py` | `TraceRecorder` 支持脱敏策略、顶层 `error_category`/`attempt`、关闭后 no-op |
| `main.py` | 管理命令（Run/Thread/Checkpoint/Resume/Metrics/Purge）、Ctrl+C 取消、错误分类下沉到平台 |
| `requirements.txt` | 新增 `langgraph-checkpoint-sqlite>=3.1.2,<4.0` |
| `README.md` / `docs/ARCHITECTURE.md` / `docs/UPGRADE_TODO.md` / `.env.example` | 文档与配置同步 |

---

## 6. 新增配置项

```dotenv
# 整次运行预算（0 = 自动：MODEL_TIMEOUT_SECONDS * (AGENT_MAX_TURNS + 1)）
AGENT_RUN_TIMEOUT_SECONDS=0

# Trace 策略：内容默认脱敏，需显式开启才记录问题/回答/工具参数
TRACE_CAPTURE_CONTENT=false
TRACE_MAX_STRING_LENGTH=200
TRACE_RETENTION_DAYS=7

# 成本估算（未配置时 --metrics 显示"未配置价格"）
MODEL_INPUT_PRICE_PER_MILLION=0
MODEL_OUTPUT_PRICE_PER_MILLION=0

# HTTP 服务边界
SERVICE_API_KEYS=team-a:replace-with-a-16-char-key   # 格式 tenant_id:api_key，密钥至少 16 位
SERVICE_HOST=127.0.0.1
SERVICE_PORT=8080
SERVICE_RATE_LIMIT_PER_MINUTE=60
SERVICE_MAX_BODY_BYTES=20000
SERVICE_REQUEST_TIMEOUT_SECONDS=120
SERVICE_AUDIT_PATH=observability/audit/service.jsonl
SERVICE_SESSION_DB_PATH=agent_runs.sqlite3
SERVICE_WEB_DIR=web
SERVICE_ALLOW_PUBLIC_EXPOSURE=false                  # 非回环绑定必须显式置 true
```

**多轮上下文要求**：`AGENT_RUNTIME_TYPE=langgraph`（否则 `/v1/health` 返回 `context_mode=stateless`，界面会显式提示）。

---

## 7. HTTP 接口契约（前缀 `/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 存活探针，返回 `api_version` 与 `context_mode`，无需认证 |
| POST | `/runs` | 创建并执行一次 Run；`Accept: text/event-stream` 时返回 SSE |
| GET | `/runs` | 列出 Run（`session_id` / `status` / `limit`） |
| GET | `/runs/{run_id}` | 查询单个 Run |
| POST | `/runs/{run_id}/cancel` | 请求取消正在流式的 Run |
| GET | `/threads` | 列出 Thread |
| GET / DELETE | `/threads/{thread_id}/checkpoints` | 列出 / 删除 Checkpoint |
| POST | `/threads/{thread_id}/resume` | 恢复暂停的 Run |
| GET / POST | `/sessions` | 列出 / 新建会话 |
| GET | `/sessions/{session_id}/messages` | 会话历史 |
| DELETE | `/sessions/{session_id}` | 删除会话及其记录 |
| POST | `/chat` | 聊天（SSE，含真实执行事件） |
| GET | `/` 及静态资源 | Web 界面（无需认证，越权路径返回 404） |

**SSE 帧契约（统一）**：所有帧都带 `type` 字段

```json
{"type": "stream.opened", "run_id": "..."}
{"type": "started", "session_id": "...", "run_id": "..."}
{"type": "status", "phase": "working"}
{"type": "tool_call", "id": "...", "name": "get_current_weather", "arguments": {"location": "合肥"}}
{"type": "tool_result", "id": "...", "name": "get_current_weather", "ok": true, "summary": "...", "error_type": null}
{"type": "answer", "content": "合肥当前天气：晴朗，20℃。"}
{"type": "error", "code": "run_timeout", "message": "运行超时。"}
{"type": "done", "session_id": "...", "run_id": "...", "saved": true}
{"type": "stream.closed", "run_id": "..."}
```

**错误响应包封**：`{"error": {"code", "message", "request_id", "run_id"}}`

稳定错误码：`unauthorized` `forbidden` `invalid_request` `not_found` `method_not_allowed` `conflict` `run_cancelled` `payload_too_large` `unsupported_media_type` `rate_limited` `run_failed` `run_timeout` `internal_error`

---

## 8. 数据存储与兼容性

默认全部落在同一个 SQLite 文件（`AGENT_CHECKPOINT_DB_PATH`，默认 `agent_runs.sqlite3`）：

| 表 | 变化 |
| --- | --- |
| `agent_runs` | 新增列 `parent_run_id`（**启动时自动 `ALTER TABLE` 加列**，无需手工迁移）；`status` 新增取值 `interrupted`；`error_type` 现为统一错误类别 |
| `agent_checkpoints` | **旧自建表保留**（迁移后仍可回查），新数据写入官方 Saver 表 |
| `agent_checkpoint_migrations` | 新增，记录迁移完成状态（幂等） |
| LangGraph 官方表 | 由 `langgraph-checkpoint-sqlite` 在首次使用时创建 |
| `chat_sessions` / `chat_messages` | 新增，Web 会话语义记录（重启后保留） |

**Checkpoint 兼容性**：旧快照迁移路径仍会读取历史 `pickle` blob，因此要求数据库可信；新数据一律使用受限的反序列化类型。

---

## 9. 行为变更与迁移注意（重要）

| 变更 | 影响与处理 |
| --- | --- |
| Run 账本 `error_type` 改为统一类别 | 历史行**不会自动迁移**，统计时会同时出现旧类名与新类别 |
| 重复工具调用不再让运行失败 | 改为返回结构化错误并让模型继续作答；如需失败信号请读 `metadata.tool_call_policy.blocked_duplicates` |
| Trace 默认脱敏 | 问题/回答/工具参数显示为 `<redacted>`；人工排查需显式 `TRACE_CAPTURE_CONTENT=true` |
| 整次运行超时语义变化 | 原本等于单次模型超时，现为整次运行预算（默认 `30 × (4+1) = 150s`） |
| Ctrl+C 时 Run 记为 `cancelled` | 此前会停留在 `running` |
| SSE 帧统一为 `type` 字段 | 此前混用 `event_type`；旧客户端需按新契约调整 |
| Checkpoint 存储结构变化 | 首次启动会执行一次性迁移；旧表不删除 |
| CLI 位置参数行为 | **未变**（`python main.py 合肥`、`--agent` 均保持原语义） |

---

## 10. 测试与验证结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 全量测试 | `python -m unittest discover -s tests -v` | **159 tests OK**（15 个测试文件） |
| 编译 | `python -m compileall -q main.py agent_platform weather_agent evals service tests` | 0 |
| 依赖一致性 | `python -m pip check` | No broken requirements |
| 前端语法 | `node --check`（ES 模块模式） | `api.js` / `app.js` / `markdown.js` 全部 OK |
| 服务实际启动 | `python -m service --port 8099` | `/` → 200；`/styles.css` → 200；`/v1/health` → 200 且 `context_mode=checkpointed`；`/v1/sessions` 无密钥 → 401 |
| CLI 未破坏 | `python main.py --list-runs --limit 1` | 正常返回真实 Run |
| 评估门禁 | `python -m evals --dataset ... --observations ...` | 7/7 passed |

覆盖范围包括：真实 `CompiledStateGraph` 编译与执行、真实多轮 Tool Loop、并行节点写入与跨进程 Resume、`pending_writes` 保留、已完成节点不重复执行、取消/超时/重试/中断恢复、租户隔离、幂等、限流、审计先于响应、keep-alive 后仍能正确处理、聊天事件序列与工具真实执行、静态资源越权路径拒绝。

---

## 11. 已知边界与未完成项

- **未解决**：`ToolSpec.timeout/retry/permission` 仍未被强制执行消费；`compare_current_weather` 仍串行执行且无连接池；`SYSTEM_PROMPT` 仍硬编码；Node 与 Checkpoint 无独立 Trace 事件；Run 账本 `result_json` 仍保存回答正文。
- **多实例限制**：幂等表与限流桶为**进程内状态**；服务与 CLI 共用同一个 SQLite 文件，未做并发压测。多实例部署需先完成 P2（Postgres Checkpointer、外部状态存储）。
- **流式诚实边界**：后端流式推送的是**执行事件**，Agent 当前不产生 token 级增量，最终回答在 `run.completed` 一次性到达——界面**没有**用逐字动画伪装流式。
- **未验证**：真实模型端到端问答（需有效模型密钥）、浏览器视觉与移动端布局实测（无浏览器自动化工具）、Markdown 的全部边界（渲染器覆盖 Agent 实际输出子集）。
- **接口文档**：未提供 OpenAPI/Schema 导出，契约以本文件与 README 为准。

---

## 12. 推送前检查清单（必读）

> 结论：**有 2 项必须先处理**，否则会把本地运行数据推到云端仓库。

### ⚠️ 必须处理

**1. `agent_runs.sqlite3` 已被 git 跟踪，且内容已变更 —— 会随推送上传**

该文件包含历史用户问题、模型回答与会话记录（约 236 KB）。

```powershell
git rm --cached agent_runs.sqlite3
```

**2. `observability/` 仅部分被忽略 —— 审计日志会被加入**

`.gitignore` 目前只有 `observability/traces/*.jsonl`，因此 `observability/audit/service.jsonl` 会被 `git add -A` 收录。

建议在 `.gitignore` 追加：

```gitignore
# 本地运行产物：数据库、Trace 与审计日志
*.sqlite3
observability/
```

然后：

```powershell
git add .gitignore
git rm -r --cached observability 2>$null
```

### ✅ 已确认安全

- `.env` 已被 `.gitignore` 忽略，**模型密钥不会上传**。
- `__pycache__/`、`*.py[cod]`、`.venv/` 已被忽略。
- 前端无 `node_modules`（免构建方案，无依赖目录）。

### 建议确认

- `.vscode/` 是否适合随仓库上传（如含个人调试配置可一并忽略）。
- 提交信息建议体现范围，例如：
  `feat: 平台化 Agent + 可观测性 + HTTP 服务边界 + Web 对话界面`

### 推送前最后自检

```powershell
python -m compileall -q main.py agent_platform weather_agent evals service tests
python -m unittest discover -s tests
python -m pip check
git --no-pager diff --check
git --no-pager status --porcelain   # 确认没有 .env / *.sqlite3 / observability/ 进入待提交列表
```
