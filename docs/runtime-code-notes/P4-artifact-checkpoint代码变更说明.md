# P4 Artifact / Checkpoint 代码变更说明

> 对应计划：`docs/plans/runtime-steps/P4-artifact-checkpoint.md`
> 当前状态：必做项已完成（2026-07-18）；artifact delta/staging 与白盒记忆治理未实现。

## 阶段结果

已激活且 `approved_plan_hash == plan_hash` 的计划任务具备不可变输入、artifact lineage、hash-bound durable checkpoint 和 completed/pending/uncertain 恢复语义；未知外部副作用不会自动重试。

## 代码落点

- `nanobot/runtime/artifacts.py`
  - 输入复制到 `.nanobot-runtime/artifacts/<task_id>/inputs/`。
  - 复制失败使用 `reference_only`，保留源 checksum 并标记 `replayable:false`。
  - `register/get/list/lineage/verify` 记录 checksum、类型、Skill/engine/version、child、sources、tool calls 和状态。
  - child artifact 强制位于 `children/<child_id>/`。
- `nanobot/agent/tools/plan.py`
  - create 消费 `_runtime_input_paths` 并生成 input artifact。
  - plan 自身登记为 artifact；confirm/update/complete 每次重写后同步刷新 checksum，避免 checkpoint 引用陈旧 plan 版本。
- `nanobot/runtime/checkpoint.py`
  - 仅 active/completed 且 plan hash 已批准的任务写 `.nanobot-runtime/checkpoints/<task_id>.json`。
  - 保存 runner payload、completed/pending/uncertain、interaction、children、artifact checksums 和整体 `state_hash`。
  - load 对 checkpoint hash、plan hash、artifact 缺失/变化 fail loud。
- `nanobot/agent/loop.py`
  - session checkpoint 与 durable 文件同步。
  - 合法 `awaiting_*` 保持等待；typed response 替换原 tool result。
  - safe pending 恢复为 `pending_recovery/safe_to_retry`，不再伪装成普通 interrupted error。
  - message/cron/未知 MCP 等 uncertain call 自动创建 `recovery_decision(required)`；回答前 provider 调用为 0。
  - `/stop` 对已计划任务保留 durable checkpoint，下一回合从验证后的状态恢复。

## 恢复语义

- `completed`：保留已持久化 tool result，恢复时跳过。
- `pending`：注入结构化 safe-to-retry result，由后续执行链重新发起，不宣称 exactly-once。
- `uncertain`：创建必答恢复卡；用户可选择 retry、mark completed 或 cancel，Runtime 不自动重放外部副作用。
- `awaiting_question|approval|plan_confirmation|recovery_decision`：只接受对应 typed interaction response；普通聊天不消费 checkpoint。

## 验证

- `tests/runtime/test_artifacts_checkpoint.py`：快照不可变、reference-only、递归 lineage、child 路径、hash/plan/artifact 校验、durable restart、uncertain recovery。
- `tests/runtime/test_plan_interaction.py`：附件路径到输入快照的端到端流转、plan artifact checksum 随状态更新。
- kill→resume 测试通过新 `SessionManager/AgentLoop` 重新加载 durable 文件，证明不是仅依赖进程内对象。

## 边界

- 不承诺通用 exactly-once；外部系统的实际状态仍需用户或幂等/查询能力确认。
- WebUI 只展示 interaction 与已有 artifact 面板；lineage 继续通过 JSON/CLI 查看。
- completed plan 的最终 artifact/index 保留；只清理不再需要的 in-flight checkpoint。
