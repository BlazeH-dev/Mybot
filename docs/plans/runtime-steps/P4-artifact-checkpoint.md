# P4 Input Snapshot、Artifact Lineage 与 Durable Checkpoint

> 状态：已完成（2026-07-18）。白盒记忆、artifact delta/staging 为选做。
> 出口：输入和产物可追踪；已激活且 hash 绑定的计划可安全 kill→resume；uncertain 副作用不自动重试。

## 1. 不可变输入

任务实际使用的输入复制到：

```text
.nanobot-runtime/artifacts/<task_id>/inputs/
```

记录原路径、snapshot 路径、SHA-256、大小、时间和复制状态。后续 facts、DSL/命令和成品引用 snapshot，不再直接依赖变化的源文件。

无法复制时允许 `reference_only`：保存路径与 checksum，标记 `replayable:false`；变化后必须创建新任务/输入版本，不计入可重放率。

## 2. Artifact Store

`runtime/artifacts.py` 提供 `register/get/list/lineage`，路径必须位于 workspace 与 task artifact 根内。

最小 metadata：

```json
{
  "artifact_id": "art_001",
  "task_id": "task_001",
  "skill": "officecli",
  "child_id": null,
  "type": "pptx",
  "path": "...",
  "checksum": "...",
  "source_artifacts": ["input_snapshot", "verified_facts"],
  "tool_calls": ["tool_005"],
  "status": "validated",
  "replayable": true
}
```

- plan、输入、facts、Skill 中间产物、验证报告和成品都是一等 artifact。
- 两个 Office Skill 可以有不同中间表示，但都回溯到输入和 facts。
- 经 P3 approval 修改用户已有文件后登记新版本，不能把覆盖后的路径伪装成不可变产物。

## 3. Checkpoint 范围

`runtime/checkpoint.py` 复用 Runner checkpoint callback，仅对同时满足以下条件的任务落 durable checkpoint：

- 已创建 plan；
- plan 状态为 active/completed，且 `approved_plan_hash` 等于当前 plan hash；激活来源为自动或显式确认；
- task id 与 artifact 根已建立。

普通问答不落完整 checkpoint；其待处理问题由 P3 InteractionRequest 自身持久化。

checkpoint 保存 task/plan/step、assistant tool call、completed/pending/uncertain 集合、InteractionRequest 引用与 deadline、input/artifact checksum、P8 child 摘要和 state hash。

## 4. 恢复语义

- `completed`：结果已持久化，恢复时跳过。
- `pending`：尚未执行，或有幂等键/可验证产物，可安全重放。
- `uncertain`：外部副作用可能发生但状态未知，进入 `awaiting_recovery_decision(required)`。

合法 suspension 单独表示：

- `awaiting_question`：required 或 auto_resolve。
- `awaiting_approval`：expire_and_deny。
- `awaiting_plan_confirmation`：只用于 plan-only/手动计划，不得超时自动确认。
- `awaiting_recovery_decision`：默认 required。

等待期 LLM token 为 0；回答/deadline 原子恢复同一 task/turn。恢复保持原 assistant tool call 和 tool_call_id，将回答或超时结果作为匹配 tool result，不能补成普通 interrupted error。

邮件、消息、删除和无幂等键远程写不能仅凭 tool_call id 自动重试；Office 生成可用 checksum、validation sidecar 和 run metadata 判断完成状态。不宣称 exactly-once。

## 5. 恢复入口与展示

- CLI/API 读取最近合法 checkpoint，校验 plan hash、input/artifact checksum 和 pending interaction。
- checkpoint 损坏、引用缺失或状态冲突时 fail loud，不静默从头执行。
- 恢复动作写 audit，P5 trace 记录 `resume_from`。
- WebUI 只列出/下载 artifact；lineage 首版用 CLI/JSON，不做前端图。

## 测试与出口

- snapshot 后修改源文件不影响任务输入；reference_only 诚实标注不可重放。
- 两个 Office Skill 的成品都可回溯到输入和 facts。
- 普通聊天不写完整 checkpoint；已激活且 hash 绑定的计划任务写必要阶段状态。
- completed 跳过、pending 安全恢复、uncertain 转 required。
- 三档 InteractionRequest 跨刷新/重启恢复，回答/deadline 只消费一次，等待期不调用 provider。
- checkpoint 损坏、plan hash 改变、input/artifact 缺失均拒绝恢复。
- kill→resume 回归报告能验证 Office 任务恢复，未知副作用不自动重试。
