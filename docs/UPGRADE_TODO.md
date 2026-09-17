# Agent Platform Upgrade Todo

按阶段执行，每个阶段完成后必须通过编译、单元测试和回归测试。

## 已完成

- [x] 平台 Domain：AgentContext、AgentDefinition、AgentRunResult
- [x] ModelSpec / ModelProvider 适配边界
- [x] ToolSpec / ToolRegistry 版本化注册边界
- [x] AgentSpec / PromptSpec / ToolReference 及运行前校验
- [x] Agent 工具集合由 AgentSpec + ToolRegistry 动态解析
- [x] AgentDefinition 作为 AgentSpec 生成的运行时视图
- [x] AgentCompiler 统一绑定 Model、Prompt、Tool、State、Checkpoint
- [x] LegacyAgentRuntime 与 LangGraphAgentRuntime
- [x] RuntimeRegistry、Runtime capability、stream、resume
- [x] AgentState / ExecutionState 与 LangGraph state_schema
- [x] AgentEvent / EventSink，并接入 CLI JSONL Trace
- [x] RunRecord / RunRepository 生命周期模型
- [x] SQLite RunRepository 本地持久化
- [x] AgentApplicationService 编排运行、流式运行和恢复
- [x] Run 级总超时、取消前检查和有限重试
- [x] AGENT_RUNTIME_TYPE 配置
- [x] 真实 CompiledStateGraph 编译/执行集成测试
- [x] Compiler、Runtime、Streaming、Resume、Checkpoint、Trace、Retry 回归测试
- [x] 删除未使用函数、工具列表、旧空目录和重复升级提示词

## 部分完成

- [x] Checkpoint Provider 已支持 SQLite 持久化，并可跨 Saver 实例读取
- [ ] AgentState 已接入 schema，但业务扩展字段尚未由自定义节点持续更新
- [x] Run 持久化已支持 SQLite，并提供 Run / Thread 查询；Checkpoint 提供独立查询仓储
- [ ] 取消支持当前为执行前检查，尚未实现强制中断正在运行的外部调用

## 下一阶段

- [x] 增加持久化 Checkpoint Provider 和 Thread / Run / Checkpoint 查询
- [ ] 将 Thread / Run / Checkpoint 查询接入 HTTP/API 层
- [ ] 完善 Checkpoint pending writes 和删除/清理策略
- [ ] 增加真实 Tool trajectory、多轮 Tool Loop 和 LangGraph graph 集成测试
- [ ] 统一模型、工具、节点、状态、重试、Checkpoint 事件的 Trace 结构
- [ ] 增加离线 Eval Dataset、规则评估和版本回归报告
- [ ] 需要明确收益后再引入 MCP Registry、Sandbox 和 Multi-Agent

## 当前边界

- 默认仍使用 legacy，保持现有 CLI 行为兼容。
- langgraph Runtime 已可运行和恢复；默认通过 `AGENT_CHECKPOINT_DB_PATH` 使用 SQLite Checkpoint，测试可显式选择内存实现。
- 事件流尚未接入 HTTP、SSE 或 WebSocket。
- 当前天气业务不引入长期 Memory、Vector DB、MCP 或 Multi-Agent。
