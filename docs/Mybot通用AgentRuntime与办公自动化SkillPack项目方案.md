# Mybot 通用 Agent Runtime 与办公自动化 Skill Pack 项目方案

## 0. 项目定位

**Mybot 是基于现有 `nanobot/` 代码库二次开发的个人 Agent Runtime。它不是从零重写一个新平台，而是在 nanobot 已有的 AgentLoop、AgentRunner、MessageBus、工具系统、MCP 接入、Skill 加载、Workspace 安全策略和 WebUI 之上，补齐 Skill Pack、Permission Kernel、Context Engine、Artifact Graph、Trace Replay、Checkpoint、Human-in-the-loop 和分层 Eval Harness。**

本方案的核心原则是：

> 删除的是“已实现基础设施的重复建设”，不是删除更好的工程设计。  
> 高级 Runtime 能力仍然保留，但要明确它们是基于当前 Mybot 的渐进二开目标。

最终项目叙述：

> Mybot 是基于 nanobot v0.2.1 二次开发的可治理 Agent Runtime。底层复用 nanobot 的 WebSocket WebUI、消息总线、Agent 循环、工具调用、MCP 与 workspace 安全策略；上层通过 Skill Pack 扩展领域能力。Office Automation Skill Pack 是第一个样板插件，用来验证 Mybot 在复杂多步骤任务中的状态管理、工具治理、产物追踪、权限控制、质量评测和失败恢复能力。

---

## 1. 如何阅读这份方案

为了兼顾“符合当前项目”和“业内最佳实践”，本文把能力分成三类：

| 类别 | 含义 | 文档写法 |
|---|---|---|
| 已有基线 | 当前代码已经实现或基本具备 | 标为“现有承接点”，不重复设计一套 |
| 二开目标 | 当前项目应该新增或增强的能力 | 给出模块、数据结构、落地路径和测试方式 |
| 远期增强 | 面向生产级/展示级/面试亮点的高级能力 | 保留设计，但放到后续里程碑，不承诺 MVP 立即完成 |

这避免两个问题：

- 只写当前已实现内容，方案会太薄，看不到工程含量。
- 只写理想化平台设计，又会和当前 Mybot 代码脱节。

---

## 2. 当前 Mybot 基线

### 2.1 代码基线

当前 Mybot 不是空白项目，已有大量可复用设施：

| 方向 | 当前代码位置 | 已有能力 |
|---|---|---|
| 消息入口 | `nanobot/channels/websocket.py`、`nanobot/bus/queue.py` | 默认 WebSocket 通道，消息经异步 bus 分发 |
| Agent 核心 | `nanobot/agent/loop.py`、`nanobot/agent/runner.py` | 消费消息、构造上下文、调用模型、执行工具、流式响应 |
| 上下文 | `nanobot/agent/context.py`、`autocompact.py` | 身份、bootstrap、skills、runtime lines、上下文压缩 |
| 记忆 | `nanobot/agent/memory.py` | workspace 级记忆、历史持久化、Dream 整合 |
| Skill 加载 | `nanobot/agent/skills.py`、`nanobot/skills/*/SKILL.md` | 内置和 workspace skills、frontmatter、依赖检查、always skills |
| 工具系统 | `nanobot/agent/tools/` | 文件、Shell、搜索、长任务、子代理、消息、图片、runtime state |
| MCP 接入 | `nanobot/agent/tools/mcp.py`、`nanobot/webui/mcp_presets_api.py` | MCP tools/resources/prompts 包装为 nanobot tools，支持配置与热重载 |
| 安全边界 | `nanobot/security/workspace_policy.py`、`workspace_access.py`、`network.py` | workspace 路径限制、网络目标校验、WebUI workspace scope |
| WebUI 设置 | `nanobot/webui/settings_api.py`、`settings_routes.py` | settings API、provider 配置、模型预设、MCP presets |
| WebUI 会话 | `nanobot/webui/thread_disk.py`、`transcript.py`、`file_preview.py` | WebUI 线程持久化、转录/记录、文件预览 |
| 产物基础 | `nanobot/utils/artifacts.py`、`document.py`、`file_edit_events.py` | 图片产物、文档工具辅助、文件编辑事件 |
| Office 依赖 | `pyproject.toml` | 已包含 `openpyxl`、`python-docx`、`python-pptx`、`pypdf` |

### 2.2 当前产品边界

当前 Mybot 二开边界应保持一致：

- 默认仅启用 WebSocket 通道；其他通道代码保留，但不是主线。
- 模型预设只保留 DeepSeek V4 Pro/Flash 和 Xiaomi MiMo V2.5 Pro/V2.5。
- API Key 通过 `~/.nanobot/config.json` 配置，不依赖环境变量。
- WebUI 对话框右下角模型下拉通过 settings API 保存 `modelPreset`，不会发送聊天消息。
- 不新建独立 `mybot/` 包来复制 `nanobot/` 逻辑。
- 不重写 AgentLoop/AgentRunner；优先以 hook、event、metadata、adapter 的方式增强。

### 2.3 已实现内容不要重复建设

下面这些能力不要在方案里写成“需要从零实现”：

| 能力 | 当前已有 | 二开重点 |
|---|---|---|
| Agent loop | 已有 `AgentLoop` / `AgentRunner` | 加 task state、event hook、trace span |
| Tool registry | 已有 `ToolRegistry` / `ToolLoader` | 加 capability、risk、artifact metadata |
| MCP gateway | 已有 MCP wrapper 和 WebUI presets | 加信任级别、权限决策、tool poisoning 防护 |
| Skill loader | 已有 `SKILL.md` loader | 加 SkillPack manifest、registry、eval |
| Workspace guard | 已有路径边界 | 加规则化 Permission Kernel 和 audit |
| WebUI settings | 已有 settings API | 加 Skill/Artifact/Eval 管理视图 |

---

## 3. 设计目标与非目标

### 3.1 核心目标

Mybot 的目标不是一个泛用聊天机器人，而是一个可治理、可扩展、可评测的 Agent Runtime。目标能力包括：

- Planner / ReAct / Replan / Reviewer。
- Task Intake 和结构化任务状态。
- Skill Pack 插件协议。
- MCP-style Tool Gateway。
- Permission Kernel 和 Runtime Guardrails。
- Context Engine 和上下文压缩。
- Artifact Store、Artifact Graph、Delta Engine。
- Event Log、Trace Replay、Checkpoint 恢复。
- Human-in-the-loop。
- 分层 Eval Harness。
- Observability Dashboard。

### 3.2 非目标

MVP 阶段不做：

- 不做新的独立 `mybot/` runtime 包。
- 不做远程 marketplace 和插件分发平台。
- 不恢复 Docker 作为主线部署方案。
- 不做多租户企业权限后台。
- 不把所有原 nanobot 通道重新启用。
- 不用 LLM Judge 替代确定性测试。
- 不把 Office 自动化做成唯一产品方向。

### 3.3 设计取舍

| 取舍 | 选择 | 理由 |
|---|---|---|
| 重写 vs 增强 | 增强现有 `nanobot/` | 保留当前 WebUI、MCP、工具、配置和会话能力 |
| Skill 形式 | 兼容 `SKILL.md`，逐步加 `skill.yaml` | 和当前 loader 兼容，减少迁移成本 |
| 权限实现 | 先策略层，后沙箱层 | 当前已有 workspace/network guard，先补规则和审计 |
| 产物存储 | 先本地 JSON/JSONL，后 SQLite | MVP 简单可调试，后续可升级 |
| Eval | 规则优先，LLM Judge 辅助 | 数据一致性、权限、安全必须确定性 |
| WebUI | 先展示关键状态，后做完整 Dashboard | 避免前端过早复杂 |

---

## 4. 行业最佳实践对齐

### 4.1 Agent Runtime 的最小核心

业内成熟 Agent 系统的核心不是“模型调用”，而是围绕模型调用的运行时：

| 最佳实践方向 | 对 Mybot 的意义 |
|---|---|
| Agent loop | 保持简单循环，但把状态、工具、权限、trace 放在循环周围 |
| Tools / MCP | 工具必须有 schema、权限、审计和结果归一化 |
| Handoffs / Subagents | 专家代理之间要有结构化交接，不靠自由文本乱传 |
| Guardrails | 输入、输出、工具调用前后都需要可编程检查 |
| Sessions | 会话历史要可持久化、可裁剪、可恢复 |
| Tracing | 每次运行要可复盘，工具、模型、handoff、guardrail 都应有 span |
| Human-in-the-loop | 高风险动作要能暂停、确认、恢复 |
| Sandbox / Workspace | 文件和命令能力必须有边界 |

### 4.2 MCP 对齐

MCP 官方规范把外部能力分为：

- Resources：上下文和数据。
- Prompts：模板消息和工作流。
- Tools：模型可执行函数。
- Roots：客户端暴露给服务器的文件/URI 边界。
- Sampling：服务器发起的模型调用请求。
- Elicitation：服务器向用户请求补充信息。

Mybot 已有 MCP tools/resources/prompts 包装能力，二开重点是：

- 给 MCP server 加 trust level。
- 对 MCP tool 描述做静态校验，避免 tool poisoning。
- MCP 工具默认不自动高权限执行。
- 把 workspace scope 映射到 MCP roots 思路。
- sampling/elicitation 类能力必须经用户确认。

### 4.3 Claude Code 类产品对齐

Claude Code 类产品体现的工程经验：

- 权限规则要独立于模型提示，不能靠 prompt 约束代替 enforcement。
- 工具调用前要有 PreToolUse 类 hook。
- 工具调用后要有 PostToolUse 类 hook，用于审计、脱敏、产物登记。
- permission mode 要支持 read-only、ask、allow、deny、bypass 等模式，但 bypass 只能用于隔离环境。
- Skill 应通过 `SKILL.md` 和 supporting files 做渐进加载，避免一次性把所有说明塞进上下文。

Mybot 可以借鉴这些模式，但落地到现有 `nanobot/`：

- `AgentRunner` 工具调用前后插入 policy/hook。
- `SkillsLoader` 扩展 metadata 和 supporting files。
- WebUI settings 增加 skill/tool permission 管理。
- workspace policy 和 shell/network policy 作为硬边界。

### 4.4 OWASP GenAI 安全对齐

Mybot 需要重点防范：

| 风险 | 对应措施 |
|---|---|
| Prompt Injection | 工具输出和文档内容不直接转成系统指令，重要工具前做 policy check |
| Sensitive Information Disclosure | trace、tool output、artifact summary 支持脱敏 |
| Supply Chain | SkillPack/MCP server 有来源、版本、hash、trust level |
| Data/Model Poisoning | eval fixtures、verified facts、引用来源记录 |
| Improper Output Handling | LLM 输出经过 schema/DSL/renderer 校验 |
| Excessive Agency | 高风险动作默认 ask/deny，权限最小化 |
| System Prompt Leakage | skill/policy 分层，敏感配置不进入模型 |
| Vector/Embedding Weaknesses | 后续 memory 检索要隔离 workspace 和来源 |
| Misinformation | Office 数字必须来自 verified facts |
| Unbounded Consumption | token、工具次数、时间、重试次数预算控制 |

---

## 5. 总体架构

### 5.1 当前适配架构

```text
WebUI / WebSocket
        |
        v
MessageBus
        |
        v
AgentLoop / AgentRunner
        |
        +--> Task Intake / Router / State
        |
        +--> Context Engine
        |       +--> Memory
        |       +--> Skill Context
        |       +--> Artifact Context
        |       +--> Policy Context
        |
        +--> Planner / ReAct / Replan / Reviewer
        |
        +--> Policy & Guardrails (策略层)
        |       +--> Permission Decision (在 prepare_call 外包)
        |       +--> Guardrails
        |       +--> Human Approval (走 injection_callback)
        |       +--> Budget Control
        |
        +--> Tool Gateway
        |       +--> Builtin Tools
        |       +--> MCP Tools
        |       +--> Office Tools
        |
        +--> SkillPack Layer
        |       +--> SKILL.md
        |       +--> skill.yaml
        |       +--> workflows / schemas / evals
        |
        +--> Artifact Runtime
        |       +--> Store
        |       +--> Metadata
        |       +--> Lineage
        |       +--> Delta
        |
        +--> Trace / Replay / Eval
                +--> JSONL Event Log
                +--> Span Timeline
                +--> Eval Reports
```

> **读图须知（对齐 `.agent/design.md` "core stays small"）**：上图是**逻辑视图**，不代表这些能力都塞进 `loop.py`/`runner.py`。核心循环保持精简，能力以**工具、AgentHook（+ `CompositeHook`）、registry 包装、skill 内容、或 loop 之上的薄编排层**形式挂在核心**外围**。
>
> **术语提醒**：代码里 "governance" 已专指 runner 的**上下文治理**（orphan 清理 / backfill / microcompact / snip_history，见 `tests/agent/test_runner_governance.py`）。本方案的权限/护栏/预算层改称 **"Policy & Guardrails / 策略层"**，避免撞名。

### 5.2 模块映射

| 目标模块 | 当前承接点（已逐一核对代码） | 二开内容 |
|---|---|---|
| Task Runtime / 状态机 | `session/manager.py`、`goal_state.py`、`turn_continuation.py`（`loop.py`/`runner.py` 是受保护核心，勿改） | task id、state machine 放 session 层或 loop 之上薄层，经 runtime event 广播 |
| Checkpoint / Durable | ✅ **已有** `AgentRunSpec.checkpoint_callback`（runner 已发 `awaiting_tools`/`tools_completed`/`final_response` 三阶段，含 assistant message + 工具状态） | 在 checkpoint payload 加 stage/artifact 元数据并落盘；恢复建其上 |
| Context Engine | `context.py`、`memory.py`、`autocompact.py`；runner 已有 `microcompact`/`snip_history`/工具结果 offload/budget | 多数已具备；新增 artifact/skill/policy **分层注入** |
| Tool Gateway | `tools/registry.py`、`loader.py`、`mcp.py` | result normalizer、审计；权限见下 |
| PreToolUse 权限（deny） | ✅ **已有拦截点** `ToolRegistry.prepare_call`（返回 error 即被 runner `_classify_violation` 当可恢复边界处理） | 在 `prepare_call` 外包同步权限决策（deny/allow） |
| 人工确认（ask） | ✅ **已有** runner `injection_callback` + `_try_drain_injections` + `goal_active_predicate` | 高风险动作：暂停 → 注入确认 → 续跑 |
| PostToolUse 审计 / Trace 发射 | ✅ **已有** `AgentHook.after_iteration`（带 `tool_calls`/`tool_events`/`usage`）+ `CompositeHook` 错误隔离 | 审计与 trace 作为 AgentHook 子类组合进 runner |
| Policy 硬边界 | ✅ **已有** `filesystem._resolve_path`、`security/network.validate_url_target`、`tools/sandbox._wrap_<name>`(bwrap)、`config.tools.ssrf_whitelist` | 策略层包在硬边界**外**；新增 risk rules、approval、secret redaction |
| Tool 能力元数据 | ✅ **已有基类属性** `Tool.read_only`/`concurrency_safe`/`exclusive` | 扩展基类，新增 `risk_level`/`capability`/`requires_approval`（勿建平行 dataclass） |
| Skill Pack | `agent/skills.py`（已解析 frontmatter `metadata.nanobot`、`requires`、`always`） | `skill.yaml` 兄弟文件旁路读取 + schema 校验 |
| Skill 启用/禁用 | ✅ **已有** `config.agents.defaults.disabled_skills` + `SkillsLoader(disabled_skills=)` | 复用，勿新建 `skills.disabled` |
| Progressive loading | ✅ **已有** `SkillsLoader.build_skills_summary()`（name+desc+path，按需读全文） | 仅补 `skill.yaml`/workflow/schema 懒加载 |
| MCP 治理 | ✅ **已有** `config.MCPServerConfig.enabled_tools`（按服务器白名单）；HTTP MCP 已过 `validate_url_target` | `trust_level` 加到 `MCPServerConfig`；tool 描述静态校验 |
| Artifact Runtime | ⚠️ `utils/artifacts.py` 仅**图片专用**（其"文件旁 sidecar JSON"模式可借鉴）；`webui/file_preview.py` | 通用 artifact store / lineage / delta / metadata index **全新** |
| Trace | ⚠️ `bus/runtime_events.py` 是 **UI 状态 pub-sub**，非 span/JSONL；`webui/transcript.py` | trace store **全新**（AgentHook 写 JSONL）；UI 事件可订阅 trace |
| Eval | ⚠️ `utils/evaluator.py` 是后台任务"是否通知用户"的 LLM 判断，**与本 eval 无关**；`tests/` | eval runner / metric plugins / fixture cases / CI smoke **全新**，确定性优先 |
| WebUI | `webui/settings_*`、`skills_api.py`（已列 skill） | 新增 skill/artifact/eval/trace 页面 |

### 5.3 目录策略（少结构优先）

遵循 `.agent/design.md` 的 "Less structure, more intelligence" 与 "prefer duplication over premature abstraction"，**MVP 不新建顶层包**：

- **不新建独立 `mybot/` 包**复制 nanobot 逻辑。
- Office 能力先**全部落在 skill 内**：`nanobot/skills/office-automation/`（含 `scripts/`、`assets/`、`references/`），确定性逻辑用脚本、由现有 exec 工具运行；少量通用助手放 `utils/`。
- 仅当**第二个 skill 真的复用**某能力时，再把它提升为薄模块。届时（远期）可考虑：

```text
nanobot/
├── runtime/      # trace writer / checkpoint 元数据（建在 checkpoint_callback 之上）
├── artifacts/    # 通用 artifact store / lineage
└── evals/        # eval runner / metrics / reports
```

> 原则：先用已有承接点跑通闭环，新结构最后；每个新包都应由"≥2 处真实复用"证明其必要性。不要为了 MVP 就先建 `runtime/`、`artifacts/`、`evals/` 三个空壳包。

---

## 6. Runtime Core

### 6.1 Task Intake

Task Intake 把用户自然语言请求转成结构化任务：

| 字段 | 示例 |
|---|---|
| `goal` | 根据销售 Excel 和会议纪要生成周报和 PPT |
| `domain` | office |
| `inputs` | `sales.xlsx`、`meeting_notes.md` |
| `outputs` | `weekly_report.docx`、`weekly_review.pptx` |
| `constraints` | PPT 不超过 6 页，数字必须来自 Excel |
| `risk_actions` | 文件写入、邮件发送、shell 执行 |
| `missing_slots` | 受众、模板、页数、语言 |
| `candidate_skills` | `office-automation` |

MVP 可以用轻量规则 + LLM 结构化输出实现。后续可以加 intent classifier。

### 6.2 Task State

统一任务状态是 Runtime 的核心：

| 状态类型 | 内容 |
|---|---|
| Task State | task_id、goal、status、created_at、updated_at |
| User Constraint | 输入文件、输出格式、页数、风格、受众 |
| Skill State | 当前 skill、manifest、权限、工具依赖 |
| Plan State | plan_id、steps、dependencies、current_step |
| Tool State | tool call id、参数、结果、错误、耗时 |
| Artifact State | 输入、中间产物、最终产物、版本、血缘 |
| Context State | 当前上下文摘要、memory 引用、压缩记录 |
| Governance State | permission decision、approval、budget |
| Eval State | metrics、issues、passed、repair suggestions |

### 6.3 状态机

```text
created
  -> intake
  -> planning
  -> awaiting_user_input
  -> executing
  -> evaluating
  -> repairing
  -> completed
  -> failed
  -> cancelled
```

关键规则：

- 缺少关键信息进入 `awaiting_user_input`。
- 工具失败进入 `repairing` 或 `failed`。
- 高风险动作进入 `awaiting_user_input` 或 `approval_required`。
- eval 失败可进入 `repairing`。
- checkpoint 完成后可以从最近稳定状态恢复。

### 6.4 Planner / ReAct / Replan

Mybot 不需要把所有任务固定成 workflow，但应有受控自主循环：

```text
Intake
  -> Skill select
  -> Context build
  -> Plan create
  -> Plan critic
  -> ReAct execute
  -> Tool observe
  -> Artifact register
  -> Eval
  -> Replan / Repair
  -> Deliver
```

PlanStep 建议字段：

```json
{
  "step_id": "step_003",
  "title": "生成 PPT DSL",
  "skill": "office-automation",
  "agent_role": "slide_writer",
  "required_tools": ["office.build_slide_dsl"],
  "input_artifacts": ["verified_facts.json", "meeting_summary.json"],
  "expected_outputs": ["slide_dsl.json"],
  "acceptance_criteria": [
    "PPT 不超过 6 页",
    "每页至少一个标题",
    "所有数字引用 fact_id"
  ],
  "risk_level": "low"
}
```

### 6.5 Plan Critic

Planner 生成计划后不能直接执行。Plan Critic 检查：

| 检查项 | 说明 |
|---|---|
| 完整性 | 是否覆盖用户目标和 expected artifacts |
| 依赖关系 | step 输入输出是否闭合 |
| 权限风险 | 是否调用未授权工具 |
| 文件边界 | 是否读写 workspace 外路径 |
| 成本预算 | 是否超过最大步数、token、工具调用次数 |
| 人工确认 | 是否在高风险动作前加入确认 |
| 可评测性 | 是否有明确产物和验收标准 |

失败时进入 Replan。

### 6.6 Handoff Protocol

当任务交给 specialist agent 或 skill 子流程时，不传自由文本，而传结构化 Handoff：

```json
{
  "from": "planner",
  "to": "slide_writer",
  "task_scope": "根据 verified_facts 和会议摘要生成 slide_dsl",
  "input_artifacts": ["verified_facts.json", "meeting_summary.json"],
  "expected_output": "slide_dsl.json",
  "constraints": [
    "不超过 6 页",
    "不得编造数字",
    "每个数字必须引用 fact_id"
  ],
  "acceptance_criteria": [
    "schema 校验通过",
    "所有 fact_id 存在",
    "每页标题非空"
  ]
}
```

这对应业内 handoff 的最佳实践：明确交接目标、输入、输出、约束和验收。

### 6.7 Durable Execution

长任务要支持：

- 暂停和恢复。
- step 级 checkpoint。
- 幂等工具调用。
- 失败局部重试。
- artifact 复用。
- 用户确认后继续。
- 超时后恢复。

Mybot MVP 不需要上复杂工作流引擎，可以先用 task state + JSONL event log + artifact metadata 实现。

---

## 7. Tool Gateway 与 MCP

### 7.1 Tool Gateway 职责

Tool Gateway 是 Agent 与外部世界之间的治理层：

| 职责 | 说明 |
|---|---|
| Discovery | 根据 skill、权限、runtime state 暴露可用工具 |
| Schema Registry | 工具输入输出有 schema |
| Permission Check | 调用前生成 PermissionDecision |
| Execution Adapter | 本地工具、MCP、未来微服务统一适配 |
| Result Normalizer | 工具结果归一化为 observation 和 artifact |
| Audit Log | 记录参数摘要、结果摘要、耗时、错误 |
| Budget Control | 限制调用次数、并发、超时 |
| Guardrails | 调用前后做敏感信息、路径、网络、格式检查 |

### 7.2 当前承接点

当前已有：

- `ToolRegistry` 负责注册和导出工具定义。
- `ToolLoader` 负责加载内置工具。
- `mcp.py` 负责连接 MCP server 并包装 tools/resources/prompts。
- 文件/Shell 工具已接入 workspace scope。

二开增强（**扩展现有 `Tool` 基类，而非平行 dataclass**）：

`Tool` 基类已有 `read_only`/`concurrency_safe`/`exclusive`。在其上新增能力/风险元数据：

```python
class Tool(ABC):
    # 已有：read_only / concurrency_safe / exclusive
    capability: str = ""            # 工具能力分类
    risk_level: str = "low"         # low / medium / high / forbidden
    requires_approval: bool = False # 是否默认需要人工确认
    skill_scope: list[str] = []     # 仅在这些 skill 下暴露（空=通用）
    # artifact_inputs / artifact_outputs / permission_tags 视需要再加
```

> 这些属性由 `prepare_call` 外的策略层与 trace AgentHook 读取，**无需改 runner 主循环**。

### 7.3 MCP 安全策略

MCP 工具不是天然可信。策略：

| 项 | 策略 |
|---|---|
| Server 来源 | builtin / user / workspace / remote 分级 |
| Tool 描述 | 不把 tool description 当可信指令 |
| Tool 名称 | 做 canonical name 和冲突检测 |
| Tool 参数 | schema 校验和敏感字段扫描 |
| Tool 调用 | 默认 ask，只有低风险 allow |
| Remote MCP | 网络目标校验，base URL allowlist |
| Sampling | 必须用户确认 |
| Roots | 映射到 workspace scope，不暴露全盘 |
| Elicitation | UI 中显示来源和请求字段 |

### 7.4 Office 能力：脚本优先，按需提升为工具

> 对齐本仓库 skill 惯用模式（`skill-creator` 倡导、`tmux/scripts`、`skill-creator/scripts` 已实践）与 `.agent/design.md` 的"少结构"：**MVP 用 SKILL.md + 捆绑脚本（由现有 `exec` 工具运行）**，不一次性注册一堆 `office.*` 工具。

Office 能力清单（先实现为 `nanobot/skills/office-automation/scripts/*.py`）：

| 能力 | 形态(MVP) | 输入 | 输出 | 风险 |
|---|---|---|---|---|
| inspect_workbook | 脚本 | xlsx path | workbook_schema.json | low |
| extract_facts | 脚本 | xlsx、metric spec | verified_facts.json | low |
| build_report_dsl | 脚本/LLM | notes、facts | report_dsl.json | low |
| build_slide_dsl | 脚本/LLM | notes、facts | slide_dsl.json | low |
| render_docx | 脚本 | report_dsl、template | docx | medium |
| render_pptx | 脚本 | slide_dsl、template | pptx | medium |
| validate | 脚本 | artifact 路径 | quality_report.json | low |

**取舍**：工具的优势是 schema 校验 + 权限元数据 + 结构化返回；脚本的优势是简单、渐进披露、不污染全局工具表。**脚本起步**，等某操作确实需要 schema 校验/结构化返回（如 `render_pptx`），再把那一两个提升为 `office.*` 工具并按 §7.2 标注 metadata。

渲染脚本写入 artifacts 目录，**不覆盖用户原文件**；workspace 边界（`_resolve_path`）已管住写入路径，MVP 不必先上 Permission Kernel。

---

## 8. Permission Kernel 与 Guardrails

### 8.1 为什么需要 Permission Kernel（策略层）

通用 Agent 能读文件、写文件、执行 shell、访问网络、调用 MCP、生成文档。如果只靠 prompt 约束，风险不可控。Permission Kernel（策略层）的作用是把工具调用转成可执行的策略决策。

**承接点（务必复用，勿重造）**：

- 拦截点：`ToolRegistry.prepare_call`（同步 deny/allow）+ runner `_classify_violation`（把拒绝当可恢复边界，含统一话术）。
- 人工确认：runner `injection_callback` + `_try_drain_injections`（异步 ask → 续跑）。
- 硬边界：`filesystem._resolve_path`、`security/network.validate_url_target`、`tools/sandbox._wrap_<name>`(bwrap)、`config.tools.ssrf_whitelist`。
- 策略层只做"决策 + 审计"，包在硬边界**之外**；deny 提示语复用既有 `WORKSPACE_BOUNDARY_NOTE`/`_SSRF_BOUNDARY_NOTE`。

### 8.2 权限规则模型

建议采用 deny / ask / allow 三层：

```text
deny > ask > allow
```

规则来源：

| 来源 | 示例 |
|---|---|
| Runtime 默认策略 | 禁止 workspace 外写入，禁止删除原始文件 |
| Skill manifest | Office 允许 artifacts 写入，拒绝 shell |
| WebUI settings | 用户配置允许/询问/禁止某些工具 |
| Workspace policy | 当前项目的 AGENTS.md 或后续策略文件 |
| Session approval | 本轮临时授权 |
| Tool hook | 调用前动态判断 |

### 8.3 PermissionDecision

```json
{
  "decision_id": "perm_001",
  "task_id": "task_001",
  "tool": "office.render_pptx",
  "action": "file.write",
  "risk_level": "medium",
  "allowed": true,
  "requires_approval": false,
  "reason": "office-automation skill allows generated artifact writes",
  "matched_rules": ["skill:office-automation:file_write:artifacts_only"],
  "artifact_outputs": ["weekly_review.pptx"]
}
```

### 8.4 风险分级

| 风险 | 动作 | 默认策略 |
|---|---|---|
| low | 读 workspace 内输入文件、读 schema、生成摘要 | allow + trace |
| medium | 写 artifacts、生成 docx/pptx、运行只读分析 | allow if skill declares |
| high | 修改用户原文件、执行 shell、远程 API、发送邮件 | ask |
| forbidden | 删除原始文件、读 workspace 外敏感路径、绕过策略 | deny |

### 8.5 Guardrails 覆盖点

| 阶段 | Guardrail |
|---|---|
| UserPromptSubmit | prompt injection 初筛、任务范围识别 |
| PlanCreated | 计划越权、缺验收标准、成本超限 |
| PreToolUse | 工具参数、路径、网络、权限、敏感字段 |
| PostToolUse | 输出脱敏、结果 schema、artifact 登记 |
| PreModelCall | 上下文脱敏、token 预算 |
| PostModelCall | JSON/DSL/schema 校验 |
| PreDelivery | 质量评测、安全检查、引用检查 |

### 8.6 Shell 与网络

Shell（`exec`）是高风险工具，但**Office 脚本优先方案需要用 `exec` 运行 skill 自带脚本**，因此策略区分两类用法：

- ✅ **运行 skill 自带、已审阅的脚本**（如 `python skills/office-automation/scripts/render_docx.py ...`），限定 workspace 内、无网络、不安装依赖——视为 low/medium，允许。
- ⚠️ 任意写操作、安装依赖、删除、网络命令——默认 ask/deny。
- shell 子命令要独立匹配，不能只匹配整条复合命令。
- URL 过滤不要靠 shell 字符串匹配，优先使用受控 Web 工具。
- （远期）把 `render_docx`/`render_pptx` 提升为原生 `office.*` 工具后，Office 可不再依赖 `exec`，进一步收紧。

网络：

- Web search/fetch 经过 provider 和 domain 策略。
- MCP HTTP server 经过 SSRF/network validation（`validate_url_target`）。
- Office MVP 默认不需要外网。

### 8.7 Secret Redaction

敏感信息处理：

- API key 不进入模型上下文。
- trace 默认不记录完整 secret。
- tool input/output 可配置 redaction。
- `.env`、ssh key、config key 默认高风险。
- quality report 不暴露隐私路径或 key。

---

## 9. Context Engine

### 9.1 目标

Context Engine 负责“让模型看到必要信息，而不是所有信息”。

核心目标：

- 关键信息不丢。
- 大文件不直接塞 prompt。
- 工具结果结构化。
- Skill 和 policy 分层注入。
- 长任务中上下文不漂移。
- 敏感信息进入模型前脱敏。

### 9.2 上下文分层

| 类型 | 来源 | 注入策略 |
|---|---|---|
| System Context | identity、platform policy、tool contract | 固定小体积 |
| Task Context | task state、用户目标、约束 | 每轮必带摘要 |
| Skill Context | `SKILL.md`、`skill.yaml`、workflow | 只加载当前候选 skill |
| Artifact Context | metadata、schema、summary、facts | 懒加载，不放完整文件 |
| Memory Context | `MemoryStore`、用户偏好、模板偏好 | 检索后注入 |
| Policy Context | permission mode、workspace scope、risk rules | 每轮简短注入 |
| Recent Observations | 最近工具结果、错误、eval issues | 滚动窗口 |
| Compressed Context | 历史 trace 和长对话摘要 | 达到阈值后压缩 |

### 9.3 Progressive Loading

> ✅ **第 1–2 步已实现**：`SkillsLoader.build_skills_summary()` 已只列出 name + description + path，agent 用 `read_file` 按需读取 `SKILL.md` 全文。二开只需补第 3–5 步。

SkillPack 不应一次性把所有 prompts、schemas、examples 全塞给模型：

1. （已具备）默认只列出 skill name、description、path。
2. （已具备）命中候选 skill 后用 `read_file` 加载 `SKILL.md` 主说明。
3. （新增）某个 step 需要时再读取 workflow/schema/template。
4. （新增）Office 大文件通过脚本生成摘要和 verified facts，不进上下文。
5. （新增）引用源文件时使用 artifact id 和摘要，不直接复制全文。

### 9.4 Artifact Context

Artifact summary 示例：

```json
{
  "artifact_id": "verified_facts.json",
  "type": "verified_facts",
  "summary": "包含 GMV、订单量、转化率、Top 区域等 12 个指标",
  "schema": "verified_facts.schema.json",
  "source": ["sales_data.xlsx"],
  "quality": {
    "validated": true,
    "issues": []
  }
}
```

模型只看摘要，必要时通过工具读取具体字段。

### 9.5 Context Compression

压缩策略：

- 对话历史按 turn 摘要。
- 工具结果按 artifact 摘要。
- trace 按阶段摘要。
- 保留 unresolved issues 和 user constraints。
- 保留所有高风险 permission decision。

压缩结果必须可追溯到原始 artifact/trace。

---

## 10. Skill Pack 插件系统

### 10.1 Skill Pack 的定位

普通 skill 解决“Agent 知道怎么做某类事”。Skill Pack 解决“一个领域能力如何被安全加载、可靠执行、持续评测和版本化管理”。

| 对比点 | 当前 `SKILL.md` | Skill Pack |
|---|---|---|
| 内容 | instructions | instructions + manifest + workflows + schemas + evals |
| 生命周期 | 目录扫描 | 安装、启用、禁用、inspect、eval |
| 权限 | 依赖检查为主 | 声明 tool、artifact、network、shell 权限 |
| 上下文 | markdown 注入 | progressive loading |
| 产物 | 不显式 | 声明输入输出 artifact |
| 评测 | 无或手工 | 自带 eval cases |
| 版本 | 目录状态 | semver + compatibility |
| 可观测 | 工具级 | skill 级 trace 和 metrics |

### 10.2 当前兼容方式

第一阶段必须兼容当前 `SkillsLoader`：

```text
nanobot/skills/office-automation/
├── SKILL.md               # 必需，当前 loader 可识别
├── skill.yaml             # 可选，二开扩展读取
├── prompts/
├── schemas/
├── workflows/
├── evals/
└── examples/
```

Workspace skill 也可使用：

```text
<workspace>/skills/office-automation/SKILL.md
```

### 10.3 `SKILL.md` Frontmatter

```markdown
---
name: office-automation
description: Generate Word reports and PowerPoint decks from spreadsheets and notes.
metadata:
  nanobot:
    always: false
    requires:
      bins: []
      env: []
    skill_pack: true
    manifest: skill.yaml
---

# Office Automation

Use this skill when the user asks to analyze spreadsheets, write reports,
create slides, or turn meeting notes into office artifacts.
```

### 10.4 `skill.yaml`

```yaml
name: office-automation
version: 0.1.0
description: Generate reports and slides from spreadsheets and notes.
runtime:
  min_nanobot_version: "0.2.1"
  compatible_channels:
    - websocket
inputs:
  extensions:
    - .xlsx
    - .csv
    - .md
    - .txt
outputs:
  artifacts:
    - verified_facts
    - report_dsl
    - slide_dsl
    - docx
    - pptx
    - quality_report
tools:
  # MVP 脚本优先：skill 实际依赖的是这些内置工具
  required:
    - exec          # 运行 scripts/*.py
    - read_file
    - write_file
  # 某操作提升为原生工具后再追加（见 §7.4）：
  # - office.render_docx
  # - office.render_pptx
permissions:
  file_read: workspace_only
  file_write: artifacts_only
  shell_exec: denied
  network_access: denied_by_default
  email_send: requires_human_approval
context:
  max_skill_tokens: 6000
  load_strategy: progressive
artifacts:
  store: workspace_runtime
  lineage: required
evals:
  smoke:
    - office_weekly_report_minimal
  metrics:
    - artifact_completion
    - data_consistency
    - format_quality
    - policy_compliance
```

### 10.5 生命周期

MVP 不做远程安装，先做本地 registry：

1. Discover：扫描 builtin/workspace skills。
2. Parse：读取 `SKILL.md` frontmatter 和可选 `skill.yaml`。
3. Validate：schema、版本、依赖、权限字段校验。
4. Register：写入 skill registry cache。
5. Select：根据 task intake 匹配候选 skill。
6. Load：按需加载主说明、workflow、schema。
7. Execute：通过 Tool Gateway 执行工具。
8. Evaluate：运行 skill 自带 smoke eval。
9. Observe：记录 skill success rate、失败类型、成本。
10. Disable/Rollback：禁用或回退到上一版本。

未来可以加：

```text
nanobot skill list
nanobot skill inspect office-automation
nanobot skill eval office-automation
nanobot skill disable office-automation
```

这些命令是未来挂到现有 `nanobot` CLI，不使用 `mybot skill`。

### 10.6 SkillPack 治理原则

| 治理点 | 设计 |
|---|---|
| 最小权限 | Skill 只能声明完成任务需要的权限 |
| 显式工具依赖 | required tools 缺失时不可用 |
| 版本兼容 | 声明 runtime/tool/schema 版本 |
| 隔离加载 | prompt、policy、eval 不污染其他 skill |
| 启用前评测 | 安装/升级后先跑 smoke eval |
| 可回滚 | 保存上一版本 metadata |
| 可观测 | skill 级成功率、失败率、成本、权限拦截 |
| 供应链安全 | 记录来源、hash、作者、签名预留字段 |

---

## 11. Artifact Runtime

### 11.1 为什么需要 Artifact

Mybot 不应该只输出聊天文本。复杂任务需要产出可追踪文件：

| Skill | Artifact |
|---|---|
| Office | docx、pptx、verified_facts、quality_report |
| Code | patch、diff、test_report、changelog |
| Research | sources.json、notes.md、report.md |
| Data | analysis.json、chart.png、summary.md |

### 11.2 存储位置

MVP 推荐：

```text
<workspace>/.nanobot-runtime/
├── artifacts/
│   └── task_001/
├── traces/
│   └── task_001.jsonl
├── evals/
│   └── task_001.eval.json
├── checkpoints/
│   └── task_001/
└── index.sqlite 或 index.json
```

如果不希望污染项目目录，可放 `~/.nanobot/runtime/<workspace_hash>/`。但 WebUI 需要能根据 session/workspace 查到。

### 11.3 Artifact Metadata

```json
{
  "artifact_id": "art_001",
  "task_id": "task_001",
  "skill": "office-automation",
  "type": "pptx",
  "path": ".nanobot-runtime/artifacts/task_001/weekly_review.pptx",
  "version": 1,
  "source_artifacts": ["art_facts", "art_slide_dsl"],
  "tool_calls": ["tool_005"],
  "checksum": "sha256:...",
  "status": "validated",
  "created_at": "2026-06-16T10:00:00+08:00"
}
```

### 11.4 Artifact Graph

```text
sales_data.xlsx
meeting_notes.md
  -> workbook_schema.json
  -> verified_facts.json
  -> report_dsl.json
  -> weekly_report.docx
  -> slide_dsl.json
  -> weekly_review.pptx
  -> quality_report.json
```

价值：

- 结果可追溯。
- 失败可定位。
- 局部重生成。
- 版本 diff。
- 用户追问“结论从哪里来”时可解释。
- eval 可复用中间产物。

### 11.5 Delta Engine

Delta 修改避免整体重做：

| 场景 | Delta |
|---|---|
| PPT | 只重写第 3 页或某个 slide block |
| Word | 只重写某一节 |
| Excel 分析 | 只重算某个指标 |
| Research | 只替换一个来源 |
| Code | 只改一个文件/函数 |

Office MVP 的 delta 可以先从 DSL 粒度做：

```json
{
  "target": "slide:3",
  "operation": "rewrite",
  "reason": "用户要求突出华东区域风险",
  "input_artifacts": ["slide_dsl.json", "verified_facts.json"],
  "output_artifacts": ["slide_dsl.v2.json", "weekly_review.v2.pptx"]
}
```

---

## 12. Event Log、Trace Replay 与 Observability

### 12.1 Event Log

所有关键状态变化写 JSONL：

```json
{"type":"TaskCreated","task_id":"task_001","goal":"生成销售周报"}
{"type":"SkillSelected","skill":"office-automation"}
{"type":"PlanCreated","steps":7}
{"type":"PermissionChecked","tool":"office.extract_facts","allowed":true}
{"type":"ToolCallStarted","tool":"office.extract_facts","tool_call_id":"tool_001"}
{"type":"ToolCallCompleted","tool_call_id":"tool_001","duration_ms":230}
{"type":"ArtifactCreated","artifact_id":"art_facts","type":"verified_facts"}
{"type":"EvaluationCompleted","passed":true}
{"type":"TaskCompleted","task_id":"task_001"}
```

### 12.2 Trace Span

Trace 更适合表示层级关系：

```text
trace: office_weekly_report
  span: intake
  span: planning
  span: tool office.inspect_workbook
  span: tool office.extract_facts
  span: llm build_report_dsl
  span: tool office.render_docx
  span: tool office.render_pptx
  span: eval
```

Trace span 字段：

| 字段 | 说明 |
|---|---|
| trace_id | 一次端到端任务 |
| span_id | 单个阶段 |
| parent_span_id | 层级关系 |
| name | 阶段名 |
| actor | user / agent / tool / evaluator |
| start/end | 时间 |
| status | ok / error / cancelled |
| input_summary | 输入摘要，默认脱敏 |
| output_summary | 输出摘要，默认脱敏 |
| artifacts | 关联产物 |
| permission_decision | 关联权限 |

### 12.3 Replay

Replay 分三层：

| 层级 | 作用 |
|---|---|
| View Replay | 只展示 trace、tool、artifact，不重新执行 |
| Deterministic Replay | 跳过 LLM，复用已保存 DSL/工具结果重新渲染/eval |
| Full Replay | 重新调用模型和工具，用于回归对比 |

MVP 做 View Replay 即可，后续再做 deterministic/full replay。

### 12.4 Observability Dashboard

WebUI 或报告展示：

| 指标 | 价值 |
|---|---|
| Task Success Rate | 整体任务稳定性 |
| Skill Success Rate | skill 质量对比 |
| Tool Success Rate | 工具失败热点 |
| Permission Blocks | 高风险动作拦截 |
| Eval Score Trend | prompt/tool 修改后质量变化 |
| Avg Tool Calls | 成本和效率 |
| Avg Latency | 响应性能 |
| Token Usage | 成本控制 |
| Artifact Lineage Completeness | 产物可追溯 |
| Checkpoint Recovery Rate | 恢复能力 |

---

## 13. Checkpoint 与失败恢复

> **承接点**：runner 已通过 `AgentRunSpec.checkpoint_callback` 发射 `awaiting_tools`/`tools_completed`/`final_response` 三阶段 checkpoint（含 assistant message + 已完成/待执行工具）。二开**不另造发射机制**，只在该回调里给 payload 补 stage/artifact 元数据并落盘，恢复时读回。

### 13.1 Checkpoint 粒度

Office 任务 checkpoint：

1. 输入文件解析完成。
2. workbook schema 生成完成。
3. verified facts 生成完成。
4. report/slide DSL 生成完成。
5. docx/pptx 渲染完成。
6. eval 完成。

### 13.2 Checkpoint Metadata

```json
{
  "checkpoint_id": "ckpt_003",
  "task_id": "task_001",
  "stage": "verified_facts_ready",
  "artifacts": ["art_facts"],
  "state_hash": "sha256:...",
  "created_at": "2026-06-16T10:00:00+08:00",
  "resume_instructions": "从 verified_facts 继续生成 report_dsl 和 slide_dsl"
}
```

### 13.3 失败处理

| 失败类型 | 处理 |
|---|---|
| 输入缺失 | 请求用户补充 |
| schema 校验失败 | 让模型修复 DSL |
| 工具异常 | 局部重试，必要时降级 |
| 权限拒绝 | 解释原因，提供可选路径 |
| eval 失败 | 生成 repair plan |
| 模型输出不稳定 | 使用结构化 schema + retry |
| 长任务超时 | 从 checkpoint 恢复 |

---

## 14. Office Automation Skill Pack

### 14.1 样板场景

核心样板：

> 会议纪要 + Excel 销售数据 -> 周报 Word + 汇报 PPT

输入：

- `meeting_notes.md`
- `sales_data.xlsx`
- 用户自然语言约束。

输出：

- `workbook_schema.json`
- `verified_facts.json`
- `meeting_summary.json`
- `report_dsl.json`
- `slide_dsl.json`
- `weekly_report.docx`
- `weekly_review.pptx`
- `quality_report.json`
- `trace.jsonl`

### 14.2 执行流程

```text
Task Intake
  -> 匹配 office-automation skill
  -> 检查输入文件和权限
  -> inspect workbook
  -> extract verified facts
  -> summarize meeting notes
  -> generate report outline
  -> generate report_dsl
  -> generate slide_dsl
  -> optional user outline approval
  -> render docx / pptx
  -> run Office eval
  -> repair if needed
  -> deliver artifacts
```

### 14.3 Verified Facts

LLM 不能直接编写关键数字，必须引用事实表：

```json
{
  "fact_id": "f_gmv_week",
  "name": "本周 GMV",
  "value": 1234000,
  "display_value": "123.4 万元",
  "unit": "CNY",
  "source": {
    "file": "sales_data.xlsx",
    "sheet": "销售明细",
    "columns": ["gmv"],
    "calculation": "sum(gmv)"
  },
  "confidence": 1.0
}
```

规则：

- 文档/PPT 中出现的关键数字必须引用 `fact_id`。
- `display_value` 由工具格式化，不由模型随意改。
- eval 检查引用完整性和数值一致性。

### 14.4 Report DSL

```json
{
  "title": "本周销售复盘",
  "sections": [
    {
      "id": "sec_summary",
      "title": "核心结论",
      "blocks": [
        {
          "type": "paragraph",
          "text": "本周 GMV 达到 {{fact:f_gmv_week.display_value}}。",
          "fact_refs": ["f_gmv_week"]
        }
      ]
    }
  ]
}
```

### 14.5 Slide DSL

```json
{
  "deck_title": "本周销售复盘",
  "slides": [
    {
      "id": "slide_01",
      "layout": "title_metrics",
      "title": "本周核心指标",
      "metrics": [
        {
          "label": "GMV",
          "fact_ref": "f_gmv_week"
        }
      ],
      "speaker_notes": "强调 GMV 环比变化和区域差异。"
    }
  ]
}
```

### 14.6 Renderer 原则

Renderer 负责版式，不让 LLM 直接操作底层 docx/pptx API。

| 原则 | 说明 |
|---|---|
| 模板优先 | 支持用户提供模板，默认模板可用 |
| 样式固定 | 标题、正文、表格、图表样式由 renderer 控制 |
| 内容限长 | 渲染前检查每页/每段文字长度 |
| 可打开校验 | 生成后用库重新打开检查 |
| 可预览 | WebUI file preview 或后续缩略图 |
| 可重渲染 | DSL 相同则输出稳定 |

### 14.7 Layout Validator

PPT 检查：

- 页数限制。
- 每页标题非空。
- 每页文本块数量。
- 单页字数阈值。
- 图表/指标卡数量。
- fact_ref 完整性。
- 图片/图表是否缺失。

Word 检查：

- 标题层级。
- 必要章节存在。
- 表格可读。
- 数字引用。
- 段落过长提醒。

### 14.8 Human-in-the-loop

触发人工确认：

| 场景 | 行为 |
|---|---|
| 输入文件不明确 | 询问用户选择 |
| 受众/风格缺失但影响很大 | 询问或使用默认 |
| 邮件发送 | 必须确认 |
| 覆盖原文件 | 必须确认 |
| 读取 workspace 外文件 | 默认拒绝，提示移动到 workspace |
| 数据异常波动 | 展示异常并询问是否需要解释 |
| 大纲生成后 | 可选确认，用户可跳过 |

### 14.9 Office Eval

| 指标 | 方法 |
|---|---|
| artifact_completion | 检查所有 expected artifacts |
| file_openable | 用 `python-docx` / `python-pptx` 打开 |
| data_consistency | fact_ref 与 verified facts 对齐 |
| format_quality | 页数、标题、文本长度、结构 |
| meeting_coverage | 覆盖结论、风险、行动项 |
| policy_compliance | trace 中无越权动作 |
| delta_correctness | 局部修改只影响目标 section/slide |

### 14.10 Office MVP 最小 Demo

MVP 只需一个稳定 demo：

```text
tests/fixtures/office_weekly/
├── sales_data.xlsx
├── meeting_notes.md
├── expected_metrics.json
└── expected_constraints.json
```

运行后生成：

```text
.nanobot-runtime/artifacts/task_x/
├── verified_facts.json
├── report_dsl.json
├── slide_dsl.json
├── weekly_report.docx
├── weekly_review.pptx
└── quality_report.json
```

---

## 15. 分层 Eval Harness

### 15.1 为什么要分层

通用 Agent 很难用一个分数评估。Mybot 应分三层：

```text
总评估 = Harness Eval + Skill Eval + Task Eval
```

### 15.2 Harness Eval

不依赖具体 skill：

| 指标 | 含义 |
|---|---|
| task_success_rate | 任务是否完成 |
| trace_complete_rate | trace 是否完整 |
| tool_success_rate | 工具调用成功率 |
| permission_violation_rate | 是否出现越权 |
| blocked_action_accuracy | 高风险动作是否正确拦截 |
| checkpoint_recovery_rate | 是否可恢复 |
| artifact_lineage_complete | 产物血缘是否完整 |
| budget_compliance | 是否遵守 token/时间/工具次数 |
| human_approval_precision | 人工确认触发是否合理 |

### 15.3 Skill Eval

| Skill | 指标 |
|---|---|
| Office | 数据一致性、格式质量、产物完整性、局部修改 |
| Code | 测试通过、diff 最小、接口不破坏 |
| Research | 引用准确、来源可信、事实覆盖 |
| Data | 计算准确、图表正确、结论有数据支持 |

### 15.4 Task Eval Case

```json
{
  "case_id": "office_001",
  "skill": "office-automation",
  "task": "根据 sales.xlsx 和 meeting.md 生成周报和 6 页以内 PPT",
  "input_artifacts": ["sales.xlsx", "meeting.md"],
  "expected_artifacts": ["weekly_report.docx", "weekly_review.pptx"],
  "constraints": [
    "PPT 不超过 6 页",
    "必须包含 GMV、订单量、转化率",
    "所有数字必须来自 Excel"
  ],
  "evaluators": [
    "artifact_completion",
    "file_openable",
    "data_consistency",
    "format_quality",
    "policy_compliance"
  ]
}
```

### 15.5 LLM Judge 使用边界

LLM Judge 可以评：

- 逻辑是否顺畅。
- 会议要点是否覆盖。
- 汇报文案是否自然。
- 受众风格是否匹配。

LLM Judge 不应单独评：

- 数字是否正确。
- 权限是否合规。
- 文件是否生成。
- 是否越权读取。
- 是否发送邮件。

### 15.6 Agent CI

每次改这些内容后跑 smoke eval：

- prompt。
- skill workflow。
- tool adapter。
- permission policy。
- renderer。
- evaluator。

输出：

- 成功率变化。
- 成本变化。
- 失败 case。
- trace 对比。
- 质量退化指标。

---

## 16. WebUI 产品化设计

### 16.1 现有入口

当前 WebUI 重点保留：

- Chat 主界面。
- 对话框右下角模型下拉。
- Settings 中模型/provider/MCP 配置。
- Workspace 控制。
- File preview。

### 16.2 新增页面

| 页面 | MVP 内容 | 后续增强 |
|---|---|---|
| Skills | skill list、可用性、描述、来源 | enable/disable、eval、版本 |
| Artifacts | 当前任务产物列表、下载、预览 | lineage graph、diff |
| Trace | JSONL 时间线 | span tree、replay |
| Eval | quality_report 展示 | 趋势、case 对比 |
| Permissions | 工具风险、ask/allow/deny | 规则编辑、审批历史 |

### 16.3 Chat 内交互

用户体验：

- 上传或引用 Excel/会议纪要。
- Mybot 自动识别 Office Skill。
- 缺信息时简短追问。
- 生成大纲后可确认或跳过。
- 产物生成后展示文件卡片。
- 质量报告作为可展开面板。
- 用户说“改第 3 页”，触发 delta。

---

## 17. 数据结构

### 17.1 核心结构

| 数据结构 | 关键字段 | 作用 |
|---|---|---|
| Task | task_id、goal、status、workspace、created_at | 任务主体 |
| TaskState | phase、current_step、constraints、budgets | 状态机 |
| SkillManifest | name、version、tools、permissions、evals | SkillPack 声明 |
| Plan | plan_id、steps、created_by、status | 执行计划 |
| PlanStep | step_id、inputs、outputs、criteria、risk | 计划步骤 |
| Handoff | from、to、scope、constraints、expected_output | 结构化交接 |
| ToolCall | tool、arguments_summary、status、latency、error | 工具审计 |
| PermissionDecision | action、risk、allowed、reason、rules | 权限决策 |
| Artifact | id、type、path、version、checksum、status | 产物 |
| ArtifactEdge | from、to、relation、step_id | 血缘 |
| RuntimeEvent | type、actor、timestamp、payload | 事件日志 |
| TraceSpan | span_id、parent、name、status、duration | 可观测 |
| EvalResult | metrics、issues、passed、suggestions | 评测结果 |

### 17.2 配置结构

> **对齐 `.agent/design.md` "Explicit over magical"**：所有新增配置**必须**是 `config/schema.py` 的 Pydantic 模型（camelCase 别名），不是裸 JSON 约定。**skill 启用/禁用复用已有的 `agents.defaults.disabledSkills`，勿新建 `skills.disabled`。**

新增字段（Pydantic 模型，示意 JSON）：

```json
{
  "runtime": {
    "artifactStore": "workspace",
    "traceEnabled": true,
    "evalSmokeOnSkillLoad": false
  },
  "permissions": {
    "mode": "default",
    "deny": ["shell.delete", "file.writeOutsideWorkspace"],
    "ask": ["shell.exec", "network.remote", "email.send"],
    "allow": ["office.extract_facts", "office.validate"]
  },
  "agents": {
    "defaults": {
      "disabledSkills": []
    }
  }
}
```

注意：模型/provider 配置仍沿用当前 settings API 和 `modelPreset`；`disabledSkills` 已存在于 `AgentDefaults`，直接复用。

---

## 18. 技术栈建议

### 18.1 MVP 技术栈

| 模块 | 技术 |
|---|---|
| 后端 | 现有 nanobot + asyncio |
| WebUI | 现有 React/Vite/Tailwind |
| 配置 | Pydantic + `~/.nanobot/config.json` |
| Trace | JSONL |
| Artifact index | JSON，后续 SQLite |
| Office 读取 | `openpyxl` |
| Word 渲染 | `python-docx` |
| PPT 渲染 | `python-pptx` |
| PDF/预览 | `pypdf`，后续可选 PyMuPDF |
| Schema | JSON Schema / Pydantic |
| Eval | pytest + 自定义 eval runner |

### 18.2 后续增强

| 模块 | 技术 |
|---|---|
| Artifact index | SQLite |
| Trace processor | OpenTelemetry 风格 exporter |
| Dashboard | WebUI trace/eval 页面 |
| Sandbox | macOS/Linux 原生限制或容器化隔离 |
| Long tasks | durable task queue |
| Memory | workspace 隔离检索，后续向量库 |
| Office preview | LibreOffice/headless 或云端预览可选 |

---

## 19. 实施路线图

> **更细的逐步拆解见 `docs/plans/2026-06-16-agent-runtime增量开发计划.md`**（每个里程碑切成 1–3 天可完成、各自有验收的小步）。本节只给里程碑级目标。

### M0：方案与架构对齐

目标：文档符合当前项目，保留最佳实践。

交付：

- 更新本方案。
- AGENTS 修改记录同步。
- 明确当前基线、二开目标、远期增强。

### M1：Office Skill MVP（脚本优先，仅用已有设施）

目标：跑通一个真实 Office 自动化闭环，**不碰 loop/runner，不加新配置，不建新包**。

交付：

- `nanobot/skills/office-automation/SKILL.md`（含 workflow 与 verified facts 规则）
- `scripts/`：`inspect_workbook.py`、`extract_facts.py`、`render_docx.py`、`render_pptx.py`、`validate.py`
- `references/`：`verified_facts.schema.json`、`report_dsl.schema.json`、`slide_dsl.schema.json`
- `assets/`：默认 docx/pptx 模板
- `tests/fixtures/office_weekly/` 固定 fixture + 一个 pytest 确定性 eval

验收：

- 输入 Excel + 会议纪要，生成 docx/pptx/json（脚本经 `exec` 运行）。
- 所有关键数字来自 verified facts。
- 文件可用 `python-docx`/`python-pptx` 重新打开。
- pytest 绿（artifact_completion / file_openable / data_consistency）。

> trace JSONL 完整性属于 M5，不在 M1 验收内。

### M2：SkillPack Registry

目标：让 Skill 变成可治理能力包。

交付：

- `SkillsLoader` 读取 `skill.yaml`。
- manifest schema 校验。
- skill dependency check。
- WebUI skills API 展示 metadata。
- skill enable/disable 配置。

验收：

- 缺工具/依赖时 skill unavailable。
- 禁用 skill 后不会被选中。
- manifest 错误给出可读错误。

### M3：Permission Kernel

目标：工具调用前后有统一权限和审计。

交付：

- ToolMetadata。
- PermissionDecision。
- PreToolUse/PostToolUse hook。
- file/network/shell/office 风险规则。
- WebUI 展示权限拦截。

验收：

- Office 默认不能 shell。
- workspace 外读写被拦截。
- artifacts 写入允许。
- 高风险动作触发确认。

### M4：Artifact Graph 与 Checkpoint

目标：复杂任务可追踪、可局部恢复。

交付：

- artifact metadata。
- lineage graph。
- checkpoint metadata。
- delta 修改接口。
- WebUI artifacts 页面。

验收：

- docx/pptx 可追溯到 verified facts 和输入文件。
- 工具失败后可从 checkpoint 继续。
- “只改第 3 页”不重做全流程。

### M5：Eval Harness 与 Agent CI

目标：用评测支撑后续迭代。

交付：

- eval runner。
- metric plugins。
- JSON/Markdown eval report。
- pytest smoke eval。
- WebUI eval 页面。

验收：

- prompt/tool/renderer 修改后可跑回归。
- 输出成功率、失败原因、质量指标。
- 至少覆盖 5 个 Office cases。

### M6：扩展 Code/Research Skill

目标：证明 Runtime 通用性。

交付：

- Code Skill：diff、test、lint、change summary。
- Research Skill：source collection、citation、report。
- 复用 artifact/trace/eval/permission。

---

## 20. 测试策略

### 20.1 单元测试

| 模块 | 测试 |
|---|---|
| SkillsLoader | manifest 解析、依赖检查、禁用逻辑 |
| Permission Kernel | deny/ask/allow、路径、shell、network |
| Office Excel | 指标计算、字段缺失、异常值 |
| DSL Schema | 合法/非法 report_dsl、slide_dsl |
| Renderer | docx/pptx 可打开、基础结构 |
| Artifact Store | metadata、checksum、lineage |
| Eval Metrics | completion、consistency、format |

### 20.2 集成测试

- 固定 Office fixture 全流程。
- MCP 工具注册和权限过滤。
- WebUI settings skill 开关。
- workspace 限制下的文件预览和 artifacts 读取。
- checkpoint 恢复。

### 20.3 安全测试

- prompt injection 文档输入。
- tool description poisoning。
- `.env` 和 secret 文件读取。
- workspace 外路径。
- shell 删除命令。
- 远程 MCP server 不可信工具。
- 过大文件和 token 预算。

---

## 21. Demo 与展示

### 21.1 Demo 顺序

1. 打开 WebUI，展示当前模型和 workspace。
2. 用户提交 Excel + 会议纪要任务。
3. Mybot 识别 Office Skill。
4. 展示计划和需要的产物。
5. 运行 Excel 分析，生成 verified facts。
6. 生成 Word/PPT DSL。
7. 渲染 Word/PPT。
8. 展示 quality_report。
9. 展示 Artifact Graph。
10. 用户要求“把第 3 页改成突出华东区域风险”。
11. 只重写对应 slide 并重新渲染。
12. 展示 trace replay 或 JSONL 时间线。
13. 运行 Office eval case。

### 21.2 README 展示结构

```text
Mybot
├── 项目简介
├── 当前 nanobot 二开基线
├── Runtime 架构
├── Skill Pack 协议
├── Tool Gateway 与 MCP
├── Permission Kernel
├── Context Engine
├── Artifact Graph
├── Trace / Checkpoint
├── Eval Harness
├── Office Automation Skill
├── Demo
└── Roadmap
```

### 21.3 简历表述

> 基于 nanobot v0.2.1 二次开发个人 Agent Runtime，复用其 WebSocket WebUI、MessageBus、AgentLoop、工具系统、MCP 接入和 workspace 安全策略，在此基础上设计 Skill Pack 插件协议、Permission Kernel、Context Engine、Artifact Graph、Trace Replay、Checkpoint 和分层 Eval Harness。以 Office Automation Skill Pack 作为样板，支持从 Excel/会议纪要生成 Word/PPT，通过 Verified Facts、Office DSL 和确定性评测保证数据一致性、产物质量和可复盘性。

### 21.4 技术亮点

- 在现有 AgentLoop/AgentRunner 周围增强 Runtime，而不是重写执行链路。
- 兼容 `SKILL.md`，扩展 `skill.yaml` 成为可治理 SkillPack。
- MCP 工具纳入 Tool Gateway，增加 trust、permission、audit。
- Permission Kernel 把 prompt 约束升级为代码层 enforcement。
- Context Engine 使用 progressive loading，避免上下文膨胀。
- Verified Facts + Office DSL 降低 LLM 编造数字和版式不稳定。
- Artifact Graph 支持追溯、局部修改和失败定位。
- Trace/Checkpoint 支持长任务恢复和复盘。
- 分层 Eval Harness 解决通用 Agent 难评估问题。

---

## 22. 风险与规避

| 风险 | 表现 | 规避 |
|---|---|---|
| 方案过大 | 一次性做所有 Runtime 能力 | 严格按 M1-M6 分期 |
| 和当前代码脱节 | 写成新 `mybot/` 平台 | 所有模块映射到 `nanobot/` |
| 重复造轮子 | 重新做 tool/skill/mcp | 复用现有 registry/loader/mcp |
| 安全承诺过度 | 文档说完整权限内核，代码只有路径限制 | 明确已有基线和二开目标 |
| Office 质量不稳定 | PPT 溢出、Word 样式乱 | DSL + renderer + validator |
| LLM Judge 不可靠 | 分数不可复现 | 规则校验优先 |
| 依赖膨胀 | 引入重型渲染/浏览器依赖 | 优先使用已有依赖 |
| WebUI 过早复杂 | 还没闭环就做 dashboard | 先 JSON/Markdown report |
| MCP 供应链风险 | 不可信 server 注入恶意 tool 描述 | trust level、静态校验、权限确认 |

---

## 23. 参考资料与对齐点

| 来源 | 对齐点 |
|---|---|
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) | Agent loop、tools、handoffs、guardrails、sessions、tracing、human-in-the-loop |
| [OpenAI Agents SDK Guardrails](https://openai.github.io/openai-agents-python/guardrails/) | input/output/tool guardrails、tripwire、blocking vs parallel checks |
| [OpenAI Agents SDK Handoffs](https://openai.github.io/openai-agents-python/handoffs/) | specialist agent 结构化交接 |
| [OpenAI Agents SDK Tracing](https://openai.github.io/openai-agents-python/tracing/) | trace/span 结构、敏感数据控制 |
| [OpenAI Agents SDK Sessions](https://openai.github.io/openai-agents-python/sessions/) | 会话持久化、恢复、历史裁剪 |
| [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro) | MCP 作为 AI 应用连接外部工具和数据的标准协议 |
| [MCP Specification](https://modelcontextprotocol.io/specification/2025-06-18) | resources、prompts、tools、roots、sampling、elicitation、安全原则 |
| [Claude Code Permissions](https://code.claude.com/docs/en/permissions) | deny/ask/allow、permission modes、shell/file/web/MCP 权限 |
| [Claude Code Hooks](https://code.claude.com/docs/en/hooks) | PreToolUse、PostToolUse、PermissionRequest 生命周期 hook |
| [Claude Code Skills](https://code.claude.com/docs/en/skills) | `SKILL.md`、frontmatter、supporting files、skill 可见性 |
| [OWASP Top 10 for LLMs and Gen AI Apps 2025](https://genai.owasp.org/llm-top-10/) | Prompt injection、sensitive disclosure、supply chain、excessive agency 等安全风险 |

---

## 24. 最终建议

Mybot 二开的正确方向不是“缩成一个简单办公助手”，也不是“脱离当前代码重写一个大平台”。更好的路线是：

1. **保留当前 nanobot 基线**：WebSocket WebUI、AgentLoop、ToolRegistry、MCP、SkillsLoader、workspace policy。
2. **补 Runtime 工程层**：Task State、Permission Kernel、Context Engine、Artifact Graph、Trace、Checkpoint、Eval。
3. **用 Office Automation 做样板闭环**：复杂、多工具、多产物、可评测、可局部修改。
4. **再扩展 Code/Research/Data Skill**：证明 Runtime 的通用性。
5. **每阶段都有可运行 demo 和自动化验收**：避免方案停留在概念层。

最终原则：

> 当前已有的基础设施不要重复造；原方案中更好的工程实践要保留，并映射到当前 Mybot 的真实代码路径和分阶段落地计划。
