# P4 Input Snapshot + Artifact Lineage + Durable Checkpoint — 详细步骤

> 所属：`docs/plans/Mybot通用AgentRuntime与办公自动化SkillPack整合方案.md`
> 状态：仅规划，未执行。2026-07-14 移除冻结前白盒记忆主线，聚焦可重放输入、产物血缘和安全恢复。
> 阶段出口：已确认计划任务拥有不可变输入快照、可查询产物血缘和 completed/pending/uncertain 恢复语义。

---

## S4.1 不可变输入快照

任务开始时把实际使用的输入复制到：

```text
.nanobot-runtime/artifacts/<task_id>/inputs/
```

metadata 记录原路径、snapshot 路径、SHA-256、大小、时间与复制状态。后续 facts、DSL、OfficeCLI 命令和最终产物引用 snapshot，不再直接读取会变化的源文件。

无法或不允许复制的大文件可使用 `reference_only`：

- 仍记录路径与 checksum。
- 标记 `replayable: false`。
- 不计入 100% 可重放指标。
- 文件变化后必须重新创建任务或输入版本。

## S4.2 通用 Artifact Store

新增 `nanobot/runtime/artifacts.py`：

- `register(path, task_id, skill, type, source_artifacts, tool_calls, status, child_id=None)`。
- 计算 checksum，写 sidecar metadata。
- `get/list/lineage`。
- 路径必须位于 workspace 与任务 artifact 根内。
- `plan.json` 与输入 snapshot 都是一等 artifact。

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
  "status": "validated",
  "replayable": true
}
```

## S4.3 双 Office Skill 接入

- `office-automation` 登记 shared facts、自有 DSL、quality report、docx/pptx。
- `officecli` 登记 shared facts（数据任务）、自有命令/batch、engine version、validation、run metadata、preview 与成品。
- 两个 Skill 不要求相同中间 artifact，但都必须回溯到输入 snapshot 和 verified facts。
- 用户已有文件只有经 P3 approval 修改后才登记新版本；不把覆盖后的同一路径伪装成不可变产物。

## S4.4 Checkpoint 落盘

复用 `AgentRunSpec.checkpoint_callback` 的 awaiting_tools/tools_completed/final_response 阶段，在 `nanobot/runtime/checkpoint.py` 落盘。

只为以下任务启用：

- 已创建 `plan.json`。
- plan hash 已由用户确认。
- task_id 与 artifact 根已建立。

普通问答和短任务不产生 durable checkpoint。

checkpoint 至少包含：

- task/plan/step 状态。
- assistant message 与工具调用。
- completed/pending/uncertain 调用集合。
- pending approval 引用。
- input/artifact checksum 引用。
- 父子 Agent 状态摘要（P8 接入后）。
- state hash 与恢复说明。

## S4.5 安全恢复语义

不宣称通用 exactly-once。

- `completed`：结果与 checkpoint 已持久化，恢复时跳过。
- `pending`：尚未执行，或具备幂等键/可验证产物，可安全执行或重放。
- `uncertain`：外部副作用可能已发生但未持久化完成状态，必须暂停并让用户决定。

Office 生成可通过目标 checksum、validation sidecar 和 run metadata 判断是否已完成。邮件、消息、删除和无幂等键远程写操作不能仅凭 tool_call id 自动重试。

## S4.6 恢复入口

- 提供 CLI/API 读取最近合法 checkpoint。
- 校验 plan hash、input snapshot、artifact checksum 和 pending approval。
- checkpoint 损坏、引用缺失或状态冲突时 fail loud，不静默从头重跑。
- 恢复动作写 audit，P5 接入 trace `resume_from=checkpoint_id`。

## S4.7 Artifact 展示

WebUI 只需列出和下载任务产物；lineage 通过 CLI/JSON 展示，不做前端图。现有 Office artifact panel 继续识别 OfficeCLI sidecar 与 preview。

## S4.8 机动项

- Artifact v2/delta 局部重渲染。
- staging/正式 artifact 不可变发布模型。
- 白盒记忆条目、Dream change set、召回审计和回滚。

这些不进入冻结前 P4/P7 必做验收。

## 定向测试

- 输入复制后修改源文件，不影响 task snapshot checksum。
- reference-only 标记不可重放。
- Python/OfficeCLI 产物均可回溯到同一输入和 facts。
- 普通聊天不写 durable checkpoint。
- 已确认计划任务写三阶段 checkpoint。
- completed 跳过、pending 安全恢复、uncertain 停止并请求用户。
- checkpoint 损坏/计划 hash 改变/输入 snapshot 缺失时拒绝恢复。

## 阶段出口检查

- [ ] 输入 snapshot 默认启用，reference-only 诚实标注。
- [ ] 两个 Office Skill 全链血缘可查。
- [ ] Durable checkpoint 只服务已确认计划任务。
- [ ] 恢复使用 completed/pending/uncertain，不以 tool_call id 宣称 exactly-once。
- [ ] kill → resume demo 能恢复可验证 Office 任务；未知副作用不会自动重试。
- [ ] 白盒记忆不阻塞阶段出口。
