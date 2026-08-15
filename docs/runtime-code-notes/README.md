# Mybot Agent Runtime 二次开发教学指南

> 面向秋招 Agent / AI 应用开发岗位。这里不按开发阶段记流水账，而是按“可以写进简历并经得住追问
> 的工程亮点”组织。每篇都以当前代码和测试为事实源，讲清问题、架构、调用链、难点、取舍、证据、
> 边界和面试表达。01-13 每篇都提供自测参考答案和进阶面试题，第 14 篇集中训练跨模块系统设计追问。

## 1. 先建立正确的项目认知

Mybot 不是“接一个模型、加几个工具”的聊天项目。它在 nanobot 的 AgentLoop/AgentRunner 基础上，
重点补齐了生产型 Agent 最容易缺失的六类能力：

1. **执行治理**：Agent 能做什么、何时问人、怎样防越权和覆盖。
2. **可靠执行**：复杂任务怎样计划、并行、暂停、恢复和验证完成。
3. **领域交付**：Office 文件不是生成一段文本，而是形成可打开、可校验、可追踪的真实产物。
4. **证据闭环**：系统行为有 Trace，结果质量有 hard gate、Judge 和人工审核。
5. **持续改进**：Bad Case 不只做复盘，还能生成受控派生 Skill 并隔离回归。
6. **执行效率**：PTC 在不绕过治理的前提下压缩多轮工具 round-trip 和中间上下文。

你面试时的核心观点应该是：

> LLM 负责不确定的理解和决策，Runtime 负责确定性的权限、状态、并发、恢复和验证。

## 2. 推荐阅读顺序

### 第一遍：先能讲完整项目

1. [从 nanobot 到可治理 Agent Runtime](./01-从nanobot到可治理AgentRuntime.md)
2. [Plan Mode 与 DAG 任务编排](./04-PlanMode与DAG任务编排.md)
3. [Sandbox、Policy、HITL 与文件 OCC](./05-Sandbox-Policy-HITL与文件OCC.md)
4. [Artifact 血缘与 Checkpoint 恢复](./06-Artifact血缘与Checkpoint恢复.md)
5. [Trace、Langfuse 与评测闭环](./08-Trace-Langfuse与评测闭环.md)

读完后，你应该能用 3 分钟从用户请求讲到安全执行、状态落盘和评测证据。

### 第二遍：补业务与并发亮点

1. [Skill Manifest 与运行时治理](./02-SkillManifest与运行时治理.md)
2. [OfficeCLI 办公自动化 Skill Pack](./03-OfficeCLI办公自动化SkillPack.md)
3. [受控多 Agent 协作](./07-受控多Agent协作.md)
4. [评测驱动的 Skill 自进化](./09-评测驱动的Skill自进化.md)

### 第三遍：补产品化和工程化

1. [长任务目标与跨回合续跑](./10-长任务目标与跨回合续跑.md)
2. [WebUI 运行时控制面](./11-WebUI运行时控制面.md)
3. [测试体系与工程交付](./12-测试体系与工程交付.md)
4. [PTC Code Mode：程序化工具调用](./13-PTC-CodeMode程序化工具调用.md)

### 第四遍：做跨模块故障推演

1. [全链路融会贯通与 Agent 面试手册](./14-全链路融会贯通与Agent面试手册.md)
2. 回到 01-13 的“面试级深挖”，完成其中的故障实验，不再只读结论。

01-13 足以建立模块级理解，14 用于练习组合推理。但文档本身不会让你自动获得面试深度；
至少要亲自跟一次源码调用链、解释两个回归测试、复现一个故障边界。

## 3. 亮点与证据矩阵

| 专题                                                | 简历关键词                                            | 主要代码                                                   | 核心测试                                               |
| ------------------------------------------------- | ------------------------------------------------ | ------------------------------------------------------ | -------------------------------------------------- |
| [01 Runtime 架构](./01-从nanobot到可治理AgentRuntime.md) | 增量二开、状态机、控制面/执行面分离                               | `nanobot/agent/loop.py`、`runner.py`、`nanobot/runtime/` | loop/runner integration、runtime tests              |
| [02 Skill 治理](./02-SkillManifest与运行时治理.md)        | typed manifest、fail closed、热刷新、显式路由              | `skill_manifest.py`、`skills.py`                        | `test_skill_manifest.py`、`test_skill_selection.py` |
| [03 OfficeCLI](./03-OfficeCLI办公自动化SkillPack.md)   | 固定供应链、DSL 编译、OpenXML、verified facts              | `skills/officecli/`、`officecli_runtime.py`             | `test_officecli_runtime.py`                        |
| [04 Plan/DAG](./04-PlanMode与DAG任务编排.md)           | plan-only、hash、DAG、失败重派、完成闸门                     | `tools/plan.py`、`plan_scheduler.py`                    | `test_plan_dag.py`、`test_plan_tool.py`             |
| [05 安全治理](./05-Sandbox-Policy-HITL与文件OCC.md)      | macOS Seatbelt 沙箱、Policy、参数绑定审批、OCC              | `runtime/policy.py`、`security/sandbox/`                | sandbox/policy/OCC、interaction、redteam tests       |
| [06 恢复](./06-Artifact血缘与Checkpoint恢复.md)          | snapshot、lineage、state hash、uncertain            | `artifacts.py`、`checkpoint.py`                         | `test_artifacts_checkpoint.py`                     |
| [07 多 Agent](./07-受控多Agent协作.md)                  | 权限收紧、上下文隔离、后台调度、父子 Trace                         | `subagent.py`、`plan_scheduler.py`                      | `test_subagent_governance.py`                      |
| [08 观测评测](./08-Trace-Langfuse与评测闭环.md)            | semantic trace、cassette、Dataset Run、Judge、resume | `trace.py`、`langfuse*.py`、`evaluations/`               | trace/eval/benchmark contract tests                |
| [09 Skill 自进化](./09-评测驱动的Skill自进化.md)             | Bad Case、派生 Skill、diff/digest、隔离 A/B             | `evaluations/skill_evolution.py`                       | `test_skill_evolution.py`                          |
| [10 长任务](./10-长任务目标与跨回合续跑.md)                     | sustained goal、内部续回合、预算边界                        | `goal_state.py`、`turn_continuation.py`                 | goal/continuation/runner tests                     |
| [11 WebUI 控制面](./11-WebUI运行时控制面.md)               | 实时投影、刷新恢复、计划/HITL/Trace/评测工作台                    | `webui/src/components/`                                | `webui/src/tests/`                                 |
| [12 测试交付](./12-测试体系与工程交付.md)                      | hard gate、红队、fake provider、可复现 benchmark         | `tests/`、`benchmarks/`、`cli/benchmark.py`              | CI + deterministic suites                          |
| [13 PTC Code Mode](./13-PTC-CodeMode程序化工具调用.md)   | 程序化工具编排、生成 SDK、子进程 RPC、有界并发、上下文裁剪                | `nanobot/agent/ptc/`、`runner.py`                       | `test_ptc_*.py`、Runner/WebUI activity tests        |
| [14 融会贯通](./14-全链路融会贯通与Agent面试手册.md)              | 真相源、跨模块不变量、故障推演、设计取舍                             | 串联 `loop.py`、`runner.py`、`runtime/`                    | 按专题追踪集成/故障测试                                       |

Skill 自进化的真实运行证据见 [r18 重测报告](../Skill自进化-r18重测报告.md)：记录 27 个目标
Case 的分类 delta、提分机制、持平/下降原因，以及测试后继续迭代的当前能力边界。

## 4. 面试主线怎么选

### 3 分钟版本

1. 先讲为什么基于 nanobot 增量二开，而不是重写框架。
2. 用 Plan DAG 说明复杂任务怎样从自然语言变成版本化执行契约。
3. 用一次高风险 Shell/文件调用说明 Policy、HITL、Sandbox、Approval 和 OCC 怎样串起来。
4. 用 Artifact/Checkpoint 说明进程被杀或等待用户后怎样恢复。
5. 用 Subagent 说明并行不是简单 `asyncio.gather`，还要治理权限、上下文和产物。
6. 用 PTC 说明怎样压缩工具 round-trip，同时不建立权限旁路。
7. 用 Langfuse + deterministic hard gate 说明怎样证明系统行为和模型质量。
8. 最后用 Skill 自进化说明评测结果怎样反哺系统。

### 30 秒版本

> 我基于 nanobot 做了一套可治理 Agent Runtime。核心不是增加几个工具，而是把复杂任务变成 hash
> 绑定的 DAG，把工具调用统一接入 Policy、OS Sandbox、可恢复 HITL 和文件 OCC，再用 Artifact 与
> Checkpoint 支持 kill-resume。多 Agent 只能继承或收紧权限，执行过程进入本地 Trace/Langfuse，
> PTC 通过受限子进程和 Runner 工具桥接压缩多轮调用。结果由确定性 hard gate 和 Dataset Judge 评估；低分 Case 还能生成独立派生 Skill，支持隔离回归、
> 人工应用和回退。OfficeCLI 是我验证这套 Runtime 的第一个复杂业务 Skill。

## 5. 面试官最可能沿哪些方向追问

### 架构类

- 为什么不直接修改 AgentRunner 的 Prompt，让模型“自觉”遵守权限？
- Runtime 层和 Tool 层的职责怎么划分？
- 哪些能力来自 nanobot，哪些是你实现的？
- 为什么不用 LangGraph、Temporal 或现成工作流引擎？

### 安全类

- Full Access 为什么还需要 Policy？
- 审批怎样防止参数被替换或重复使用？
- 沙箱不可用时怎么办？
- Seatbelt 当前覆盖哪些进程，为什么 stdio MCP 和 OfficeCLI 内部进程明确未覆盖？
- OCC 和文件锁、数据库事务有什么区别？

### 可靠性类

- 如何判断一个 Tool Call 已完成、可以重试还是状态不确定？
- 为什么不承诺 exactly-once？
- Plan 修改后怎样处理已经完成的节点和旧审批？
- WebSocket 断开后为什么不会丢失审批和任务状态？

### 多 Agent 类

- 多 Agent 真比单 Agent 好吗，如何证明？
- 为什么禁止嵌套？
- Child 怎样共享事实又避免复制全部上下文？
- 两个 Agent 同时写同一文件怎么办？

### PTC 类

- PTC 和让模型一次返回多个 native tool call 有什么区别？
- 为什么 worker 不能直接调用工具对象？
- 为什么写工具要形成 barrier，而不是全部 `gather`？
- PTC 审批后为什么不恢复 Python 栈？
- 怎样证明 PTC 真的节省 token/时间而不降低成功率？

### 评测类

- LLM 不稳定，pytest 有什么意义？
- 为什么 Judge 不能替代确定性校验？
- Resume 和 Retry 有什么区别？
- 如何避免为了 Benchmark Case 过拟合 Skill？

每篇专题已经把这些问题拆开回答。你的目标不是背标准答案，而是能沿实际数据结构和调用链解释。

## 6. 简历写法原则

### 应该写

- 你改变了什么系统契约。
- 为什么这个问题不能只靠 Prompt 解决。
- 关键数据结构和状态机。
- 可核对的测试、指标或真实运行证据。
- 明确的失败边界和取舍。

### 不应该写

- “独立研发通用 Agent 框架”，因为底层基于 nanobot。
- “实现绝对安全沙箱”，因为网关和全部 MCP 进程不在统一 microVM 中。
- “保证任务 exactly-once”，因为外部副作用只能分类恢复。
- “多 Agent 显著提升效果”，除非给出同 Case 的成功率、成本和时延对比。
- “Skill 自动上线且无回退”，当前是派生 Skill + 局部回归 + 人工应用。
- “OCB 官方分数”，当前是 Mybot 使用 OCB 数据和自定义 Judge 的项目评测。

## 7. 学习方法

每读完一篇专题，完成三件事：

1. 不看笔记，画出一次请求的调用链和关键状态。
2. 找到至少两个对应测试，解释它们在防什么回归。
3. 用“为什么不用更简单方案”反问自己，必须能说出取舍而不是只背实现。

自测和面试题建议分三轮使用：

1. **闭卷回答**：先只看题目，用 30-90 秒口述，强制说出真相源、状态转换和失败边界。
2. **对照答案**：参考答案用于查漏，不是逐字背诵；把答案里的模块名替换成你亲自看过的代码位置。
3. **故障追问**：继续追问“如果崩溃、重复回调、参数变化或评分缺失会怎样”，并用测试或 Trace 证明。

01-13 合计包含 130 道带答案的专题自测/进阶题；第 14 篇另有自我验收框架和 30 道综合题，适合
按 3 分钟项目介绍、单模块深挖、跨模块系统设计三个层次反复练习。

建议在本地实际运行：

```bash
source venv/bin/activate
pytest tests/runtime/ -q
pytest tests/skills/test_officecli_runtime.py -q
pytest tests/evaluations/test_skill_evolution.py -q
pytest tests/agent/test_ptc_sdk.py tests/agent/test_ptc_runtime.py tests/agent/test_ptc_runner.py -q
cd webui && bun run test
```

真实模型和 Langfuse 评测成本较高，不要为了准备面试反复全量执行；优先读固定 Case、checkpoint、
Dataset Run 和 Bad Case 分析，再挑少量 smoke 复现。

## 8. 统一维护约定

- Runtime 相关代码或方案变化，必须更新 `docs/修改记录.md` 和受影响的专题笔记。
- 新增独立能力时新增专题，不再恢复 P0-P9 阶段编号。
- “已实现”必须能指向代码和测试；未实现内容只能写在“边界与后续改进”。
- 指标必须说明口径、数据集、模型、评测器、样本数和缺失值处理。
- 总体边界以 `docs/Mybot通用AgentRuntime与办公自动化SkillPack整合方案.md` 为准。
