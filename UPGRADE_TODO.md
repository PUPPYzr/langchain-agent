# Agent Platform 升级待办清单

本清单是项目升级计划的唯一维护入口。按优先级由上至下执行；每个功能阶段都需先明确验收条件，再实现并运行回归测试。计划面向当前天气 Agent 平台的实际边界，不以引入更多框架或组件为目标。

更新日期：2026-10-14

## 已完成的高优先级改进

- [x] 使用 LangGraph 官方 SQLite Saver 持久化 Checkpoint 和 `pending_writes`，避免仅保存快照导致中断恢复时丢失节点写入。
- [x] 将 Run 的 `interrupted` 状态纳入领域模型、同步运行、流式事件和 SQLite Run 账本。
- [x] 通过同一 `thread_id` 和 `Command(resume=...)` 恢复真实 `interrupt()`。
- [x] 为 `resume()` 复用现有 Run 超时、取消前检查和有限重试策略。
- [x] 将旧版 `agent_checkpoints` 快照一次性迁移到官方 Saver 表；迁移不删除旧表或 Run 记录。
- [x] 限制新 Checkpoint 的 MsgPack 反序列化类型；旧版 pickle 数据迁移要求数据库可信。
- [x] 提供按 Thread 显式删除 Checkpoint 的 Repository 操作。
- [x] 增加真实集成测试：并行节点写入、另一个节点中断、关闭并重建 Saver、恢复后验证已完成节点不会重复执行。
- [x] 修复 Trace/进度跨线程丢失：`_execute_with_policy` 执行前 `copy_context()`，使 `stage=model/tool/http` 事件与回调线程内进度重新可见。
- [x] 将整次运行预算与单次模型超时分离：新增 `AGENT_RUN_TIMEOUT_SECONDS`，未配置时按 `MODEL_TIMEOUT_SECONDS * (AGENT_MAX_TURNS + 1)` 推导。
- [x] Run 账本增加 `parent_run_id`，记录被恢复的 Run，并做向后兼容列迁移。
- [x] 工具调用策略下沉到执行层：Compiler 用 `make_guarded_tool` 包装 Tool，Runtime 绑定 `ToolCallGuard`；重复调用与调用上限返回结构化错误且不执行原 Tool，统计写入 Run metadata。原因：LangGraph `ToolNode` 会吞掉回调异常，回调层 raise 无法阻止执行。
- [x] 新增版本化离线 Eval Dataset 与确定性规则评估 CLI（`python -m evals`），含 7 个基线用例，退出码可作发布门禁。
- [x] 增加 HTTP 服务边界（`service/`，`python -m service`）：复用 `AgentApplicationService`，含认证、令牌桶限流、租户命名空间隔离、幂等键、请求校验、稳定错误码、SSE 流式、审计（先审计后响应）。零新增依赖（stdlib + pydantic）。

## P0：运行生命周期可操作

- [x] 为 CLI 增加 Thread/Run/Checkpoint 查询和显式 Resume 入口；保留现有 `python main.py <地点>` 和 Agent 调用方式。
- [x] 定义 Resume 请求契约：Thread 必须存在且处于可恢复状态；每次恢复创建新的 `run_id`，沿用同一个 `thread_id`，记录被恢复的 Run（`parent_run_id`）。
- [x] 增加针对完成、失败、重复 Resume、无效 Thread、无 `resume_value` 和连续多次 interrupt 的测试。
- [x] 明确运行取消语义：引入 `CancellationToken` 协作式契约，检查点覆盖执行前、每次工具调用前与流式事件间隙；超时与 Ctrl+C 都会请求取消并记为 `cancelled`，异常信息明确提示在途调用可能仍在排空。异步 Runtime 与进程隔离方案已在 `docs/ARCHITECTURE.md` 评估并给出不采纳理由。

验收：CLI 可查询并继续一个真实暂停的 Thread；旧调用不破坏；所有状态转换有测试；超时或取消不将仍在后台执行的任务误报成已停止。

## P1：Agent 行为质量门禁

- [x] 建立小型、版本化的离线 Eval Dataset，覆盖当前天气、预报、多地点比较、工具错误和部分失败。
- [x] 添加确定性规则评估：必需字段、工具选择、参数边界、事实来源、错误分类、最大工具调用数。
- [x] 增加真实多轮 Tool Loop 集成测试，验证工具调用、工具结果回传、最终回答和失败轨迹。
- [ ] 明确 `tool_results`、`errors`、`execution` 等平台 State 扩展字段的所有者与更新规则；只在业务/API 确实需要时添加自定义节点，避免重复保存 LangGraph 已维护的消息轨迹。
- [x] 记录基线结果；Prompt、Model、Tool 或 Runtime 变更必须对比回归结果，不以单一 LLM Judge 作为发布依据。

验收：核心样例有可重复结果；回归失败能阻止发布；真实失败案例可脱敏后加入数据集。

## P1：运行可观测性与数据保护

- [x] 统一 Run、Model、Tool、Retry、Interrupt、Resume 事件字段与错误分类（`agent_platform/telemetry.py` + `emit_event`）。Node 与 Checkpoint 事件仍由 LangGraph 内部产生，通过 `state.updated` 与 Checkpoint 查询仓储间接覆盖，未新增独立事件。
- [x] 为 Trace 增加脱敏策略、内容采集开关与保留期限；默认避免记录不必要的用户正文和敏感工具参数。
- [x] 增加结构化指标：成功率、失败阶段、延迟分位数、模型/工具调用数、Token 和估算成本。
- [x] 补齐异常路径 Trace，确保取消、超时、重试和中断恢复都关联相同的 Thread 与相应 Run。
- [ ] Run 账本仍保存回答正文（`result_json`）；是否对账本正文脱敏或加密需要单独决策。

验收：从 `run_id` 可追踪完整执行轨迹；Trace 不包含密钥；敏感字段行为有测试；指标可用于比较版本质量和成本。

## P1：对外服务边界

- [x] 在现有 `AgentApplicationService` 上增加 HTTP API，不在 API 层复制 Runtime、Compiler 或 Tool Registry 逻辑。
- [x] 提供创建 Run、查询 Run/Thread/Checkpoint、流式事件和 Resume 接口。
- [x] 定义请求校验、稳定错误响应、幂等键、请求超时和 API 版本。
- [x] 在开放服务前增加认证、授权、限流、租户隔离和审计；未完成这些边界前不暴露公网（非回环绑定在 `SERVICE_ALLOW_PUBLIC_EXPOSURE` 为假时拒绝启动）。

验收：API 契约测试通过；不同租户不能读写彼此 Thread；重复请求不会重复产生非幂等副作用。

## P2：生产存储与部署

- [ ] 按预期并发、部署拓扑和恢复目标评估 SQLite 的适用边界；SQLite 先定位为本地/单实例存储。
- [ ] 当需要多实例并发时，评估 LangGraph Postgres Checkpointer 与 Run Repository 的事务和迁移方案。
- [ ] 增加数据库备份/恢复演练、Checkpoint 保留策略、显式清理命令和存储容量监控。
- [ ] 只有出现排队、限流或横向扩容需求后，再评估任务队列和缓存，不预先引入 Redis/Celery。

验收：明确 RPO/RTO、并发目标和迁移回滚步骤；备份能够在隔离环境恢复；清理操作可预览并有审计。

## P2：安全与工具治理

- [ ] 将 Tool 权限校验从描述性元数据落实为可执行策略；区分只读、可逆写入和外部副作用。
- [ ] 对高风险副作用增加用户确认、幂等键、审计记录和重复执行保护。
- [ ] 验证错误、Trace、Checkpoint 与模型上下文不会泄漏密钥或不必要的个人数据。
- [ ] 在实际需要执行不可信代码时再设计 Sandbox；当前天气 Agent 不开放 Shell、任意网络或文件系统 Tool。

验收：越权和注入测试失败关闭；副作用有明确授权记录；密钥扫描与日志脱敏检查纳入 CI。

## 暂不纳入

- [ ] 不因“企业级”标签直接引入 Multi-Agent、长期 Memory、Vector DB、MCP、Redis、Celery 或 Kubernetes。
- [ ] 仅在 Eval、负载数据或业务需求证明收益后，单独立项并定义成本、风险与回滚验收条件。

## 每阶段统一检查

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m compileall -q main.py agent_platform weather_agent tests`
- [ ] `python -m pip check`
- [ ] `git diff --check`
- [ ] 更新 README 与架构说明，确保能力、入口和限制与代码一致。
