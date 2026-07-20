# P8 多 Agent 编排代码变更说明

> 对应计划：`docs/plans/runtime-steps/P8-多agent编排.md`
> 当前状态：必做治理与测量已完成（2026-07-18）；2026-07-20 取消 child 工作量配额，保留取消、单次 LLM 超时与循环熔断。共享 workspace 文件租约未实现，按计划保留为选做。

## 阶段结果

Subagent 继续复用原 `SpawnTool/SubagentManager`，但派生、数量、嵌套、权限、生命周期、上下文、artifact、HITL、trace 与对比报告已成为硬约束。

## 代码落点

- `nanobot/agent/tools/spawn.py`
  - 只有父 plan `status=active` 且 `approved_plan_hash == plan_hash` 才允许模型调用 spawn。
  - 参数只包含 task、label 和可选 temperature；不允许父 Agent 为 child 估算 token、总时长或工具调用配额。
  - RequestContext 绑定 parent task/plan，变 hash 或无 plan 直接 `policy_denied`。
- `nanobot/agent/subagent.py`
  - 每父任务最多 5 个 direct child；subagent registry 不加载 spawn，禁止嵌套。
  - child workspace 固定为 `.nanobot-runtime/artifacts/<parent>/children/<child>/`，scope 总是 restricted，Full Access 父任务也只能被收紧。
  - child 使用独立 `FileStates`，不能继承父/兄弟 fresh-read 资格。
  - Runner spec 的 `total_token_budget/max_tool_calls` 对 child 保持 `None`，整个 lifecycle 不再套总 wall-clock timeout；长任务执行到完成或被显式取消。
  - 保留单次 LLM 请求 timeout 和 `max_tool_iterations=200` 循环保护；`max_iterations` 以失败状态返回部分进展，并记录 `mybot.subagent.loop_guard`。
  - child 使用与父相同的 `PolicyEngine/ApprovalManager/InteractionManager`；approval 额外绑定 `child_id`，业务问题生成父 chat 的 typed card，等待后恢复同一 child。
  - 父上下文不复制给 child；child 只接收系统约束、目标文本和自己的 artifact root，结果以结构化 announcement 返回父任务。
  - 仅用于旧单元测试的非 `Path` workspace double 使用自动清理的系统临时目录；生产 child 仍必须使用父 task 下的固定 artifact root。
- `nanobot/runtime/trace.py`：父子同 trace id、child parent span、usage/artifact/interaction/cancel/failure/loop-guard 事件；spawn 记录 `workload_quotas_enabled=false`。
- `nanobot/runtime/evals/subagent_compare.py` 与 `benchmarks/subagent-comparison.*`：固定 fake-provider single/multi 对比。

## 验证

- 第 6 个 child 被拒；嵌套 spawn 不在 child 工具表。
- 缺 active/hash-bound parent plan 的 SpawnTool 被拒。
- spawn schema 与 child Runner spec 均验证无 token/time/tool 工作量配额。
- 长生命周期 child 不会被总 timeout 中止，仍可由会话取消。
- 循环保护返回部分进展并标记失败，不再误报正常完成。
- child question 路由到父 chat，等待期间无 provider call，typed 回答恢复同一 child/tool_call。
- child 对正式 artifact/父 workspace 的写入在 policy/scope 层 hard deny。
- trace 测试校验同 trace id 与 `parent_span_id`。
- 对比报告记录成功率、wall/P95、input/output token、成本、父上下文、失败/取消/循环熔断数量。
- 2026-07-20 本地完整 Runtime 验证：`56 passed, 1 skipped`；跳过项为 macOS 主机上的 Linux Bubblewrap real smoke。

## 选做项

未实现 `FileLeaseRegistry`、`file_busy` UI、跨 child 等待队列和共享 workspace 协作锁。当前设计以 child artifact 隔离 + actor-local OCC 为必做边界，不把进程内租约描述为跨进程安全能力。
