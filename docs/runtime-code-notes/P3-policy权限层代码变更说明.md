# P3 Policy 权限层代码变更说明

> 对应计划：`docs/plans/runtime-steps/P3-policy权限层.md`
> 当前状态：仅规划，尚未执行；本文件用于同步已经确认的阶段边界，不表示相关代码已经落地。
> 2026-07-15 方案修订：HITL 扩展为三档持久化 `InteractionRequest`；共享 workspace 文件租约不属于 P3 必做。
> 2026-07-16：计划压缩后仍以 policy gate、三档 HITL、参数绑定 approval 和已有文件 OCC 为唯一必做范围。
> 2026-07-16：P1 已新增普通 WebUI 自动激活计划和 plan-only 显式确认；P3 只持久化确实需要人工确认的 plan confirmation，工具风险审批仍独立执行。
> 2026-07-16：核对现有 WebUI 后确认 Default Permission / Full Access 已实现为会话级 `WorkspaceScope`，不是工具审批；P3 必须复用该 scope 与现有 security hard boundary，不得平行重建权限或 sandbox。

## 阶段目标

P3 计划在现有 ToolRegistry、AgentRunner、WebSocket 和文件工具外增加最小治理层：

- 工具调用执行前统一 allow / ask / deny，并消费当前会话的 `WorkspaceScope`。
- 通用问题、审批、需要人工确认的 plan-only/手动计划和恢复决定使用持久化 InteractionRequest。
- `required`、`auto_resolve`、`expire_and_deny` 三档等待策略恢复同一 task/turn。
- 等待期间当前 LLM 调用已经结束，不产生 token；回答或 deadline 只消费一次。
- 高风险 approval 超时永不自动放行。
- 已有文件通过 actor-local fresh-read SHA-256 OCC 防止过期读取静默覆盖。

## 当前代码状态

截至本次修订，以上 P3 Runtime 模块尚未实现；现有代码仅提供：

- `ToolRegistry.prepare_call` 的同步解析和参数校验。
- WebUI 持久化的 `WorkspaceScope`：Default Permission 对应 `restricted`，Full Access 对应 `full`；scope 已传递给文件、Shell 等工具。
- 浏览器 WebUI 的项目目录选择：`/api/workspaces/directories` 仅在持有效 API token 且连接来自 localhost（native surface 沿用既有本地宿主能力）时可用；接口只枚举已解析目录的直接子目录，最多 200 项，不暴露文件名或文件内容。选中的绝对路径仍必须经过既有 `WorkspaceScope` 校验，目录浏览本身不授予任何文件或 Shell 权限。
- `nanobot/security/` 的 workspace path guard、现有 SSRF / command guard 和 workspace sandbox 状态；它们是 P3 不可绕过的底层 hard boundary。
- P1 `execution_mode` 的单回合 plan-only 工具收敛，以及普通 WebUI plan 自动激活；这不是通用 policy gate 或持久化 HITL。
- AgentRunner 的正常工具结果/错误执行路径。
- WebSocket 通用事件通道。
- `agent/tools/file_state.py` 的会话级 read-before-edit 状态与内容 hash。

因此当前项目虽已有计划模式边界，但尚不能宣称已具备通用 allow/ask/deny、三档 HITL、durable suspension 或参数绑定 approval。

当前 `FileStates` 只会为 `edit_file` 产生 read-before-edit 警告；`write_file` 与 `apply_patch` 尚无 P3 要求的已有文件 fresh-read preflight。因此现有 hash 记录是 OCC 的复用基础，不是已完成的 OCC hard gate。

## 已确认的实现边界

- `runtime/interactions.py` 负责公共 InteractionRequest、状态、deadline 和幂等消费。
- `runtime/approvals.py` 只负责参数/计划绑定的 `expire_and_deny` 安全审批。
- `runtime/policy.py` 只接收已解析的 `WorkspaceScope` 和规范化参数；不得重复实现 workspace/path/sandbox 解析。Default profile 默认 ask 修改已有本地文件和高风险本地 Shell；Full Access 可 allow 这些本地操作，但两者均不放宽 OCC、hard deny 或外部副作用审批。
- 合法等待使用 `awaiting_question|awaiting_approval|awaiting_plan_confirmation|awaiting_recovery_decision`，不能恢复成普通工具失败。
- 普通 WebUI 自动激活 plan 不产生 `awaiting_plan_confirmation`；自动激活也不能替代任何高风险工具 approval。
- 当前 LLM 调用结束、Runner 资源可释放，但 task/turn 不发送 completed。
- P3 文件必做项是已有文件 OCC；子 Agent 文件租约由 P8 作为选做增强评估。

## 后续验证要求

- 三档策略、回答/deadline 竞争、重复响应和重启恢复的确定性测试。
- 等待期间 provider 调用次数不增加。
- approval 超时自动放行次数为 0。
- 未读、读后变化、mtime 不变但 hash 变化和多文件 preflight 冲突均硬失败。
