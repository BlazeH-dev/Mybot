# Runtime P0-P8 学习与代码变更索引

> 记录范围：Mybot 通用 Agent Runtime 与 Office Skill Pack 的 P0-P8。
> 这组文档不是简单的文件清单，而是一套从测试基线、业务闭环、插件治理、安全、恢复、评测到多 Agent 的项目教程。

## 先用一句话理解整个项目

Mybot 在 nanobot v0.2.1 的 Agent 循环上，补出了一套“可以安全执行真实任务、可以中断恢复、可以确定性验收”的 Runtime：

```text
固定测试输入
  -> Office 业务闭环
  -> Skill 声明与可用性
  -> Sandbox / Policy / 人工确认 / 文件冲突保护
  -> [可选增强] 聊天级 Git Worktree 隔离
  -> Artifact 血缘与 Checkpoint 恢复
  -> Trace / Eval / 红队
  -> [选做] 用第二类 Skill 验证通用性
  -> 整理 benchmark、最终结果页和面试证据
  -> 受控 Subagent 并行
```

P0-P8 阶段不是互不相关的功能，而是逐层回答工程问题；当前主线与选做阶段在下表中分开标注；P3.1 是插在 P3 之后的独立工作区增强，不改变阶段编号和冻结依赖：

| 阶段 | 它回答的问题 | 核心关键词 |
| --- | --- | --- |
| P0 | 怎么证明以后没有把系统改坏？ | fixture、golden truth、CI |
| P1 | Agent 怎样完成一个真实 Office 任务，并公平比较两种 Office 实现？ | verified facts、双 Skill、OfficePython、plan |
| P2 | Skill 怎样被机器发现、校验、禁用和诊断？ | manifest、availability、fail closed |
| P3 | Agent 能做什么、何时要问人、怎样防越权和覆盖？ | sandbox、policy、HITL、OCC |
| P3.1 | Git 项目聊天怎样隔离工作树，又不丢 dirty 状态？ | explicit worktree、binding、safe cleanup |
| P4 | 输入和产物怎样追踪，进程被杀后怎样恢复？ | snapshot、artifact、lineage、checkpoint |
| P5 | 含 LLM 的系统怎样做可重复测试、Langfuse 主导的观测与评估？ | cassette、本地 hard gate、OTel/Langfuse Dataset/Experiment/Score |
| P6 | [选做] 这套 Runtime 是否真的不只服务 Office？ | Research Skill、引用、untrusted content |
| P7 | 怎样把工程能力变成可复现的最终结果和面试证据？ | benchmark、README 最终结果页、架构/指标展示 |
| P8 | 怎样让多个 Agent 并行，又不扩大风险？ | active-plan gate、child isolation、parent-child trace |

## 推荐阅读顺序

第一次阅读不要按代码目录跳来跳去，按下面顺序读：

1. 先读 P0，理解为什么“测试真值”必须先于 Agent 能力。
2. 再读 P1，跑通一次从 Excel 到 facts、DOCX、PPTX、plan 的完整链路。
3. 读 P2，理解 Skill 指令、机器声明、运行依赖和权限为什么必须分层。
4. 重点读 P3 和 P4。这两篇是 Runtime 的核心，也是面试最容易追问的部分。
5. 按需读 P3.1，理解这项未排期的选做设计为什么不是 sandbox 或主线冻结前置。
6. 读 P5，学会回答“LLM 输出不稳定，测试有什么意义”。
7. 读 P8，理解多 Agent 不是多开几个模型，而是新的并发和治理问题。
8. 最后读 P6、P7。P6 是未排期的选做通用性验证，P7 是持续维护的交付阶段。

每篇都按相近结构组织：

- 先讲这一阶段之前有什么问题。
- 再讲实际做了什么，以及请求经过哪些模块。
- 解释关键代码，而不只罗列文件名。
- 说明为什么采用当前方案、为什么没有采用看似更“高级”的方案。
- 给出测试、边界和面试追问。

## 阶段状态

| 阶段 | 状态 | 当前真实边界 |
| --- | --- | --- |
| [P0 准备](./P0-准备阶段代码变更说明.md) | 已完成 | 固定 Office fixture 与 CI 确定性回归门已落地。 |
| [P1 Office 垂直切片](./P1-office垂直切片代码变更说明.md) | 已完成 | `officecli` 与通用 `office-python` 双 Skill 已落地；后者使用中立 JSON CLI、只读输入和原子 artifact 发布。 |
| [P2 Skill Pack Manifest](./P2-skillpack-manifest代码变更说明.md) | 已完成 | typed manifest、局部 fail closed、结构化 availability、统一开关与显式单轮路由已落地。 |
| [P3 Sandbox / Policy](./P3-policy权限层代码变更说明.md) | 已完成 | Exec/session/CLI Apps 统一 `LaunchSpec`，Seatbelt/Bubblewrap、受批直接 curl、Policy/HITL/OCC 与 fail-closed 已落地。 |
| [P3.1 Workspace / Worktree](./P3.1-worktree隔离代码变更说明.md) | 选做（未实现） | 已完成 WebUI per-chat worktree、HEAD 基线、持久绑定、fork 和保守清理的契约设计；未排期，不计入 Runtime Core 验收。 |
| [P4 Artifact / Checkpoint](./P4-artifact-checkpoint代码变更说明.md) | 已完成 Core | 输入快照、artifact/lineage、hash-bound checkpoint、pending/uncertain 恢复已落地；不承诺通用 exactly-once。 |
| [P5 Trace / Eval](./P5-trace-eval代码变更说明.md) | P5.1 代码完成，外部 smoke 待配置 | cassette、脱敏 trace、Langfuse SDK observation、Dataset/Experiment、OfficeBench evaluator、三套 adapter、独立 benchmark venv、Annotation Queue/export contract 已落地；Terra Judge、Cloud真实回读、人工审核和发布分数待配置。 |
| [P6 通用性扩展](./P6-通用性扩展代码变更说明.md) | 选做（未实现） | 仅完成 Research 最小闭环设计，未排期，不计入项目冻结和最终验收。 |
| [P7 最终交付](./P7-面试交付物代码变更说明.md) | 代码完成，外部证据待配置 | README 最终结果页、Mermaid 架构图、CI/benchmark 入口、三套 adapter、Langfuse Experiment/Annotation Queue/export contract 已落地；真实 Cloud/Terra/人工审核结果尚未发布。 |
| [P8 多 Agent 编排](./P8-多agent编排代码变更说明.md) | 已完成 Core | active-plan gate、最多 5 个 direct child、禁止嵌套、隔离 artifact/HITL/trace 已落地；共享文件租约未实现。 |

## 一条任务在系统里的总调用链

以 WebUI 中一个复杂 Office 任务为例：

```text
WebUI WebSocket 消息
  -> AgentLoop 读取 session、execution_mode、selected_skills、附件路径
  -> ContextBuilder 注入可用 Skill 或显式选中的 Skill
  -> AgentRunner 调用 LLM
  -> LLM 先调用 plan 创建任务契约
  -> PlanTool 快照输入、保存 plan、绑定 plan_hash
  -> 每个工具调用先经过 PolicyEngine
       ├── deny：直接拒绝
       ├── ask：创建持久化 InteractionRequest，停止当前执行链
       └── allow：继续
  -> Shell/CLI 由 SandboxLauncher 生成并执行不可变 LaunchSpec
       ├── restricted one-shot 默认断网；受批直接 curl 使用 pinned argv
       └── 持久 exec session 始终断网，不继承一次性 grant
  -> 文件写入前经过 actor-local SHA-256 OCC
  -> 产物登记到 ArtifactStore，执行中状态写 CheckpointStore
  -> TraceHook 记录脱敏事件
  -> Eval Harness 对固定 case 做硬门验收
```

理解这条链后，你就不会把几个容易混淆的概念混为一谈：

- Skill 告诉模型“怎么做”，不代表它有权限做。
- manifest 声明“需要什么”，不代表 Runtime 自动授权。
- plan 说明“准备做什么”，不代表高风险工具已经获批。
- Policy 决定“逻辑上能否做”，Sandbox 决定“进程实际上能碰到什么”。
- Checkpoint 保存“执行到哪”，Artifact 保存“产生了什么以及从哪来”。
- Trace 记录行为，Eval 判断行为和结果是否满足硬门。

## 面试时建议使用的主线

不要从“我接了几个模型、做了几个页面”开始讲。更有区分度的讲法是：

> 我基于 nanobot 的 AgentLoop/AgentRunner 做二次开发，先用固定 Office fixture 建立确定性真值，再做两个独立 Office Skill 验证真实任务链。随后把 Runtime 补成分层治理：Skill manifest 负责声明和诊断，Policy 负责 allow/ask/deny，Seatbelt/Bubblewrap 负责 OS 强制，InteractionRequest 负责可持久化的人机等待，OCC 防止并发覆盖，Artifact/Checkpoint 负责血缘和恢复，Cassette/Trace/Eval 负责无 Key 回归与硬门量化，最后再把同一套约束复用到 Subagent。

如果被追问“最难的是什么”，优先讲：

1. 权限不是一个布尔开关，而是 sandbox、policy、approval reviewer 三个正交维度。
2. 人工确认不能靠普通聊天文本续跑，必须持久化并恢复原 `tool_call_id`。
3. checkpoint 不能假装 exactly-once，外部副作用必须区分 pending 和 uncertain。
4. 多 Agent 的难点不是并发调用模型，而是权限、上下文、文件、产物和 trace 的隔离。

## 维护约定

- 代码说明只描述真实状态。规划项必须明确写“尚未实现”。
- Runtime 阶段方案或代码变化时，同一任务内更新对应阶段计划、代码说明和 `docs/修改记录.md`。
- 测试数量是某次验证快照，不把历史数字冒充永久保证；当前结果以实际重新运行命令为准。
- 面试材料只能引用有代码、测试、trace 或 benchmark 证据的能力。
