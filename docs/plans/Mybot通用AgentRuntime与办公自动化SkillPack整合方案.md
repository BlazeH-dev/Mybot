# Mybot 通用 Agent Runtime 与办公自动化 Skill Pack 整合方案

> 本文合并自：
> - `docs/plans/Mybot通用AgentRuntime与办公自动化SkillPack项目方案.md`
> - `docs/plans/2026-06-16-agent-runtime增量开发计划.md`
>
> 合并口径：保留定位、架构、面试叙事、二开重点、阶段优先级、时间线、依赖关系、cutline 与最终验收；不再列出详细步骤文档索引，也不展开各阶段逐步实施清单。

## 修订脉络

- 2026-06-16：形成 Agent Runtime 增量开发计划。
- 2026-07-03：以秋招面试为目标重排优先级；新增面试亮点地图、量化指标基线、checkpoint 恢复、安全红队、多模型/成本回归、P7 面试交付物阶段。
- 2026-07-03 二次修订：新增计划契约（Plan Mode）、KV cache 成本工程、受控多 Agent 编排（P8）。
- 2026-07-06 三次修订：二开代码统一收进 `nanobot/runtime/` 内核包；新增轻量 LLM cassette 回归层设想；WebUI 自研面积收缩；新增定位辩护与 S0.2 真 CI。
- 2026-07-06 微调：S5.0 收缩为轻量 cassette smoke，只覆盖计划确认、ask/deny、提示词注入诱导越权副作用或泄漏、checkpoint 恢复 3-4 个关键 case，作为 CI / demo 无 API key 可复现证明层，不抢 P1/P3/P4 主线时间。
- 2026-07-11 四次修订：曾将 Office Skill 的底层文档引擎改为固定版本 OfficeCLI，并把 Python 渲染器降为差分路径；该单 Skill 后端结构已由 2026-07-14 修订取代。P2 manifest 提升为必做，P6 Research 前移验证通用性，当时 P8 曾降为机动项。
- 2026-07-11 五次修订：计划模式从 Skill 内自然语言约定升级为静态注册的 `plan` 工具。工具 schema 与定义保持稳定并进入内建工具排序缓存，动态计划状态只放在调用参数、plan artifact 和用户消息尾部 Runtime Context，减少稳定前缀抖动；工具负责 plan hash、确认、依赖、步骤状态和产物完成核对。
- 2026-07-12 六次修订：P5 增加 `LLM-as-a-Verifier` 离线轨迹评测 PoC，定位为 S5.4 加分项而非必做项；确定性硬校验仍拥有最终否决权，Verifier 只评软质量、进度与候选轨迹排序，并以自有 case set 上的相关性、误判、成本和时延数据决定是否保留。
- 2026-07-12 七次修订：曾增加 `mtime + SHA-256` 文件并发写保护和白盒记忆治理；2026-07-14 后，OCC 收缩为已有文件 fresh-read hash 硬校验，白盒记忆移出冻结前主线。
- 2026-07-14 八次修订：经 grill-me 逐项压力测试，Office 能力改为两个独立 Skill：`office-automation` 保留 Python 工作流，`officecli` 基于官方固定快照提供完整 OfficeCLI 能力；两者只共享输入快照、verified facts、公共约束与 Runtime 治理，不强制共享 DSL。白盒记忆移出冻结前主线，文件 OCC 收缩为已有文件读后 hash 硬校验，HITL 改为持久化 `pending_approval` 后结束当前执行，checkpoint 仅服务已确认计划任务并使用 completed/pending/uncertain 恢复语义。P8 提升为主线能力，最多 5 个直接子 Agent，禁止嵌套，权限只紧不松，隔离上下文/产物并强制父子 trace 与成本时长对比。

## 1. 一句话定位

Mybot 不是重写 nanobot，而是在现有 nanobot v0.2.1 基础上做一层“可控、可扩展、可评测”的个人 Agent Runtime。

二次开发重点：

> 复用现有 AgentLoop、AgentRunner、MessageBus、WebSocket WebUI、工具系统和 MCP 接入；重点补齐 Skill Pack、权限治理、产物追踪、任务状态、Office 自动化和评测闭环。

面试版 30 秒电梯陈述：

> 我在开源 nanobot 上二开了一个个人 Agent Runtime：Skill manifest 与开关、工具权限内核（allow/ask/deny + 可恢复人工确认）、文件冲突保护、计划契约、产物血缘、断点恢复、OTel 风格 trace、确定性 eval 和受控 Subagent 编排。Office 侧用两个独立 Skill 验证能力治理：Python `office-automation` 与官方能力适配后的 `officecli` 共享 verified facts 和 Runtime 安全边界，但保留各自工作流；高风险动作不会阻塞 Runner 等待确认，子 Agent 权限只能收紧且执行成本可量化。

## 2. 秋招目标与优先级

### 2.1 时间线

| 时间 | 阶段 | 产出 |
| --- | --- | --- |
| 7/7-7/18 | P0 + P1（双 Office Skill + S1.9） | Python/OfficeCLI 两个独立 Skill + 共享事实层 + 计划契约 |
| 7/19-7/21 | P2 | 双 Skill manifest、依赖/可用性治理与启用/禁用开关 |
| 7/22-7/31 | P3 + 轻量 S5.0 | 权限内核 + 参数哈希绑定的人工确认 + 文件冲突硬保护；用 cassette/确定性 case 固化关键行为 |
| 8/1-8/7 | P4 | 输入快照 + 产物血缘 + 计划任务 checkpoint/resume |
| 8/8-8/14 | P8 | 最多 5 个直接子 Agent 的权限/预算/上下文/产物治理 |
| 8/15-8/20 | P5 Core + P6 Research | 父子 trace、确定性 eval、安全红队与第二领域验证 |
| 8/21-8/24 冻结 | P7 | 一键 benchmark、README、demo 脚本、答辩稿；此前持续维护，冻结期只修 bug |
| 机动 | 白盒记忆 / S4.5 / S5.4 / S5.7 / S5.8 | 记忆治理、delta、judge/Verifier、多模型与 cache 优化服从主线完整度 |

### 2.2 阶段优先级

| 阶段 | 内容 | 秋招优先级 | 面试价值 |
| --- | --- | --- | --- |
| P0 | 固定 fixture + 真 CI | 必做 | 评测方法论起点 + 公开可见回归门 |
| P1 | 双 Office Skill + 共享事实层 + 计划契约 | 必做 | Skill 边界、grounding、两套完整 Office 能力 |
| P2 | SkillPack manifest | 必做 | 外部能力版本、依赖、权限与可用性治理 |
| P3 | Policy 权限层 + HITL + 文件并发写保护 | 必做 | 安全叙事核心亮点 + 防止覆盖用户并发修改 |
| P4 | 输入快照 + Artifact 血缘 + Checkpoint 恢复 | 必做 | 可重放输入、血缘与安全恢复 demo |
| P5 | Trace / 确定性 Eval / 视觉质量 / 红队 / 轻量回放 | 必做 | 评测、安全、可观测性核心亮点 |
| P6 | Research skill 验证通用性 | 必做 | 证明 runtime 接口没有 Office 私有假设 |
| P7 | 面试交付物 | 必做 | 把工程成果转成面试表达 |
| P8 | 受控 Subagent 编排 | 必做 | 权限继承、预算、隔离上下文/产物与父子 trace |

### 2.3 面试亮点与阶段映射

| 面试常问 | 对应阶段 |
| --- | --- |
| 如何防幻觉 / 编数字 | P1（verified facts + DSL + 校验） |
| 长任务不跑偏 / 不烂尾 | S1.9（计划契约）+ P4（plan 入血缘）+ P5（plan_completion 指标） |
| Agent 执行命令的安全 | P3（deny/ask/allow + HITL） |
| 用户或 IDE 同时修改文件怎么办 | P3（read snapshot + 写前冲突硬拦截） |
| 提示词注入 / 工具投毒 | P5（红队 eval）+ P3（权限策略与硬边界） |
| 怎么评测、改动怎么回归 | P0 + P1.8 + 轻量 S5.0 + P5 |
| 线上问题怎么排查 | P5（OTel 风格 trace） |
| 长任务挂了怎么办 | P4（checkpoint 落盘 + 恢复） |
| 产物追溯 / 局部修改 | P4（血缘 + delta） |
| 成本控制 / 缓存 / 换模型 | S5.7（成本矩阵）+ S5.8（KV cache 命中率） |
| Subagent 如何治理 | P8（最多 5 个直接子 Agent + 权限/预算/隔离/父子 trace） |
| 基于开源二开，哪些是你写的 | `nanobot/runtime/` 内核包边界 + P7 diff 统计与架构图分色 |
| 为什么不用 LangChain | 全局叙事：轻量二开、自研关键层、own your control flow |

## 3. 面试叙事与亮点地图

### 3.1 为什么这个项目能让面试官眼前一亮

- **不是“调 API 的套壳 demo”**：项目补的是 Agent 工程层，包括权限治理、评测回归、可观测性、产物治理，呼应 Anthropic《Building Effective Agents》、12-Factor Agents、OWASP LLM Top 10、OpenTelemetry GenAI 语义约定、MCP 安全实践。
- **每个亮点都是闭环**：失败模式 -> 工程对策 -> 量化验证。
- **轻量二开而非重框架堆砌**：不依赖 LangChain/LangGraph/Dify，runtime 关键层在真实 nanobot 承接点上实现。
- **有数据**：每个阶段都记指标基线，面试能报“从 X 到 Y”，而不是只报“做了什么”。

### 3.2 亮点地图

| 面试常问 | 项目里的答案 | 对应业内实践 |
| --- | --- | --- |
| 如何防止大模型编造数字 / 幻觉？ | 两个 Office Skill 在定量任务中都消费同一 verified facts；Python Skill 使用自有 DSL，OfficeCLI Skill保留自身命令工作流，最终成品数字都必须回溯到 `fact_id`。 | OWASP LLM Top 10、grounding、code-over-model |
| Agent 能执行命令、读写文件，安全怎么办？ | 权限内核：策略层包在 workspace/SSRF 硬边界外，`deny` 同步拦截、`ask` 异步人工确认；权限决策全部进审计 trace。 | 最小权限、PreToolUse 门控、Human-in-the-loop |
| 用户或 IDE 在 Agent 执行期间改了同一文件怎么办？ | 所有内建文件修改工具执行前校验本会话读取快照；文件未读或 `mtime/content_hash` 已变化时返回结构化 `file_conflict` 并硬失败，要求重新读取。多文件 patch 先完成全量 preflight，再逐文件原子替换。 | Optimistic concurrency control、read-before-write、TOCTOU 防护 |
| 提示词注入 / 工具投毒怎么防？ | 不承诺识别全部注入；外部内容统一视为不可信数据，即使模型受影响，workspace/SSRF/敏感信息硬边界和 ask/deny 策略仍保证越权副作用与泄漏为 0。 | OWASP Prompt Injection、Excessive Agency、defense in depth |
| 长任务怎么保证不跑偏、不烂尾？ | 静态 `plan` 工具创建结构化契约并落盘；plan hash 经用户确认后激活，依赖未完成不能越级，步骤/产物未齐不能 complete。工具定义稳定进入缓存前缀，动态状态放 Runtime Context 尾部。 | Claude Code plan mode、todo 驱动长任务、prompt caching |
| 怎么评测 Agent？改了 prompt 怎么知道没变差？ | 三层测试：确定性脚本单测覆盖事实、权限和文件冲突；轻量 cassette 覆盖计划、持久化审批和恢复；真模型 benchmark 手动跑。安全/数字/副作用是 100% 硬门，Judge/Verifier 不能推翻。 | Agent CI、轻量 record-replay、LLM-as-judge/verifier 边界 |
| 线上 Agent 出问题怎么排查？ | AgentHook 非侵入采集 span 树写 JSONL，字段对齐 OTel GenAI 语义约定，可导入 Jaeger/Langfuse；trace 与 UI 事件流解耦。 | OpenTelemetry GenAI semconv、observability via hooks |
| 长任务跑一半挂了怎么办？ | 仅对已确认计划任务落 durable checkpoint；恢复将工具调用区分 completed/pending/uncertain，只有有幂等键或可验证产物的调用自动重放，未知副作用转人工处理。 | Durable execution、at-least-once、idempotency |
| 产物怎么管理、错误怎么定位来源？ | 任务开始保存不可变输入快照；Python/OfficeCLI Skill 各自产物都登记 checksum、来源、工具与验证结果，可回溯到同一 verified facts 和原始快照。 | Provenance/lineage、reproducible build |
| 为什么同时保留两个 Office Skill？ | Python Skill 是受约束的 grounded report 工作流；OfficeCLI Skill 是通用 Office DOM/检查/编辑能力。两者独立加载、可禁用、共享治理但不强制共享 DSL，能真实验证 Skill Pack 而非伪装成单一 renderer 开关。 | Ports and adapters、capability package、A/B implementation |
| 成本优化具体做过什么？ | usage 进 trace 折算每任务 token/成本；上下文按“稳定前缀在前”重排提升 KV cache 命中，用 `prompt_cache_hit_tokens` 量化命中率与成本降幅；跨模型矩阵给成本-质量曲线。 | Prompt caching 经济学、context engineering |
| Subagent 如何治理？ | 父 Agent 可按提示与计划决定是否派生最多 5 个直接子 Agent；禁止嵌套，权限只能收紧，预算、上下文和产物隔离，使用时必须记录父子 trace 并对比顺序执行成本与时长。 | Context isolation、least privilege、budgeted delegation |
| 换个模型还能用吗？ | 同一 eval set 在 DeepSeek V4 Pro/Flash、MiMo 上跑回归矩阵，输出成功率、成本、时长，给模型分级路由建议。 | 模型无关设计、成本-质量权衡 |
| 为什么不用 LangChain/LangGraph？ | 场景是单 agent 主干 + 确定性脚本 + 受控子代理，重框架收益低、黑盒多；基于 nanobot 真实承接点二开，每层可解释、可测试。 | 12-Factor Agents: own your control flow |
| 能力怎么扩展？会不会越加越乱？ | Skill Pack：`skill.yaml` 声明版本/依赖/权限/eval，Registry 可校验可禁用；用第二个 skill 验证 runtime 通用性后才收敛公共接口。 | 插件化、refactor-when-proven |
| 基于开源二开，哪些是你自己写的？ | 二开核心收在 `nanobot/runtime/` 内核包 + office skill + tests；README 给出上游 v0.2.1 基线 diff 统计与架构图分色。 | 模块边界即架构叙事、可验证贡献声明 |

### 3.3 量化指标基线

每个阶段完成后跑一键 benchmark，把指标记入 `docs/plans/metrics-baseline.md`，随迭代形成趋势表。

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| 任务成功率 | eval case set 确定性指标通过率 | >= 90% |
| 数字一致性 | 成品 docx/pptx 中关键数字可追溯到 `fact_id` 的比例 | 100% |
| 计划完成率 | `plan.json` 承诺步骤/产物的实际交付比例 | 100% |
| 注入诱导越权副作用 | 红队 case 中因不可信内容导致越权写入、敏感读取或未确认外发的次数 | 0 |
| 越权写入 | workspace 外 / 覆盖用户原文件次数 | 0 |
| 文件冲突拦截率 | fixture 中读取后被外部修改的目标文件，在写入前被 `file_conflict` 拦截的比例 | 100% |
| 并发误覆盖 | Agent 覆盖读取后由用户/IDE 修改内容的次数 | 0 |
| 每任务成本 | trace usage 汇总 token 数，按模型单价折算 | 记录并给出优化前后对比 |
| KV cache 命中率 | `prompt_cache_hit_tokens` / 总输入 token | 记录并给出布局优化前后对比 |
| Subagent 编排开销 | 同任务集单 Agent 顺序执行 vs 含子 Agent 执行的成功率/时长/token 成本 | 只要启用子 Agent 就必须记录并对比 |
| P95 时长 | 端到端任务耗时 | 记录趋势 |
| 断点恢复成功率 | demo 路径 kill -> resume 成功比例 | 100% |
| 回归耗时 | pytest smoke，不调真实 LLM，含轻量 cassette agent case | < 60s |
| Office 构建可重放率 | 固定输入快照 + facts + Skill 自有中间产物 + 引擎版本可重新生成等价产物的比例 | 可重放任务 100%；reference-only 输入不计入 |
| OfficeCLI 结构校验通过率 | 生成 docx/pptx 经固定版本 OfficeCLI OpenXML validate 通过的比例 | 100%，兼容性基线例外显式登记 |
| 视觉质量通过率 | screenshot 的空页、文本缺失、明显溢出/遮挡等确定性检查通过率 | >= 95% |
| Verifier 有效性（可选） | 细粒度轨迹评分与人工评价/确定性结果的相关性、误判、额外成本与时延 | 只在能发现现有 eval 漏检问题且成本可接受时保留，不设为硬门 |

从 P1 起就记基线，不等最后补。面试叙事要能说“从 X 优化到 Y”。

### 3.4 定位辩护：通用 Runtime + 办公 Skill 为什么是对的

这个组合复刻行业主流形态：Claude Code = 通用 runtime + Skills 领域包。

- **两层叙事互补**：纯垂直工具讲不出架构深度，纯框架讲不出落地与评测；组合让平台工程与垂直交付都有话讲。
- **Office 是为“可评测”服务的设计决策**：docx/pptx/数字可确定性校验，是 eval 和防幻觉叙事成立的前提。
- **差异化**：校招 agent 项目多为 coding agent / RAG 问答，office + 治理层撞车率低。
- **为什么不做 coding agent 主线**：面试官常用 Claude Code/Cursor，自制 coding agent 容易被工业品正面对比；code 场景留作 P6 第二 skill。

配套风险对策：

1. **通用性证据不能只靠 P6**：上游内建 skill 与 office 走同一 `SkillsLoader`、同一渐进披露机制；office 未走私有通道本身就是通用性证据。
2. **防“脚本项目”误读**：简历标题、README 第一句、demo 第一步都必须是 runtime/治理；office 永远以验证场景身份出现。
3. **定位语避免显小**：README 不以“个人 AI 助手”开头，而以“Agent Runtime + 领域 Skill 样板”开头。

## 4. 已有能力不重复建设

这些能力由 nanobot 已提供，二开不重造：

| 已有能力 | 当前承接点 | 二开方式 |
| --- | --- | --- |
| WebSocket 入口 | `nanobot/channels/websocket.py` | 保持默认唯一主通道 |
| 消息总线 | `nanobot/bus/queue.py` | 继续用 `InboundMessage` / `OutboundMessage` |
| Agent 循环 | `nanobot/agent/loop.py`、`runner.py` | 不重写，只加状态、hook、策略 |
| 工具注册 | `nanobot/agent/tools/` | 扩展工具元数据和权限检查 |
| 子代理 | `nanobot/agent/tools/`（spawn 类工具） | 复用派生能力，增加数量、嵌套、权限、预算、上下文和产物治理 |
| Skill 加载 | `nanobot/agent/skills.py` | 兼容 `SKILL.md`，旁路增加 `skill.yaml` |
| MCP 接入 | `nanobot/agent/tools/mcp.py` | 增加 trust level、白名单和审计 |
| Workspace 边界 | `nanobot/security/` | 继续作为硬边界，外层加策略层 |
| 上下文治理 | runner 的 microcompact / snip / offload | 不重造，只做用量量化、缓存优化与分层注入 |
| WebUI 设置 | `nanobot/webui/settings_*` | 增加 Skill、Artifact、Trace 轻量页面 |

## 5. 二次开发重点

### 5.0 代码布局：`nanobot/runtime/` 内核包

P3-P5 的新增运行时代码统一收进：

```text
nanobot/runtime/
├── policy.py        # P3 权限决策（PermissionDecision + decide）
├── trace.py         # P5 TraceHook（OTel GenAI semconv）
├── artifacts.py     # P4 通用 artifact store + lineage
├── checkpoint.py    # P4 checkpoint 落盘 + 恢复
├── approvals.py     # P3 pending_approval 持久化与恢复
├── replay.py        # 轻量 LLM cassette 回归层（S5.0）
└── evals/           # P5 eval runner / metrics / report
```

原则：

- 接线方式不变，仍在 loop / registry 处最小接线，不改 runner 主循环。
- 包内单文件起步，不过度分层；只有 evals 因 metric 插件较多使用子包。
- `security/` 代表上游硬边界，`runtime/policy` 代表自研策略层；这个包边界就是架构图上的“硬边界 vs 策略层”分层线。
- 这些设施立项时已有多个消费者（office skill + loop/registry 接线），不必等 P6 再高风险搬家。

面试点：README 架构图、“二开新增 vs nanobot 原有”边界、`git diff --stat <上游基线>..HEAD -- nanobot/runtime` 互相印证。

### 5.1 Skill Pack 插件化

Mybot 要从“会加载提示词的机器人”升级为“能加载领域能力包的 Runtime”。

要做：

- 保留现有 `SKILL.md`，新增可选 `skill.yaml`。
- `skill.yaml` 声明版本、输入输出、依赖工具、权限、产物类型、eval case。
- 做本地 Skill Registry：发现、解析、校验、启用、禁用、展示状态。
- Skill 内容渐进加载：默认只进上下文 name/description，需要时再读 workflow、schema、example。
- 首批样板是两个独立 Office Skill：`office-automation` 与 `officecli`；默认 Office 请求优先匹配 `officecli`，用户可显式选择 Python Skill，也可通过 `disabledSkills` 禁用任意 Skill。

建议目录：

```text
nanobot/skills/office-automation/
├── SKILL.md
├── skill.yaml
├── scripts/
├── schemas/
├── workflows/
├── evals/
└── examples/

nanobot/skills/officecli/
├── SKILL.md
├── skill.yaml
├── references/
└── scripts/

nanobot/skills/_shared/office_core/
├── scripts/
└── references/
```

面试点：渐进披露就是上下文工程，能力包再多也不膨胀系统提示。

### 5.2 工具权限治理

Agent 能读写文件、跑命令、调 MCP，必须有权限层。

要做：

- 扩展 `Tool` 基类元数据：`capability`、`risk_level`、`requires_approval`；Skill manifest 只声明能力需求，不能授予权限，也不依赖模型可伪造的 `skill_scope` 放宽策略。
- 在 `ToolRegistry.prepare_call` 外层做策略判断；决策核为纯函数，实现在 `runtime/policy.py`。
- 采用 `allow / ask / deny` 三档：
  - `allow`：读 workspace 内文件、生成中间产物。
  - `ask`：改用户原文件、执行高风险 shell、远程 API、发送消息或邮件。
  - `deny`：越界路径、删除原始文件、读取敏感配置、绕过策略。
- `ask` 生成参数 hash 绑定的 `pending_approval` 并落盘，结束当前执行；用户批准/拒绝作为新事件恢复任务，不让 Runner 或 WebSocket 长时间阻塞等待。
- MCP server 增加 trust level、enabled tools、描述静态检查，避免 tool poisoning。
- 每次权限决策写入审计 trace。
- 复用 `agent/tools/file_state.py` 的会话级读状态，为 `write_file`、`edit_file`、`apply_patch` 增加最小写前硬校验：已有文件必须先读，且当前 SHA-256 与读取快照一致；否则返回结构化 `file_conflict`。多文件 patch 在任何写入前统一 preflight。冻结前不承诺完整文件事务、新文件竞态消除、fsync 或最终微小 TOCTOU 窗口保护。

注意：权限层只负责决策和审计；路径、网络、sandbox 硬边界继续复用现有实现。

面试点：`deny` 在同步执行前拦截，`ask` 变为可持久化中断；页面刷新、断线或网关重启不会丢失审批请求。

### 5.3 Artifact Runtime

复杂任务不能只输出聊天文本，要能追踪产物。

要做：

- 建立统一 artifact 目录和 metadata。
- 任务开始时把实际使用的输入复制到任务 `inputs/` 目录形成不可变快照；无法复制的 reference-only 输入标记 `replayable: false`。
- 记录输入、中间产物、最终文件、版本、checksum、来源、生成工具。
- 产物默认写入 runtime/artifacts，不覆盖用户原始文件。
- 血缘可查：成品能回溯到中间 DSL、facts、原始输入；`plan.json` 也登记为任务血缘源头。
- 支持 delta 局部重渲染：“只改第 3 页”只重生成受影响产物，产出 v2 并记录版本关系。
- 让 WebUI 能展示生成的 docx、pptx、json、quality report。

建议结构：

```text
<workspace>/.nanobot-runtime/
├── artifacts/
│   └── task_001/
├── traces/
│   └── task_001.jsonl
├── evals/
│   └── task_001.eval.json
├── checkpoints/
│   └── task_001/
└── index.json
```

最小 metadata：

```json
{
  "artifact_id": "art_001",
  "task_id": "task_001",
  "skill": "office-automation",
  "type": "pptx",
  "path": ".nanobot-runtime/artifacts/task_001/weekly_review.pptx",
  "source_artifacts": ["verified_facts.json", "slide_dsl.json"],
  "tool_calls": ["tool_005"],
  "status": "validated"
}
```

### 5.4 任务状态、Trace 和 Checkpoint

没有任务状态，就无法恢复、复盘、评测。

要做：

- 为每个复杂请求生成 `task_id`，记录 created、planning、executing、awaiting_approval、evaluating、completed、failed。
- 使用 `AgentHook.after_iteration` 写 JSONL trace，字段命名对齐 OpenTelemetry GenAI 语义约定。
- 只为已创建且确认 `plan.json` 的复杂任务落 durable checkpoint；普通聊天继续使用 session history。
- 基于 `AgentRunSpec.checkpoint_callback` 扩展 checkpoint metadata 并落盘，实现在 `runtime/checkpoint.py`。
- 恢复时把调用区分为 `completed`、`pending`、`uncertain`：已持久化完成项跳过；有幂等键或可验证产物的 pending 可重放；邮件、消息等无法确认的副作用转人工决定。
- 可参照上游 `_set_runtime_checkpoint` / `_restore_runtime_checkpoint` 路径，但不宣称通用 exactly-once。

白盒记忆治理保留为冻结后的独立机动项，不进入 P4/P5/P7 必做验收，也不写入冻结前简历和 demo 承诺。

Trace 至少记录用户目标、plan 状态、工具调用、权限与 pending approval、文件冲突、artifact、父子 Agent 关联、token usage、eval、错误和重试。

### 5.5 双 Office Skill Pack

P1 交付两个独立、可启用/禁用的 Skill：

- `office-automation`：原 Python 工作流，使用 verified facts、自有 report/slide DSL、`python-docx` 与 `python-pptx`。
- `officecli`：基于官方固定快照的通用 Office Skill，保留 help/view/DOM/raw/MCP/plugin 等能力；具体调用由 Runtime Policy 按目标和参数分级，而不是在 Skill 层删功能。

两者只共享确定性事实层、输入快照和公共约束，不强制共享 DSL。普通 Office 请求默认优先 `officecli`；用户显式要求原 Python 方案，或 OfficeCLI 被禁用/不可用时，使用 `office-automation` 并说明原因。

| 组件 | 输入 | 输出 |
| --- | --- | --- |
| `_shared/office_core/inspect_workbook.py` | xlsx/csv | workbook schema |
| `_shared/office_core/extract_facts.py` | 表格 + 指标规则 | verified facts |
| `office-automation` 自有 DSL/renderer | facts + notes | docx/pptx + quality report |
| `officecli` 官方命令/可选 helper | facts + OfficeCLI 自有命令或 batch | docx/xlsx/pptx + validate/view sidecar |

关键原则：

- 定量分析、指标计算或生成定量结论时必须消费 verified facts；纯格式调整、内容提取和批注不空跑事实抽取。
- 两个 Skill 都只写任务产物或经批准修改用户文件，最终关键数字必须能追溯到 fact id。
- OfficeCLI 固定项目验证版本，Mybot 负责准备二进制，用户无需单独安装；任务内 install/update 等动作仍受 Policy 管理。
- OfficeCLI 的 L1 读取/校验通常 allow，任务目录新产物的常规 DOM 操作可 allow；修改用户已有文件、raw XML、MCP/plugin/config/watch/install/update 根据具体参数进入 ask/deny。
- P2 为两个 Skill 分别声明 manifest；`officecli` 通过路径引用唯一 provider contract，不重复版本与 checksum。
- CI 不从 latest 动态安装；若分发二进制，必须同步维护 Apache-2.0 NOTICE 与第三方声明。

### 5.6 Eval Harness

否则 demo 能跑，但质量不可控。

要做：

- Skill 自带 smoke eval。
- 优先确定性检查，不依赖 LLM Judge。
- 轻量 LLM cassette 回归层（S5.0，`runtime/replay.py`）：provider 外包一层最小 record/replay，让关键 agent 行为在 CI / demo 中无 API key 可复现，而不是做完整 VCR 框架。
- `record` 模式写入规范化 request 摘要、response、工具调用/人工确认事件快照，剥离时间戳、随机 id、token usage 等易变字段。
- `replay` 模式按序返回 response，并弱校验请求哈希/关键字段，失配时提示“行为变化，需要重录或显式确认”。
- 覆盖范围限定为 3-4 个 smoke cassette：计划确认、pending approval、越权副作用防护、checkpoint 恢复。
- 不做多模型 cassette、自动重录、复杂流式 chunk 对齐、全量多轮对话回放；真模型 benchmark 仍手动跑。

测试结构三层化：

1. 纯脚本单测。
2. 轻量回放 agent smoke。
3. 真模型 benchmark（手动）。

Office eval 重点：

- 是否生成所有目标产物。
- Skill 自有中间产物是否符合各自 schema/契约。
- PPT 页数、标题、布局是否合格。
- docx/pptx 中数字是否来自 verified facts。
- 权限策略是否生效。
- 计划完成率 planned vs delivered。
- 错误输入是否能给出可恢复提示。

安全红队 case：

- 恶意会议纪要 / 表格单元格里藏注入指令。
- MCP 工具描述投毒样本。
- 不以“识别全部提示词注入”为目标；断言攻击未造成越权副作用、敏感信息泄漏或未确认外发，攻击尝试进 trace。
- 关键 smoke 样本经轻量 S5.0 回放进 CI；完整红队集保留为手动 / 阶段性 benchmark。

多模型回归矩阵：

- 同一 case set 在各模型预设上跑。
- 输出“模型 × 成功率 × 成本 × 时长”。
- 支撑模型选型与分级路由。

单/多 agent 对比：

- 同一任务集分别以单 Agent 顺序执行与含子 Agent 的执行方式跑。
- 量化并行收益与成本溢价。

S5.4 加分项：LLM Judge / LLM-as-a-Verifier 离线软评测：

- `LLM-as-a-Verifier` 不进入默认 Agent Runtime，只作为 `nanobot/runtime/evals/` 下的可选适配器和手动 benchmark 后端。
- 从 S5.1 JSONL trace 构造 `problem + trajectory`，先对 20-30 个自有 case 做离线 `compare` / `track` PoC；只有在已有安全隔离与无副作用候选轨迹时才试 `select`。
- 首期只评任务完成证据、恢复动作合理性、文案/受众匹配等 2-3 个软标准；数字一致性、权限、文件完整性、OpenXML 校验仍由确定性 metric 裁决。
- 记录与人工评价/确定性结果的相关性、确定性失败被误判为成功的 case、额外 token/成本和 P95 时延；以数据决定保留、调整或删除。
- verifier backend 必须显式配置为支持 token-level logprobs 的模型；不假设现有 DeepSeek/MiMo provider 可直接复用，也不把其 `.env`/环境变量约定渗入 Mybot 主配置。
- 暂不接在线 `ProgressTracker`、TurboAgent 代理或默认 Best-of-N 执行，避免每步额外调用和重复工具副作用。

LLM Judge / Verifier 只用于风格、可读性、轨迹质量等软指标，不能替代数据一致性测试，也不能推翻确定性失败。

### 5.7 WebUI 轻量产品化

WebUI 只补 demo 必需体验，不做复杂后台。2026-07-06 收缩：权限审批历史、artifact 血缘图、trace/eval 页投入产出比低，砍掉换主线时间。

要做：

- Chat 中显示当前模型、Skill、任务状态。
- 计划确认与高风险动作确认交互：ask 弹窗或文本确认，含拒绝原因。
- 生成产物列表 + 下载入口，复用 `file_preview.py`。

不做：

- trace/eval 页面：改走 OTLP 导出脚本 + Jaeger/Langfuse 查看，README 放截图。
- 血缘图前端：改用 CLI/JSON 输出 `lineage(artifact_id)`，demo 时终端展示。
- 审批历史页、Skill 权限摘要页：Settings 现有 Skill 列表微调即可。

设计取舍：不自研 dashboard，导出到标准生态，本身就是可讲的工程判断。

### 5.8 上下文工程与成本可观测

nanobot 已有 microcompact、snip_history、工具结果 offload，不重造机制，只做量化与利用。

要做：

- usage 进 trace，按模型单价折算每任务 token/成本，进指标基线。
- 大文件不进上下文：Excel 先经 `inspect_workbook.py` 变结构化摘要。
- KV cache 友好布局：稳定前缀（system prompt、skills 摘要）在前，易变内容后置或固定化。
- DeepSeek API 原生支持上下文缓存且 usage 返回 `prompt_cache_hit_tokens`，记录任务级命中率。
- 跑同一 eval set，对比布局调整前后的命中率与成本，以 eval 成功率不降为前提。
- 可选优化：模型分级路由，强模型做规划/写 DSL，便宜模型做抽取/judge，用 eval 证明质量不降、成本下降。

面试点：能报“单任务平均 X tokens、成本 Y 元、缓存命中率 Z%，经布局优化后成本降 W%”。优化的不只是 prompt 内容，而是 prompt 的缓存经济学。

### 5.9 计划契约（Plan Mode）

长任务可靠性不靠模型自觉，靠显式契约。

要做：

- 新增静态注册内建工具 `plan`，固定 action schema：`create/get/confirm/update_step/complete`。不按 Skill 动态增删工具，不把每个计划编进 system prompt。
- `create` 负责生成 `plan.json`、初始化步骤状态并计算仅覆盖不可变契约内容的 `plan_hash`；模型展示计划后等待用户确认。
- `confirm` 必须携带精确 plan hash；计划替换或修订后旧 hash 自动失效。P3 再把确认绑定到 `task_id + message_id + params_hash + expires_at`。
- `update_step` 负责状态更新和依赖检查，前置步骤未完成时不能把后续步骤置为 in_progress/done。
- `complete` 核对全部步骤为 done/skipped，且所有 expected_artifacts 位于任务目录并真实存在。
- plan 本身登记为 artifact，是后续所有产物的血缘源头。
- session metadata 只保存当前 plan 快照；每轮只把紧凑摘要附加到用户消息尾部 Runtime Context，避免破坏 system/tool 稳定前缀。
- 工具定义由 `ToolRegistry.get_definitions()` 按稳定名称排序并缓存，直到工具注册表实际变化才重建。
- 执行中每次工具调用和步骤变化进入 trace。
- 收尾对照计划核对承诺的产物是否都交付。
- eval 新增 `plan_completion` 指标。
- 与自修复循环衔接：validate 失败自动修复，连续 N 次（默认 2）失败则升级为 ask 人工介入，防止空转烧 token。

面试点：计划既是可校验数据结构，也是静态工具状态机；不仅防跑偏，还结合了 prompt-cache 友好布局。稳定定义在前、动态计划在尾，能讲清“功能设计为什么影响推理成本”。

### 5.10 受控 Subagent 编排

要做：

- 复用 nanobot 已有子代理工具；是否启动及如何拆分由父 Agent 根据用户提示、计划和当前状态决定。
- 每个父任务最多 5 个直接子 Agent，禁止子 Agent 再派生子 Agent。
- 治理必须贯通：
  - 子代理继承父任务权限约束，只能收紧不能放宽。
  - 每个子 Agent 有明确 token、时间与工具预算。
  - 子 Agent 上下文隔离，产物写入独立子目录；父 Agent 负责共享事实层与最终汇总。
  - trace 记父子 span 关联。
  - 子代理产物归同一 task 血缘，并保留 child id 和来源。
- 必须有单/多 agent 对比 eval：同一任务集量化成功率、时长、token 成本。
- 只要使用子 Agent，就必须记录父子 trace 并与单 Agent 顺序执行比较；对比结果必须如实报告，但不作为是否保留 P8 的前置条件。

面试点：多 Agent 的正确答案不是“我用了”，而是“我测过什么时候值得用”。重点是收益边界和治理贯通。

## 6. 不做什么

当前二开不做：

- 不新建独立 `mybot/` 包复制 nanobot。
- 不重写 `AgentLoop` / `AgentRunner`。
- 不恢复 Docker 作为主线部署。
- 不重新启用所有原 nanobot 通道。
- 不做远程 Skill Marketplace。
- 不做多租户企业权限后台。
- 不一开始就引入复杂工作流引擎。
- 不把 Office 自动化做成唯一产品方向。
- 不用 LLM Judge 替代确定性测试。
- 不做过度复杂的前端 Dashboard。
- 不自研 trace/eval 前端页面，trace 对齐 OTel 后导出 Jaeger/Langfuse 查看。
- 不允许 Subagent 嵌套或绕过父任务权限/预算；父任务直接子 Agent 上限为 5。
- 不堆概念名词；写进简历的每个亮点必须有代码承接点 + 指标支撑，面试追问追不穿。

## 7. 实施顺序、依赖与 cutline

### 7.1 技术依赖关系

```text
P0 -- P1(双 Office Skill) -- P2 -- P3(Policy+Approval+最小 File OCC) -- 轻量 S5.0 -- P4(Artifact+Resume) -- P8 -- P5 Core -- P6 -- P7
```

### 7.2 价值优先执行顺序

```text
P0 -> P1（双 Office Skill）-> P2 -> P3 -> 轻量 S5.0 -> P4 -> P8 -> P5 Core -> P6 Research -> P7
白盒记忆、S4.5、S5.4（Judge/Verifier）、S5.7/S5.8 为机动项
```

- P1 已完成双 Skill 拆分：共享 facts/constraints，但各自保留工作流和中间表示。
- P2 不可砍：两个 Skill 都要有 manifest、可用性和 `disabledSkills` 开关；OfficeCLI contract 保持单一真相源。
- 轻量 S5.0 建议紧随 P3：用 1 天内把 S1.9/S3.5 关键行为固化成 smoke 回归，后续每个阶段都受益。
- P4 聚焦不可变输入快照、artifact/lineage 与已确认计划任务的 checkpoint/resume；白盒记忆移出主线。
- P5 Core 先完成 trace、确定性 eval、视觉质量和红队；多模型、KV cache、Judge/Verifier 不阻塞主线。S5.4 Verifier 仅在 Core 完整且有 1-2 天机动时间时做离线 PoC。
- P8 在 P4 后进入主线：最多 5 个直接子 Agent、禁止嵌套、权限只紧不松、上下文/产物隔离、父子 trace 与预算必做。
- P6 Research 同时作为第二领域与 Subagent 编排的验证场景之一。
- 每完成一个阶段，更新 `docs/修改记录.md` 与本计划状态，并跑一次指标记录进 `metrics-baseline.md`。

### 7.3 秋招执行时间线

2026-07-06 按“尚未动工”现实重排；提前批 7-8 月投递，正式批 9 月。

- 7/7-7/18：P0 + P1；把现有 `office-automation` 恢复为 Python Skill，新建独立 `officecli` Skill，抽共享 facts/constraints，并保持 plan tool 闭环。
- 7/19-7/21：P2 manifest，完成双 Skill 声明、局部 fail closed、可用性与启用/禁用开关。
- 7/22-7/31：P3 权限内核 + 持久化 pending approval + 已有文件 hash 冲突硬拦截；随后收敛轻量 S5.0 cassette。
- 8/1-8/7：P4 输入快照、artifact 血缘与已确认计划任务 checkpoint/resume。
- 8/8-8/14：P8 Subagent 权限/预算/上下文/产物治理，最多 5 个直接子 Agent。
- 8/15-8/20：P5 Core + P6 Research，补父子 trace、硬门 eval、安全红队和第二领域验证。
- 8/24 feature freeze。
- P7 从 P1 起持续维护指标和证据位置；8/21-8/31 集中完成 README、架构图、完整治理 demo、答辩稿和修 bug。
- 机动：白盒记忆、S4.5、S5.4（含 LLM-as-a-Verifier 离线 PoC）、S5.7、S5.8。
- 机会项：深读上游源码期间若发现真 bug，修复后向 nanobot 上游提 PR；merged PR 是硬开源协作信号，遇到就做，不刻意找。

### 7.4 每周检查点与砍范围顺序

每周五检查：

> 本周产出能否写成一行新简历 bullet？

连续两周写不出，触发砍范围。砍序：

```text
白盒记忆 -> S4.5 -> S5.4 -> S5.7/S5.8
```

原则：不要均匀做完每件事的 60%。任何时候停下，已完成的行都必须是完整闭环。

### 7.5 底线 cutline

8 月底前必须完成：

- P1。
- P2。
- P3。
- P4。
- P8。
- 轻量 S5.0（3-4 个 cassette smoke）。
- P5 核心（S5.1-S5.3、S5.6）。
- P7。
- P6 Research 最小闭环。

强烈建议完成：

- S5.7 / S5.8。

加分项，可砍：

- 白盒记忆治理。
- S4.5。
- S5.4（LLM Judge / LLM-as-a-Verifier 离线软评测 PoC）。

## 8. P7 面试交付物要求

P7 的目标是把工程成果转成面试表达，让陌生面试官 10 分钟内看懂价值、能复现 demo。

### 8.1 一键 benchmark + 指标基线报告

要产出一条命令，跑全部确定性 eval + 红队 + 可选多模型矩阵，汇总输出双份报告：

- repo 内 `benchmarks/latest.md`：进 git，README 引用它。
- 本地 `docs/plans/metrics-baseline.md`：保留详尽历史趋势。

验收：

- 一条命令出完整报告。
- 至少含两个时间点的趋势对比。

### 8.2 README + 架构图 + 完整治理 demo 脚本

README 要包含：

- 定位。
- 分层架构图。
- quickstart。
- 指标表。
- 设计取舍 FAQ。

固定 demo 路径：

```text
选择/禁用 Skill -> 计划确认 -> 生成 -> pending approval/文件冲突 -> 输入快照与血缘 -> kill 恢复 -> Subagent 父子 trace -> eval
```

新增二开边界证据：

- 架构图把“二开新增 vs nanobot 原有”分色。
- 对上游 v0.2.1 基线给 `git diff --stat` 统计表。
- quickstart 提供轻量 cassette 的无 API key demo 路径。

验收：

- 按 README 从零跑通 demo，含无 key 路径。
- GIF / 截图齐全。

### 8.3 面试叙事与答辩稿

要准备：

- STAR 叙事。
- 30 秒 / 3 分钟摘要 / 完整技术演示三档讲法。
- 高频追问题库：
  - 为什么不用 LangChain？
  - 如何防注入？
  - eval 怎么设计？
  - 含 LLM 的系统怎么做确定性测试？
  - 基于开源二开你写了哪部分？
  - checkpoint 一致性怎么保证？
  - 用户或 IDE 并发修改文件时怎么避免覆盖？
  - 上下文管理怎么做？
  - 多 agent 何时该用？

验收：

- 对照亮点地图逐条有话可讲、有据可查。

## 9. 最终验收标准

方案做成后，Mybot 应该证明四件事：

1. **能扩展**：通过 Skill Pack 加载领域能力，而不是改核心循环；受控多 Agent 编排复用同一套治理设施。
2. **能治理**：工具、文件、MCP 与子 Agent 都受同一权限、预算、审计和可恢复审批约束；不可信内容不能造成越权副作用。
3. **能交付**：两个独立 Office Skill 都能在共享事实层上生成或修改 Office 产物，输入与质量全程可追踪，已确认计划任务可断点恢复。
4. **能证明**：每条能力都有硬门指标和可复现 demo，一键 benchmark + 完整治理演示 + 答辩稿；关键 cassette smoke 无 API key 也能跑。

一句话总结：

> Mybot 二开的重点不是“再造一个 Agent 框架”，而是把现有 nanobot 打磨成一个可插 Skill、可管工具、可追产物、可测质量、可讲数据的个人 Agent Runtime——每个能力都对着一个真实失败模式，每个亮点都有指标和 demo 兜底。

## 10. 备注

- 简历措辞原则：只写已完成且有指标支撑的能力；未完成阶段不进简历。
- 任何阶段都可独立喊停或调整，但主线必须优先保证完整闭环。
- 本整合版用于统一项目定位、阶段优先级和高层方案，避免总方案与增量计划分散阅读。
