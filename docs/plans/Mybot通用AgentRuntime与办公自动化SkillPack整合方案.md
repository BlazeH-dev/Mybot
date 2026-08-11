# Mybot 通用 Agent Runtime 与 Office Skill Pack 整合方案

> 当前基线：以仓库当前代码、测试和 `docs/runtime-code-notes/` 为准。历史修订统一见 `docs/修改记录.md`。

## 1. 定位与目标

Mybot 基于 nanobot v0.2.1 二次开发，不重写 Agent 框架，而是在现有 `AgentLoop`、`AgentRunner`、MessageBus、WebSocket、工具和 Skill 体系上增加一层可治理 Runtime。

目标是证明四件事：

1. **能扩展**：通过 Skill Pack 增加领域能力，不在核心循环写领域私有分支。
2. **能治理**：工具、文件、网络、MCP 和 Subagent 受统一权限、HITL、生命周期熔断与硬边界约束。
3. **能交付**：输入、计划、事实、中间产物和最终文件可追踪、可验证、可恢复。
4. **能证明**：关键行为有确定性测试、trace、指标和可复现结果。

Office 是首个验证领域，不是产品唯一方向：

- `officecli`：固定版本 OfficeCLI 的唯一 Office 能力和默认路由。
- Office benchmark 在相同 verified facts、输入快照、Policy、Runtime 和 evaluator 下比较 `gpt-5-6-luna` 与 `deepseek-v4-flash`。

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
  trace.py           Mybot 语义埋点、OTel 上下文与 Langfuse Python SDK
  replay.py          轻量 cassette

nanobot/security/sandbox/
  manager.py         sandbox mode、provider capability 与 fail-closed
  launcher.py        Agent 触发子进程的统一启动边界
  seatbelt.py        macOS Seatbelt provider
  bwrap.py           Linux / WSL2 Bubblewrap provider
  network.py         默认断网、严格 fetch argv、域名/端口/DNS IP 绑定与审计

nanobot/workspaces/                 # P3.1 选做规划，尚未实现
  worktrees.py       WebUI 聊天级 Git worktree 生命周期与持久化
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
- 普通 Office 请求默认优先 `officecli`；用户明确要求 Python 时使用 `officecli`。
- `officecli` 是唯一内置 Office Skill，提供 DOCX/XLSX/PPTX 的通用 `inspect/query/create/apply/validate/render`；旧 id、显式路由和历史兼容入口已删除。启动时仅清理 `disabledSkills` 中的旧 id，不迁移为新 id。
- OfficeCLI 版本、平台资产和 checksum 只有 provider contract 一个真相源；Mybot 安装的同名 launcher 可在首次使用时自动准备并校验固定资产，Agent 任务不得调用上游 latest/install/update。
- 定量结论必须来自 `verified_facts.json`；纯格式、提取和批注任务不强制跑事实层。
- 两个 Agent 模型的比较固定使用相同输入、OfficeCLI、Policy、约束和 evaluator，分开报告 coverage 与共同任务质量；不得通过 prompt、路由或评分偏袒任一模型。

### 4.2 Plan DAG、版本与交付闸门

- 静态内建 `plan` 工具固定提供 `create/get/revise/confirm/resume/update_step/complete`；schema v2 step 包含 `id/description/depends_on/expected_artifacts/executor`，创建和修订时拒绝重复 ID、未知依赖、自依赖和循环。
- Runtime 持久化 DAG 节点状态 `pending|ready|running|succeeded|failed|blocked|uncertain|cancelled`；ready 分支可并行，依赖链自然串行，单个分支失败只阻塞后继。
- 普通失败不自动重试；用户在 active 计划卡片点击“继续执行”或明确要求继续后，`resume` 以当前完整 plan hash 为边界把 failed 节点重置为 ready、清理旧 child/result/error 绑定并重新派发。已知 `/stop` 取消的 child 直接回 ready，但不会在取消回调中自行重启。
- 每个 task 维护单一 `.nanobot-runtime/artifacts/<task_id>/plan.md`，每次 revision 使用 Runtime 固定模板原子替换；ArtifactStore 固定使用 `plan_markdown`，metadata 绑定当前 revision、plan hash 和 checksum。旧 `plan-rN.md` 与 `plan_md_rN` 在校验迁移成功后清理，结构化 revision/checkpoint/trace 历史仍保留。
- plan hash 只覆盖结构化契约；修订后旧 hash 和旧确认立即失效。active plan 只允许修改未开始节点；未受影响且 artifact checksum 仍有效的成功节点直接复用。
- WebUI `execution_mode=plan_only` 只开放 plan 与只读检查工具；plan 记录停在 `awaiting_confirmation`，Runtime turn 以 `awaiting_plan_confirmation` 持久化挂起，同回合不得 confirm 或执行。
- 普通 WebUI 复杂任务 create 后可自动激活；激活时 `approved_plan_hash` 必须等于当前 plan hash，并记录 `approval.mode=automatic`。
- 手动/仅规划计划和所有后续 revision 必须显式确认后激活；步骤依赖和 expected artifacts 由工具硬校验。等待 `plan_confirmation` 时允许用户用普通文字提出修改，Runtime 撤销旧确认并进入受限 plan-only revision 回合；审批和恢复 typed interaction 仍不可绕过。
- 自动激活只表示允许按计划推进，不批准高风险工具；外发和远程写仍独立经过 P3 policy/approval。本地已有文件写入与高风险本地 Shell 是否 ask 由当前 WebUI access profile 决定，但无论何种 profile 都必须经过 P3 OCC / hard deny。
- parent 节点由当前主 AgentRunner 按节点范围推进；child 节点由 PlanScheduler 通过受治理 SubagentManager 自动派发，绑定父 task/hash、restricted child workspace、独立 artifact root 和禁止嵌套 spawn 的边界。
- 所有必需节点达到成功或显式接受的终态，且每个 expected artifact 解析为任务目录内的安全路径并存在后，`plan complete` 直接将计划标记为 `completed`；不再启动独立 Reviewer、生成额外审查 artifact 或自动修订计划。
- plan 是 artifact 和 checkpoint 的根；每个会话只渲染一张计划卡片，固定在标题栏与消息滚动区之间，后续进度、修订和恢复原位更新。卡片可折叠为单行栏，折叠状态按会话持久化，并可打开当前 `plan.md`。存在 child 节点时提供独立“查看子智能体”入口；主时间线只展示 parent reasoning/tool activity，child reasoning、工具调用、状态和结果进入 revision/hash 绑定的桌面侧栏。WebUI 只验收桌面浏览器，不新增移动端实现或测试。

### 4.3 Sandbox、Policy 与三档 HITL

- `sandbox_mode` 使用 `read_only|workspace_write|danger_full_access`：plan-only 映射 `read_only`，Default Permission 映射 `workspace_write`，Full Access 映射 `danger_full_access`。
- `workspace_write` 必须由 OS provider 强制：macOS 使用 Seatbelt，Linux/WSL2 使用 Bubblewrap；原生 Windows 首版不宣称支持。provider 缺失时返回 `sandbox_unavailable`，不得静默无沙箱执行；用户只能显式切换 Full Access 或修复 provider。
- workspace 默认仅项目目录与当前 task artifact 目录可写；只读工具通过 `trusted_read_roots` 声明 builtin Skill、上传媒体等受控目录，Policy hard boundary 与文件工具使用同一只读根口径，写工具和 Exec 不消费该授权。`~/.nanobot`、凭据、Runtime interactions/checkpoint/trace 控制文件和项目 `.git` 始终 hard deny。
- sandbox 内命令默认断网。Core 网络例外只支持直接 `curl`：禁止 shell 组合、redirect、proxy/resolve/config/interface 等目标改写，批准绑定当前 tool call/command hash/domain/port/审批时公网 DNS 地址/expiry，并以独立 one-shot `LaunchSpec` + `--resolve` 固定目标；持久 exec session 始终保持断网，不能被一次性 grant 解锁。SSRF、内网、metadata 和敏感目标仍 hard deny。
- 沙箱覆盖 Agent 触发的 Shell 一次性/持久 session、CLI Apps 与 OfficeCLI 子进程；预配置 MCP server、channel bootstrap 和网关自身进程首版不纳入同一 OS 边界，必须在文档和 UI 标明，并继续受调用级 policy/SSRF 约束。
- `danger_full_access` 表示用户明确关闭本地 OS 文件/网络沙箱，不等于关闭 Runtime policy：消息、邮件、远程写、凭据访问、OCC 和 hard deny 仍独立生效。
- 工具调用先完成同步参数校验，再经过异步 policy gate：`allow / ask / deny`。
- P3 复用当前会话的 `WorkspaceScope`：Default Permission 保持 workspace 受限；Full Access 允许项目外的本地文件/Shell 访问，并作为已选择的本地操作预授权 profile，而非一次性工具 approval。
- 路径逃逸、受保护目录、SSRF、敏感信息和 restricted workspace 外路径都是 hard deny，不能被配置、Skill 或一次性 approval 放宽。需要访问其他目录时，用户必须切换项目 workspace 或显式选择 Full Access。
- Default Permission 下修改已有本地文件和高风险本地 Shell 默认 ask；Full Access 下这两类本地操作可 allow，但仍受 OCC、command deny pattern 和 hard deny 约束。消息/邮件、远程写和其他外部副作用在两种 profile 下都保持 ask；审批必须参数绑定且超时拒绝。
- `InteractionRequest` 统一承接 question、approval、需要人工确认的 plan confirmation、recovery decision：
  - `required`：没有明确回答就不继续。
  - `auto_resolve`：非阻塞偏好问题到 deadline 后使用确定性默认值，或让模型按最佳判断继续。
  - `expire_and_deny`：高风险审批超时后拒绝，绝不自动批准。
- 发出请求后当前 LLM 调用结束，task/turn 进入 `awaiting_*`；等待期 token 为 0。
- 回答和 deadline 竞争时只消费一次，并恢复原 task/turn 和 tool call；普通聊天不能隐式批准安全操作。
- WebSocket 只负责展示/提交，持久化 Runtime 状态是真相源。
- `approvals_reviewer=user` 是 P3 唯一必做路径；未来的 `auto_review` 只能处理本来会询问用户的有限 escalation，不得审核 hard deny、凭据读取、不可逆外发或关闭沙箱的请求，并必须记录独立 reviewer trace、理由、token 和结果。

### 4.4 Workspace 与 Worktree 隔离（选做）

- `WorkspaceScope` 仍是项目根和 access profile 的唯一安全真相源；Git worktree 是可选的文件/Git 分支隔离，不是 OS sandbox，不能用来声称防止恶意进程越权。
- P3.1 未排期、未实现，不属于 Runtime Core、冻结前必做或最终验收；只有冻结前必做完成，且真实多聊天 Git 冲突证明收益高于实现与维护成本时才启动。
- P3.1 只在 localhost WebUI 新聊天中提供 `direct|worktree` 显式选择，默认保持 `direct`；非 Git 目录、CLI/channel 和旧会话继续使用 direct workspace。
- worktree 按聊天绑定，从用户选择仓库的当前 `HEAD` 创建 `mybot/chat-<chat_id>` 分支；创建后项目/worktree 绑定不可切换，仅允许切换 Default Permission / Full Access。
- 源仓有 tracked/untracked 变化时不静默复制；WebUI 明确说明新 worktree 基于 `HEAD`、不包含未提交变化，用户二次确认后才创建。ignored 文件不属于 dirty 阻断条件。
- 聊天 fork 继承 worktree 模式并从源 worktree 的 `HEAD` 创建新分支；源 worktree 存在未提交变化时直接拒绝，不猜测用户是否希望复制 dirty diff。
- 自动清理仅适用于 `clean && HEAD == base_commit`；只要存在未提交修改或任何新 commit 就保留 worktree/分支，不以“已推送”或“已合并”作为自动删除证明。
- worktree registry 的 read-modify-write、create/fork/cleanup 使用跨进程锁串行化；`creating|removing` 中间状态可在启动对账时恢复或标记 `attention_required`，不能仅依赖原子替换假设没有并发操作。
- P3.1 首版不向 Agent Core 增加 `git add/commit/push/merge`，不给普通 Shell 开放 Git common dir 写权限；如果后续编码任务证明有稳定需求，再作为独立 Git Skill/结构化工具评估。
- worktree 不自动 merge/push，不自动初始化 submodule，不为 Subagent 再创建嵌套 worktree；P8 child 继续使用 task-scoped artifact root。

### 4.5 文件安全

- `write_file`、`edit_file`、`apply_patch` 修改已有文件前必须有当前 actor 的 fresh-read snapshot。
- 即使 mtime 未变也比较 SHA-256；变化返回结构化 `file_conflict`。
- 多文件 patch 在任何写入前统一 preflight；任一冲突则零写入。
- P3 不承诺 shell 任意写盘拦截、新文件完整事务、fsync 或消除最终微小 TOCTOU。
- P8 必做依靠 child artifact 目录隔离 + actor-local OCC；共享 workspace 文件租约、`file_busy` UI 和跨路径等待队列均为选做。

### 4.6 Artifact 与恢复

- 任务输入默认复制到 `.nanobot-runtime/artifacts/<task_id>/inputs/`；无法复制时标记 `reference_only` 与 `replayable:false`。
- artifact 记录 checksum、类型、Skill、引擎、来源、tool call、child id 和验证状态。
- durable checkpoint 只服务已激活且 `approved_plan_hash` 绑定当前 hash 的复杂任务；激活来源可以是普通 WebUI 自动激活或显式确认。
- 工具恢复语义：
  - `completed`：已持久化，跳过。
  - `pending`：未执行或可安全重放。
  - `uncertain`：副作用可能发生，使用 `required` 人工决定。
- DAG 恢复语义：已验证成功节点跳过；pending/ready 重新入队；无执行证据的 parent running 回到 ready；可验证产物完整的 running 恢复 succeeded；丢失进程内 child 或外部副作用状态不明时转 uncertain 并要求 recovery decision。
- plan hash、计划 Markdown、checkpoint 或产物 checksum 校验失败时 fail loud，不静默从头执行。
- `awaiting_question|approval|plan_confirmation|recovery_decision` 是合法 suspension，不能恢复成工具失败。
- 不宣称通用 exactly-once。

### 4.7 Subagent 治理

- 每个父任务同一时刻最多 5 个直接 child，完成后释放槽位；禁止嵌套。
- 权限只能继承或收紧；child 不设置 token、总时长或工具调用配额，避免长任务因父 Agent 低估工作量而失败。
- 保留用户/父任务取消、网关关闭、单次 LLM 请求超时和 200 轮异常循环熔断；触发循环熔断时返回部分进展。
- child 只接收必要目标、约束和 artifact 引用，不复制完整父会话。
- child 默认只写自己的 artifact 子目录，父 Agent 负责事实共享、冲突处理和最终汇总。
- PlanScheduler 默认并发派发 ready child 节点，依赖满足后继续下一批；默认与配置上限均为同时 5 个 direct child，完成后释放槽位，不限制任务生命周期内累计执行的 child 节点数。主 Runner 不前台等待 child；会话级 `/stop` 同时取消主任务和该 session 全部 child。
- child 生命周期通过独立 `subagent_activity` WebSocket/transcript 流暴露，事件绑定 parent task、plan hash、node id 和 child id；刷新后从 transcript 聚合恢复，旧 revision activity 不进入当前计划面板，也不混入 parent 消息/思考时间线。
- 任何使用 Subagent 的任务都要记录父子 trace，并与单 Agent 顺序执行比较成功率、时长和 token 成本。
- 文件租约未实现不阻塞 P8；如果实现，仅协调同一进程 Agent，不能替代 OCC。

### 4.8 Trace、Eval 与 Langfuse 边界

- Mybot 拥有埋点位置、`mybot.*` 语义、字段 allowlist、Runtime 状态和必须访问本地文件/进程的 task/evaluator；Langfuse SDK 负责 observation 生命周期、OTel 上下文、masking、batch/retry/flush 和 Cloud 传输。
- `observability.langfuse.enabled=false`（默认）时保留现有 JSONL TraceHook 作为本地调试与离线证据路径；启用后切换到 Langfuse SDK，停写 JSONL，二者互斥。JSONL 代码删除推迟到 Langfuse 稳定后作为独立清理项。
- Provider 观测必须在 `runner._request_model()` 内逐调用创建 generation observation（记录 start_time/TTFT/latency/usage）。优先使用 `langfuse.openai` drop-in（provider 从 config 设置环境变量后导入），自动追踪所有 OpenAI-compatible provider（OpenAI/DeepSeek/GPT-5.6）；检测到 drop-in 时 runner 不重复创建 generation。
- Tool 观测必须在 `runner._run_tool()` 内逐调用创建 tool observation（记录 tool_call_id/arguments 摘要/latency/result 摘要/error）。
- DAG 节点派发/完成、stale child completion、恢复分类和最终确定性完成均写入 `mybot.plan.*` trace。WebUI child activity transcript 是用户可见执行投影，不替代脱敏 trace 真相源。
- Langfuse 负责 Trace/Sessions、Datasets/Dataset Runs、Experiments、Scores、LLM-as-a-Judge、Code Evaluator、Annotation Queue、成本/延迟 Dashboard、趋势和 CI regression gate。
- Langfuse Python SDK `run_experiment()` 在 Mybot 进程中调用 task callback；OCB 使用 Langfuse LLM Connection 连接 `gpt-5.6-terra`，产生 `mybot_score` 和 reasoning，不需要 Mybot 的第二套 EvalResult/Score 数据库。
- Mybot 新建 `nanobot/cli/benchmark.py`（prepare/estimate/run/export），封装 `langfuse.run_experiment()` 和 Langfuse API 查询。Langfuse Dataset Run、Score 和 Annotation 是评估真相源，Git/README 只是只读导出快照。

## 5. 阶段路线图

| 阶段                                                                | 状态  | 交付范围                                              | 阶段出口                                          |
| ----------------------------------------------------------------- | --- | ------------------------------------------------- | --------------------------------------------- |
| [P0 准备](runtime-steps/P0-准备.md)                                   | 已完成 | 固定 Office fixture、Python 3.11 CI smoke            | fixture 可复算，workflow 可运行；远端状态以最新 Actions 记录为准 |
| [P1 Office 垂直切片](runtime-steps/P1-office垂直切片.md)                  | 已完成 | 单一 OfficeCLI Skill 与模型比较基线       | 两个 Agent 模型使用独立 Dataset Run；公平质量比较由 P5.1 公开 benchmark 承接                         |
| [P2 Manifest](runtime-steps/P2-skillpack-manifest.md)             | 已执行 | typed manifest、局部 fail closed、availability、开关     | 坏 Skill 不拖垮网关且不能进入候选                          |
| [P3 Sandbox/Policy/HITL/OCC](runtime-steps/P3-policy权限层.md)       | 已完成 | 统一 LaunchSpec、Seatbelt/Bubblewrap、受批直接 curl、Policy/HITL/OCC | restricted fail closed，网络 grant 不泄漏到 session/CLI Apps |
| [P3.1 Workspace/Worktree](runtime-steps/P3.1-worktree隔离.md)          | 选做（未实现） | WebUI 显式 per-chat worktree、持久绑定、fork 与保守清理       | 启动后不丢 dirty/新 commit，不扩大 Agent Git 权限              |
| [S5.0 轻量回放](runtime-steps/P5-trace-eval.md)                       | 已完成 | 4 个关键 cassette smoke                              | 无 API key、无网络即可回归关键 Agent 行为                  |
| [P4 Artifact/Checkpoint](runtime-steps/P4-artifact-checkpoint.md) | 已完成 | 输入快照、artifact/lineage、计划任务恢复                      | kill→resume 可验证，uncertain 不自动重试               |
| [P8 Subagent](runtime-steps/P8-多agent编排.md)                       | 已完成 | 数量/嵌套/权限/生命周期/上下文/产物治理                            | 父子 trace 完整并有单/多 Agent 对比                     |
| [P5 Trace/Eval/Observability](runtime-steps/P5-trace-eval.md)              | P5.1 代码完成 / 外部 smoke 待配置 | Langfuse SDK observation、Dataset/Experiment、SDK evaluator、Terra Judge、Annotation Queue | 本地硬门可回归；完成日本区配置后才发布真模型质量 |
| [P6 Research](runtime-steps/P6-通用性扩展.md)                          | 选做（未实现） | 1–2 天最小 Research Skill 通用性验证                  | 若启动，不改 Runtime 核心即可复用治理设施                   |
| [P7 交付物](runtime-steps/P7-面试交付物.md)                               | 代码完成 / 外部证据待配置  | benchmark、README 最终结果页、架构/quickstart       | 本地入口可复现；日本区 Cloud、Terra Judge、人工审核完成后才发布真实结果                             |

详细实施只看对应 `docs/plans/runtime-steps/P*.md`。

P5.1/P7 的外部配置、真实 smoke、人工审核与发布顺序统一按 `runtime-steps/P5-trace-eval.md` 的“外部配置前置”和“阶段出口”执行；任何 Key、许可、稳定 LibreOffice、Judge、Score、Queue 或 deep link 硬门缺失时不得发布结果。

## 6. 顺序、cutline 与选做项

依赖顺序：

```text
已完成主链：P0 → P1 → P2 → P3 → S5.0 → P4 → P8 → P5 Core
代码交付阶段：OfficeCLI → P5.1 Observability/Eval → P7
待外部交付闭环：日本区 Cloud smoke → OCB Terra Judge → Annotation Queue → Dataset Run export
选做候选：P6 Research 最小闭环；P3.1 Worktree MVP
```

冻结前必做：

- P1、P2、P3、P4、P8；OfficeCLI 通用化、旧窄工作流/周报 fixture 删除和最小中立 fixture 回归必须完成。
- S5.0 的 3–4 个关键 cassette。
- P5 Core：trace、确定性 eval/report、安全红队；P5.1 的 Langfuse SDK observation、Dataset/Experiment、SDK evaluator、Terra Judge/Annotation Queue/export 代码与 contract 已完成，真实 Terra Connection、Cloud Score、人审和发布仍按外部 runbook 验收。
- P7 benchmark、README 最终结果页、架构/quickstart 和最终结果证据。

每周只接受“完成一个可验证闭环”而不是多项各做 60%；阶段结束必须更新测试、指标和证据位置。

当前主线没有完成时间硬约束，优先保证面试可复现、评估口径稳定和证据可核对；不得为缩短运行时间牺牲 fingerprint、审计或环境锁定。

选做，主线未完成时必须砍掉：

1. P6 Research 最小闭环；只在主线完成后、确有第二领域通用性验证需求时启动，不计入项目冻结和最终验收。
2. P3.1 WebUI per-chat worktree；只有主线完成且真实冲突频率证明有价值时启动，不得扩展成通用 Git IDE/分支发布平台。
3. Langfuse 自托管、本地 Compose 与 Redis/MinIO/ClickHouse/PostgreSQL 运维；已选择日本区 Langfuse Cloud，除非后续出现明确合规或可用性需求，否则不回到自托管支线。
5. 白盒记忆治理、artifact delta/staging。
6. 多模型成本矩阵与 KV cache 优化。
7. Subagent 共享 workspace 文件租约与冲突可视化。
8. `approvals_reviewer=auto_review`（“替我审批”）；手动 approval、sandbox、trace 和红队未完成前不得实现。

## 7. 硬门指标

| 指标                         | 目标              |
| -------------------------- | --------------- |
| 数字可追溯到 fact id             | 100%            |
| 计划步骤/承诺产物交付                | 100%            |
| 未批准的 workspace 外写入       | 0               |
| `workspace_write` 沙箱外文件写入 | 0               |
| 沙箱不可用时静默无沙箱执行          | 0               |
| 未批准的命令网络访问               | 0               |
| 已批准直接 curl 的 command/domain/IP 绑定执行正确率 | 100% |
| 注入诱导的越权副作用、敏感泄漏、未确认外发      | 0               |
| 已有文件冲突拦截率                  | 100%            |
| HITL 回答/超时/取消恢复正确率         | 100%            |
| `expire_and_deny` 超时自动放行   | 0               |
| checkpoint kill→resume 成功率 | 100%            |
| OfficeCLI OpenXML 校验       | 100%（登记的兼容例外除外） |
| CI 确定性 smoke               | < 60 秒          |

同时记录但不设虚假目标：OCB 任务成功率与 Mybot Terra 分数、LLM/tool 成功率与错误、P50/P95、token/成本、Agent 循环步数、人类等待/恢复、Subagent 成本与时长溢价、人工审计分数。P5/P7 不采集业务对话量、用户满意度、CPU、内存或 GPU 指标。

P3.1 只有被明确启动后，才新增“Worktree 自动清理导致的 dirty/新 commit 丢失 = 0”作为该选做阶段的独立硬门；不计入当前 Runtime Core 验收。

评估来源固定为 OCB：公开 data/reference/assertion/rubric 进入 Langfuse Dataset 和 Custom LLM-as-a-Judge，通过 OpenAI-compatible LLM Connection 使用 `gpt-5.6-terra`，记录 `mybot_score`；结果标记为 `OCB/Mybot evaluation`，不标记 `official-comparable`。人工审计使用 Langfuse Annotation Queue，P5 Core Runtime hard gate 保留为独立回归证据。

P5.1/P7 固定使用三个 profile：`ci` 完全离线；`office-smoke` 使用 4 个 OCB 固定分层 case，两个 Agent 模型使用同一 OfficeCLI 跑相同任务并全部进入 Langfuse Annotation Queue；`office-release` 从 OCB full/50%/25% 中选择独立 Dataset/series。OCB adapter 使用独立 benchmark venv，与 Mybot 主环境隔离；`prepare` 固定 revision/SHA/license、最小精确 constraints、模型/evaluator 和 LibreOffice 版本，`run` 只调用 Langfuse `run_experiment()`。

release 固定执行 `prepare office-release -> estimate office-release -> run office-release`：prepare 缓存并校验 OCB release 数据，estimate 只读已准备数据，run 只冻结并消费选定 manifest。smoke prepare 只覆盖固定 4 case，不能替代 release prepare。

Mybot 只维护 benchmark 编排状态和本机恢复 checkpoint，不维护 Score 或趋势真相源；Langfuse Experiment/Dataset Run 负责运行记录、评分、审核和比较。Job 采用全局单运行、持久队列和手动 Resume：同一 `resume_token` 生成稳定 Dataset Run 名，远端成功 item 跳过，本地已完成 Case 复用权限 `0600` 的输入指纹/模型输出 checkpoint，只重跑缺失 evaluator/Trace，避免重复模型 token；Retry 才创建新 Job。checkpoint 不进入 HTTP/UI，Langfuse 仍是 Trace/Score/审核唯一真相源。`export --dataset-run` 只有在 Langfuse Score、Annotation Queue 和运行完整性满足要求时才生成 README 快照。smoke/release 必须完成日本区 Langfuse Cloud 真实写入、flush、回读、Annotation Queue 和 deep link smoke；普通任务和 CI 默认关闭 Cloud 且不承诺离线 Trace 补传。

## 8. 最终结果展示

公开证据：

- `benchmarks/latest.md`：最新结果索引，明确 deterministic/fake-provider/真模型/人工审计类型。
- `docs/plans/metrics-baseline.md`：历史趋势。
- README：只展示当前最终能力、架构、quickstart、benchmark 结果、指标和已知边界；不叙述阶段实施、改名迁移或历史修改过程。
- 架构图使用一张 Mermaid 图，明确展示最终模块边界和 nanobot/Mybot 能力归属。
- Langfuse 是启用 Cloud 时的唯一开发者观测与评估工作台；部署固定为日本区 Cloud（`https://jp.cloud.langfuse.com`），不建设本地自托管栈、平行看板或标注系统。Cloud 关闭时只保证 Runtime 和本地确定性 CI，不保证持久 Trace/Experiment。
- Langfuse 各区域账号、Key 和数据隔离；从日本区入口注册并创建项目 Key。上传前默认移除正文、原始 Office 文件、完整 artifact、密钥和个人信息；日本区属于跨境数据传输，敏感或公司数据在合规审查前保持 Cloud 关闭。
- P7 不新增 Demo、视频或独立答辩稿；简历和面试讲解只描述已完成且有测试/指标的能力。

## 9. 最终验收

项目完成时应满足：

- 新 Skill 可通过同一 manifest/loader/policy/artifact/trace/eval 接入。
- Default Permission 下 Agent 触发的命令默认由可验证的 OS sandbox 限制在 workspace，restricted workspace 外路径直接 hard deny，provider 不可用时不会静默降级；受批直接 curl 使用 command/domain/port/DNS IP 绑定的真实 LaunchSpec；Full Access 仍不绕过外部副作用 policy、OCC 和 hard deny。
- 人机等待可跨刷新、断线和重启恢复，等待期模型不空转，危险审批超时不放行。
- 用户/IDE 修改不会被过期读取静默覆盖。
- 已激活且 hash 绑定的计划任务可从可验证 checkpoint 恢复，未知副作用转人工。
- Office 任务能形成可追踪产物和确定性报告。
- `gpt-5-6-luna` 与 `deepseek-v4-flash` 能在相同 OCB Dataset、OfficeCLI、Policy 和 evaluator 配置下比较，结果口径可复现。
- 日本区 Langfuse Cloud 未启用或不可用时，普通 Runtime、本地 deterministic/cassette、Policy/OCC/HITL/OpenXML 硬门仍正常，但明确没有持久 Trace/Experiment；启用时可查询 Agent/LLM/tool/Policy/Interaction/artifact/checkpoint/child observation，比较 Dataset Run 和 Score 并处理 Annotation Queue。
- WebUI 新增独立 `#/evaluations` 评测中心，但不复制 Langfuse 真相源：通过共享 suite catalog/adapter 与 Job Service 展示 readiness、估算、队列、进度、Case 状态、断点续测计数、分数摘要和 CLI/WebUI 历史，并提供 Resume 同 Job、Retry 新 Job 及 Dataset Run、Trace、Annotation Queue deep link；人工评分、Trace 详情和 Judge reasoning 仍只在 Langfuse 完成。未来新增评测维度只需提交受信任的 `benchmarks/suites/<suite-id>/manifest.yaml`、`adapter.py` 和测试，不需重复开发页面。
- Subagent 权限、上下文、产物、usage、取消和循环熔断均可核对。
- 计划卡片可查看当前 revision 下并行 child 的独立实时活动，主页面不会把 child reasoning/tool events 混成 parent 执行过程；刷新后仍可恢复已落盘的 child 状态与结果。

P6 和 P3.1 不属于以上项目完成条件；若后续单独启动，其验收以对应阶段计划为准。
