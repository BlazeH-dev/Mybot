# P8 受控 Subagent 编排

> 状态：待执行。治理与测量是必做，共享 workspace 文件租约是选做。
> 出口：最多 5 个直接 child，禁止嵌套，权限/预算/上下文/产物受控，父子 trace 和单/多 Agent 对比完整。

## 1. 派生与权限

- 复用现有 spawn/SubagentManager，不引入图工作流引擎。
- 父 Agent 根据用户目标、已激活且 hash 绑定的 plan 和状态决定是否派生、并行或顺序执行；激活可来自普通 WebUI 自动模式或显式确认。
- 每个父任务最多 5 个直接 child；child 调用 spawn 必须硬拒绝。
- child policy 带父任务 hard boundary、配置、plan/task scope；权限只能相等或更严。
- child 的业务问题进入父任务 InteractionRequest；安全 approval 固定 `expire_and_deny`，不能自行批准。

## 2. 预算与上下文

每个 child 必须有 token、wall-clock、最大工具调用数和可选成本上限。超限返回 `budget_exceeded` 与部分结果。

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

父子 trace 至少记录 parent span、child goal、模型、预算、工具、usage、artifact、InteractionRequest、失败/取消/超限和汇总。

P4 checkpoint：completed child 跳过；未启动可重建；运行中被 kill 依据可验证 artifact 标记 pending/uncertain，不承诺 exactly-once。

只要使用 child，就在同一任务集比较：

- 成功率；
- wall-clock/P95；
- input/output token 和成本；
- 父上下文大小；
- 失败、取消和超预算数量。

结果可以证明没有收益，但必须真实记录。

## 测试与出口

- 第 6 个 child 和嵌套 spawn 被拒。
- child 不能放宽父权限或写其他 child/正式 artifact 目录。
- token/time/tool budget 分别触发中止。
- child InteractionRequest 正确路由到父任务。
- 共享 workspace 变化由 OCC 硬失败，不静默覆盖。
- 父子 trace、usage、lineage 和单/多 Agent 报告完整。
- 选做租约仅在实现后测试 TTL、取消、all-or-none、死锁和 file_busy；未实现不影响阶段出口。
