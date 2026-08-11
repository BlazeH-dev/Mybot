# P4 Input Snapshot、Artifact Lineage 与 Durable Checkpoint 代码说明

> 对应计划：`docs/plans/runtime-steps/P4-artifact-checkpoint.md`
> 当前状态：Core 已完成。artifact delta/staging、白盒记忆治理和通用 exactly-once 未实现。

## 这一阶段解决什么问题

P3 解决“一次工具调用能否安全执行”，但长任务还会遇到另外一组问题：

- 用户上传的 Excel 在任务执行过程中被替换了，最终报告到底基于哪一版？
- DOCX/PPTX 是从哪个输入、哪组 facts、哪个 Skill 和哪些 tool call 生成的？
- 网关被 kill 后，哪些工具已经完成，哪些还没开始？
- 一条消息可能已经发出但进程没来得及保存结果，重启后能否自动重发？
- checkpoint 被手工改过、plan 变了或 artifact 丢了，系统是否会静默继续？

P4 把“文件产物追踪”和“执行状态恢复”分开处理：

```text
ArtifactStore
  管输入和产物：它是什么、在哪里、checksum、来源是谁

CheckpointStore
  管执行状态：模型已经返回什么、哪些 tool call 完成/待执行/不确定
```

## 先区分三个容易混淆的概念

### Input snapshot

任务开始时把输入复制到 task 目录，并记录原路径和 checksum。它解决“任务到底看的是哪一版输入”。

### Artifact lineage

给每个输入、中间文件和成品登记 metadata，并用 `source_artifacts` 形成血缘。它解决“这个结果从哪里来”。

### Checkpoint

保存一次 Agent turn 的执行进度和恢复分类。它解决“进程中断后从哪里继续”。

Artifact 不是 checkpoint，checkpoint 也不是历史聊天全文的另一个副本。

## 1. 输入快照怎样实现

文件：`nanobot/runtime/artifacts.py`

`ArtifactStore.snapshot_input()` 的步骤：

1. resolve 原路径。
2. 读取原文件 SHA-256 和大小。
3. 创建 `.nanobot-runtime/artifacts/<task_id>/inputs/`。
4. 复制到临时文件。
5. 再算一次临时文件 checksum，必须与源一致。
6. 用 `os.replace()` 原子替换成最终 snapshot。
7. 登记 `ArtifactRecord`。

同名输入冲突时会在文件名中加入 checksum 前缀，避免两个不同目录的 `report.xlsx` 相互覆盖。

### reference_only 降级

如果复制失败，不是假装快照成功，而是：

```text
path = 原文件路径
snapshot_status = reference_only
replayable = false
checksum = 当时读取到的源 checksum
```

这是一种“诚实降级”：任务仍可继续引用原文件，但 P5 replayability 不能把它算作可重放输入。

注意，当前 snapshot 是 task-local copy + checksum，不是操作系统只读文件。若 snapshot 后来被修改，Artifact/Checkpoint 校验会发现 checksum 不一致并拒绝恢复。

## 2. ArtifactRecord 记录什么

每个 artifact 最少记录：

```text
artifact_id / task_id / type / path
checksum / size / created_at
skill / engine / engine_version / child_id
source_artifacts / tool_calls
status / replayable
source_path / snapshot_status / metadata
```

这让最终 PPT 可以回答：

- 是哪个 task 生成的？
- 使用 Python Skill 还是 OfficeCLI？
- 来自哪些 input snapshot 和 verified facts？
- 哪些工具调用参与生成？
- 当前文件是否仍与登记 checksum 一致？

### 路径边界

`register()` 要求普通 artifact 位于 workspace 或 task root 内；如果带 `child_id`，还必须位于：

```text
.nanobot-runtime/artifacts/<task_id>/children/<child_id>/
```

因此 child 不能把父任务正式目录中的文件冒充成自己的产物。

### Index 与 upsert

每个任务的 metadata 写在：

```text
.nanobot-runtime/artifacts/<task_id>/artifacts.json
```

相同 `artifact_id` 会更新记录。例如 `plan` 使用固定 artifact id；计划状态改变后重新保存，checksum 也同步刷新，避免 checkpoint 永远引用 create 时的旧 plan。

### Lineage 递归

`lineage(task_id, artifact_id)` 递归展开 `source_artifacts`：

- source 不存在时明确标记 missing。
- 出现循环时抛 `artifact_lineage_cycle`。
- 不会无限递归或静默丢失依赖。

## 3. PlanTool 怎样接入 snapshot 和 artifact

文件：`nanobot/agent/tools/plan.py`

WebSocket 附件路径通过本轮 metadata 的 `_runtime_input_paths` 进入 PlanTool。`plan(create)` 时：

1. 创建 task id 和 plan contract。
2. 计算稳定 `plan_hash`。
3. 对附件路径去重并调用 `snapshot_inputs()`。
4. 把返回的 input artifact id 写入 plan 的 `input_artifacts`。
5. 把 `plan.json` 本身登记为 artifact，source 指向这些 input。

之后 confirm、update_step、complete 每次 `_save()` 都重写 plan，并 upsert 同一个 `plan` artifact checksum。

这说明 plan 不只是 UI 状态，也是任务血缘中的一等产物。

## 4. 什么任务才有 durable checkpoint

文件：`nanobot/runtime/checkpoint.py`

`CheckpointStore.eligible(plan)` 要求：

```text
plan.status in {active, completed}
plan_hash 是字符串
approved_plan_hash == plan_hash
```

因此：

- 普通闲聊不写完整 durable checkpoint。
- plan-only 还没确认时不写。
- 用户确认的是旧 hash、计划后来变化时不写。
- 普通 WebUI 自动激活的计划会把当前 hash 作为 approved hash，因此可写。

为什么这么严格？checkpoint 会导致系统未来继续执行，必须先证明任务契约已经激活且没有被确认后篡改。

## 5. Checkpoint 保存什么

路径：

```text
.nanobot-runtime/checkpoints/<task_id>.json
```

主要字段：

```text
task_id / session_key
plan_hash / approved_plan_hash / plan_status
runner payload
completed / pending / uncertain tool call ids
interaction request ids
children 字段
artifact metadata/checksum
state_hash
```

当前 AgentLoop 写入 runner 状态和 pending interaction；schema 支持传入 children 摘要，但主循环当前没有把完整 child 状态表作为每次 checkpoint 的强制内容，child 的生命周期主要由 P8 artifact、announcement 和 trace 记录。面试时不要把“字段支持”说成“所有 child 状态都已完整 checkpoint 化”。

### state hash

写入前把除 `state_hash` 外的 JSON 按 key 排序、紧凑序列化，再做 SHA-256。加载时重新计算：

- 不一致：`checkpoint_hash_mismatch`。
- expected plan hash 不一致：`plan_hash_mismatch`。
- artifact 丢失：`artifact_missing`。
- artifact 内容改变：`artifact_checksum_mismatch`。

这叫 fail loud：恢复条件不可信时明确报错，不静默从头执行。

## 6. completed、pending、uncertain 为什么必须分开

`CheckpointStore.write()` 根据 pending tool 的名称分类：

- `message`、`cron`、`mcp_*` 视为 uncertain。
- 其他尚未完成调用视为 pending。
- 已有 tool result 的调用视为 completed。

### completed

结果已经持久化。恢复时保留原 tool result，不重复执行。

### pending

工具还没有完成，可以给模型一个结构化结果：

```json
{
  "status": "pending_recovery",
  "safe_to_retry": true,
  "reason": "tool had not completed before interruption"
}
```

它不宣称工具自动 exactly-once，只告诉后续执行链这次调用没有完成且可重新发起。

### uncertain

外部副作用可能已发生，但 Runtime 没有可靠结果。例如消息已经到达远端，进程在保存 tool result 前崩溃。

此时不能自动重试，否则可能重复发送。AgentLoop 创建：

```text
InteractionKind.RECOVERY_DECISION
InteractionStrategy.REQUIRED
```

用户选择 retry、mark completed 或 cancel。创建卡片后、用户回答前 provider 调用为 0。

## 7. Interaction suspension 怎样与 checkpoint 合并

合法等待状态包括：

```text
awaiting_question
awaiting_approval
awaiting_plan_confirmation
awaiting_recovery_decision
```

Runner checkpoint 保存原 assistant tool call、已经完成的 tool results、pending calls 和 interaction payload。

用户回答后，`AgentLoop._materialize_interaction_response()`：

1. 读取持久化 request。
2. 确认它不再 pending。
3. 找到 checkpoint 中匹配的原 `tool_call_id`。
4. 用 typed resolution 替换该 tool result。
5. 把 phase 改成 `tools_completed`。
6. 按 kind 决定何时 consume interaction。
7. 计算 `human_wait_ms` 并写 trace event。

普通聊天消息不会消费 pending interaction，也不能让模型“假装已经批准”。

## 8. kill 到 resume 的路径

```text
AgentRunner 每个关键阶段调用 checkpoint_callback
  -> AgentLoop 先写 session.metadata.runtime_checkpoint
  -> 若 plan eligible，再写 durable checkpoint 文件
  -> 进程被 kill 或 /stop
  -> 新 AgentLoop / SessionManager 启动
  -> 从 session 或 durable 文件读取 runner payload
  -> 校验 state_hash、plan_hash、artifact checksum
  -> 若有 uncertain，先创建 required recovery card
  -> completed tool result 保留
  -> pending/uncertain 形成结构化 tool result
  -> 恢复后的消息去重后写回 session history
```

正常完成后会清理 in-flight checkpoint；artifact、plan 和 index 继续保留作为交付证据。

## 为什么不宣称 exactly-once

exactly-once 意味着一个副作用无论崩溃发生在哪里都只发生一次。仅靠本地 checkpoint 和 tool call id 做不到，因为：

- 远端可能成功，本地没收到响应。
- 本地可能收到响应，还没持久化就崩溃。
- 对方系统不一定支持幂等键或状态查询。

Mybot 采用更诚实的语义：

- 本地可验证、未开始的操作可安全重试。
- 有 checksum/sidecar 的成品可验证完成状态。
- 状态未知的外部副作用必须问用户。

这是面试中的加分点：不要为了听起来高级而把 at-least-once/unknown 说成 exactly-once。

## 验证与证据

`tests/runtime/test_artifacts_checkpoint.py` 覆盖：

- snapshot 后源文件变化不影响副本。
- 复制失败时 reference-only 和 replayable=false。
- artifact register/list/get/verify 和递归 lineage。
- child artifact 路径边界。
- plan/hash/state/artifact 校验失败。
- durable restart、completed/pending/uncertain 恢复。

`tests/runtime/test_plan_interaction.py` 覆盖附件路径到 input artifact、plan artifact checksum 更新和 typed confirmation。

kill→resume 使用新的 SessionManager/AgentLoop 读取磁盘文件，不是只在同一 Python 对象中暂停再继续。

## 未实现和不能夸大的部分

- 不保证通用 exactly-once。
- 没有 artifact delta、staging 或版本化大文件存储。
- lineage 当前主要通过 JSON/代码查看，没有完整前端图。
- `reference_only` 任务不能算完全可重放。
- child checkpoint 摘要字段不是完整分布式任务调度器。
- 白盒长期记忆治理未在 P4 Core 落地。

## 面试怎么讲

### 30 秒回答

> P4 我把输入、产物和执行状态分开治理。输入在 plan create 时复制到 task 目录并记录 SHA-256；所有中间产物和成品登记 ArtifactRecord，通过 source_artifacts 形成血缘。只有 active/completed 且 approved_plan_hash 等于当前 plan_hash 的任务才写 durable checkpoint。恢复时把工具分成 completed、pending、uncertain；未知外部副作用绝不自动重试，而是创建 required recovery decision，所以我不宣称通用 exactly-once。

### 高频追问

**Artifact 和 Checkpoint 有什么区别？**

Artifact 是任务数据和文件的事实记录；Checkpoint 是执行控制状态。一个回答“产出了什么”，一个回答“运行到哪里”。

**为什么 plan hash 要进入 checkpoint eligibility？**

防止用户确认 A 计划后，系统把计划改成 B 仍沿用旧确认继续执行。只有当前 contract hash 与 approved hash 完全一致才可恢复。

**为什么 uncertain 不直接查询远端？**

如果具体工具提供可靠查询或幂等能力，可以在工具级加入验证；Runtime Core 不能假设所有消息、邮件、MCP 和远程系统都有统一查询语义。

**snapshot 复制失败为什么不直接终止？**

有些输入可能因权限或文件系统限制无法复制。reference-only 允许任务继续，但明确降低 replayability；是否接受由上层任务和 eval 决定。

## 自测：读完 P4 应该能回答

1. input snapshot、artifact lineage、checkpoint 各解决什么问题？
2. 为什么只有 hash-bound active/completed plan 才能落 durable checkpoint？
3. completed、pending、uncertain 的恢复语义分别是什么？
4. 为什么外部副作用不能自动 exactly-once？
5. state hash、plan hash、artifact checksum 分别防什么？
6. reference-only 为什么必须标记 replayable=false？

## 对后续阶段的影响

- P5 的 replayability、artifact completion、OpenXML 和 trace 都以 P4 metadata 为证据。
- P8 child 产物必须进入固定 child root，父 Agent 再汇总或发布正式 artifact。
- P7 最终结果页可以用可复现报告展示输入快照、lineage、kill→resume 和 uncertain 决策。

## 2026-08-11：Plan DAG、Markdown 与确定性完成

- 新增 `nanobot/runtime/plan_scheduler.py`：DAG validation、topological batches、节点状态机、增量 revision 复用、running node recovery 和固定 Markdown renderer。
- `PlanTool` 固定原子生成当前任务的 `plan.md`，ArtifactStore 固定 id 为 `plan_markdown`；metadata 绑定当前 revision/hash/checksum，修订覆盖文件但不覆盖结构化 revision、checkpoint 和 trace 历史。
- `CheckpointStore` 持久化 plan revision、node recovery sets 和 children；`AgentLoop` 在下一次继续任务前对孤立 running node 做恢复分类。
- `plan complete` 在节点终态与 expected artifact 路径/存在性校验通过后直接标记 `completed`；不再生成 `reflection-rN.json` 或持久化审查状态。
- 定向回归覆盖 Markdown 稳定章节、单文件 revision 替换、旧 `plan-rN.md`/`plan_md_rN` 迁移清理、tamper 检测、orphan child→uncertain→typed retry、kill/resume 和 complete 闸门。
- WebUI 预览跨 workspace 的当前 `plan.md` 时，HTTP 层从 session `plan_state` 提取精确绑定并复核 task/revision/hash/artifact id/path/SHA-256；文件被替换或内容变化时返回 checksum 错误，不展示 stale/tampered artifact。

## 2026-08-11：Failed Node Resume

- `PlanScheduler.retry_failed()` 在不修改 contract/revision/hash 的前提下保存失败 attempt 摘要、增加 retry count，并清除旧 child/artifact/result/error 执行绑定后回到 ready。
- `PlanTool` 新增 hash-bound `resume`；只接受 active confirmed plan，显式重试 failed 节点、重新计算 DAG 并派发 ready child。`failed → running/in_progress` 的模型调用兼容转换为同一安全 retry 路径。
- active 计划卡片新增“继续执行”，即使历史 card 仍显示 running 也保留恢复入口；按钮提示模型先调用 `plan.resume`，不得直接把 failed 标记为 succeeded。

## 2026-08-11：固定计划文件与会话级单卡片

- `_plan_markdown_path()` 固定返回 task artifact root 下的 `plan.md`；写入使用唯一临时文件和 `os.replace()`，登记后再次调用 ArtifactStore verify。
- `ArtifactStore.remove()` 只删除指定 task 的精确 artifact id 记录。PlanTool 在新文件验证成功后清理 `plan_md_rN` 记录，再删除精确匹配 `plan-r<digits>.md` 的文件，避免 checkpoint 引用已移除历史文件。
- `_ensure_plan_markdown()` 可识别并验证当前 revision 的旧绑定，迁移后同步回写 `plan.json`、session metadata 和 checkpoint；其他 stale path/id/hash 继续 fail loud。
- `session_plan_preview_files()` 只接受固定 id `plan_markdown` 和固定尾路径 `.nanobot-runtime/artifacts/<task_id>/plan.md`。
- WebUI 从完整 `displayMessages` 提取最新 plan snapshot，`ThreadMessages` 不再渲染卡片；`ThreadShell` 在 Header 与 Viewport 之间维护唯一实例，并按 session key 将单行折叠状态写入 localStorage。

## 2026-08-11：修订计划单击确认使用完整 Hash

- 等待初次或 revision 确认时，`plan_state_runtime_lines()` 保留完整 `plan_hash`，并给出可直接调用 `plan(confirm)` 的精确 `expected_plan_hash`；active 等非确认状态仍使用短 hash 保持上下文紧凑。
- typed plan confirmation 恢复 checkpoint 时，替换后的 plan tool result 显式携带 `task_id` 和完整 `plan_hash`，避免 Agent 从展示用短 hash 猜测确认参数。
- 回归覆盖完整 hash 的 Runtime 注入和 interaction materialization，保证计划卡片单击执行后不会先触发 hash mismatch、再 `plan(get)`、再确认。
