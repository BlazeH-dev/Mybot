# Runtime 阶段代码变更说明

> 记录范围：Mybot 通用 Agent Runtime 与 Office Skill Pack 的 P0-P8 阶段落地说明。
> 目的：把“计划里为什么要做”与“代码里实际怎么做”对齐，方便复盘、面试讲解和后续阶段接续开发。

## 阅读方式

每个阶段说明按同一结构组织：

1. 阶段目标：这一阶段解决什么工程问题。
2. 代码变更：实际改了哪些文件或模块。
3. 为什么这么做：设计取舍、风险控制和面试叙事价值。
4. 怎么做的：关键实现路径和数据流。
5. 验证方式：对应测试、lint、build 或人工验收。
6. 后续影响：它给后续阶段留下了哪些接口、基线或约束。

## 阶段状态

| 阶段                       | 状态  | 说明                                                                                                                                  |
| ------------------------ | --- | ----------------------------------------------------------------------------------------------------------------------------------- |
| P0 准备                    | 已执行 | 已建立 Office golden fixture 与 CI smoke 回归门。见 [P0-准备阶段代码变更说明.md](./P0-准备阶段代码变更说明.md)。                                                  |
| P1 Office 垂直切片           | 已执行 | 已落地双 Office Skill、共享 verified facts、WebUI 仅规划/自动 plan-and-execute、计划步骤卡片、OfficeCLI 固定契约与 artifact 面板。见 [P1-office垂直切片代码变更说明.md](./P1-office垂直切片代码变更说明.md)。 |
| P2 Skill Pack Manifest   | 已执行 | 已落地可选 typed manifest、局部 fail closed、结构化 availability、复用 disabledSkills 与 WebUI 诊断。见 [P2-skillpack-manifest代码变更说明.md](./P2-skillpack-manifest代码变更说明.md)。 |
| P3 Sandbox / Policy 权限层  | 待执行 | 已记录 Codex 风格 OS sandbox、三轴权限模型、三档 InteractionRequest、审批超时不放行与最小文件 OCC；代码未落地。见 [P3-policy权限层代码变更说明.md](./P3-policy权限层代码变更说明.md)。 |
| P4 Artifact / Checkpoint | 待执行 | 已记录合法 `awaiting_*` 与 completed/pending/uncertain 分离的恢复边界；代码未落地。见 [P4-artifact-checkpoint代码变更说明.md](./P4-artifact-checkpoint代码变更说明.md)。 |
| P5 Trace / Eval          | 待执行 | 已记录 P5 Core 与选做评测边界；代码未落地。见 [P5-trace-eval代码变更说明.md](./P5-trace-eval代码变更说明.md)。 |
| P6 通用性扩展                 | 待执行 | 已记录 1–2 天 Research 最小闭环；代码未落地。见 [P6-通用性扩展代码变更说明.md](./P6-通用性扩展代码变更说明.md)。 |
| P7 面试交付物                 | 持续规划 | 已记录 benchmark、README、demo 与答辩证据边界。见 [P7-面试交付物代码变更说明.md](./P7-面试交付物代码变更说明.md)。 |
| P8 多 Agent 编排            | 待执行 | 已记录治理主线与选做文件租约边界；代码未落地。见 [P8-多agent编排代码变更说明.md](./P8-多agent编排代码变更说明.md)。 |

## 维护约定

- 默认记录已经落地或正在落地的真实代码变更；若 AGENTS.md 要求方案变更同步阶段说明，可新增明确标注“仅规划、尚未执行”的边界说明，但不得把规划描述成已实现能力。
- 每完成一个阶段，新增对应阶段说明，并在本索引表更新状态。
- 某阶段的方案或代码发生变化时，必须在同一次任务中同步更新对应代码变更说明。
- 方案演进和修改记录继续写入 `docs/修改记录.md`；这里专注“代码为什么这样改、怎么运行和怎么验证”。
