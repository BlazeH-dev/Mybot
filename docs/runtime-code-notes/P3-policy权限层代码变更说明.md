# P3 Sandbox、Policy、HITL 与文件 OCC 代码说明

> 对应计划：`docs/plans/runtime-steps/P3-policy权限层.md`
> 当前状态：已完成。Policy/HITL/Approval/OCC、Seatbelt/Bubblewrap、Exec one-shot/session 与 CLI Apps 的统一 `LaunchSpec`、受批直接 curl 闭环均已落地。`auto_review`、通用网络代理和跨进程文件租约仍未实现。

## 这一阶段解决什么问题

P1 能生成 Office 文件，P2 能管理 Skill，但 Agent 一旦开始调用 Shell、改文件、联网或发送消息，就必须回答更困难的问题：

- 模型说“我要执行”时，谁判断它是否可以执行？
- Default Permission 是提示词约束，还是操作系统真的拦得住？
- 高风险操作怎样暂停并等待用户，而不是靠模型自己问一句？
- 用户刷新页面或重启网关后，待确认操作怎样还在？
- 用户或 IDE 在 Agent 读取后修改了文件，Agent 怎样避免覆盖新内容？
- 用户批准了一条命令，模型能否偷换参数、目标或 child 身份复用审批？

P3 把这些问题拆成五层，而不是做一个笼统的“安全模式”开关。

## 五层安全模型

```text
WorkspaceScope
  当前项目根和 restricted/full 会话模式

PolicyEngine
  根据工具、规范化参数、scope 和风险做 allow / ask / deny

InteractionRequest + ApprovalBinding
  把需要人的决定持久化，并绑定精确参数

SandboxLauncher
  用 Seatbelt/Bubblewrap 对 Agent 启动的进程做 OS 级强制

FileStates OCC
  写已有文件前确认“当前 actor 读过的版本仍是最新版本”
```

这五层解决的是不同问题：

- WorkspaceScope 是会话范围真相源。
- Policy 是逻辑决策。
- Approval 是一次具体越权请求的人工授权。
- Sandbox 是进程实际能触碰的系统资源边界。
- OCC 是并发修改时的数据一致性保护。

只做其中一层都不够。例如 Policy 说“禁止读凭据”，但进程没有 OS 隔离，恶意脚本仍可能直接打开文件；反过来，Sandbox 允许 workspace 写，也不能自动判断“发送邮件”是否获得业务批准。

## 1. 三个正交控制轴

P3 明确区分：

```text
SandboxMode       = read_only | workspace_write | danger_full_access
ApprovalPolicy    = on_request | never
ApprovalsReviewer = user
```

### SandboxMode：技术边界

- `read_only`：工作区只读，主要用于 plan-only。
- `workspace_write`：工作区和明确 artifact root 可写，命令默认断网。
- `danger_full_access`：不套本地 OS sandbox wrapper，但仍经过 Policy、hard boundary、SSRF、外部副作用审批和 OCC。

### ApprovalPolicy：遇到 ask 怎么办

- `on_request`：生成 approval，等待用户。
- `never`：原本需要 ask 的操作直接 deny，不代表自动批准。

### ApprovalsReviewer：谁来审

Core 只有 `user`。计划中的 `auto_review` 尚未实现，因此项目不能声称支持 AI 自动审批。

### WebUI 模式怎样映射

| WebUI 语义 | SandboxMode | 典型行为 |
| --- | --- | --- |
| 仅规划 | `read_only` | 只开放 plan 和只读工具，副作用拒绝 |
| Default Permission | `workspace_write` | workspace 内有限自治，已有文件写入、高风险命令和外部副作用可能 ask |
| Full Access | `danger_full_access` | 取消本地 OS wrapper，但不是关闭全部安全检查 |

## 2. PolicyEngine：每个工具调用先做确定性决策

文件：`nanobot/runtime/policy.py`

核心返回值是：

```python
PermissionDecision(
    action="allow|ask|deny",
    reason="...",
    matched_rules=(...),
    risk_level="low|medium|high|critical",
    target="...",
    hard_deny=False,
)
```

它是确定性规则，不调用 LLM。相同的工具名、参数、scope 和模式应得到相同结果。

### 当前决策顺序

1. 先检查 hard boundary。
2. read-only 模式拒绝副作用工具。
3. 消息、cron、MCP/remote call 等外部副作用进入 ask/deny。
4. 写文件：Default 下修改已有文件 ask，新文件可 allow；两者都保留 OCC。
5. exec：Default 下命中高风险命令模式时 ask，否则允许在 sandbox 内运行。
6. 其他工具按默认规则 allow。

### 当前 hard boundary

`_hard_boundary()` 会解析工具参数中的 `path/working_dir/workdir`，以及 `apply_patch.edits[*].path`，然后拒绝：

- `.git`、`.ssh`、`.aws`、`.kube`、credentials 等受保护路径。
- `~/.nanobot/config.json`。
- `.nanobot-runtime/interactions`、`checkpoints`、`trace` 等 Runtime 控制目录。
- restricted WorkspaceScope 下 resolve 到 workspace 外的路径。

hard deny 不能通过 approval 放宽。特别要以实际代码为准：当前 restricted scope 的 workspace 外路径是直接拒绝，不存在“弹一次审批就可写任意外部目录”的 Core 能力。用户若明确选择 Full Access，scope 不再限制到 workspace，但受保护路径等 hard boundary 仍保留。

### Policy audit

配置 `audit_path` 后，每次决策追加 JSONL，权限为 `0600`。这为 P5 trace/eval 提供确定性证据，也方便排查“为什么这个工具被拒绝”。

## 3. Sandbox：把提示约束变成 OS 强制

代码目录：`nanobot/security/sandbox/`

### 类型与状态

`types.py` 定义：

- `SandboxMode`。
- `SandboxStatus`：provider、是否 enforce、读写根、网络模式、未覆盖进程。
- `LaunchSpec`：最终 argv、cwd、env、command hash、读写根和网络绑定。
- `SandboxUnavailableError`：restricted 模式无法强制时使用结构化错误。

### Provider 探测不是看环境变量

`SandboxManager.provider_available()` 做真实 smoke：

- macOS：查找 `sandbox-exec`，运行最小 Seatbelt profile。
- Linux/WSL2：查找 `bwrap`，运行最小 Bubblewrap 隔离命令。
- 原生 Windows/未知系统：返回 unsupported。

为什么要真实执行 smoke？因为“机器上有 binary”不等于 user namespace、权限和 profile 真能运行。安全能力不能靠配置自报成功。

### Fail closed

当模式是 `read_only/workspace_write`，provider 不可用或未 enforce，`SandboxLauncher` 抛出 `sandbox_unavailable`，不会打印警告后继续裸跑。

只有用户显式选择 `danger_full_access` 时才不包装本地命令。这是用户改变会话访问模式，不是 Runtime 自动降级。

### 统一启动入口

`SandboxLauncher.prepare_shell/prepare_argv()` 已经实现，负责：

1. 规范化 workspace、cwd、readable roots、writable roots。
2. 获取 SandboxStatus。
3. 计算 command SHA-256。
4. 检查是否存在匹配且未过期的 NetworkGrant。
5. macOS 生成 Seatbelt profile，Linux 生成 Bubblewrap argv。
6. 返回不可变 `LaunchSpec` 给 Exec/CLI Apps 等调用方执行。

当前调用关系已经收敛：

- CLI Apps 直接使用 `SandboxLauncher.prepare_argv()`。
- Exec one-shot 使用 `prepare_shell()` 返回的 `LaunchSpec`，`_spawn()` 只执行精确 `argv/cwd/env`，不再解析沙箱字符串。
- `ExecSessionManager.start()` 接收同一个 `LaunchSpec`；持久 session 不会重建 shell 或改变网络 profile。
- `agent/tools/sandbox.py::wrap_command()` 只保留历史 API，内部也委托统一 launcher，不再维护第二套 provider profile。
- restricted shell 强制 non-login，`HOME` 指向 workspace，`PATH` 作为清洗后的环境值显式传入，避免宿主 profile 成为环境内的隐式可执行配置。

### 实际覆盖范围

已覆盖 Agent 触发的：

- Exec one-shot 和 session。
- CLI Apps。
- 通过这些入口启动的 OfficeCLI/Skill helper。
- Subagent 的同类命令。

未覆盖：

- gateway 本身。
- channel bootstrap。
- 预配置 stdio MCP server 进程本体。

MCP tool call 仍有调用级 Policy/approval，HTTP 目标仍有 SSRF，但不能宣称整个 MCP 进程已被 P3 sandbox 包住。

## 4. Restricted 网络闭环

文件：`nanobot/security/sandbox/network.py`

网络是最容易出现“审批目标和实际目标不一致”的地方。P3 没有做通用代理，而是将 capability 收窄为经过批准的单条直接 `curl`。命令解析、SSRF/DNS 绑定、ApprovalBinding、Runner `NetworkGrant`、Exec 与 `pinned_curl_argv()` 已形成端到端闭环。

grant 只能用于新建的 one-shot curl 进程，不能把现有持久 exec session 切换成联网模式。只要调用携带 `yield_time_ms`，Exec 就不会消费 grant；CLI Apps 的 `prepare_argv()` 也不隐式读取 Exec 的 ContextVar。这样一次审批不会在 session 或相邻子进程中变成可复用网络能力。

### 审批前提取

`command_network_targets()`：

- 从命令中提取 HTTP/HTTPS URL。
- 归一化域名和端口。
- 要求命令第一项是 curl。
- 拒绝 `;`、`&&`、`||`、管道、命令替换和重定向等 shell 组合。
- 拒绝 redirect、proxy、`--resolve`、`--connect-to`、config、interface、unix socket 等目标改写参数。

### SSRF 与 DNS 绑定

`normalize_domain()` 和 `resolve_public_addresses()` 拒绝：

- localhost。
- metadata 内网域名。
- 私网、loopback、link-local、非 global IP。
- 解析结果中包含私有地址的域名。

审批记录绑定：

```text
command_hash
domains
ports
expires_at
审批时解析到的公网 IP
```

执行前由 `pinned_curl_argv()` 重新解析原命令形状，添加 `--resolve domain:port:approved_ip` 并禁用 redirect。集成测试已覆盖 `Runner execution_context.network_grant → Exec one-shot → SandboxLauncher → pinned LaunchSpec`；command hash、domain、port、IP 或 expiry 不匹配时不能获得网络放宽。

### 为什么不做通用代理

通用代理需要正确处理任意 CLI、TLS、redirect、认证、DNS、子进程和协议，范围很大。Core 先选择一个可以证明安全边界的最小能力，比“看起来支持所有网络、实际有绕过”更可靠。

## 5. InteractionRequest：真正可恢复的人机等待

文件：`nanobot/runtime/interactions.py`

支持四种 kind：

```text
question
approval
plan_confirmation
recovery_decision
```

支持三种 strategy：

| 策略 | deadline 到达后的行为 | 适用场景 |
| --- | --- | --- |
| `required` | 继续等待，不擅自决定 | 计划确认、关键业务问题、uncertain 恢复 |
| `auto_resolve` | 使用明确默认值或标记 model best judgment | 非安全关键偏好问题 |
| `expire_and_deny` | 过期并拒绝 | 高风险 approval |

### 为什么不能只发一张前端卡片

WebUI 卡片只是投影，真实状态保存在：

```text
.nanobot-runtime/interactions/<request_id>.json
```

每条记录包含 request id、revision、task/turn/plan/step/child、continuation、原 `tool_call_id`、payload、questions、deadline、状态和响应。

文件写入使用临时文件加 `os.replace()`，避免只写了一半的 JSON。刷新或重启后可以重新读取并投影到 WebUI。

### revision 和 idempotency key

用户响应必须带：

- `expected_revision`：防止对旧卡片提交。
- `idempotency_key`：重复点击或网络重试只消费一次。

如果 revision 不匹配，返回 `revision_mismatch`；同一个 idempotency key 重复到达时直接返回当前状态。

### 恢复原工具调用

Runner 遇到 ask 时不会伪造普通错误，而是返回 `awaiting_*` suspension。AgentLoop 在收到 typed response 后，把回答/审批结果写回原 assistant tool call 对应的 tool result，保留 `tool_call_id`，然后恢复同一条消息链。

这点非常关键：如果把用户回答当成一条新聊天消息，模型可能无法知道它回答的是哪个工具，也可能绕过 pending approval。

等待期间当前 Runner 已结束，没有循环调用 provider；因此 human wait 和模型运行时间可以在 P5 trace 中分开计算。

## 6. ApprovalBinding：审批只对精确调用生效

文件：`nanobot/runtime/approvals.py`

`normalized_params_hash()` 对排序后的 JSON 参数做 SHA-256。`ApprovalBinding` 还可以绑定：

```text
tool_name
task_id / plan_hash / step_id / child_id
target / risk / reason
sandbox_mode / provider
command_hash / writable_roots
network domains / ports / addresses
```

approval 固定使用 `expire_and_deny`。只有持久化记录中的完整 binding 与当前调用逐字段相等，并且状态是 approved，才能命中。

命中后还会被 consume，形成一次性授权。参数、计划 hash、child 或目标发生变化，都需要重新审批。

## 7. 文件 OCC：防止“最后写入者覆盖一切”

文件：`nanobot/agent/tools/file_state.py`，接线在 `filesystem.py`、`apply_patch.py`。

OCC 是 Optimistic Concurrency Control，乐观并发控制。它假设冲突不一定发生，不提前锁文件，但写入前必须检查版本。

### 当前实现

1. 当前 actor 成功读取文件后，记录 resolved path、mtime 和 SHA-256。
2. 写已有文件前调用 `check_fresh()`。
3. 没读过：`file_conflict:not_read`。
4. 当前 SHA-256 与读取时不同：`file_conflict:modified_since_read`。
5. `apply_patch` 先对所有已有目标做全量 preflight，任一冲突则零部分写入。
6. 写成功后更新当前 actor 的 FileStates。

actor-local 表示主 Agent、每个 child 和不同 session 不能继承彼此的“我读过这个文件”资格。否则 child A 读取的旧版本可能被 child B 当成自己的 fresh read。

### 为什么用内容 hash，不只用 mtime

mtime 可能因为 touch 或编辑器保存策略变化，也可能在很短时间内无法可靠区分内容。真正的硬 preflight 始终比较 SHA-256；mtime 主要帮助旧的读取去重逻辑。

### OCC 的诚实边界

- 它保护已有文件的 read-before-write。
- 它不能完全消除两个进程同时创建同名新文件的竞态。
- 它不是跨进程锁。
- 它不能代替原子写或应用层事务。
- P8 的 FileLeaseRegistry 仍未实现。

## 一次高风险工具调用的完整路径

```text
LLM 返回 tool call
  -> ToolRegistry 做 schema/参数解析
  -> AgentRunner 调用异步 policy gate
  -> PolicyEngine
       ├── hard deny -> 返回 policy_denied
       ├── allow -> 继续执行
       └── ask
            -> 生成 ApprovalBinding
            -> 查找完全匹配的未消费 approved request
                 ├── 找到 -> consume，一次性 allow
                 └── 未找到 -> 创建 InteractionRequest
                              -> WebSocket 投影卡片
                              -> Runner 以 awaiting_approval 停止
  -> 用户 typed response
  -> revision/idempotency 校验并持久化
  -> AgentLoop 恢复原 tool_call_id
  -> CLI App/Exec 由 SandboxLauncher 生成 OS 强制 LaunchSpec
       ├── one-shot approved curl：精确 pinned argv + 一次性网络 profile
       └── exec session：始终使用默认断网 profile
  -> 若写已有文件，OCC 检查 SHA-256
  -> 执行并记录 audit/trace
```

## 关键设计取舍

### Plan 确认与工具审批为什么分开

plan confirmation 只表示用户同意“按这个任务契约继续”。具体步骤中发送消息、修改已有文件、联网等仍可能 ask。否则用户确认一份笼统计划就等于给后续未知参数无限授权。

### Full Access 为什么仍有安全检查

Full Access 只取消本地进程的 workspace OS wrapper。它不应自动批准外发、远程写，不应允许读取 provider key，也不应跳过 OCC。访问范围和业务副作用是不同维度。

### 为什么超时审批必须拒绝

“用户没回应”不等于“用户同意”。安全 approval 使用 `expire_and_deny`，避免无人值守时越权操作被自动放行。

## 验证与证据

主要测试：

- `tests/runtime/test_sandbox_policy_occ.py`
  - Seatbelt/Bubblewrap profile 与真实 smoke。
  - restricted provider 缺失 fail closed。
  - 默认断网、Runner → Exec 直接 curl、DNS/IP 绑定、session 不继承 grant 和 Windows unsupported。
  - protected metadata masking、后台子进程、workspace escape、Policy 和 OCC。
- `tests/tools/test_exec_platform.py`、`test_exec_session_tools.py` 与 `test_sandbox.py`
  - one-shot/session 精确执行 `LaunchSpec`，Windows 显式 argv，restricted non-login/workspace `HOME`。
  - provider 在探测后启动失败时，one-shot/session 都返回结构化 `sandbox_start_failed`。
  - 旧字符串 adapter 也只能委托统一 launcher，不能维护独立 profile。
- `tests/runtime/test_interactions_approvals.py`
  - 三档 strategy、revision、idempotency、迟到响应和一次性 binding。
- `tests/runtime/test_plan_interaction.py`
  - plan-only suspension、typed confirmation 和同回合禁止执行。
- `tests/runtime/test_websocket_interactions.py`
  - 卡片投影、刷新重放和 typed response。
- `tests/runtime/test_redteam.py`
  - workspace、凭据、恶意 MCP 和 child 绕过的攻击后果为零。

工具 schema、plan confirmation UI 和真实 Seatbelt/Bubblewrap provider 都有对应回归；macOS 本机不能运行 Linux Bubblewrap real smoke，该项由 Linux CI 覆盖。

## 未实现和不能夸大的部分

- `ApprovalsReviewer.AUTO_REVIEW` 未实现。
- restricted 网络不支持任意 wget、浏览器、CLI 或通用代理。
- gateway/channel/预配置 stdio MCP 进程未纳入 OS sandbox。
- 未提供容器或 microVM 镜像、依赖锁定和环境快照，因此 P3 是安全执行边界，不是 SWE-bench/OSWorld 所需的完整可重建 benchmark 环境。
- 没有跨进程文件锁或共享 workspace FileLeaseRegistry。
- 新文件创建竞态不能靠当前 OCC 完全消除。
- 原生 Windows restricted sandbox 未实现，当前是 fail closed。

## 面试怎么讲

### 30 秒回答

> P3 把权限拆成 WorkspaceScope、确定性 Policy、持久化 InteractionRequest、参数绑定 Approval、OS Sandbox 和文件 OCC。Exec one-shot/session 与 CLI Apps 统一执行不可变 LaunchSpec；Default 下 Seatbelt/Bubblewrap 强制 workspace 边界并默认断网，受批直接 curl 只获得绑定 command/domain/port/DNS IP/expiry 的一次性能力，持久 session 不能继承；高风险等待可恢复，已有文件写入前用 actor-local SHA-256 OCC 防覆盖。

### 高频追问

**Policy 和 Sandbox 有什么区别？**

Policy 是“应该不应该做”的应用决策；Sandbox 是“即使代码想做，操作系统是否允许”的强制边界。一个负责意图治理，一个负责技术隔离。

**为什么普通聊天不能回答 approval？**

普通文本没有 request id、revision 和 tool call 绑定，无法证明用户回答的是哪次精确操作，也容易被 pending 状态绕过。必须使用 typed response。

**OCC 和锁有什么区别？**

锁是先占有再写，OCC 是读取后乐观执行、写入前检查版本。OCC 能覆盖用户/IDE/其他进程修改，不依赖它们配合拿锁；缺点是冲突发生后需要重读重试。

**DNS rebinding 怎样防？**

审批时解析并只接受公网 IP，把域名、端口、命令 hash 和地址写进 binding；执行时 Runtime 重建 curl argv，用 `--resolve` 固定到已批准 IP，并禁止 redirect/代理/自定义 resolve。

## 自测：读完 P3 应该能回答

1. SandboxMode、ApprovalPolicy、ApprovalsReviewer 为什么是三个轴？
2. hard deny、ask、allow 的优先级是什么？
3. InteractionRequest 为什么要 revision 和 idempotency key？
4. 为什么恢复时必须保留原 `tool_call_id`？
5. Full Access 具体取消了什么，又没有取消什么？
6. OCC 怎样防止 Agent 覆盖 IDE 刚改过的文件？
7. 当前网络 capability 为什么只支持直接 curl？

## 对后续阶段的影响

- P4 直接复用 InteractionRequest、plan hash、OCC 和 policy 状态做恢复。
- P5 从 audit、interaction 和 sandbox 事件建立 trace/eval 硬门。
- P8 child 复用同一个 PolicyEngine、ApprovalManager 和 InteractionManager，且 approval 额外绑定 child id。
- 选做 P3.1 将 worktree 作为 WebUI workspace 生命周期增强；它未排期、未实现，不改变 P3 sandbox/policy 语义，也不给普通 Agent Shell 新增 Git common dir 写权。
