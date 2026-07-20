# P3 OS Sandbox、Policy、三档 HITL 与最小文件 OCC

> 状态：已完成（2026-07-18）。文件租约不属于本阶段必做。
> 出口：Default Permission 下 Agent 触发命令受 OS sandbox 强制且不静默降级；工具执行前统一 allow/ask/deny；等待可持久化恢复；危险审批超时不放行；已有文件冲突拦截率 100%。

## 0. 现有能力与复用边界

P3 不重建 workspace 路径解析，但需要把现有“可选、仅 Exec、默认关闭”的 sandbox
补齐为 Runtime 硬边界。当前 WebUI 已将每个会话的项目目录和
`restricted|full` access mode 持久化为 `WorkspaceScope`；文件、Shell、网络校验和
workspace application guard 已消费该 scope 或现有 `nanobot/security/` hard boundary。

- 必须复用 `WorkspaceScope`、`current_tool_workspace`、`workspace_policy`、现有 SSRF/command guard、`agent/tools/sandbox.py` 与 `agent/tools/file_state.py`；不得在 `runtime/` 重复解析路径或判断 workspace 边界。
- `agent/tools/sandbox.py` 当前只有 Linux `bwrap` 命令包装器，默认 `tools.exec.sandbox=""`；没有 macOS provider、网络隔离、统一 capability preflight，也没有覆盖 CLI Apps/MCP。Windows 配置 sandbox 时还存在警告后无沙箱执行的 fail-open。P3 必须先收口这些边界，才能把它称为沙箱能力。
- P3 policy 接收已解析的 scope 和规范化参数，负责风险分类、`allow / ask / deny`、审计、InteractionRequest 和恢复；hard boundary 仍由原有安全模块最终执行。
- 当前 WebUI 的 Default Permission / Full Access 是会话级 access profile，不是参数绑定的一次性 approval；P3 不新增平行的 access-mode 配置或第二个权限菜单。

## 1. Codex 三轴模型与 Mybot 映射

Codex 将 sandbox、approval policy 和 approvals reviewer 分开。Mybot 采用同样的职责边界：

参考基线：Codex 官方 [Sandbox](https://learn.chatgpt.com/docs/sandboxing)、
[Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security) 和
[Auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review)。这里只复用控制面语义和安全不变量，不复制其私有实现。

```text
SandboxMode       = read_only | workspace_write | danger_full_access
ApprovalPolicy    = on_request | never
ApprovalsReviewer = user | auto_review
```

- `SandboxMode` 是 OS 技术边界；它决定命令能读写哪些目录、能否联网。
- `ApprovalPolicy` 决定命令需要跨边界时是否创建 approval；P3 默认 `on_request`，`never` 表示不弹审批而 fail closed，不表示自动放行。
- `ApprovalsReviewer` 决定 approval 交给谁。P3 只实现 `user`；“替我审批”对应未来的 `auto_review`，不是沙箱模式。
- `auto_review` 只允许审核本来会交给用户的有限 escalation；不得扩展 writable roots、打开全局网络、关闭 sandbox 或覆盖 hard deny。

### WebUI profile 映射

| WebUI 模式 | SandboxMode | ApprovalPolicy | Reviewer | 语义 |
| --- | --- | --- | --- | --- |
| 计划模式 | `read_only` | `on_request` | `user` | 只读检查；同轮禁止执行副作用 |
| Default Permission | `workspace_write` | `on_request` | `user` | workspace 内自治，越界/联网/高风险操作 ask |
| Full Access | `danger_full_access` | `on_request` | `user` | 用户明确取消本地 OS sandbox；外发、远程写、凭据、OCC、hard deny 仍受 policy |
| 无人值守安全任务 | `read_only|workspace_write` | `never` | 无 | 所有原本 ask 的 escalation 直接拒绝 |

不新增第二个项目目录或 access mode 真相源；`WorkspaceScope` 决定项目根和 access profile，Runtime 从它和本轮 execution mode 推导 sandbox/policy 组合。

## 2. OS Sandbox 硬边界

### 2.1 模块归属

将现有 `agent/tools/sandbox.py` 收敛为 `nanobot/security/sandbox/`：

```text
types.py       SandboxMode、SandboxStatus、SandboxViolation、LaunchSpec
manager.py     provider 探测、mode 解析、capability/status、fail-closed
launcher.py    Agent 触发子进程的统一启动入口
seatbelt.py    macOS Seatbelt profile 生成与执行
bwrap.py       Linux / WSL2 Bubblewrap profile 生成与执行
network.py     默认断网、严格 fetch argv、域名/端口/DNS IP 绑定和目标审计
```

- `SandboxManager` 只接收已解析的 `WorkspaceScope`，不得重新解释 WebUI 路径。
- `SandboxStatus` 至少返回 `mode/provider/enforced/available/reason/writable_roots/readable_roots/network`，供 `/status`、WebUI 和 trace 展示。
- restricted mode 下 provider 缺失、启动失败或策略无法表达时返回结构化 `sandbox_unavailable|sandbox_start_failed`；不得继续无沙箱执行。
- 用户可修复 provider，或显式将当前会话切到 Full Access。系统不能替用户自动切换。

### 2.2 Provider 与平台范围

- macOS：使用系统 Seatbelt；不依赖额外安装。
- Linux / WSL2：使用 Bubblewrap；启动时检查 binary、user namespace 和最小 profile 是否可运行。
- 原生 Windows：首版明确 `unsupported`，不沿用当前“警告后无沙箱运行”；Default Permission 下 Shell/CLI Apps fail closed，用户可选择 WSL2 或显式 Full Access。后续再评估 native Windows provider。
- provider 探测必须是实际 smoke，不以环境变量自报“已 enforce”作为安全事实；环境变量只能补充宿主说明。

### 2.3 文件系统边界

`workspace_write` 默认 profile：

- 当前 workspace 与 `.nanobot-runtime/artifacts/<task_id>/` 可写。
- builtin Skill、上传媒体和必要系统二进制/动态库只读。
- `~/.nanobot/config.json`、provider key、SSH/云凭据、浏览器数据和其他 home 内容不可读。
- `.nanobot-runtime/interactions|checkpoints|trace` 由 Runtime 自身写，Agent 命令只读或不可见。
- `.git` 默认只读；`git commit/checkout/rebase` 等需要写 Git 元数据的操作走参数绑定 escalation，不因 workspace 可写自动放行。
- symlink、hardlink、`..`、alternate cwd、shell substitution 和 child process 均不得逃逸最终 resolved boundary。
- session temp 目录可写且每 task/session 隔离；不得复用宿主全局 `/tmp` 作为隐式共享写区。

`read_only` 使用同一可读根，但 workspace、artifact 和 Git 元数据均不可写。`danger_full_access` 不包装本地命令，但仍使用环境变量清洗、command guard、SSRF、policy、OCC 和审计。

### 2.4 网络边界

- `read_only|workspace_write` 内命令默认无网络；不能只依赖 `curl/wget` 正则。
- 需要网络的命令先从规范化参数/实际请求提取目标，经过 SSRF 与 policy；approval 绑定 `tool_call_id + command_hash + domains + ports + expires_at`。
- approved network 首版只允许直接 `curl` argv：禁止 shell 组合、redirect、proxy/resolve/config/interface 等目标改写，按 command hash + domain + port + 审批时公网 DNS 地址绑定，并以 `--resolve` 固定目标；DNS rebinding、私网、loopback、link-local 和 metadata 地址继续 hard deny。通用代理为后续扩展，不进入 Core。
- 代理不得把 Provider/API key 原样注入命令环境；命令只拿必要的最小凭据或无凭据网络。
- OfficeCLI 固定资产准备是可信 provider bootstrap：只访问 contract 指定 release URL、校验 SHA-256 后缓存，再让 OfficeCLI 在 sandbox 内离线执行；不为 Agent 开放上游 latest/install/update。
- Full Access 可以访问公网，但仍经过现有 SSRF/敏感目标 hard deny；消息、邮件、远程写等外部副作用继续由工具级 policy ask。

### 2.5 覆盖范围

P3 必做纳入统一 `SandboxLauncher`：

- `ExecTool` 一次性命令与持久 exec session。
- Agent 触发的 CLI Apps 子进程。
- 通过 Exec/CLI Apps 启动的 OfficeCLI 和 Skill helper。
- Subagent 使用的同类命令；只能继承或收紧父 sandbox。

首版明确不纳入：网关自身、channel 安装/启动、预配置 stdio MCP server 进程。MCP tool call 仍经过 policy/approval，HTTP MCP 仍经过 SSRF；若要让 MCP/hooks/所有内建工具共享一个 OS boundary，应后续运行整个 gateway/worker 于 container/VM 或独立 sandbox worker，不能声称 P3 已覆盖。

## 3. Policy Gate

### 元数据与决策

`Tool` 增加 `capability`、`risk_level`、`requires_approval`。`runtime/policy.py` 提供纯函数：

```python
PermissionDecision(
    action="allow|ask|deny",
    reason="...",
    matched_rules=[...],
    risk_level="...",
)
```

输入包含工具、规范化参数、当前 `WorkspaceScope`、request/task/plan、父任务约束和配置。规则优先级：

1. 路径逃逸、受保护目录、SSRF、敏感信息 hard deny，任何配置和审批都不能放宽。
2. 配置 deny。
3. 精确参数绑定的一次性 approval。
4. 当前 access profile 的本地操作规则，以及参数绑定的 workspace/domain sandbox escalation。
5. 配置 ask/allow 与安全默认值。

### WebUI access profile 与 sandbox escalation 语义

| 操作 | Default Permission（`restricted`） | Full Access（`full`） |
| --- | --- | --- |
| 项目外本地文件与 Shell 工作目录 | sandbox deny；可创建最小参数绑定 approval | allow |
| 修改已有本地文件 | ask + OCC | allow + OCC |
| 高风险本地 Shell | ask | allow，仍受 command deny pattern / hard deny 约束 |
| sandbox 命令联网 | 默认 deny；按 domain/command ask | 本地命令可联网，仍受 SSRF/hard deny |
| 消息、邮件、远程写等外部副作用 | ask | ask |
| SSRF、敏感信息与其他 hard deny | deny | deny |

Full Access 是用户在当前 WebUI 会话中明确选择的本地访问预授权，不会绕过 OCC、
hard deny，也不自动批准外部副作用；若工具名、目标、参数或 plan 发生变化，已有的一次性 approval 仍不可复用。

### 接线

- `ToolRegistry.prepare_call` 只做同步解析、转换和 schema 校验；不得复制 workspace/path/sandbox 判定。
- Runner/Runtime 在实际执行前调用异步 policy gate，负责持久化和事件 I/O。
- deny 返回稳定结构化错误；allow 执行；ask 不执行工具，创建 approval。
- sandbox violation 先形成结构化事实，再由 policy 判断 deny/ask；policy 不能假装 provider 已 enforce，也不能把 hard deny 转成 approval。
- 每次决策先写本地 audit，P5 接入完整 trace。

## 4. InteractionRequest

`runtime/interactions.py` 统一承接：

- `question`：`request_user_input` 的单选、多选和自由文本。
- `approval`：高风险工具批准/拒绝。
- `plan_confirmation`：只用于 plan-only、手动计划或其他明确要求人工确认的计划，绑定 plan hash；普通 WebUI 自动激活计划不创建该请求。
- `recovery_decision`：uncertain 副作用人工决定。

最小记录：

```text
request_id / revision / kind
task_id / turn_id / plan_hash / step_id / child_id
continuation / tool_call_id
payload / questions / target summary
strategy: required|auto_resolve|expire_and_deny
created_at / expires_at
status: pending|answered|approved|denied|timed_out|expired|cancelled|superseded|consumed
```

首版可按 request 原子写入 `.nanobot-runtime/interactions/<request_id>.json`；session/task metadata 只保存活动引用。

### 三档等待

- `required`：必要参数、文件、不可推断选择、需要人工确认的 plan、uncertain recovery。无回答就不继续，只能回答、取消或 `/stop`。
- `auto_resolve`：非阻塞偏好问题，deadline 建议 60–240 秒。到期优先用声明的确定性默认值，否则返回 `timed_out` 让模型最佳判断；不得伪造用户答案。
- `expire_and_deny`：Default Permission 下修改用户原文件和高风险本地 Shell，以及两种 profile 下的消息/邮件、远程写操作。到期 expired/denied，原工具不得执行；Full Access 下允许的本地写入仍必须先通过 OCC。

普通聊天不能隐式消费 approval。客户端响应必须包含 request id、expected revision 和幂等键。

### Suspension 与恢复

1. 模型调用 `request_user_input`，或 policy 返回 ask。
2. Runtime 原子保存请求并推送 WebUI 卡片。
3. Runner 返回 typed `awaiting_question|approval|plan_confirmation|recovery_decision`，不是 tool error。
4. 当前 LLM 调用结束，释放 Runner 资源；task/turn 不发送 completed，等待期 token 为 0。
5. 用户响应或 deadline 恢复原 task/turn 和 tool call；原子竞争只允许一次消费。
6. 回答、`timed_out`、denied/expired 作为匹配原 `tool_call_id` 的结构化 tool result 注入，不重复添加用户文本。

`InteractionManager` 启动时扫描 overdue pending 请求；运行中维护最近 deadline 的 timer，不为每个请求建立永久 cron。刷新、断线和重启后 WebUI 必须重放未处理卡片。

## 5. 参数绑定 Approval

`runtime/approvals.py` 是 `InteractionRequest(kind="approval")` 的安全专用逻辑，额外保存：

```text
tool_name / normalized_params_hash
task_id / plan_hash / step_id
target / risk / reason / expires_at
sandbox_mode / provider / command_hash
writable_roots / network_domains / ports
```

- approval 固定 `expire_and_deny`，只能消费一次。
- 工具名、plan hash、参数 hash 任一改变，或请求过期/已消费，都必须重新审批。
- sandbox escalation 只对当前 tool call 生效，授予最小 writable root/domain/port；不得把单次 approval 持久化成 Full Access。
- plan 的自动激活状态不能替代工具 approval；自动 plan-and-execute 仍逐次经过 policy gate。
- WebSocket 不是状态真相源。

OfficeCLI 基线：只读 help/view/get/query/validate 通常 allow；任务目录新产物的常规 DOM 操作可 allow；修改用户文件与高风险本地 raw 操作按 access profile 走 ask/allow 且必须保留 OCC；MCP/plugin/install/update/config/watch 与其他外部副作用按参数 ask/deny；硬边界始终 deny。

### “替我审批”后续选做边界

`approvals_reviewer=auto_review` 不进入 P3 必做出口。只有以下前置条件全部满足后才能进入后续实验：

1. 用户审批路径、参数绑定、过期拒绝和重启恢复已稳定。
2. sandbox provider 为 `enforced`，不得在 `danger_full_access` 或 provider unavailable 时启用。
3. P5 trace 能区分 main/reviewer，记录输入摘要、决定、理由、token、延迟和最终执行结果。
4. 红队 eval 证明 reviewer 不能批准 hard deny、凭据读取、关闭 sandbox、不可逆外发和未知 MCP 副作用。

首版 reviewer 最多评审本地、可回滚、目标明确的 sandbox escalation；任何不确定、参数变化或风险超阈值都返回 deny/转用户，不得“最佳判断后放行”。

## 6. 最小文件 OCC

复用 `agent/tools/file_state.py`，但 read snapshot 必须按 actor 隔离：

- 已有文件在 `write_file`、`edit_file`、`apply_patch` 前必须由当前 actor 读取。
- 未读返回 `file_conflict:not_read`；SHA-256 变化返回 `file_conflict:modified_since_read`，即使 mtime 未变也失败。
- 多文件 patch 在第一次写入前检查全部目标；任一冲突则零写入。
- 成功写入后刷新当前 actor read state；提示模型重新读取、重新生成 patch。

冻结前不承诺：新文件完整竞态消除、数据库式事务/fsync、shell 任意写盘拦截、最终微小 TOCTOU、Subagent 文件租约或跨进程锁。

## 7. 安全口径

- 网页、文档、表格、邮件和 MCP 描述都是 untrusted content。
- 不承诺检测全部注入；验收的是越权写入、敏感泄漏和未确认外发为 0。
- 启发式 injection signal 只用于审计，不是安全边界。

## 8. 分步实施顺序

### S0：冻结语义与状态模型

- 增加 typed `SandboxMode/SandboxStatus/SandboxViolation/LaunchSpec`。
- 保持 `WorkspaceScope` 为项目根/access profile 唯一真相源，完成 WebUI 模式映射。
- settings/status API 只展示 provider、enforced、network 和失败原因，不提供任意 writable-root 管理后台。

### S1：统一子进程启动与 fail-closed

- 抽取 `SandboxLauncher`，先接入 Exec one-shot/session 和 CLI Apps。
- 删除 Windows “配置 sandbox 后继续 unsandboxed”的分支。
- restricted mode provider 缺失、wrapper 构造失败、启动失败均结构化拒绝；Full Access 仅在用户显式选择后无 wrapper 运行。

### S2：文件系统 provider

- 实现 macOS Seatbelt 与 Linux/WSL2 Bubblewrap profile。
- 加入 workspace/artifact rw、Skill/media/system ro、home/credential/runtime-control/git 保护。
- 用真实 child process 测试 symlink、cwd、subshell、重定向和后台进程无法越界。

### S3：命令网络隔离

- restricted sandbox 默认断网。
- 增加域名/端口规范化、严格 curl argv、DNS/IP 固定、redirect/DNS rebinding 校验和一次性网络 capability。
- OfficeCLI bootstrap 走固定 contract 的独立可信下载路径，不借用 Agent 网络权限。

### S4：Policy、Approval 与 HITL 接线

- 在 ToolRegistry 参数校验后、SandboxLauncher 前运行 policy gate。
- sandbox violation 转参数绑定 approval；approve 后以最小 capability 重试原 tool call，deny/expire 不执行。
- 完成三档 InteractionRequest、durable suspension 和一次性恢复。

### S5：OCC、WebUI、trace 与回归

- 完成已有文件 OCC hard gate。
- WebUI 展示当前 mode/provider/enforcement、越界目标、网络域名、一次/拒绝操作，不把“替我审批”混入 sandbox 开关。
- audit 记录 sandbox decision、policy decision、reviewer、approval 和实际 launch spec 摘要。
- 扩大 CI/平台 smoke；没有对应 provider 的 runner 必须显式 skip 并保留至少一个真实 provider 集成门。

## 测试与出口

- Default Permission 的真实子进程不能写 workspace 外、home、config、Git 元数据和 Runtime 控制文件；child/background process、symlink 和 cwd trick 同样失败。
- restricted sandbox 默认不能联网；批准域名以外、redirect 到私网、DNS rebinding、loopback/metadata 全部失败。
- macOS Seatbelt 与 Linux/WSL2 Bubblewrap 至少各有 profile 单测；可用平台有真实 smoke。原生 Windows 不得静默 fallback。
- provider 缺失/启动失败返回 `sandbox_unavailable|sandbox_start_failed`，工具未执行；只有用户显式 Full Access 才允许无 OS wrapper。
- Exec one-shot/session、CLI Apps、OfficeCLI/Skill helper 和 Subagent 继承路径覆盖；MCP/channel/gateway 的未覆盖边界在 status/文档中可见。
- hard deny 不能被配置、Skill、approval 或 child 放宽。
- 同一工具调用在 Default / Full profile 下符合上述矩阵；Full Access 不绕过 OCC、SSRF、敏感信息、command deny pattern 或外部副作用审批。
- policy 只消费现有 `WorkspaceScope` 与安全模块结果，不创建第二套 workspace/path/sandbox resolver。
- required 不回答不继续；auto_resolve 到期恢复；expire_and_deny 到期不执行。
- 等待期间 provider 不被调用；回答/deadline 只恢复一次，重复、迟到和错误 revision 被拒。
- approval 的 approve/deny/expire、参数/计划 hash mismatch 和一次性消费通过。
- 单次 sandbox approval 不会变成 session Full Access；command/root/domain 任一变化必须重新审批。
- `approvals_reviewer=user` 路径完成即可通过 P3；不得用尚未实现的 auto-review 替代人工 approval 验收。
- 刷新、断线、重启后卡片和 continuation 可恢复。
- OfficeCLI allow/ask/deny 分级通过。
- 未读、读后修改、mtime 不变但 hash 变化、多文件第 N 个冲突均硬失败且零部分写入。
