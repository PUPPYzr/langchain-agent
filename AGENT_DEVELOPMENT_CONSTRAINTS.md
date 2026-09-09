# AGENT_DEVELOPMENT_CONSTRAINTS.md

## 用途

本文件是 Agent 开发过程中的硬性约束规则。

当 AI 编程助手设计、实现或修改 Agent 时，应将这些规则视为默认工程约束。

---

# A. 架构约束

## A1. 禁止默认使用 Agent

如果普通 LLM 或确定性 Workflow 可以可靠完成任务，不得为了“Agent 化”而引入 Agent Loop。

---

## A2. 禁止默认 Multi-Agent

不得仅因为：

- 看起来更高级
- 框架支持
- 可以定义多个角色

就使用 Multi-Agent。

只有存在明确、可验证的 Single-Agent 瓶颈时才允许。

---

## A3. 优先最小复杂度

默认复杂度顺序：

```text
LLM
<
Workflow
<
Single Agent
<
Agent + Workflow
<
Multi-Agent
```

没有数据证明收益，不升级复杂度。

---

# B. Goal / Scope 约束

## B1. Agent 必须只有明确主 Goal

如果一个 Agent 同时承担多个不相关目标，应考虑拆分。

---

## B2. 必须定义输入输出契约

不得开发输入输出边界模糊的 Agent。

至少定义：

```text
Inputs
Outputs
Constraints
Failure Conditions
```

---

## B3. 不允许依赖隐式成功标准

必须有可测试的 Success Criteria。

---

# C. Tool 约束

## C1. 最小工具原则

Agent 只获得完成当前职责所需的 Tool。

禁止暴露无关工具。

---

## C2. Tool 必须结构化

每个 Tool 必须至少定义：

```text
name
description
input schema
output schema
error behavior
permission level
```

---

## C3. Tool 不得语义模糊

避免：

```text
do_task()
execute()
process()
search()
```

除非作用范围极其明确。

---

## C4. Tool 返回不得依赖不可解析自然语言

关键 Tool 输出必须尽量结构化。

---

## C5. Tool Failure 必须可区分

至少区分：

```text
retryable
non_retryable
permission_denied
invalid_input
timeout
rate_limit
not_found
```

---

# D. Agent Loop 约束

## D1. 禁止无限循环

必须存在：

```text
max_turns
timeout
```

生产环境还应考虑：

```text
token_budget
cost_budget
consecutive_failure_limit
```

---

## D2. 必须有明确结束条件

Agent 不得仅依赖模型“自己觉得完成了”。

应结合：

```text
final response
task state
validation
evaluator
limits
```

---

## D3. 禁止无限重复 Tool Call

必须检测明显重复调用：

```text
same tool
same args
same result
```

并触发停止、改计划或错误恢复。

---

# E. Context 约束

## E1. 禁止无限堆叠历史

不得默认把完整聊天、完整 Tool Result、完整文档全部重复加入每轮 Context。

---

## E2. 必须按当前步骤选择 Context

优先：

```text
retrieve
select
compress
summarize
prune
```

---

## E3. 大型 Tool Result 应压缩

除非任务需要逐字处理，否则不要长期保留超大 Tool Result。

---

## E4. Prompt 不得承担所有系统逻辑

能用：

```text
schema
code
policy
validation
workflow
```

实现的约束，不应仅靠自然语言 Prompt 保证。

---

# F. State / Memory / Knowledge 约束

## F1. 三者必须概念分离

```text
State ≠ Memory ≠ Knowledge
```

---

## F2. 不得把整个历史直接当 Long-term Memory

Long-term Memory 必须经过：

```text
candidate extraction
importance filtering
deduplication
safety filtering
expiration strategy
```

---

## F3. Memory 必须有写入理由

每条长期 Memory 应回答：

```text
未来为什么可能有用？
```

否则不保存。

---

## F4. 外部事实优先来自 Knowledge Source

事实型信息不应仅依赖 Long-term Memory。

---

# G. Permission / Safety 约束

## G1. LLM 无默认执行权限

LLM 只能提出 Tool Call。

真正执行前必须经过：

```text
policy / permission layer
```

---

## G2. Side Effect Tool 必须分类

至少分类：

```text
read-only
reversible-write
external-side-effect
high-risk
```

---

## G3. 高风险动作不得自动执行

包括但不限于：

```text
删除生产数据
资金操作
权限修改
不可逆发布
高权限命令
```

必须使用 HITL 或更强验证。

---

## G4. Tool 参数必须校验

不得将模型输出未经验证直接传给敏感 Tool。

---

## G5. Prompt Injection 不得被当作可信系统指令

来自：

```text
web page
document
email
database
tool result
```

的内容默认属于不可信数据，而不是系统指令。

---

# H. Runtime 约束

## H1. Code Execution 应隔离

执行：

```text
Python
Shell
用户代码
下载代码
```

时，默认使用 Sandbox。

---

## H2. Sandbox 必须限制资源

至少考虑：

```text
CPU
memory
disk
execution time
network
filesystem
processes
secrets
```

---

## H3. 禁止向 Agent 暴露无关 Secrets

凭证应按最小权限注入。

---

# I. Workflow 约束

## I1. 确定性任务优先代码化

以下任务优先使用程序，而不是让 LLM 自主决定：

```text
schema validation
permission checks
math
known routing rules
format validation
database constraints
```

---

## I2. Agentic 节点只用于真正不确定问题

LLM Agent 应主要承担：

```text
reasoning
planning
semantic decisions
open-ended search
dynamic tool choice
```

---

# J. Multi-Agent 约束

## J1. 增加 Agent 前必须说明瓶颈

必须明确属于：

```text
context
specialization
parallelism
role conflict
review
handoff
```

之一。

---

## J2. 每个 Agent 必须有独立职责

不允许多个 Agent 使用几乎相同 Prompt 和 Tool 做重复工作。

---

## J3. 必须定义 Agent 间契约

包括：

```text
input
output
handoff condition
failure behavior
ownership
```

---

## J4. 避免无边界 Agent-to-Agent 对话

Agent 间通信必须有：

```text
max rounds
stop condition
orchestrator
```

---

# K. Evaluation 约束

## K1. 没有 Eval 不允许声称优化成功

Prompt、模型、Tool 或架构调整必须通过 Eval 比较。

---

## K2. Eval 必须包含 Regression

修复一个问题时，应验证旧能力没有明显退化。

---

## K3. Production Failure 必须回流

真实失败案例应成为后续 Eval Dataset 的候选样本。

---

## K4. 不得只使用 LLM Judge

关键指标尽量组合：

```text
deterministic check
unit test
LLM judge
human review
```

---

# L. Observability 约束

## L1. 每个 Run 必须有唯一 ID

例如：

```text
run_id
session_id
```

---

## L2. Tool Call 必须可追踪

记录：

```text
tool
args
result
latency
error
permission decision
```

---

## L3. 模型调用必须可测量

至少记录：

```text
model
tokens
latency
cost
```

---

## L4. 必须可定位失败阶段

禁止只保留最终：

```text
Agent failed
```

而无法还原 trajectory。

---

# M. 成本约束

## M1. 不默认使用最强模型

简单任务应允许使用更小模型。

---

## M2. 不允许无限 Token 消耗

必须有上下文和运行预算。

---

## M3. 优先减少无价值 LLM Round Trip

如果 Tool / Code 可以直接完成计算，不要额外调用模型。

---

# N. 可靠性约束

## N1. Tool 必须设置 Timeout

外部服务不可无限等待。

---

## N2. Retry 必须有限

不得无限重试。

建议：

```text
max retries
exponential backoff
retryable error classification
```

---

## N3. 长任务应支持 Checkpoint

对于长执行任务，应考虑：

```text
persist
resume
recover
```

---

## N4. Partial Failure 必须有策略

例如：

```text
skip
retry
fallback
ask user
terminate
```

---

# O. 代码结构约束

推荐模块职责分离：

```text
agents/
tools/
context/
state/
memory/
knowledge/
runtime/
orchestration/
guardrails/
evals/
observability/
tests/
```

避免把所有逻辑写入：

```text
agent.py
```

单一超大文件。

---

# P. 开发顺序约束

默认必须按照：

```text
Goal
↓
Success Metrics
↓
Baseline
↓
Architecture
↓
Tools
↓
Context
↓
Loop
↓
State
↓
Permissions
↓
Eval
↓
Tracing
↓
Optimization
↓
Multi-Agent
```

禁止默认采用：

```text
选框架
↓
堆多个 Agent
↓
写超长 Prompt
↓
最后再测
```

---

# Q. MVP 约束

第一版优先只包含：

```text
Single Agent
3–5 essential tools
Simple State
Basic Permission
Stop Conditions
Tracing
Small Eval Dataset
```

第一版默认不包含：

```text
复杂 Multi-Agent
无明确价值的 Long-term Memory
过度 Planning
复杂自主反思循环
大量冗余 Tools
```

除非业务需求明确要求。

---

# R. 设计评审 Gate

进入编码前必须能够回答：

```text
[ ] 为什么需要 Agent？
[ ] 为什么 Workflow 不够？
[ ] Goal 是否明确？
[ ] Success Metrics 是否明确？
[ ] Tool 是否最小化？
[ ] Context 如何控制？
[ ] State 保存什么？
[ ] 哪些行为需要权限？
[ ] Agent 如何停止？
[ ] Agent 失败如何恢复？
[ ] 如何 Eval？
[ ] 如何 Trace？
```

如果关键问题没有答案，应先补架构设计，而不是继续堆代码。

---

# S. 上线 Gate

生产上线前至少确认：

```text
[ ] 核心 Eval 达标
[ ] Regression Eval 通过
[ ] max_turns 已设置
[ ] timeout 已设置
[ ] cost / token budget 已考虑
[ ] Tool 参数有验证
[ ] 高风险 Tool 有权限控制
[ ] Sandbox 隔离已考虑
[ ] Trace 可用
[ ] Error handling 可用
[ ] Retry 有上限
[ ] Production failure 可收集
[ ] Secrets 不会泄漏到模型或日志
```

---

# T. 最终硬规则

## Rule 1

```text
不要为了 Agent 而 Agent。
```

## Rule 2

```text
不要为了 Multi-Agent 而 Multi-Agent。
```

## Rule 3

```text
不要把 Prompt 当成安全边界。
```

## Rule 4

```text
不要把 Memory 当成数据库。
```

## Rule 5

```text
不要把所有历史都塞进 Context。
```

## Rule 6

```text
不要把所有工具都给模型。
```

## Rule 7

```text
不要允许无限循环。
```

## Rule 8

```text
不要允许未经权限检查的 Side Effect。
```

## Rule 9

```text
不要在没有 Eval 的情况下判断 Agent 变好了。
```

## Rule 10

```text
不要在没有 Trace 的情况下上线复杂 Agent。
```

---

# U. 决策优先级

如果多个方案都能完成需求，按下面顺序选择：

```text
Correctness
↓
Safety
↓
Reliability
↓
Simplicity
↓
Observability
↓
Maintainability
↓
Latency
↓
Cost
↓
Extra Autonomy
```

Agent 的“自主程度”不是最终目标。

稳定完成用户任务才是。
