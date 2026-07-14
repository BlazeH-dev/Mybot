# P8 受控 Subagent 编排 — 详细步骤

> 所属：`docs/plans/Mybot通用AgentRuntime与办公自动化SkillPack整合方案.md`
> 状态：仅规划，未执行。2026-07-14 由机动项提升为主线必做。
> 阶段出口：父 Agent 可按提示与计划决定是否派生最多 5 个直接子 Agent；禁止嵌套，权限只紧不松，预算/上下文/产物隔离，使用时必须有父子 trace 和成本时长对比。

---

## S8.1 派生模型与数量限制

- 复用 nanobot 现有 spawn/subagent 承接点，不新造图工作流引擎。
- 是否启动、拆成什么子任务、并行还是顺序，由父 Agent 根据用户提示、已确认计划和当前状态决定。
- 每个父任务最多创建 5 个直接子 Agent。
- 子 Agent 禁止再调用 spawn；嵌套尝试由工具层硬拒绝。
- 不限制只能用于 Office 或 Research，也不写死固定任务模板。

## S8.2 权限继承

- child policy context 带父任务硬边界、配置、plan/task 范围和已批准能力。
- 子 Agent 权限只能等于或严于父任务，不能增加 allow 范围。
- 父任务 deny 在 child 永远 deny。
- child 需要 ask 时写入父任务 approval 流，由父 Agent/用户处理；不得自行视为批准。
- 红队加入“通过子 Agent 绕过策略”样本。

## S8.3 预算

每个 child 必须有：

- token budget。
- wall-clock timeout。
- 最大工具调用数。
- 可选成本上限。

父 Agent 负责分配与汇总；超预算 child 返回 `budget_exceeded` 和部分结果，不继续隐藏消耗。

## S8.4 上下文隔离

- child 只获得完成子任务所需的目标、约束、输入 artifact 引用和工具集。
- 不复制完整父会话历史。
- child 返回结构化 summary、事实引用、错误和 artifact 路径，不把原始大工具输出灌回父上下文。
- 父 Agent 负责冲突处理和最终答案。

## S8.5 Artifact 隔离

```text
.nanobot-runtime/artifacts/<task_id>/children/<child_id>/
```

- child 默认只能写自己的子目录。
- 共享输入 snapshot 和 verified facts 以只读 artifact 引用传入。
- 父 Agent 汇总时把 child 产物登记到同一 task lineage，保留 `child_id` 和 source edge。
- child 不直接覆盖父任务正式产物。

## S8.6 父子 Trace

只要创建 child，必须记录：

- orchestration parent span。
- child span 的 `parent_span_id`。
- child goal、预算、使用模型、工具调用、状态与 usage。
- artifact 与 approval 事件。
- 汇总、失败、取消和超预算结果。

P8 可先发本地事件；P5 TraceHook 负责完整 JSONL/OTel 映射和查看。

## S8.7 恢复边界

- P4 checkpoint 记录 child 状态摘要和 artifact checksum。
- 已完成 child 可跳过。
- 未启动 child 可重新创建。
- 运行中被 kill 的 child 按其最后可验证 artifact 判断 pending/uncertain。
- 首版不承诺任意并行 child 的 exactly-once。

## S8.8 Eval

只要任务使用 child，就必须在相同任务集上提供：

- 单 Agent 顺序执行。
- 父 Agent + 子 Agent 执行。
- 成功率。
- wall-clock 时长/P95。
- 总 input/output token 与估算成本。
- 父上下文大小。
- 失败/超预算数量。

对比结果可以显示没有收益，但必须真实记录；P8 的完成标准是治理与测量闭环，而不是强行证明多 Agent 更快。

## 代表场景

- 双 Office Skill 对比：父 Agent准备 input snapshot 与 verified facts，两个 child 各跑一个 Skill，父 Agent 汇总。
- Research：child 分别收集独立来源，父 Agent 统一引用核对和成文。
- 其他任务由父 Agent 动态决定，只要满足同一治理约束。

## 定向测试

- 第 6 个 child 创建被拒。
- child 调用 spawn 被拒。
- child 尝试放宽父权限失败。
- child 只能写自己的 artifact 子目录。
- token/time/tool budget 分别可触发中止。
- child ask 进入父 pending approval。
- 父子 trace 树完整，usage 汇总正确。
- 单 Agent/子 Agent 对比报告可生成。

## 阶段出口检查

- [ ] 每个父任务最多 5 个直接 child，禁止嵌套。
- [ ] 权限继承只能收紧，不能通过 child 绕过硬边界。
- [ ] 每个 child 有 token、时间和工具预算。
- [ ] 上下文和 artifact 子目录隔离，父 Agent 负责最终汇总。
- [ ] 任何含 child 的任务都有父子 trace。
- [ ] 单 Agent 顺序执行与子 Agent 执行有成本、时长和成功率对比。
