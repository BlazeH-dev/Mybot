# P5 Trace、Eval、可观测与公开测评

> 状态：S5.0 与 P5 Core 已完成（2026-07-18）；P5.1 Langfuse 可观测与公开 benchmark 扩展待实施。
> Langfuse 部署固定采用日本区 Langfuse Cloud（东京，`https://jp.cloud.langfuse.com`），不实施本地自托管。P5.1 以 Langfuse Python SDK 为观测与评估主干；Mybot 不再建设平行的 Trace、Experiment、Score、Judge 或人工审核系统。

## 1. 最终职责边界

P5.1 只保留四类本地责任：

1. 在 AgentLoop、AgentRunner、Provider、Tool 和 Runtime 状态转换处产生 Mybot 领域语义。
2. 执行 Mybot Agent、Office Skill、LibreOffice、OpenXML 和第三方官方 evaluator 等必须访问本地进程或文件的代码。
3. 准备并校验公开 benchmark 资产、依赖、revision、license 和运行前成本。
4. 运行无 Key 的 deterministic/cassette/Policy/OCC/HITL/恢复硬门；这些是代码正确性测试，不是第二套线上评估平台。

其余能力优先交给 Langfuse：

- Trace、Session、Observation、Token、成本、延迟、查询、下钻、Dashboard、Monitor；
- Dataset、Dataset Run、Experiment、并发、错误隔离、item/run evaluator 编排和跨 run 比较；
- LLM-as-a-Judge、轻量 Code Evaluator、Scores、Score Analytics；
- Annotation Queue、评论、纠正输出和人工审核状态；
- 需要真模型的 CI/CD experiment 与 regression gate。

唯一真相源按领域划分：Runtime 状态在 Mybot，原始 Office 文件和受许可证约束的资产在本地/外部缓存，观测与评估记录在 Langfuse，代码硬门在 CI。README 中的 benchmark 数字只是从 Langfuse Dataset Run 导出的发布快照，不是另一套评估数据库。

实施时以 Langfuse 官方的 [Evaluation Overview](https://langfuse.com/docs/evaluation/overview)、[Experiments via SDK](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)、[LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge)、[Code Evaluators](https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators)、[Annotation Queues](https://langfuse.com/docs/evaluation/evaluation-methods/annotation-queues) 和 [Experiments in CI/CD](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd) 为能力边界。锁定 SDK 后若实际 API 变化，先更新本计划再实现。

## 2. 固定 benchmark 评分来源

1. **OfficeBench Office subset**：在 Langfuse Experiment Runner 进程中调用固定 revision 的官方确定性 evaluator，返回 SDK `Evaluation` 并在 Langfuse 记录 `official_score`。Adapter 只准备官方脚本要求的目录和文件，不修改 evaluation function 或 pass/fail 规则。
2. **OCB**：把公开 reference answer、atomic assertion 和评分规则放入 Langfuse Dataset/Custom LLM-as-a-Judge；通过 Langfuse LLM Connection 连接 OpenAI-compatible 的 `gpt-5-6-terra`，记录 `mybot_score` 和 Judge reasoning，标记 `OCB/Mybot evaluation`。
3. **PresentBench**：把公开 task、material、checklist、rubric 和获许可的渲染媒体放入 Langfuse Dataset，由 Custom LLM-as-a-Judge 使用 `gpt-5-6-terra` 评分内容与视觉维度。实施 spike 必须先验证媒体能进入 Judge；若锁定版本不支持，只有视觉维度退化为 Experiment Runner 中的本地 SDK evaluator，Score 仍只写 Langfuse。
4. **人工审计**：使用 Langfuse Annotation Queue 记录 Score、评论和 reviewer；不再同步成本地 `human_audit` 数据库。

OCB/PresentBench 复用公开数据、gold、checklist、评分维度和聚合口径，但更换了 Judge 模型与执行平台，不能标记 `official-comparable` 或进入官方榜单。P5 Core 的 cassette、Runtime metric、Policy/OCC/HITL、OpenXML 和红队仍是独立代码回归证据，不重复进入 benchmark 总分。三套 benchmark 分别展示，不合成 Office 总分。

结果标识固定为：

- OfficeBench：`evaluation_source=officebench_official`、`official_score`；
- OCB/PresentBench：`evaluation_source=langfuse_terra`、`mybot_score`、Langfuse evaluator config/version 和 LLM Connection model；
- 人工审核：Langfuse Score Config、queue、reviewer 和 comment；
- 被测 Agent：固定 `gpt-5-6-luna`，两个 Skill 使用相同 Dataset、Policy 和约束。

## 3. 已完成的本地确定性基础

### S5.0 Cassette

`runtime/replay.py` 包装 `LLMProvider`，record/replay 规范化 request、response、tool calls 和 InteractionRequest/control event，剥离时间戳、随机 id 和 usage 等易变字段，并对 hash 失配输出可读 diff。

Cassette 只验证 AgentLoop、工具协议、Policy、交互和恢复回归，不代表真模型质量，也不上传 Langfuse。

### P5 Core Trace

现有 `TraceHook` 是当前 Mybot trace 语义入口，记录 task/actor、model/usage、plan/tool/policy、InteractionRequest、artifact/checkpoint、Subagent 父子关系和错误。当前 JSONL/OTLP-shaped 输出属于已完成 Core 的实现事实。

P5.1 接入后，启用 Cloud 时生产观测迁移到 Langfuse SDK，测试逐步替换为 OTel `InMemorySpanExporter` 或临时测试 fixture。`observability.langfuse.enabled=false`（默认）时保留现有 JSONL TraceHook 原样运行，作为本地调试与离线证据路径——否则默认配置下将没有任何持久观测；启用 Langfuse 后停写 JSONL，二者互斥切换而非双写。不新增 `LocalJsonlSpanExporter`、历史重放、查询或同步能力；现有 JSONL 代码的删除推迟到 Langfuse 稳定运行后作为独立清理项。

## 4. P5.1a Langfuse 可观测接入

### 目标结构

```text
AgentLoop / AgentRunner / Provider / Tool / Runtime
                       ↓
       Mybot semantic instrumentation
    allowlist + Langfuse SDK masking hook
                       ↓
      Langfuse Python SDK / OpenTelemetry
                       ↓
       batch / retry / flush / ingestion
                       ↓
             Langfuse Japan Cloud
 Trace / Session / Cost / Dashboard / Monitor
```

- 不安装 Langfuse Skill，不注册 Agent tool，不使用前端 SDK 上报主 Trace。
- 不新建 `TraceEvent/TraceSink/CompositeTraceSink`、本地 span exporter、HTTP 客户端、发送队列或通用重试。
- `TraceHook` 仅保留为 Mybot 语义埋点外观，内部直接调用锁定版本 SDK 的 observation API。
- Mybot 用字段 allowlist 决定哪些语义可离开进程；具体属性删除/替换复用 SDK `mask_otel_spans`，不再维护第二套通用脱敏流水线。
- SDK 负责 OTel 上下文、observation 生命周期、batch、retry、flush/shutdown 和 Cloud 传输。

### Observation 映射

| Mybot 语义 | Langfuse 表达 | 必须记录 | 创建位置 |
| --- | --- | --- | --- |
| task / chat | root observation + session | task/session、model preset、Skill digest、release/version | AgentLoop.handle() |
| main/child Agent | agent observation | parent、stop reason、步骤、错误 | TraceHook.before_run/after_run |
| LLM provider 调用 | generation | model、参数、TTFT/总延迟、usage/cache token、cost、error | **runner._request_model() 或 langfuse.openai drop-in** |
| tool 调用 | tool observation | tool call id、参数摘要、延迟、重试、结果摘要、error | **runner._run_tool()** |
| Policy / approval | guardrail 或 event | decision、rule、risk、绑定摘要 | emit_trace_event() 改为 Langfuse API |
| InteractionRequest | span + event | strategy、状态、human wait | emit_trace_event() |
| artifact/checkpoint/recovery | event/span | id、hash、验证状态、恢复语义，不上传原文件 | emit_trace_event() |
| benchmark item | experiment trace | dataset item、Skill、fingerprint、artifact id/hash | run_experiment() 自动 |

具体 observation 类型以锁定 SDK 为准；无原生类型时使用 span/event + `mybot.kind`，不自建类型系统。

**关键改动**：
- Provider 调用在 `runner._request_model()` 内逐调用创建 generation observation，记录 start_time/TTFT/latency/usage。如果启用 `langfuse.openai` drop-in（从 `config.observability.langfuse` 设置环境变量后导入），则 generation 由 drop-in 自动创建，runner 不重复创建。
- Tool 调用在 `runner._run_tool()` 内逐调用创建 tool observation，记录 tool_call_id/arguments 摘要/latency/result 摘要/error。
- `after_iteration` 只做轻量汇总，不再作为 observation 创建点。
- 使用 `propagate_attributes()` 将 task_id/session_id/plan_hash/sandbox_mode 等传播到所有子 observation。

### 配置、安全与故障语义

```json
{
  "observability": {
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

- SDK 随 Mybot 默认安装并锁定版本（`langfuse>=4.14.0`）；配置兼容 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`，密钥不得进入仓库、WebUI、日志或 Trace。
- 普通任务默认 `enabled:false`，此时保留现有 JSONL TraceHook 作为本地调试与离线证据路径。启用后 Langfuse 是唯一持久观测后端，停写 JSONL；Cloud 不可用时 Runtime 继续执行，但允许遥测丢失，不承诺本地无损补传。
- `openai_compat_provider._ensure_client()` 在启用 Langfuse 时，从 config 读取密钥并设置环境变量 `LANGFUSE_SECRET_KEY`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_BASE_URL`，然后导入 `from langfuse.openai import AsyncOpenAI`，让 drop-in 自动追踪所有 OpenAI-compatible provider（OpenAI/DeepSeek/GPT-5.6 中转）的 LLM 调用。检测到 drop-in 生效时，runner 不再手动创建 generation observation。
- `office-smoke/release` 必须通过日本区写入、flush、API 回读和 deep link smoke；失败则 experiment profile 失败。
- 普通任务默认只上传 metadata、hash、长度、状态和指标。公开 benchmark 内容仅在许可证允许时上传；公司、客户、个人或敏感数据在合规审查前保持关闭。
- 日本区属于跨境数据传输；计划不声称满足中国数据出境要求。
- Mybot WebUI 不新增 Trace、成本、Dashboard 或 benchmark 面板，只提供 Langfuse deep link。Runtime 的 approval/checkpoint/artifact 当前状态仍由 Mybot UI 展示。

## 5. P5.1b Langfuse Evaluation 主流程

### 在线评估

- JSON Schema、正则、长度、字段完整性等两秒内、无第三方包、无网络的检查使用 Langfuse Code Evaluator。
- 线上 final response 的正确性、相关性、安全性等使用 Langfuse LLM-as-a-Judge，按 observation metadata/filter 和 sampling 执行。
- 失败分类、质量趋势、成本/质量关联使用 Scores、Score Analytics、Custom Dashboards 和 Monitors。
- 人工复核、评论和 corrected output 使用 Annotation Queue。
- Policy 是否允许执行、HITL 是否到期、OCC 是否冲突仍是 Runtime 行为，不交给异步 evaluator 决策。

### 离线 Experiment

使用 Langfuse Python SDK `run_experiment()`/Dataset `run_experiment()` 作为唯一真模型 experiment runner：

- Langfuse Dataset 保存 input、expected output、rubric、case metadata 和允许上传的 media；
- Experiment Runner 负责并发、自动 Trace、错误隔离、item/run evaluators、Dataset Run 和比较；
- Mybot 提供 task callback，实际执行 Luna Agent、OfficeCLI/OfficePython 和本地文件操作；
- OfficeBench 官方 evaluator、OpenXML/渲染/文件检查作为本地 SDK evaluator function 运行并返回 `Evaluation`，不落入第二套 `EvalResult` 数据库；
- OCB/PresentBench 使用 Langfuse Custom LLM-as-a-Judge；只有已验证的平台缺口才允许单一维度退化为本地 SDK evaluator；
- item/run Score、Judge reasoning、人工 Score 和 experiment metadata 只以 Langfuse 为评估真相源。

本地 deterministic/cassette/安全硬门继续用 pytest/现有 eval 执行，因为它们必须无 Key、无网络并在代码接入 Langfuse 前发现回归；不把这套代码扩展成线上评估、Experiment 或报告平台。

## 6. Benchmark 入口、数据与发布

### 收缩后的 CLI

```bash
nanobot benchmark prepare --profile office-smoke
nanobot benchmark prepare --profile office-release
nanobot benchmark estimate --profile office-release --model-preset gpt-5-6-luna
nanobot benchmark run --profile ci
nanobot benchmark run --profile office-smoke --model-preset gpt-5-6-luna
nanobot benchmark run --profile office-release --model-preset gpt-5-6-luna --presentbench-sample 50 --confirm-cost
nanobot benchmark export --dataset-run <langfuse-dataset-run-id>
```

- `ci` 只运行 deterministic/cassette/adapter contract，无 API Key、无网络、不开 Langfuse。
- `prepare` 只做本地资产/依赖/license 校验，并将允许上传的 case、expected output、rubric 和 media 写入 Langfuse Dataset。
- `estimate` 只做调用前预算和用户确认；实际 token/cost 由 Langfuse generation 与 Dashboard 统计。
- `run` 是 `run_experiment()` 的薄封装，不实现并发器、Dataset Run、Score 存储、状态机或跨 run 分析。
- `export` 从一个已完成且审计齐全的 Langfuse Dataset Run/API 读取结果，生成去敏 JSON/Markdown 和 README 受控区块；它不修改 Langfuse Score，也不建立本地可编辑真相源。
- 删除 `benchmark status/resume/audit-sync/publish`、单 active run 锁和 Mybot benchmark WebUI。进度、错误 case、Score 和审核状态直接查看 Langfuse。

Experiment Runner 当前不承诺断点续跑。首版依赖其错误隔离；失败 item 使用新的 retry Dataset Run，并在 metadata 记录 `parent_run_id` 和筛选条件，不自建 case checkpoint。只有真实成本数据证明整次/子集重跑不可接受时，才单独立项最小恢复层。

### 数据与环境

- 三套 adapter 共用独立 benchmark venv；只安装 task callback 和本地 SDK evaluator 实际需要的精确 constraints。
- OfficeBench 官方 evaluator 所需旧依赖留在该 venv；OCB/PresentBench Judge 已由 Langfuse LLM Connection 执行，不在 Mybot 安装 Azure/Gemini/Anthropic Judge SDK。
- OCB revision 固定为 `f5b560356c8c5fff78569307d655f76d9ea9f6f7`，OfficeBench 为 `b978b808667c32b52ce19a67ce1def1de9ae02b7`，PresentBench 为 `2f01aaf2957004f4f136796147e11f7e52d84684`。
- 每个上游 revision 使用新的不可变 Dataset 名称或固定 Dataset version；不得在同名趋势序列中静默改 case/rubric。Dataset metadata 记录 upstream SHA、license、adapter/constraints、Python、LibreOffice 和 evaluator config。
- 不能上传的原始 Office 文件保存在外部缓存，Dataset item 只存 URI/id、checksum 和 revision；获许可且 SDK 支持的渲染图使用 Langfuse media。
- Git 只提交从已审核 Dataset Run 导出的结果快照和最小环境说明，不提交原始数据、完整 Trace 或大文件。

### profile 与审核

- `office-smoke`：OCB、OfficeBench Office subset、PresentBench 各 4 个固定分层 case，两个 Skill 同题；12 个 experiment traces 全部加入 Annotation Queue。
- `office-release`：OCB 全量、固定 OfficeBench Office subset；PresentBench 在 full/50%/25% 三档成本估算后确认。换档使用不同 Dataset/series，不混合分母。
- release 顺序固定为 `prepare -> estimate -> run_experiment -> Annotation Queue -> export`；run 阶段禁止下载或修改依赖。
- release 约 5% 分层抽样，并额外审核高风险、Judge 异常和视觉 `unscored`；审核完成状态直接从 Annotation Queue/API 判断，不回写本地。
- Score 下降由 Langfuse Dashboard/CI regression gate 展示或阻断；环境、Cloud smoke、运行完整性和必需审核缺失是 export 硬失败。

## 7. 公开 Benchmark 选择

| Benchmark | 主要维度 | 固定口径 |
| --- | --- | --- |
| [OCB](https://github.com/microsoft/OfficeComprehensionBench) | DOCX/XLSX/PPTX 理解和问答 | Langfuse Dataset + Terra Custom LLM-as-a-Judge；标记 `OCB/Mybot evaluation` |
| [OfficeBench](https://github.com/zlwang-cs/OfficeBench) Office subset | Word/Excel 创建、读取、编辑和跨文件操作 | Experiment Runner 中原样调用官方确定性 evaluator，记录 `official_score` |
| [PresentBench](https://github.com/PresentBench/PresentBench) | PPT 创建、材料忠实度、内容完整性和视觉质量 | Langfuse Dataset/media + Terra Custom LLM-as-a-Judge；视觉能力不足时仅该维度用本地 SDK evaluator |

`SpreadsheetBench Verified` 暂列高级 Excel 选做项。三套结果按 benchmark、格式和 Skill 分开展示，不合成 Office 总分。

## 8. OfficePython 与 OfficeCLI 公平比较

P1.1 已完成；每个 Langfuse Experiment 固定同一 Dataset、Luna、Policy、约束和 evaluator config，一次只启用一个 Skill。使用 Dataset Run comparison、Score Analytics 和 Dashboard 比较：

- capability coverage：`supported/unsupported/passed/failed`；
- common-task quality：OfficeBench official 或 OCB/PresentBench Mybot score、人工 Score、Token/cost、LLM/tool latency、tool success 和 Agent steps。

OfficePython 是公平 Python baseline，不得通过 prompt、Dataset 或 evaluator 配置偏袒 OfficeCLI。

## 9. 实施顺序与出口

### P5.1a 实施步骤（按风险与依赖排序）

| 阶段 | 步骤 | 产出 | 验证 |
|------|------|------|------|
| **S0 准备** | 1. Config schema 增加 `observability.langfuse.*` (enabled/baseUrl/publicKey/secretKey/captureContent)<br>2. `pyproject.toml` 添加 `langfuse>=4.14.0` 并锁定版本<br>3. 编写 spike：验证 async/context propagation、`propagate_attributes()`、`mask_otel_spans`、flush/shutdown | config/schema.py、pyproject.toml、spike 脚本 | spike 通过，日本区 Cloud 能写入/flush/回读/deep link |
| **S1 Provider drop-in** | 4. `openai_compat_provider._ensure_client()` 增加 `_should_use_langfuse()` 判断<br>5. 启用时从 config 读取密钥设置环境变量，导入 `langfuse.openai.AsyncOpenAI`<br>6. 验证 drop-in 自动追踪 LLM 调用 | Provider 自动创建 generation observation | 本地测试：启用 Langfuse 后，Cloud 能看到 OpenAI/DeepSeek/GPT-5.6 的 generation span（含 model/usage/latency） |
| **S2 TraceHook 双模式** | 7. 新建 `LangfuseTraceHook(AgentHook)`，实现 before_run（创建 agent observation + propagate_attributes）、after_iteration（轻量汇总）、after_run（更新 agent observation）<br>8. `AgentLoop.handle()` 根据 `config.observability.langfuse.enabled` 选择 `TraceHook`（JSONL）或 `LangfuseTraceHook`<br>9. `emit_trace_event()` 增加 Langfuse 分支：检测当前是否在 Langfuse observation 内，是则调用 SDK event API | runtime/langfuse_hook.py、双模式切换 | CI 仍用 JSONL（enabled=false），本地启用 Langfuse 看到 agent observation + task metadata 正确传播 |
| **S3 Tool observation** | 10. `runner._run_tool()` 开头创建 `langfuse.start_as_current_observation(as_type="tool", name=tool_call.name, input=...)`<br>11. 执行完成后 `tool_obs.update(output=..., level="DEFAULT"/"ERROR")` | Tool 独立 span | Langfuse 能看到每个 tool call 的 latency/arguments 摘要/result 摘要/error |
| **S4 Generation（非 drop-in 路径）** | 12. `runner._request_model()` 检测是否使用 `langfuse.openai` drop-in（检查 `isinstance(self.provider._client, langfuse.openai.AsyncOpenAI)`）<br>13. 未使用 drop-in 时，手动创建 `generation` observation，记录 start_time/response/usage/latency | 非 OpenAI-compatible provider 的 generation span | 测试 Bedrock/Anthropic provider：Langfuse 能看到 generation |
| **S5 Subagent context** | 14. 验证 `subagent._run_subagent()` 中，`LangfuseTraceHook` 的 parent 参数能否传递 Langfuse context（依赖 OTel 自动传播）<br>15. 如需手动传递，调用 `langfuse.get_current_trace_id()` 并在 child 中恢复 | 父子 trace 正确嵌套 | Langfuse 能看到 main/child agent 层级，child 的 parent_span_id 正确 |
| **S6 Masking** | 16. 实现 `mask_otel_spans(spans) -> MaskOtelSpansResult`，删除 `gen_ai.prompt`/`gen_ai.completion` 等敏感属性，保留 hash/length<br>17. 在 Langfuse client 初始化时传入 `mask_otel_spans=...` | 脱敏生效 | Cloud 无 messages 正文/artifact 原文/PII，只有 metadata/hash/length |
| **S7 测试迁移** | 18. 现有使用 JSONL 的测试（test_replay_trace_eval.py 的 trace 部分）改用 OTel `InMemorySpanExporter` 或保持 enabled=false | 测试通过 | CI `pytest tests/runtime/` 绿色 |

### P5.1b 实施步骤（Evaluation 与 Benchmark）

| 阶段 | 步骤 | 产出 | 验证 |
|------|------|------|------|
| **S8 Benchmark CLI** | 19. 新建 `nanobot/cli/benchmark.py`，实现 `prepare/estimate/run/export` 子命令<br>20. `prepare`：校验 OCB/OfficeBench/PresentBench 资产、revision/SHA/license，创建 Langfuse Dataset（如果 enabled）<br>21. `estimate`：计算 token/cost 预算，要求用户确认 | cli/benchmark.py | `nanobot benchmark prepare --profile ci` 成功，不依赖 Langfuse |
| **S9 Experiment Runner** | 22. `run` 子命令封装 `langfuse.run_experiment(data=dataset, task=_mybot_task_callback, evaluators=...)`<br>23. `_mybot_task_callback(item)` 执行 Luna Agent + Skill，返回 artifact 路径/内容<br>24. OfficeBench 官方 evaluator 作为 SDK evaluator function，返回 `Evaluation(value=score, comment=...)` | Experiment Runner 可用 | `nanobot benchmark run --profile office-smoke` 创建 Dataset Run，Langfuse 有 12 traces |
| **S10 LLM-as-a-Judge** | 25. 在 Langfuse UI 配置 Terra OpenAI-compatible LLM Connection（base_url/api_key/model=gpt-5-6-terra）<br>26. 配置 OCB/PresentBench Custom LLM-as-a-Judge（rubric/prompt template）<br>27. 设置 Dataset filter、scope、sampling | LLM Connection + Judge 配置 | Langfuse 对 Dataset Run 自动执行 Judge，生成 `mybot_score` + reasoning |
| **S11 SDK Evaluator** | 28. OpenXML/渲染/文件检查作为本地 SDK evaluator function（同步函数，返回 `Evaluation`）<br>29. 注册到 `run_experiment(evaluators=[officebench_official, openxml_check, ...])` | 本地 evaluator 集成 | Dataset Run 有 `official_score` + `openxml_valid` Score |
| **S12 Annotation Queue** | 30. `export` 子命令通过 Langfuse API 查询 Dataset Run 的 Scores、Annotation Queue 状态<br>31. 判断审核完整性（smoke 全部审核，release 抽样审核完成）<br>32. 生成去敏 JSON/Markdown 快照，更新 README 受控区块 | export 功能 | `nanobot benchmark export --dataset-run <id>` 生成 benchmarks/latest.md |

### 验证要求（全阶段）

- mock SDK 或真实 Cloud 验证每次 LLM/tool 恰好一条 observation，Agent/child 父子关系正确，error/cancel/timeout 全部闭合
- masking 在 SDK export 前生效，密钥、正文和原始 Office 文件不泄漏；enabled=false 时保留 JSONL 作为本地调试路径，enabled=true 时停写 JSONL
- Cloud 关闭/断网不阻塞普通 Runtime；smoke/release 的 Cloud preflight 必须失败
- `run_experiment()` 负责并发、error isolation、Dataset Run、item/run score；Mybot 没有平行的 experiment/status/score 数据模型
- OfficeBench 官方 evaluator 输出原样映射为 Score；OCB/PresentBench Terra LLM Connection、prompt/rubric 和非官方标签可追踪
- PresentBench 验证 media 到 Judge 的真实链路（如 spike 发现不支持，只允许视觉维度 evaluator fallback）
- Annotation Queue 的 Score/comment/reviewer 可直接供 export/CI 查询，不存在 `audit-sync`
- Langfuse Dashboard 能按 benchmark、Skill、model、release、score source 查看成功率、成本、P50/P95 和质量趋势
- 本地 pytest/cassette/Policy/OCC/HITL/OpenXML 硬门在 Langfuse 关闭时独立通过
- P5.1 完成后，README benchmark 区块只能由已审核 Langfuse Dataset Run 导出
