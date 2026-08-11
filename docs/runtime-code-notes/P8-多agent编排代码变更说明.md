# P8 受控 Subagent 编排代码说明

> 对应计划：`docs/plans/runtime-steps/P8-多agent编排.md`
> 当前状态：Core 已完成。Child 不设置 token/总时长/tool-call 工作量配额；共享 workspace 文件租约、自动共享父 artifact 和完整 child checkpoint 调度未实现。

## 这一阶段解决什么问题

nanobot 原本已有 SpawnTool/SubagentManager，可以后台启动子 Agent。但“能启动”不等于“可治理”。多 Agent 会引入：

- 模型无限派生，形成树状爆炸。
- child 绕过父任务计划和权限。
- child 读取完整父对话，造成上下文泄漏和 token 膨胀。
- 多个 child 写同一文件，互相覆盖。
- child 等用户输入时，问题不知道显示在哪个 chat。
- child 失败、被取消或死循环时，父 Agent误报成功。
- 多 Agent 看起来更快，但实际 token 和成本更高。

P8 不依赖外部图框架，但已经引入 Runtime 自有 `PlanScheduler`，在现有 subagent 上补 DAG 调度、硬约束、隔离、HITL、恢复和测量。

## 一句话架构

```text
父 Agent 拥有任务计划和最终责任
  -> active + hash-bound plan 的 child 节点由 PlanScheduler 自动派发
  -> child 只拿目标文本和自己的 restricted artifact root
  -> child 使用独立工具表、FileStates、Policy/HITL 和 trace span
  -> child 结果通过 MessageBus 回到父 session
  -> 父 Agent 负责核对、汇总和正式交付
```

多 Agent 的核心不是“多个模型一起聊天”，而是父子任务的授权链和隔离边界。

## 1. SpawnTool 的 active-plan gate

文件：`nanobot/agent/tools/spawn.py`

`set_context()` 从本轮 RequestContext 读取：

```text
_runtime_task_id
_runtime_plan_hash
_runtime_plan_status
_runtime_approved_plan_hash
```

`execute()` 只有同时满足以下条件才调用 manager.spawn：

```text
parent_task_id 存在
plan_status == active
plan_hash 存在
approved_plan_hash == plan_hash
```

否则返回稳定 `policy_denied`。

这防止模型在普通聊天、待确认计划、completed plan 或计划 hash 已变化时私自派生 child。

为什么要绑定 plan hash？因为用户同意的是一个具体任务契约。计划改了，旧授权不能继续用于创建新的执行主体。

## 2. 数量限制有两层

### 全局并发限制

`max_concurrent_subagents` 来自 AgentDefaults，默认值和配置上限均为 5。SpawnTool、PlanScheduler 和 SubagentManager 启动入口都会检查当前正在运行的 child 数量。

### 每父任务 direct child 上限

`SubagentManager.max_direct_children = 5`。`_spawned_by_parent` 记录某父任务当前仍在运行的直接 child；child cleanup 会释放计数，同一时刻第 6 个直接 child 被拒绝。

这两个限制不同：

- concurrency 控制 Runtime 同一时刻的总负载。
- direct child cap 控制一个父任务同一时刻的扇出。

两者当前上限都是 5，但作用域不同。已完成 child 会释放槽位，所以一个长 DAG 可以在多个批次中累计执行超过 5 个节点。

## 3. 为什么禁止嵌套 spawn

ToolLoader 按 `_scopes` 注册工具。SpawnTool 只使用默认 `core` scope，没有声明 `subagent` scope；SubagentManager 使用：

```python
ToolLoader().load(..., scope="subagent")
```

因此 child 的工具表根本没有 spawn，不是靠提示词告诉它“请勿继续派生”。

禁止嵌套带来：

- 最大深度固定为 1。
- child 数量容易计算。
- 权限和 trace 只有父->子一层。
- 避免递归爆炸和难以恢复的孙任务。

## 4. Child workspace 和上下文隔离

child root 固定为：

```text
.nanobot-runtime/artifacts/<parent_task>/children/<child_id>/
```

SubagentManager 为 child：

1. 创建该目录。
2. 构造 `ToolsConfig` 并强制 `restrict_to_workspace=True`。
3. 用 child root 创建新的 ToolRegistry。
4. 用新的 `FileStates()` 作为 actor-local 读取状态。
5. 构造 restricted WorkspaceScope 并绑定到当前 async task。

### Child 实际得到什么上下文

当前 child 初始 messages 只有：

```text
system: subagent system prompt + 时间 + child workspace + 可用 Skill summary
user: 父 Agent 传入的 task 文本
```

它不会复制完整父 session history，也不会自动继承父 Agent 的 messages。

同样要以实际代码为准：当前实现不会自动把父任务 input snapshot/facts 以只读 mount 共享给 child。若 child 需要输入，父任务必须把必要内容写进 task 描述、复制到 child root，或让 child 通过受控工具重新获取。不能在面试中说“child 自动读取共享父 artifact”。

## 5. 权限为什么只能收紧

child scope 固定为 `restricted`，即使父会话是 Full Access，child 仍使用：

```text
SandboxMode.WORKSPACE_WRITE
project_path = child root
restrict_to_workspace = true
```

child 工具调用进入与父任务相同类型的 `PolicyEngine`，但 scope 更小。写父 workspace、其他 child 目录或正式 artifact 目录会因路径边界拒绝。

### Child approval 绑定

child ask 时构造的 `ApprovalBinding` 除普通参数外还绑定：

```text
parent task id
parent plan hash
child id
child workspace writable root
sandbox provider/mode
command/network binding
```

因此给 child A 的批准不能被 child B 或父 Agent 复用。

## 6. Child HITL 怎样路由到父 chat

child 可以使用 `request_user_input`。`_child_policy_gate()` 会创建 InteractionRequest：

- `task_id` 使用 parent task。
- `child_id` 标识具体 child。
- `plan_hash` 使用 parent plan hash。
- `chat_id` 使用父任务来源 chat。
- `tool_call_id` 保留 child 原调用。

WebUI 在父 chat 显示卡片。child lifecycle 进入轮询等待：

1. 定期执行 `expire_due()`。
2. request 仍 pending 时不调用 provider。
3. typed response 到达后，把 resolution 替换回 child 原 tool result。
4. 继续同一个 child 的 messages。

高风险 approval 仍是 `expire_and_deny`；普通业务问题可使用 required/auto_resolve。

## 7. 为什么取消 child 工作量配额

早期设计允许父 Agent 为 child 估算：

- token budget。
- 总 lifecycle timeout。
- max tool calls。

当前 `spawn` schema 只保留：

```text
task
label
temperature
```

child 的 AgentRunSpec 中：

```text
total_token_budget = None
max_tool_calls = None
没有包住整个 lifecycle 的总 timeout
```

原因是父 Agent 对子任务工作量估算不可靠，过小额度会误杀正常长任务，而且失败看起来像业务失败，难以解释。

### 仍保留的安全熔断

- 全局并发限制。
- 每父任务同一时刻最多 5 个 direct child，完成后释放槽位。
- 禁止嵌套。
- 用户/会话取消。
- 网关关闭时 task 取消。
- 单次 LLM 请求 timeout。
- Policy/Sandbox/OCC。
- `max_tool_iterations=200` 循环保护。

低层 AgentRunSpec 仍支持 budget 字段供其他场景使用，只是 P8 child 不设置它们。

## 8. 循环保护和部分进展

child 默认最多 200 个工具迭代。触发 `max_iterations` 时：

- stop reason 是失败类状态。
- 记录 `mybot.subagent.loop_guard`。
- `_format_partial_progress()` 提取最近完成步骤和失败信息。
- 父 Agent 收到“失败 + 部分进展”，不会误报正常完成。

这和工作量 quota 不同：quota 可能误杀正常长任务；200 轮 guard 针对明显异常循环，是生命周期安全熔断。

## 9. Child 结果怎样回到父 Agent

`_announce_result()` 使用 MessageBus 发布一个 system InboundMessage：

```text
injected_event = subagent_result
subagent_task_id = child id
session_key_override = 父 session key
```

`session_key_override` 保证结果回到原父 session。父 Runner 仍在当前迭代时结果进入 pending queue；父 Runner 已结束时，由 session lock 串行启动后续处理，不会跨会话竞争。

announcement 包含 label、原 task、成功/失败状态和 result。父 Agent随后负责汇总，不让 child 直接代表最终用户回复。

## 9.1 Child activity 怎样独立展示

`_SubagentHook` 现在把 child 生命周期转换为结构化 activity：

```text
started / iteration / checkpoint
reasoning_delta / reasoning_end
tool_events(start|end|error)
completed / failed / cancelled
```

`SubagentManager._publish_activity()` 为每条事件附加 parent task、plan hash、node id、child id、phase、iteration、elapsed、usage 和终态结果。WebSocket 将 `agent_ui.kind=subagent_activity` 转成独立 `subagent_activity` frame，并直接追加到 WebUI transcript；它不会走普通 progress/reasoning 消息路径。

前端 `useNanobotStream` 使用独立 `subagentActivities` reducer，`messages` 不发生变化。`build_webui_thread_response()` 从 append-only transcript 合并 reasoning chunk 和同 call id 的 tool start/end，刷新后仍可恢复。计划卡片只选择当前 task/hash 的 activity，避免 revision 修订后显示旧 child。

桌面 Plan 面板使用固定 52rem 右侧 Sheet：左栏选择 child，右栏显示 reasoning、工具时间线、phase、iteration、elapsed、usage、result/error。没有新增移动端布局、断点或测试。

## 10. Artifact 集成的真实边界

已经实现：

- child 有固定隔离目录。
- `ArtifactStore.child_root()` 验证 child id/path。
- `ArtifactStore.register(..., child_id=...)` 会强制 artifact 位于该 child root。
- trace/status 记录 artifact root。

当前没有自动实现：

- 扫描 child root 并把所有文件自动登记到父 `artifacts.json`。
- 自动把父 input/facts 只读共享给 child。
- child 完整生命周期写入 P4 checkpoint 调度表。
- 父 Agent 自动验证并发布 child 文件为正式 artifact。

因此准确说法是“child 文件写入被隔离到 task-scoped root，并具备登记边界”，而不是“所有 child artifact 已全自动做完整 lineage”。

## 11. Trace 与 single/multi 测量

spawn 时记录：

```text
child_id
parent_task_id
plan_hash
workload_quotas_enabled = false
```

child TraceHook 使用父 trace id、新 span id 和 parent span id，记录模型、tool events、usage、interaction 和错误。取消、失败和 loop guard 有专门事件。

`subagent_compare.py` 比较：

- 成功率。
- wall/P95。
- input/output token。
- cost。
- 父上下文。
- failure/cancellation/loop guard。

当前 committed 报告使用固定 fake provider，只证明测量管道和治理回归，不证明真实模型多 Agent 一定更快。

## 12. 取消与清理

`cancel_by_session(session_key)`：

1. 找到该 session 仍运行的 asyncio task。
2. 逐个 `cancel()`。
3. `gather(return_exceptions=True)` 等待清理。
4. child 捕获 `CancelledError`，更新状态并写 trace。

`AgentLoop._cancel_active_tasks()` 会并发启动 child cancellation 和主任务 cancellation，避免先等待主任务清理后才停止 child。background task 完成后 callback 会从 running/status/session maps 中清除引用，同时释放父任务 direct-child 槽位；已发布的用户可见 activity 已进入 transcript，不依赖内存 status map。

## 13. 后台 DAG 执行与重复派发保护

`AgentLoop._drain_pending()` 只读取已经到达的消息，不再因 session 中仍有 child 而最多等待 300 秒。child 完成后仍通过 `session_key_override` 回到父 session；PlanScheduler completion callback 独立更新节点、artifact 和下游 ready 状态。

WebUI 输入区把 `main turn running` 与 `subagentActivities` 中存在 running child 合并为会话工作状态。因此主 Agent 已结束前台回复、child 仍在后台运行时，用户仍可点击同一个停止按钮；控制路径继续发送 `/stop`，不增加单 child 控制协议。

为防止主模型和 Scheduler 同时编排同一个节点：

- active plan 含 `executor=child` 时，RequestContext 标记 `_runtime_plan_managed_children=true`，SpawnTool 硬拒绝直接 spawn。
- `plan(update_step, status=running)` 对已有 `child_id` 的 running child 幂等，不创建第二个 child。
- running child 缺少 `child_id` 时先恢复为 ready，再由 Scheduler 创建受 task/hash/node 绑定的新 child。
- `plan(resume)` 在重试 failed 节点前先与 `get_running_task_ids()` 对账，避免模型猜测非法状态转换。

相关回归位于 `tests/agent/tools/test_subagent_tools.py`、`tests/agent/test_subagent_lifecycle.py`、`tests/agent/test_task_cancel.py`、`tests/runtime/test_plan_dag.py` 和 `webui/src/tests/thread-shell.test.tsx`。

## 为什么不引入外部工作流图框架

当前需求只有一层独立子任务，并且最重要的是权限、隔离、HITL、恢复和测量。引入图引擎会同时引入节点状态、边条件、调度器和另一套 checkpoint 真相源。

P8 选择复用已有 AgentRunner/MessageBus，并用轻量 `PlanScheduler` 维护当前单层 DAG、状态转换和恢复。只有未来出现多层动态分支、复杂条件边或分布式调度需求，才值得评估外部工作流框架。

## 验证与证据

`tests/runtime/test_subagent_governance.py` 覆盖：

- spawn schema 不暴露 token/time/tool quota。
- child AgentRunSpec 的 budget 字段为 None。
- 同时运行的第 6 个 direct child 被拒，完成后可继续派发后续节点。
- 缺 active/hash-bound parent plan 被拒。
- child question 路由父 chat并恢复同一 tool call。
- child 没有总 lifecycle timeout，仍可取消。
- max_iterations 返回部分进展和失败。
- nested spawn 不在 child 工具表。
- child scope/文件边界和父子 trace。

历史完整 Runtime 快照为 `56 passed, 1 skipped`，当前结果以重新运行测试为准。

## 未实现和不能夸大的部分

- `FileLeaseRegistry`、file_busy UI 和跨 child 等待队列未实现。
- 没有跨进程共享文件锁。
- child 不自动获得父 artifact 只读视图。
- child 文件不自动全部登记 lineage。
- child 生命周期没有完整 durable scheduler/checkpoint。
- fake-provider 报告不代表真实模型质量和成本收益。
- 当前最大深度为一层，不支持递归 agent tree。

## 面试怎么讲

### 30 秒回答

> P8 用 Runtime 自有 PlanScheduler 在 nanobot 现有 SubagentManager 上做 DAG 编排和治理。active 且 hash-bound 的 child 节点由 Scheduler 自动派发，同一时刻最多 5 个，完成后释放槽位，child 工具表没有 spawn。child 固定写自己的 artifact root，使用 restricted scope、独立 FileStates 和 child-bound approval；主 Runner 不前台等待，业务问题和结果回到父 session，父子 trace 关联。工作量不由父 Agent 估算 token/总时长/tool 配额，只保留会话级联取消、单次 LLM timeout、并发限制和 200 轮循环熔断。

### 高频追问

**为什么既有 concurrency limit 又有 5-child limit？**

全局并发限制控制 Runtime 瞬时资源，direct child limit 控制单个父任务的瞬时扇出。两者当前都是 5；完成后释放槽位，因此累计节点数可以超过 5。

**如何保证 child 不能扩大父权限？**

child 无论父模式如何都使用 restricted WorkspaceScope，project path 是自己的 child root；同一 Policy 只会在更小 scope 上判断，approval 还绑定 child id 和 writable root。

**为什么取消 token budget，不怕失控吗？**

工作量预算依赖父模型估算，容易误杀。安全失控由数量、嵌套、sandbox、policy、单次 timeout、取消和 loop guard 控制。资源计量仍进入 trace，但不作为父模型随意设置的硬停止条件。

**现在 child 能直接读取父输入吗？**

不能自动读取。当前 child 只拿 task 文本和自己的 restricted root；需要的内容必须显式复制、写入描述或通过受控工具获得。这是已知边界，不应过度宣传。

**为什么文件租约没做？**

当前 child 默认隔离写目录，跨 actor 的用户/IDE 修改由 OCC 检测。只有真实任务证明多个 child 必须协同写同一 workspace 文件时，才值得增加进程内租约；租约也不能替代 OCC。

## 自测：读完 P8 应该能回答

1. active-plan gate 为什么需要 approved hash 等于当前 hash？
2. 默认并发限制和最多 5 个 direct child 有什么区别？
3. 禁止嵌套是提示词约束还是工具注册硬约束？
4. child 实际得到哪些上下文，哪些不会自动继承？
5. child 权限、FileStates、approval 怎样隔离？
6. 为什么取消工作量 quota 仍然有安全熔断？
7. 当前 artifact/checkpoint 集成有哪些真实边界？

## 2026-08-10：DAG Child Dispatch 与 Completion Callback

- `PlanTool._dispatch_ready_children()` 按当前并发余量派发 ready child node，并把 parent task/hash/node id 传给 `SubagentManager`。
- `SubagentStatus` 新增 `node_id/final_result`；`spawn()` 新增 `completion_callback/return_task_id`，真实 lifecycle 测试确认 callback 在任务终态收到完整状态。
- callback 扫描 child artifact root 并按 node metadata 登记 ArtifactStore，然后原子更新 plan node；若 active hash 已变化，仅记录 stale completion trace，不污染新 revision。
- `PlanScheduler.refresh()` 只阻塞失败节点的后继，独立分支仍可继续；recovery 将失联 child 转 uncertain，禁止静默重复执行。
- 计划卡片按 topological batch 展示 executor/dependency/status，前端只面向桌面网页。

## 2026-08-11：Child Cancel 与显式重派

- child completion callback 将 `stop_reason=cancelled` 分类为 interrupted ready，而不是普通 failed；清除旧 child/result/error 绑定，记录 interruption trace，且取消回调不调用后续自动 dispatch。
- 用户点击 active 计划卡片“继续执行”后，PlanTool `resume` 才按当前并发余量重派 ready child；真实 failed child 也遵守同一显式重试边界。
- 回归覆盖 cancelled child 不自动重派、failed child resume 创建新 child，以及模型误用 `in_progress` 时仍走安全重试路径。

## 2026-08-11：可恢复工具错误与终态判定

- `SubagentManager` 构造 child `AgentRunSpec` 时设置 `fail_on_tool_error=False`，Runner 把单次工具错误和纠正提示交还模型继续迭代。
- Runner workspace violation marker 增加 PolicyEngine 的精确文案 `path resolves outside the current workspace`，继续受重复路径违规熔断约束。
- `_has_unresolved_tool_error()` 从后向前读取最后一个 `ok|error` 工具事件：最后为 error 时把有效 stop reason 设为 `tool_error`，防止“无法完成”的说明文字被误标成功；错误后有成功工具调用且正常结束时允许成功。
- P3 可信只读根目录修复 builtin Skill 读取，无需扩大 child workspace、写权限或工具表。
- lifecycle 回归覆盖 child spec 不立即失败、未解决错误失败、后续成功工具恢复，以及原有 max-iteration/cancel/interaction 行为。
