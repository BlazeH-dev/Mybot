# P3 Sandbox / Policy 权限层代码变更说明

> 对应计划：`docs/plans/runtime-steps/P3-policy权限层.md`
> 当前状态：必做项已完成（2026-07-18）；`auto_review` 与通用网络代理仍为选做/后续能力。

## 阶段结果

P3 已把原有 workspace 应用层检查升级为 Runtime 硬门：Default Permission 的 Agent 命令由真实 OS provider 强制，工具执行前统一经过 `allow / ask / deny`，人机等待可持久化恢复，已有文件使用 actor-local SHA-256 OCC。

## 代码落点

- `nanobot/security/sandbox/`
  - `types.py`：`SandboxMode`、`SandboxStatus`、`LaunchSpec` 与结构化错误。
  - `manager.py`：macOS Seatbelt、Linux/WSL2 Bubblewrap 的真实 smoke；缺 provider/原生 Windows fail closed。
  - `launcher.py`：Exec、CLI Apps、OfficeCLI/Skill helper、Subagent 共用启动入口。
  - `seatbelt.py` / `bwrap.py`：workspace/artifact 写边界、home/runtime control/`.git` 保护、隔离 `/tmp`、默认断网。
  - `network.py`：命令/hash/域名/端口/过期/DNS 地址绑定；restricted 网络只允许无 shell 组合、无 redirect/代理/resolve override 的直接 `curl`，执行时改为直 argv，并用 `--resolve` 固定到审批时公网 IP，阻断 DNS rebinding 与目标替换。
- `nanobot/runtime/policy.py`：确定性策略、Full/Default/read-only 映射、外部副作用/MCP 默认 ask、受保护路径 hard deny、JSONL audit。
- `nanobot/runtime/interactions.py` / `approvals.py`：按 request 原子落盘、revision/idempotency、三档 deadline、参数/plan/child/sandbox/network 绑定的一次性 approval。
- `nanobot/agent/runner.py` / `loop.py`：typed `awaiting_*`、挂起后停止后续工具、回答作为原 `tool_call_id` 的 tool result 恢复；普通聊天不能绕过 pending card。
- `nanobot/agent/tools/interaction.py`：模型侧 `request_user_input`，支持 1–3 个单选、多选或自由文本问题。
- `nanobot/agent/tools/plan.py`：plan-only create 返回 `awaiting_plan_confirmation`，显式确认后才激活。
- `nanobot/channels/websocket.py` 与 WebUI：`interaction_request`、`interaction_updated`、typed response、刷新重放、审批/计划/问题/恢复卡片。
- `agent/tools/file_state.py`、`filesystem.py`、`apply_patch.py`：`write_file`、`edit_file`、`apply_patch` 对已有文件执行 current-actor fresh-read + SHA-256 preflight；多文件 patch 零部分写入。

## 状态与协议

- 持久化路径：`.nanobot-runtime/interactions/<request_id>.json`。
- 等待状态：`awaiting_question|awaiting_approval|awaiting_plan_confirmation|awaiting_recovery_decision`。
- 策略：`required` 不自动继续；`auto_resolve` 使用确定性默认值或明确标记 model best judgment；`expire_and_deny` 到期只拒绝。
- approval 绑定：tool、规范化参数 hash、task/plan/step/child、sandbox provider/mode、command hash、writable roots、domains/ports、审批时 DNS 地址、expires_at。
- `danger_full_access` 只取消本地 OS wrapper；OCC、凭据/受保护路径、SSRF、外发和远程写审批仍保留。

## 网络实现取舍

计划原先描述通用本地代理。Core 实现采用更窄的“固定公网 IP 的直接 curl capability”：不向任意命令开放网络，不支持 wget、redirect、shell pipeline、用户自带 proxy/resolve/config/interface/unix-socket 等目标改写能力。Seatbelt/Bubblewrap 对该单进程允许网络，但 curl argv 被 Runtime 重建并固定审批目标。通用域名代理、任意联网 CLI 和预配置 MCP 进程沙箱不在 P3 Core 承诺内。

## 覆盖边界

- 已覆盖：Exec one-shot/session、CLI Apps、由其启动的 OfficeCLI/Skill helper、Subagent 同类命令。
- 未覆盖：gateway/channel bootstrap、预配置 stdio MCP server 进程本体；MCP call 仍经过调用级 policy/approval，HTTP 目标仍经过 SSRF。
- `approvals_reviewer=auto_review` 未实现；P3 唯一 reviewer 为用户。
- 文件租约、shell 任意写的事务化拦截、新文件竞态完全消除均未实现，也不属于 P3 必做。

## 验证

- `tests/runtime/test_sandbox_policy_occ.py`：Seatbelt/Bubblewrap profile、真实 Seatbelt smoke、Linux real-provider smoke（CI 安装 Bubblewrap）、默认断网、DNS/IP 绑定、Windows/provider 缺失 fail closed、OCC。
- `tests/runtime/test_interactions_approvals.py`、`test_plan_interaction.py`、`test_websocket_interactions.py`：三档等待、重复/迟到/revision、普通聊天不可绕过、typed plan suspension、刷新/响应协议。
- 2026-07-20 修复 `request_user_input` 选项 schema：字段名 `description` 改为通过 `ObjectSchema.properties` 显式声明，避免与对象 schema 自身的 description 参数冲突，导致 `StringSchema` 对象进入 LLM 工具 JSON。`ObjectSchema` 同时对非字符串 root description 立即 fail-fast；回归直接对 AgentLoop 全部 20 个工具定义执行 JSON 序列化。
- 2026-07-20 修复 plan confirmation UI 桥接：`ToolSuspensionResult` 表示工具已成功但 turn 挂起，其 progress event 保留 result；WebUI “执行计划”按 task id/plan hash 直接提交 revision-bound typed response，不再用普通聊天绕过 pending interaction。
- `plan_confirmation` 仍持久化且可刷新恢复，但 WebUI 不在通用 interaction 列表重复渲染；只由计划卡片右下角快捷键消费。
- `tests/runtime/test_redteam.py`：workspace/凭据/MCP/child 绕过的后果为零。
- 本地结果：runtime `52 passed, 1 skipped`；跳过项是当前 macOS 主机上的 Linux Bubblewrap real smoke。

## 后续影响

P4 checkpoint、P8 child policy、P5 trace/eval 都复用同一 InteractionRequest、PolicyEngine、SandboxLauncher 和 OCC 状态，不再建立平行权限真相源。
