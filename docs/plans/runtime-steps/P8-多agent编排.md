# P8 受控 Subagent 编排

> 状态：必做项已完成。Child 不设置工作量配额，但保留生命周期安全熔断；共享 workspace 文件租约是选做且未实现。
> 出口：同一时刻最多 5 个直接 child，禁止嵌套，权限/生命周期/上下文/产物受控，父子 trace 和单/多 Agent 对比完整。

## 1. 派生与权限

- 复用现有 SubagentManager，由 Runtime `PlanScheduler` 按 DAG 依赖统一派发 child；主 Agent 不直接重复 spawn 已声明的 child 节点。
- 父 Agent 根据用户目标、当前 `active` 且 hash 绑定的 plan 和状态决定是否派生、并行或顺序执行；激活可来自普通 WebUI 自动模式或显式确认，completed plan 不再派生新 child。
- 每个父任务同一时刻最多 5 个直接 child；完成后释放槽位，整个任务可继续执行后续 child 节点；child 调用 spawn 必须硬拒绝。
- child policy 带父任务 hard boundary、配置、plan/task scope；权限只能相等或更严。
- child 的业务问题进入父任务 InteractionRequest；安全 approval 固定 `expire_and_deny`，不能自行批准。

## 2. 生命周期与上下文

`spawn` 不向父 Agent 暴露 token、wall-clock 或最大工具调用数，也不为 child 设置这三类工作量配额。长任务持续执行到完成，不能因为父 Agent 预估额度偏小而失败。

保留以下安全熔断：用户或父任务取消、网关关闭、单次 LLM 请求超时、安全 policy/sandbox、同时最多 5 个直接 child，以及 `max_tool_iterations=200` 的异常循环保护。主 Runner 不在前台等待 child；child 通过 MessageBus 和 Scheduler callback 汇报。循环保护触发时以 `max_iterations` 返回已完成工具步骤和部分进展，不把它报告成正常完成。

child 只获得必要目标、约束、输入 artifact 引用和工具集，不复制完整父会话；只返回结构化 summary、事实引用、错误和 artifact 路径。

## 3. Artifact 与文件边界

```text
.nanobot-runtime/artifacts/<task_id>/children/<child_id>/
```

- child 默认只写自己的目录；共享 snapshot/facts 只读，父 Agent 发布正式产物。
- parent 与每个 child 使用独立 FileStates/read snapshot，不能继承其他 actor 的 fresh-read 资格。
- 如获准修改共享 workspace，使用 P3 OCC；冲突返回父 Agent 重新读取、重派或转人工，禁止 force overwrite。

### 选做：FileLeaseRegistry

只有真实任务证明多个 child 必须共同编辑同一 workspace 时才实现：

- workspace 级进程内写租约，记录 path、task/child/tool call、acquired/expires。
- 取消/超时释放；多文件按稳定顺序 all-or-none，获得后重新做 OCC。
- 区分 `file_busy` 与 `file_conflict`，可选 WebUI 提示持有者和等待状态。
- 租约不覆盖用户、IDE、Shell 和其他进程，不能替代 OCC；不实现也不阻塞 P8。

## 4. Trace、恢复与 Eval

父子 trace 至少记录 parent span、child goal、模型、工具、usage、artifact、InteractionRequest、失败、取消、循环熔断和汇总；spawn 明确记录 `workload_quotas_enabled=false`。用户可见 child activity 另走独立 WebSocket/transcript 投影，不替代 trace。

P4 checkpoint：completed child 跳过；未启动可重建；运行中被 kill 依据可验证 artifact 标记 pending/uncertain，不承诺 exactly-once。

只要使用 child，就在同一任务集比较：

- 成功率；
- wall-clock/P95；
- input/output token 和成本；
- 父上下文大小；
- 失败、取消和循环熔断数量。

结果可以证明没有收益，但必须真实记录。

## 测试与出口

- 同时运行第 6 个 child 和嵌套 spawn 被拒；已完成 child 释放槽位，后续节点可以继续派发。
- child 不能放宽父权限或写其他 child/正式 artifact 目录。
- `spawn` schema 不包含 token/time/tool 配额，Runner spec 对 child 保持 `None`。
- 无总生命周期 timeout；主 Agent 不做 300 秒前台等待，运行中的 child 仍可随父任务/会话级 `/stop` 一起取消。
- 200 轮循环保护触发时返回部分进展并标记失败。
- child InteractionRequest 正确路由到父任务。
- 共享 workspace 变化由 OCC 硬失败，不静默覆盖。
- 父子 trace、usage、lineage 和单/多 Agent 报告完整。
- 计划卡片只在 child 节点存在时提供查看入口；并行 child 分开聚合，reasoning/tool events 不进入 parent 主消息时间线，刷新后可恢复，旧 plan hash 不污染新 revision。
- 选做租约仅在实现后测试 TTL、取消、all-or-none、死锁和 file_busy；未实现不影响阶段出口。

## PlanScheduler DAG 编排（2026-08-10）

- plan step 新增 `executor: parent|child` 和 `depends_on`；PlanScheduler 计算 topological batches，ready child 自动并行派发，依赖满足后继续下一批。
- child spawn 绑定 parent task id、plan hash 和 node id，仍使用 restricted child workspace、独立 artifact root、无嵌套 spawn；默认和配置上限统一为并发 5。
- `SubagentManager.spawn()` 可返回 child id 并接受 completion callback；callback 登记 child artifact、写回 node result/status，并忽略旧 hash 的 stale completion。
- 单个 child 失败只阻塞后继，其他独立 DAG 分支继续；Runtime 不自动重试普通失败。
- parent executor 继续由当前主 AgentRunner 按 ready node 范围执行，child executor 用于真正可隔离、可并行的工作；最终汇总必须等待所有必需节点和 expected artifacts 通过确定性校验。
- 用户取消 child 时 completion callback 将节点回置 ready、清除旧 child 绑定且不立即重派；用户后续显式 `resume` 才按并发余量创建新 child。普通 child failure 同样只能由显式 resume 重试，不形成模型自动重试循环。

## 后台执行、级联停止与绑定恢复（2026-08-11）

- 主 Agent 的 injection callback 只消费已经到达的消息，不再因 child 运行而最多阻塞 300 秒；child 完成后通过父 session 的 MessageBus 后续回合和 Scheduler callback 推进。
- WebUI 只要当前会话仍有 running child，就继续显示会话级停止按钮；`/stop` 并发取消主任务和该 session 全部 child，不提供单 child 旁路控制。
- `max_concurrent_subagents` 默认值与本机配置统一为 5；`max_direct_children` 表示同时扇出，child 结束后计数释放，不限制任务生命周期内的累计节点数。
- active DAG 含 child 节点时，父 Agent 直接 `spawn` 被拒绝并提示使用 `plan(get|resume)`；`update_step(running)` 对已有 `child_id` 幂等，对缺失绑定的 child 回到 ready 后交给 Scheduler 派发。
- `resume` 会先核对 Runtime 当前 live child ids；`running` 但无 `child_id` 的中断节点恢复并重新入队，已绑定且仍存活的 child 不重复派发。

## Child 执行过程可见性（2026-08-11）

- `SubagentManager` 在 started、iteration、reasoning delta/end、tool start/end、checkpoint、completed/failed/cancelled 时发布结构化 activity。
- 每条 activity 绑定 `parent_task_id/plan_hash/node_id/child_id`，WebSocket 使用独立 `subagent_activity` 事件并写入 WebUI transcript。
- WebUI 使用独立 `subagentActivities` 状态聚合并行 child；事件不修改主 `messages`，因此主页面只显示 parent Agent 的思考和执行。
- 计划卡片的桌面侧栏按 child 展示状态、迭代、耗时、usage、reasoning、工具时间线和最终结果；历史接口从 append-only transcript 聚合恢复。
- 当前 revision 只读取匹配 task/hash 的 activity；旧 revision 或无关任务 activity 保留历史但不进入当前面板。

## Child 工具错误恢复（2026-08-11）

- child AgentRunner 使用可恢复工具错误语义：单次 `read_file`、搜索或路径错误作为 tool result 返回模型，不立即终止节点。
- `path resolves outside the current workspace` 纳入 workspace violation 识别，并继续受重复违规熔断约束。
- 工具错误之后出现成功工具调用并正常完成时允许节点成功；最后一个相关工具事件仍为 error 时，即使模型返回解释性文本也将有效 stop reason 收敛为 `tool_error`。
- builtin Skill 通过 P3 `trusted_read_roots` 只读开放，child workspace 外写入、Exec 和嵌套 spawn 边界不变。
- 固定计划卡片只提供 child 状态和侧栏入口，child reasoning/tool events 不进入主 Agent 消息时间线。
