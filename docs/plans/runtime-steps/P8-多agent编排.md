# P8 受控 Subagent 编排

> 状态：必做项已完成。Child 不设置工作量配额，但保留生命周期安全熔断；共享 workspace 文件租约是选做且未实现。
> 出口：最多 5 个直接 child，禁止嵌套，权限/生命周期/上下文/产物受控，父子 trace 和单/多 Agent 对比完整。

## 1. 派生与权限

- 复用现有 spawn/SubagentManager，不引入图工作流引擎。
- 父 Agent 根据用户目标、当前 `active` 且 hash 绑定的 plan 和状态决定是否派生、并行或顺序执行；激活可来自普通 WebUI 自动模式或显式确认，completed plan 不再派生新 child。
- 每个父任务最多 5 个直接 child；child 调用 spawn 必须硬拒绝。
- child policy 带父任务 hard boundary、配置、plan/task scope；权限只能相等或更严。
- child 的业务问题进入父任务 InteractionRequest；安全 approval 固定 `expire_and_deny`，不能自行批准。

## 2. 生命周期与上下文

`spawn` 不向父 Agent 暴露 token、wall-clock 或最大工具调用数，也不为 child 设置这三类工作量配额。长任务持续执行到完成，不能因为父 Agent 预估额度偏小而失败。

保留以下安全熔断：用户或父任务取消、网关关闭、单次 LLM 请求超时、安全 policy/sandbox、最多 5 个直接 child，以及 `max_tool_iterations=200` 的异常循环保护。循环保护触发时以 `max_iterations` 返回已完成工具步骤和部分进展，不把它报告成正常完成。

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

父子 trace 至少记录 parent span、child goal、模型、工具、usage、artifact、InteractionRequest、失败、取消、循环熔断和汇总；spawn 明确记录 `workload_quotas_enabled=false`。

P4 checkpoint：completed child 跳过；未启动可重建；运行中被 kill 依据可验证 artifact 标记 pending/uncertain，不承诺 exactly-once。

只要使用 child，就在同一任务集比较：

- 成功率；
- wall-clock/P95；
- input/output token 和成本；
- 父上下文大小；
- 失败、取消和循环熔断数量。

结果可以证明没有收益，但必须真实记录。

## 测试与出口

- 第 6 个 child 和嵌套 spawn 被拒。
- child 不能放宽父权限或写其他 child/正式 artifact 目录。
- `spawn` schema 不包含 token/time/tool 配额，Runner spec 对 child 保持 `None`。
- 无总生命周期 timeout；运行中的 child 仍可随父任务/会话取消。
- 200 轮循环保护触发时返回部分进展并标记失败。
- child InteractionRequest 正确路由到父任务。
- 共享 workspace 变化由 OCC 硬失败，不静默覆盖。
- 父子 trace、usage、lineage 和单/多 Agent 报告完整。
- 选做租约仅在实现后测试 TTL、取消、all-or-none、死锁和 file_busy；未实现不影响阶段出口。
