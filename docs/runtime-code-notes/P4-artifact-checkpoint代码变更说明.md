# P4 Artifact / Checkpoint 代码变更说明

> 对应计划：`docs/plans/runtime-steps/P4-artifact-checkpoint.md`
> 当前状态：仅规划，尚未执行；本文件只同步阶段恢复语义，不表示 durable checkpoint 已落地。
> 2026-07-15 方案修订：P4 必须区分合法 `awaiting_*` 与 completed/pending/uncertain 工具状态。
> 2026-07-16：计划激活分为普通 WebUI 自动激活与 plan-only/手动显式确认；checkpoint 统一以 active/completed 且 hash 绑定为准。

## 已确认的阶段边界

- Durable checkpoint 只服务 active/completed 且 `approved_plan_hash` 等于当前 hash 的复杂任务；自动激活和显式确认都可满足，awaiting confirmation 不可满足。
- checkpoint 保存 InteractionRequest 引用、等待策略、deadline、continuation 和原 tool_call_id。
- `awaiting_question|approval|plan_confirmation|recovery_decision` 是合法 suspension，不是工具执行错误；plan confirmation 仅用于 plan-only/手动计划。
- 用户回答或 deadline 恢复同一 task/turn，迟到事件不能触发第二次执行。
- 等待期间的人类时长与模型/工具执行时长分开统计，等待期 token 为 0。
- uncertain 外部副作用使用 `required` 人工决定；高风险 approval 使用 `expire_and_deny`。

## 当前代码状态

现有 `AgentLoop` runtime checkpoint 主要服务中断上下文保留，会把未完成 tool call 补为 interrupted error。P4 实现时必须新增合法等待恢复路径，不能直接复用该错误补全语义。

## 后续验证要求

- required、auto_resolve、expire_and_deny 均可跨刷新、断线和重启恢复。
- 原 assistant tool call 与回答 tool result 能保持 protocol 配对。
- 回答/deadline 原子竞争、checkpoint 损坏和计划 hash 变化均 fail loud。
