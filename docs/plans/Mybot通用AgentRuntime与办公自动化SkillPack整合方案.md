# Mybot 通用 Agent Runtime 与 Office Skill Pack 整合方案

> 当前基线：2026-07-20。历史修订统一见 `docs/修改记录.md`，本文只保留当前有效决策。

## 1. 定位与目标

Mybot 基于 nanobot v0.2.1 二次开发，不重写 Agent 框架，而是在现有 `AgentLoop`、`AgentRunner`、MessageBus、WebSocket、工具和 Skill 体系上增加一层可治理 Runtime。

目标是证明四件事：

1. **能扩展**：通过 Skill Pack 增加领域能力，不在核心循环写领域私有分支。
2. **能治理**：工具、文件、网络、MCP 和 Subagent 受统一权限、HITL、生命周期熔断与硬边界约束。
3. **能交付**：输入、计划、事实、中间产物和最终文件可追踪、可验证、可恢复。
4. **能证明**：关键行为有确定性测试、trace、指标和可复现 demo。

Office 是首个验证领域，不是产品唯一方向：

- `office-automation`：Python grounded report/deck 工作流。
- `officecli`：固定版本 OfficeCLI 的通用 Office 能力。
- 两者共享 verified facts、输入快照和 Runtime 治理，但不强制共享 DSL。

## 2. 文档权威顺序与 AI 执行规则

后续 AI 必须按以下顺序判断，不得从旧表述自行扩 scope：

1. 本文：项目边界、阶段顺序、必做/选做、全局不变量。
2. `docs/plans/runtime-steps/P*.md`：对应阶段的实施接口、测试和出口。
3. `docs/runtime-code-notes/`：真实代码状态；明确标注“仅规划”的内容不能当成已实现。
4. `docs/修改记录.md`：历史演进，不覆盖当前方案。

执行约束：

- 开始阶段前先确认依赖阶段出口；未满足时不得跨阶段补“大而全”设施。
- 必做项完成并通过出口测试前，不实现选做项。
- 只做最小接线，不重写 AgentLoop/Runner；允许为 typed suspension、policy gate、checkpoint 等增加明确的小接口。
- Skill/manifest 只能声明需求，不能授予权限或放宽 workspace、SSRF、敏感信息硬边界。

- 涉及 P0-P8 的方案或代码变化，同步更新 `docs/修改记录.md` 和对应 runtime code note。

## 3. 全局架构与代码归属

```text
WebUI / WebSocket
        ↓
MessageBus
        ↓
AgentLoop → AgentRunner → ToolRegistry
                         ↘ Skills / MCP / Subagents
        ↓
nanobot/runtime/
  policy.py          allow / ask / deny
  interactions.py    三档 HITL 与 deadline 恢复
  approvals.py       参数绑定安全审批
  artifacts.py       输入快照、产物与血缘
  checkpoint.py      计划任务安全恢复
  trace.py           JSONL / OTel 风格 trace
  replay.py          轻量 cassette
  evals/             确定性评测与报告

nanobot/security/sandbox/
  manager.py         sandbox mode、provider capability 与 fail-closed
  launcher.py        Agent 触发子进程的统一启动边界
  seatbelt.py        macOS Seatbelt provider
  bwrap.py           Linux / WSL2 Bubblewrap provider
  network.py         默认断网、严格 fetch argv、域名/端口/DNS IP 绑定与审计
```

硬边界继续归属 `nanobot/security/`。P3 在现有 `WorkspaceScope`、workspace path guard、SSRF 和 `agent/tools/sandbox.py` 基础上补齐 OS 强制沙箱，不在 `nanobot/runtime/` 平行重建路径判断。`nanobot/runtime/` 负责策略、状态、审计和恢复；WebUI 现有 Default Permission / Full Access 继续作为会话级 access profile，并确定性映射为 sandbox/policy 组合。

参考 Codex，安全控制拆为三个正交轴，禁止混称：

1. `sandbox_mode`：技术上能访问哪些文件、网络和进程资源。
2. `approval_policy`：越过当前边界时是询问、拒绝还是按既有规则继续。
3. `approvals_reviewer`：需要 approval 时由用户还是 reviewer Agent 审核。

“替我审批”只改变第 3 轴，不授予权限、不扩大 writable roots、不打开网络，也不替代沙箱。P3 必做 reviewer 只有 `user`；`auto_review` 必须等手动 approval、trace 和安全 eval 稳定后再作为选做项评估。

## 4. 不可破坏的设计契约

### 4.1 Skill 与 Office 边界

- `SKILL.md` 保持兼容；可选 `skill.yaml` 声明版本、依赖、权限需求、产物和 eval。
- manifest 缺失时兼容旧 Skill；manifest 存在但损坏时仅该 Skill fail closed。
- `disabledSkills` 是唯一启用/禁用入口，不建立平行配置。
- WebUI 开关写入 `disabledSkills` 后应热刷新主 Agent 与子代理；只影响后续回合，不要求重启网关。
- 用户可在单轮消息中用 `@skill-name` 显式指定可用 Skill；运行时必须校验其可用性并把正文作为本轮路由契约加载。未指定时，继续采用摘要 + 模型渐进选择。
- 普通 Office 请求默认优先 `officecli`；用户明确要求 Python 时使用 `office-automation`。
- OfficeCLI 版本、平台资产和 checksum 只有 provider contract 一个真相源；Mybot 安装的同名 launcher 可在首次使用时自动准备并校验固定资产，Agent 任务不得调用上游 latest/install/update。
- 定量结论必须来自 `verified_facts.json`；纯格式、提取和批注任务不强制跑事实层。

### 4.2 Plan 契约

- 静态内建 `plan` 工具固定提供 `create/get/confirm/update_step/complete`。
- plan hash 只覆盖不可变契约；修改计划后旧确认失效。
- WebUI `execution_mode=plan_only` 只开放 plan 与只读检查工具；plan 记录停在 `awaiting_confirmation`，Runtime turn 以 `awaiting_plan_confirmation` 持久化挂起，同回合不得 confirm 或执行。
- 普通 WebUI 复杂任务 create 后可自动激活；激活时 `approved_plan_hash` 必须等于当前 plan hash，并记录 `approval.mode=automatic`。
- 手动/仅规划计划必须显式确认后激活；步骤依赖和 expected artifacts 由工具硬校验。
- 自动激活只表示允许按计划推进，不批准高风险工具；外发和远程写仍独立经过 P3 policy/approval。本地已有文件写入与高风险本地 Shell 是否 ask 由当前 WebUI access profile 决定，但无论何种 profile 都必须经过 P3 OCC / hard deny。
- plan 是 artifact 和后续 checkpoint 的根；动态摘要只放用户消息尾部 Runtime Context，工具定义保持稳定以利缓存。

### 4.3 Sandbox、Policy 与三档 HITL

- `sandbox_mode` 使用 `read_only|workspace_write|danger_full_access`：plan-only 映射 `read_only`，Default Permission 映射 `workspace_write`，Full Access 映射 `danger_full_access`。
- `workspace_write` 必须由 OS provider 强制：macOS 使用 Seatbelt，Linux/WSL2 使用 Bubblewrap；原生 Windows 首版不宣称支持。provider 缺失时返回 `sandbox_unavailable`，不得静默无沙箱执行；用户只能显式切换 Full Access 或修复 provider。
- workspace 默认仅项目目录与当前 task artifact 目录可写；builtin Skill、上传媒体和必要系统运行库只读；`~/.nanobot`、凭据、Runtime interactions/checkpoint/trace 控制文件和项目 `.git` 默认不可由普通 sandbox 命令写入。
- sandbox 内命令默认断网。Core 网络例外只支持直接 `curl`：禁止 shell 组合、redirect、proxy/resolve/config/interface 等目标改写，批准绑定当前 tool call/command hash/domain/port/审批时公网 DNS 地址/expiry，并以 `--resolve` 固定目标；SSRF、内网、metadata 和敏感目标仍 hard deny。
- 沙箱覆盖 Agent 触发的 Shell 一次性/持久 session、CLI Apps 与 OfficeCLI 子进程；预配置 MCP server、channel bootstrap 和网关自身进程首版不纳入同一 OS 边界，必须在文档和 UI 标明，并继续受调用级 policy/SSRF 约束。
- `danger_full_access` 表示用户明确关闭本地 OS 文件/网络沙箱，不等于关闭 Runtime policy：消息、邮件、远程写、凭据访问、OCC 和 hard deny 仍独立生效。

- 工具调用先完成同步参数校验，再经过异步 policy gate：`allow / ask / deny`。
- P3 复用当前会话的 `WorkspaceScope`：Default Permission 保持 workspace 受限；Full Access 允许项目外的本地文件/Shell 访问，并作为已选择的本地操作预授权 profile，而非一次性工具 approval。
- 路径逃逸、受保护目录、SSRF、敏感信息等 hard deny 不能被配置、Skill 或用户审批放宽；workspace 外普通路径属于 sandbox escalation，只能由参数绑定的一次性 approval 最小放宽。
- Default Permission 下修改已有本地文件和高风险本地 Shell 默认 ask；Full Access 下这两类本地操作可 allow，但仍受 OCC、command deny pattern 和 hard deny 约束。消息/邮件、远程写和其他外部副作用在两种 profile 下都保持 ask；审批必须参数绑定且超时拒绝。
- `InteractionRequest` 统一承接 question、approval、需要人工确认的 plan confirmation、recovery decision：
  - `required`：没有明确回答就不继续。
  - `auto_resolve`：非阻塞偏好问题到 deadline 后使用确定性默认值，或让模型按最佳判断继续。
  - `expire_and_deny`：高风险审批超时后拒绝，绝不自动批准。
- 发出请求后当前 LLM 调用结束，task/turn 进入 `awaiting_*`；等待期 token 为 0。
- 回答和 deadline 竞争时只消费一次，并恢复原 task/turn 和 tool call；普通聊天不能隐式批准安全操作。
- WebSocket 只负责展示/提交，持久化 Runtime 状态是真相源。
- `approvals_reviewer=user` 是 P3 唯一必做路径；未来的 `auto_review` 只能处理本来会询问用户的有限 escalation，不得审核 hard deny、凭据读取、不可逆外发或关闭沙箱的请求，并必须记录独立 reviewer trace、理由、token 和结果。

### 4.4 文件安全

- `write_file`、`edit_file`、`apply_patch` 修改已有文件前必须有当前 actor 的 fresh-read snapshot。
- 即使 mtime 未变也比较 SHA-256；变化返回结构化 `file_conflict`。
- 多文件 patch 在任何写入前统一 preflight；任一冲突则零写入。
- P3 不承诺 shell 任意写盘拦截、新文件完整事务、fsync 或消除最终微小 TOCTOU。
- P8 必做依靠 child artifact 目录隔离 + actor-local OCC；共享 workspace 文件租约、`file_busy` UI 和跨路径等待队列均为选做。

### 4.5 Artifact 与恢复

- 任务输入默认复制到 `.nanobot-runtime/artifacts/<task_id>/inputs/`；无法复制时标记 `reference_only` 与 `replayable:false`。
- artifact 记录 checksum、类型、Skill、引擎、来源、tool call、child id 和验证状态。
- durable checkpoint 只服务已激活且 `approved_plan_hash` 绑定当前 hash 的复杂任务；激活来源可以是普通 WebUI 自动激活或显式确认。
- 工具恢复语义：
  - `completed`：已持久化，跳过。
  - `pending`：未执行或可安全重放。
  - `uncertain`：副作用可能发生，使用 `required` 人工决定。
- `awaiting_question|approval|plan_confirmation|recovery_decision` 是合法 suspension，不能恢复成工具失败。
- 不宣称通用 exactly-once。

### 4.6 Subagent 治理

- 每个父任务最多 5 个直接 child，禁止嵌套。
- 权限只能继承或收紧；child 不设置 token、总时长或工具调用配额，避免长任务因父 Agent 低估工作量而失败。
- 保留用户/父任务取消、网关关闭、单次 LLM 请求超时和 200 轮异常循环熔断；触发循环熔断时返回部分进展。
- child 只接收必要目标、约束和 artifact 引用，不复制完整父会话。
- child 默认只写自己的 artifact 子目录，父 Agent 负责事实共享、冲突处理和最终汇总。
- 任何使用 Subagent 的任务都要记录父子 trace，并与单 Agent 顺序执行比较成功率、时长和 token 成本。
- 文件租约未实现不阻塞 P8；如果实现，仅协调同一进程 Agent，不能替代 OCC。

## 5. 阶段路线图

| 阶段                                                                | 状态  | 必须交付                                              | 阶段出口                                          |
| ----------------------------------------------------------------- | --- | ------------------------------------------------- | --------------------------------------------- |
| [P0 准备](runtime-steps/P0-准备.md)                                   | 已完成 | 固定 Office fixture、Python 3.11 CI smoke            | fixture 可复算，workflow 可运行；远端状态以最新 Actions 记录为准 |
| [P1 Office 垂直切片](runtime-steps/P1-office垂直切片.md)                  | 已完成 | 双 Office Skill、共享 facts、仅规划/自动执行、计划步骤 UI       | 两条 Office 路径和两种计划模式可验证                         |
| [P2 Manifest](runtime-steps/P2-skillpack-manifest.md)             | 已执行 | typed manifest、局部 fail closed、availability、开关     | 坏 Skill 不拖垮网关且不能进入候选                          |
| [P3 Sandbox/Policy/HITL/OCC](runtime-steps/P3-policy权限层.md)       | 已完成 | OS sandbox、policy gate、三档 InteractionRequest、approval、文件 OCC | 无静默降级，硬边界不可绕过，等待可恢复，冲突拦截 100%             |
| [S5.0 轻量回放](runtime-steps/P5-trace-eval.md)                       | 已完成 | 4 个关键 cassette smoke                              | 无 API key、无网络即可回归关键 Agent 行为                  |
| [P4 Artifact/Checkpoint](runtime-steps/P4-artifact-checkpoint.md) | 已完成 | 输入快照、artifact/lineage、计划任务恢复                      | kill→resume 可验证，uncertain 不自动重试               |
| [P8 Subagent](runtime-steps/P8-多agent编排.md)                       | 已完成 | 数量/嵌套/权限/生命周期/上下文/产物治理                            | 父子 trace 完整并有单/多 Agent 对比                     |
| [P5 Core Trace/Eval](runtime-steps/P5-trace-eval.md)              | 已完成 | JSONL/OTel trace、确定性 eval、红队、报告                   | 安全/数字/文件硬门进入 CI/benchmark                     |
| [P6 Research](runtime-steps/P6-通用性扩展.md)                          | 待执行 | 1–2 天最小 Research Skill                            | 不改 Runtime 核心即可复用治理设施                         |
| [P7 交付物](runtime-steps/P7-面试交付物.md)                               | 持续  | benchmark、README、demo、答辩稿                         | 陌生人可复现，表述与实际完成度一致                             |

详细实施只看对应 `docs/plans/runtime-steps/P*.md`。

## 6. 顺序、cutline 与选做项

依赖顺序：

```text
P0 → P1 → P2 → P3 → S5.0 → P4 → P8 → P5 Core → P6 → P7
```

冻结前必做：

- P1、P2、P3、P4、P8。
- S5.0 的 3–4 个关键 cassette。
- P5 Core：trace、确定性 eval/report、安全红队。
- P6 Research 最小闭环。
- P7 benchmark、README、demo 和答辩稿。

每周只接受“完成一个可验证闭环”而不是多项各做 60%；阶段结束必须更新测试、指标和证据位置。

选做，主线未完成时必须砍掉：

1. 白盒记忆治理、artifact delta/staging。
2. LLM Judge / LLM-as-a-Verifier 离线 PoC。
3. 多模型成本矩阵与 KV cache 优化。
4. Subagent 共享 workspace 文件租约与冲突可视化。
5. `approvals_reviewer=auto_review`（“替我审批”）；手动 approval、sandbox、trace 和红队未完成前不得实现。

## 7. 硬门指标

| 指标                         | 目标              |
| -------------------------- | --------------- |
| 数字可追溯到 fact id             | 100%            |
| 计划步骤/承诺产物交付                | 100%            |
| 未批准的 workspace 外写入       | 0               |
| `workspace_write` 沙箱外文件写入 | 0               |
| 沙箱不可用时静默无沙箱执行          | 0               |
| 未批准的命令网络访问               | 0               |
| 注入诱导的越权副作用、敏感泄漏、未确认外发      | 0               |
| 已有文件冲突拦截率                  | 100%            |
| HITL 回答/超时/取消恢复正确率         | 100%            |
| `expire_and_deny` 超时自动放行   | 0               |
| checkpoint kill→resume 成功率 | 100%            |
| OfficeCLI OpenXML 校验       | 100%（登记的兼容例外除外） |
| CI 确定性 smoke               | < 60 秒          |

同时记录但不设虚假目标：任务成功率、P95、token/成本、缓存命中率、Subagent 成本与时长溢价、视觉质量。

确定性安全、数字、文件和副作用检查拥有最终否决权；LLM Judge/Verifier 只能评软质量，不能覆盖硬失败。

## 8. 交付与证明

固定 demo 链路：

```text
Skill 开关
→ read_only / workspace_write / danger_full_access
→ sandbox 网络/文件越界被拒绝或参数绑定审批
→ required/auto_resolve/expire_and_deny
→ 仅规划生成/显式执行 + 普通复杂任务自动 plan-and-execute
→ verified facts + Office 产物
→ approval 超时拒绝 + file_conflict
→ input snapshot + lineage
→ kill/resume
→ Subagent 父子 trace、取消与循环熔断
→ eval/benchmark
```

公开证据：

- `benchmarks/latest.md`：最新指标报告。
- `docs/plans/metrics-baseline.md`：历史趋势。
- README：定位、架构、quickstart、指标、设计取舍和无 key cassette 路径。
- 架构图和 diff 统计明确区分 nanobot 原有与 Mybot 二开。
- 简历和答辩只描述已完成且有测试/指标的能力。

## 9. 最终验收

项目完成时应满足：

- 新 Skill 可通过同一 manifest/loader/policy/artifact/trace/eval 接入。
- Default Permission 下 Agent 触发的命令默认由可验证的 OS sandbox 限制在 workspace，越界只能走最小参数绑定 approval，provider 不可用时不会静默降级；Full Access 仍不绕过外部副作用 policy、OCC 和 hard deny。
- 人机等待可跨刷新、断线和重启恢复，等待期模型不空转，危险审批超时不放行。
- 用户/IDE 修改不会被过期读取静默覆盖。
- 已激活且 hash 绑定的计划任务可从可验证 checkpoint 恢复，未知副作用转人工。
- Office 与 Research 都能形成可追踪产物和确定性报告。
- Subagent 权限、上下文、产物、usage、取消和循环熔断均可核对。
