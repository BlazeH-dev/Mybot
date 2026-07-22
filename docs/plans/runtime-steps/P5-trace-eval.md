# P5 Trace、Eval、可观测与公开测评

> 状态：S5.0 与 P5 Core 已完成（2026-07-18）；P5.1 可观测与公开 benchmark 扩展待实施。
> Langfuse 部署固定采用日本区 Langfuse Cloud（东京，`https://jp.cloud.langfuse.com`）；不实施本地自托管，仍保留可选 Python SDK sink 和本地 JSONL fallback。

## 1. 证据分层

P5 不把所有结果压成一个平均分，固定为三层：

1. **确定性硬门**：artifact、文件可打开、OpenXML、数字/fact、Policy、OCC、HITL、恢复、未批准副作用和安全红队。
2. **LLM 语义判断**：OCB 原子 assertion、OfficeBench 内容正确性、PresentBench 内容/视觉 rubric。被测 Agent 使用 `gpt-5-6-luna`，日常 Judge 使用独立的 `gpt-5-6-sol`；严格复现上游 Judge 时单独标注 `official-comparable`。
3. **人工审计**：100% 审计安全异常、敏感泄漏、未批准副作用和确定性/Judge 冲突；首次基线和发布版本对正常结果做约 5% 分层抽样。人工审计不是业务指标，也不替代运行时 approval。

确定性硬失败不能被 LLM 或人工软分覆盖。Judge 结果必须记录模型、prompt digest、版本、成本和评分理由。

## 2. S5.0 Cassette（已完成）

`runtime/replay.py` 包装 `LLMProvider`，record/replay 规范化 request、response、tool calls 和 InteractionRequest/control event，剥离时间戳、随机 id 和 usage 等易变字段，并对 hash 失配输出可读 diff。

保留 3–4 条高价值路径：plan-only/automatic plan、三档 HITL deadline、approval/注入/文件冲突和 checkpoint 恢复。Cassette 只验证 AgentLoop、工具协议、Policy、交互和恢复回归，不代表模型质量。

## 3. P5 Core Trace（已完成）

现有 `TraceHook` 继续是 Mybot 事件的事实源，记录：

- trace/span/parent、task、actor、状态、耗时、错误；
- model、usage、缓存 token；
- plan、tool、policy、InteractionRequest、approval；
- input/artifact/checkpoint、file conflict、Subagent 父子关系和循环熔断。

默认只写输入、输出和 tool 参数的 hash/长度摘要，等待时间与模型/工具执行时间分开。现有 JSONL 和 OTLP-shaped 导出继续服务无 Key 回归与本地报告。

## 4. P5.1 Langfuse Cloud 接入（待实施）

### 责任边界与目标结构

- 不安装 Langfuse Skill；Skill 是模型操作说明，不是 telemetry 组件。
- Mybot 继续拥有采集点、事件语义、trace/span/parent id、脱敏、Runtime 状态和评估结论；Langfuse SDK只负责把已经结构化并脱敏的数据发送到 Cloud，以及 SDK 已提供的 observation 生命周期、批处理、flush 和传输能力。
- 不用前端 JS/TS SDK发送主 Trace，不把 secret key 暴露给浏览器；只有未来确有前端行为调试需求时才单独评估匿名前端事件。
- 不依赖仅包装 OpenAI-compatible client 的隐式自动追踪，否则只能看到 LLM，无法表达 Policy/Interaction/artifact/checkpoint/child，且容易与手工 generation 重复上报。
- Langfuse 是后端遥测出口，不注册成 Agent tool，不接受模型、Skill 或用户消息在运行时改写 endpoint；生产目标固定为 `jp.cloud.langfuse.com:443`。

目标结构：

```text
AgentLoop / AgentRunner / Provider / Tool / Runtime
                       ↓
              typed TraceEvent
                       ↓
        normalize + redact（只执行一次）
                       ↓
              CompositeTraceSink
               ↙               ↘
      JsonlTraceSink       LangfuseTraceSink
      本地事实与兜底        官方 Python SDK 薄适配
```

P5.1 将当前 `TraceHook` 中“创建事件”和 `_append()` 写文件解耦：

- 新增不可变、带 `schema_version` 的 `TraceEvent`，至少包含 event id、trace/span/parent、task/session/actor、kind/name、start/end、status/error、model/usage 和脱敏 attributes。
- `TraceHook`、`emit_trace_event()` 和各 Runtime 埋点只创建 Mybot 事件，不直接调用 Langfuse；`TraceContext` 继续传播父子关系。
- `TraceSink` 只接收已经规范化、脱敏的事件；`JsonlTraceSink` 承接现有 `_append()` 行为，`CompositeTraceSink` 隔离各 sink 错误并保证 Langfuse 失败不影响本地写入。
- `LangfuseTraceSink` 只做 Mybot 事件到 SDK observation/score 的映射，不自研 HTTP client、鉴权协议或第二套通用 trace schema。现有最小 OTLP JSON 导出保留为兼容能力，不作为 Langfuse Cloud 主路径。

### Observation 粒度与映射

不能只把整个任务作为一条大 Trace 上传。P5.1 必须补到可计算单次 LLM/tool 指标的边界：

| Mybot 语义 | Langfuse 表达 | 采集要求 |
| --- | --- | --- |
| task / chat session | trace / session | 绑定 task id、session key、模型 preset、Skill digest 和 benchmark run id |
| main/child Agent run | agent observation；SDK 版本不支持时用带 `mybot.kind=agent` 的 span | start/end、父子关系、stop reason、总步骤和错误 |
| 每次 LLM provider 调用 | generation | model、参数、首 token/总延迟、usage/cache token、成本和错误 |
| 每次 tool 调用 | tool observation；不支持时用带 `mybot.kind=tool` 的 span | tool call id、参数摘要、start/end、延迟、重试、结果摘要和错误 |
| Policy / approval | guardrail 或 event/span | decision、匹配规则、风险级别和绑定摘要，不上传敏感参数 |
| InteractionRequest | span + requested/resumed event | strategy、状态和 human wait，等待时不伪装成模型延迟 |
| artifact / checkpoint / recovery | event；需要持续时间时用 span | id、hash、验证状态、恢复语义，不上传原文件 |
| semantic Judge | evaluator/generation observation + score | Judge 模型、prompt digest、理由和成本 |
| deterministic/human eval | score | 保留 hard gate、reviewer 和证据来源；只镜像本地结论 |

具体 observation 类型以实施时锁定的 Langfuse Python SDK 版本为准；没有对应原生类型时统一退化为 span/event + `mybot.kind`，不能为追随 SDK 名称而删除 Mybot 语义。现有 `after_iteration` 聚合事件继续用于摘要，但必须在 Provider 调用和 Runner 工具执行边界增加 start/end 埋点，不能从聚合后的 JSONL 反推实时 generation/tool latency。

### 配置与密钥

实现后配置形状为：

```json
{
  "observability": {
    "localJsonlEnabled": true,
    "langfuse": {
      "enabled": false,
      "baseUrl": "https://jp.cloud.langfuse.com",
      "publicKey": "pk-lf-...",
      "secretKey": "sk-lf-...",
      "captureContent": false
    }
  }
}
```

仓库默认 `enabled:false`，CI 不需要 Langfuse 凭据；个人开发环境在日本区创建账号和 Mybot 项目后，从 Project Settings 创建项目级 public/secret key，再在本机配置中启用。密钥可兼容 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` 覆盖，但不能进入仓库、日志、WebUI 或 trace。

必须直接从 [日本区入口](https://jp.cloud.langfuse.com) 注册，不能误用默认 `https://cloud.langfuse.com`（欧盟区）；区域和数据隔离口径以 [Langfuse 官方区域说明](https://langfuse.com/security/data-regions) 为准。Langfuse 各区域的账号、项目、API Key 和数据完全隔离；以后换区需要新建账号并迁移数据，P5 不设计自动跨区切换。采用 Cloud 后不提供本地 Compose，也不要求下载 Redis、MinIO、ClickHouse 或 PostgreSQL。

### 运行与故障降级

- 仓库默认和 CI 只写本地脱敏 JSONL；个人环境配置日本区凭据后同时写 JSONL 与 Langfuse，Langfuse 关闭不影响任务和确定性 eval。
- 优先复用锁定版本 SDK 已提供的后台批处理、flush 和传输重试，不在 `LangfuseTraceSink` 外再包一套队列/HTTP 重试。网关 shutdown 在有限超时内调用 SDK flush/shutdown；超时、网络或服务失败只记录本地 exporter error/drop，不阻塞或回滚 Agent。
- 增加 `nanobot observability sync`，可将已保存 JSONL/benchmark run 在之后同步到 Langfuse。
- 普通任务默认不上传正文、原始 Office 文件、完整 artifact、密钥或个人信息；只发送脱敏 metadata、hash、长度、状态和指标。公开 benchmark 只在受控开关和许可证允许范围内记录 case 内容，否则只记录 case id、revision 和结果。
- 日本区仍属于跨境数据传输；当前只用于个人开发和公开 benchmark。接入真实公司材料、客户数据、个人信息或敏感文档前，必须单独完成合规审查或保持远程 sink 关闭，计划不声称满足中国数据出境要求。
- Mybot WebUI负责实时任务步骤、artifact 和评估摘要；Langfuse UI负责 Trace、Sessions、Datasets、Experiments、Scores 和失败下钻，不作为 Runtime 状态真相源。WebUI 的下钻链接必须从已配置的日本区 `baseUrl` 和返回 id 生成，不能硬编码欧盟区域。

### 只记录技术指标

不采集业务对话量、用户满意度、CPU、内存和 GPU 作为本阶段目标。必须记录：

- LLM 成功率、错误类型、首 token/总延迟、P50/P95、输入/输出/cache token、成本；
- tool 成功率、错误类型、调用延迟和重试；
- Agent 循环步数、任务 wall time、人工等待时长、恢复次数、artifact 验证状态；
- benchmark hard/semantic/human score 及 evaluator/judge metadata。

## 5. 确定性 Eval 与报告（已完成基础，扩展待实施）

继续使用注册式 `score(case_ctx) -> {passed, score, issues}`，现有 12 个 metric 保持不变。P5.1 增加：

- `semantic_judge`：只评内容、覆盖、风格和视觉 rubric；不能覆盖 hard gate。
- `human_audit`：记录审计范围、结论、理由和 reviewer，不把人工结果混入确定性 pass/fail。
- `skill_coverage`：区分 `supported/unsupported/passed/failed`，避免把 OfficePython 不支持的任务误记成模型失败。

确定性检查、Judge 编排和人工审计先在 Mybot 中产生带版本的 `EvalResult`，本地报告仍是发布与 CI 的事实源；Langfuse 只接收 dataset/run metadata、observation 和 score 镜像。远程 score 丢失、延迟或被删除都不能改变本地 hard gate 结论。

## 6. 公开 Benchmark 选择

首批只接入三个公开集：

| Benchmark | 主要维度 | 运行口径 |
| --- | --- | --- |
| [OCB](https://github.com/microsoft/OfficeComprehensionBench) | DOCX/XLSX/PPTX 理解和问答 | 公开 revision 全量；当前公开 release 约 1,018 queries |
| [OfficeBench](https://github.com/zlwang-cs/OfficeBench) Office subset | Word/Excel 创建、读取、编辑和跨文件操作 | 只选 Word/Excel 单应用及 Word+Excel 任务，排除邮件/日历/OCR/Web；结果标记 `OfficeBench-Office/Mybot adapter` |
| [PresentBench](https://github.com/PresentBench/PresentBench) | PPT 创建、材料忠实度、内容完整性和视觉质量 | smoke 后手动 full；使用官方 rubric，非官方 Judge 时标记 compatible |

`SpreadsheetBench Verified` 暂不进入必做主线，作为高级 Excel 选做 benchmark；`PPTArena`、OSWorld、TUA-Bench 和 AGPL `docx-benchmark` 不进入首批。

所有数据和第三方 evaluator 在外部缓存中按 revision/SHA/license 固定，不把原始数据提交到 MIT 项目。报告不合成一个 Office 总分，按 benchmark、文件格式和 Skill 分开发布。

## 7. OfficePython 与 OfficeCLI 公平比较

P1.1 将现有 `office-automation` 改名为展示名 `OfficePython`、id `office-python`，扩展为基于 `python-docx/openpyxl/python-pptx` 的通用 `inspect/query/create/apply/validate/render` 基线；当前代码仍保持原 id，迁移完成前不能在报告中写新名称已实现。

每个实验固定：同一输入 snapshot、`gpt-5-6-luna`、同一 Policy/约束/evaluator、干净 workspace，一次只启用一个 Skill。报告分为：

- capability coverage：supported/unsupported/passed/failed；
- common-task quality：hard gate、Judge、人审、Token、LLM/tool latency、tool success 和 Agent steps。

OfficeCLI 的统一跨格式接口、query/DOM/batch/view/validate/raw 和复杂文件覆盖应由真实结果体现，不得通过 prompt 或评分规则人为偏袒。

## 8. P5.1 验证与出口

实施顺序固定为：

1. 引入 `TraceEvent`、`TraceSink`、`JsonlTraceSink` 和 `CompositeTraceSink`，先保证现有 JSONL/schema/test 行为等价。
2. 在 LLM Provider 与 Runner 工具执行边界增加逐调用 start/end 事件，保留当前 Agent/iteration 摘要。
3. 锁定官方 Langfuse Python SDK 版本，实现薄 `LangfuseTraceSink`、配置、生命周期和日本区 endpoint。
4. 增加 Eval score 镜像、`observability sync` 和 Mybot WebUI deep link。
5. 完成 mock contract、断网降级和专用日本区项目 Cloud smoke，再运行三套 benchmark smoke。

验证要求：

- 单测验证事件 schema、sink 选择、脱敏在 fan-out 前完成、密钥不泄漏、一个 sink 失败不影响另一个、有限超时 flush、重复 generation 防止和 score schema。
- 使用 mock Langfuse client 验证每次 LLM 调用恰好一个 generation、每次 tool call 恰好一个 tool observation、Agent/child 父子关系、Policy/Interaction/artifact/checkpoint 映射，以及 error/cancel/timeout 都能闭合 observation。
- 验证默认 Cloud endpoint 为 `https://jp.cloud.langfuse.com`、运行时输入不能改写 endpoint；SDK 缺失、未配置 Key、Cloud 断网和限流时任务、本地 JSONL、eval/report 均正常。
- 同一任务的本地 trace id、Langfuse trace/session 和 benchmark run id 可关联；历史同步幂等，重复执行不会产生无法区分的重复 observation/score。
- 提供显式的手动 Cloud smoke：使用专用测试项目验证 trace、session、experiment、score 和 WebUI deep link；测试结束前 flush，凭据只来自本机配置或环境变量。
- 每个 benchmark adapter 验证 revision/SHA 漂移、隔离 workspace、官方 evaluator 调用、结果标签和 unsupported 统计。
- CI 继续只运行 deterministic/cassette/adapter contract，不启动真实 Langfuse、不消耗 Judge API。
- P5 Core 的既有出口保持已完成；P5.1 的日本区 Langfuse sink/历史同步/Cloud smoke、三套 benchmark smoke、Sol Judge 和人工审计基线完成后，才可把 P7 公开结果写入 README。
