# AGENT_DEVELOPMENT_INSTRUCTIONS.md

## 如何使用这套文件

将以下两个文件放入未来 Agent 项目的根目录：

```text
AGENT_DEVELOPMENT_PROMPT.md
AGENT_DEVELOPMENT_CONSTRAINTS.md
```

使用 AI 编程助手开始项目时，可以给出：

```text
请阅读 AGENT_DEVELOPMENT_PROMPT.md 和
AGENT_DEVELOPMENT_CONSTRAINTS.md。

接下来所有 Agent 架构设计和代码实现都遵循这两份文件。

我的 Agent 需求是：

<在这里填写需求>

先不要直接写完整代码。

先按照 Prompt 中的 Required Output，
完成 Agent 架构设计、Tool 设计、State 设计、
Permission 设计、Eval 设计和 MVP 实现计划。
```

---

## 推荐项目结构

```text
my-agent/
│
├── AGENT_DEVELOPMENT_PROMPT.md
├── AGENT_DEVELOPMENT_CONSTRAINTS.md
├── README.md
│
├── docs/
│   ├── ARCHITECTURE.md
│   ├── TOOLS.md
│   └── EVALS.md
│
├── agents/
├── tools/
├── context/
├── state/
├── memory/
├── runtime/
├── guardrails/
├── evals/
└── tests/
```

---

## 推荐第一次 Prompt

```text
阅读并严格遵守：

1. AGENT_DEVELOPMENT_PROMPT.md
2. AGENT_DEVELOPMENT_CONSTRAINTS.md

我要开发：

[描述 Agent]

你的第一步不是写代码。

请先输出：

1. User Problem
2. Agent Goal
3. 是否真的需要 Agent
4. Workflow / Single Agent / Multi-Agent 架构判断
5. Success Metrics
6. Inputs / Outputs / Constraints
7. Action Space
8. Minimal Tool Set
9. Tool Schema
10. Context Strategy
11. State Schema
12. Memory / Knowledge Strategy
13. Agent Loop
14. Stop Conditions
15. Permission / Guardrails
16. Sandbox 需求
17. Eval Dataset 设计
18. Observability / Trace
19. MVP Implementation Plan
20. 暂时不应加入的复杂功能

如果当前需求信息不完整，在不阻塞开发的前提下做合理假设，
明确写出 assumptions，并优先给出最简单可行架构。
```

---

## 推荐后续实现 Prompt

架构确认后：

```text
基于已确认的 Agent Architecture，
开始实现 MVP。

严格遵守 AGENT_DEVELOPMENT_CONSTRAINTS.md。

优先完成：

1. Agent Contract
2. Tool Interfaces
3. State Model
4. Agent Loop
5. Permission Layer
6. Stop Conditions
7. Basic Tracing
8. Eval Harness
9. Tests

不要提前加入没有被 Eval 证明必要的：

- Multi-Agent
- Long-term Memory
- 自主反思循环
- 复杂 Planning
- 多余 Tools

每完成一个模块，都确保它可测试、可替换、可观测。
```
