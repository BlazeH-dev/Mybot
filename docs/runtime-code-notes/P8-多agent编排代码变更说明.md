# P8 多 Agent 编排代码变更说明

> 对应计划：`docs/plans/runtime-steps/P8-多agent编排.md`
> 当前状态：仅规划，尚未执行；本文件同步 P8 必做与选做边界，不表示治理代码已落地。
> 2026-07-15 方案修订：共享 workspace 文件租约、等待队列和冲突可视化降为选做。
> 2026-07-16：计划压缩后数量、嵌套、权限、预算、上下文、artifact、trace 和对比 eval 仍为必做，租约仍为选做。

## P8 必做边界

- 最多 5 个直接 child，禁止嵌套。
- 只有 active/completed 且 `approved_plan_hash` 绑定当前 hash 的父计划可驱动派生；激活可来自普通 WebUI 自动模式或显式确认。
- 权限只能继承或收紧，预算、上下文、artifact 和父子 trace 完整。
- child 默认只写自己的 artifact 子目录，不直接覆盖父任务正式产物。
- parent 与各 child 使用独立 read snapshot；不能继承其他 actor 的 fresh-read 资格。
- 获准编辑共享 workspace 时复用 P3 OCC；冲突结构化返回父 Agent，不允许 force overwrite。
- child 的安全 approval 使用 `expire_and_deny`；业务问题可选择 required 或 auto_resolve。

## 选做增强

只有真实任务证明多个 child 必须共同编辑同一 workspace 时，才考虑：

- workspace 级进程内 FileLeaseRegistry。
- 路径等待队列、TTL/取消释放和层级冲突。
- 多文件按稳定顺序 all-or-none 获取。
- `file_busy` 与 `file_conflict` 分离及 WebUI 提示。

租约不进入 P8 阶段出口、冻结前 cutline 或简历承诺，也不能替代检测用户/IDE/外部进程修改的 OCC。

## 当前代码状态

现有 SubagentManager 已能运行子代理，但上述数量、嵌套、权限继承、预算、artifact 所有权和父子 trace 治理尚未按 P8 方案完整落地。

## 后续验证要求

- P8 核心测试不依赖租约即可通过。
- 共享编辑发生变化时必须 OCC fail-safe，不静默覆盖。
- 只有实现选做租约后，才增加 file_busy、TTL 和多文件死锁相关测试。
