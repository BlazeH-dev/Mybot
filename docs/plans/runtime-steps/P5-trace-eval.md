# P5 Trace、Eval、可观测与公开测评

> 状态：S5.0、P5 Core 与 P5.1 代码实现已完成（2026-07-24）；日本区 Cloud 写入/回读、Luna、Terra Connection、两个 Judge、licensed Dataset 和 token 估算已验证。真实 smoke 因两个 OCB PDF 转 PPTX 资产缺少 Adobe PDF Services 凭据而停止；完整机器评分、PresentBench media、人工审核与发布数字仍未完成。
> Langfuse 部署固定采用日本区 Langfuse Cloud（东京，`https://jp.cloud.langfuse.com`），不实施本地自托管。P5.1 以 Langfuse Python SDK 为观测与评估主干；Mybot 不再建设平行的 Trace、Experiment、Score、Judge 或人工审核系统。

## 1. 最终职责边界

P5.1 只保留四类本地责任：

1. 在 AgentLoop、AgentRunner、Provider、Tool 和 Runtime 状态转换处产生 Mybot 领域语义。
2. 执行 Mybot Agent、Office Skill、LibreOffice、OpenXML 和第三方官方 evaluator 等必须访问本地进程或文件的代码。
3. 准备并校验公开 benchmark 资产、依赖、revision、license 和运行前 token 规模。
4. 运行无 Key 的 deterministic/cassette/Policy/OCC/HITL/恢复硬门；这些是代码正确性测试，不是第二套线上评估平台。

其余能力优先交给 Langfuse：

- Trace、Session、Observation、Token、延迟、查询、下钻、Dashboard、Monitor；
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
| LLM provider 调用 | generation | model、参数、TTFT/总延迟、usage/cache token、error | **runner._request_model() 或 langfuse.openai drop-in** |
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
- Mybot WebUI 不新增 Trace、Token、Dashboard 或 benchmark 面板，只提供 Langfuse deep link。Runtime 的 approval/checkpoint/artifact 当前状态仍由 Mybot UI 展示。

## 5. P5.1b Langfuse Evaluation 主流程

### 在线评估

- JSON Schema、正则、长度、字段完整性等两秒内、无第三方包、无网络的检查使用 Langfuse Code Evaluator。
- 线上 final response 的正确性、相关性、安全性等使用 Langfuse LLM-as-a-Judge，按 observation metadata/filter 和 sampling 执行。
- 失败分类、质量趋势、token/质量关联使用 Scores、Score Analytics、Custom Dashboards 和 Monitors。
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
nanobot benchmark run --profile office-release --model-preset gpt-5-6-luna --presentbench-sample 60
nanobot benchmark export --dataset-run <langfuse-dataset-run-id>
```

- `ci` 只运行 deterministic/cassette/adapter contract，无 API Key、无网络、不开 Langfuse。
- `prepare` 只做本地资产/依赖/license 校验，并将允许上传的 case、expected output、rubric 和 media 写入 Langfuse Dataset。
- `estimate` 只统计调用前预计 token 规模；实际 token 由 Langfuse generation 与 Dashboard 统计。
- `run` 是 `run_experiment()` 的薄封装，不实现并发器、Dataset Run、Score 存储、状态机或跨 run 分析。
- `export` 从一个已完成且审计齐全的 Langfuse Dataset Run/API 读取结果，生成去敏 JSON/Markdown 和 README 受控区块；它不修改 Langfuse Score，也不建立本地可编辑真相源。
- 删除 `benchmark status/resume/audit-sync/publish`、单 active run 锁和 Mybot benchmark WebUI。进度、错误 case、Score 和审核状态直接查看 Langfuse。

Experiment Runner 当前不承诺断点续跑。首版依赖其错误隔离；失败 item 使用新的 retry Dataset Run，并在 metadata 记录 `parent_run_id` 和筛选条件，不自建 case checkpoint。只有真实 token 数据证明整次/子集重跑不可接受时，才单独立项最小恢复层。

### 数据与环境

- 三套 adapter 共用独立 benchmark venv；只安装 task callback 和本地 SDK evaluator 实际需要的精确 constraints。
- OfficeBench 官方 evaluator 所需旧依赖留在该 venv；OCB/PresentBench Judge 已由 Langfuse LLM Connection 执行，不在 Mybot 安装 Azure/Gemini/Anthropic Judge SDK。
- OCB revision 固定为 `f5b560356c8c5fff78569307d655f76d9ea9f6f7`，OfficeBench 为 `b978b808667c32b52ce19a67ce1def1de9ae02b7`，PresentBench 为 `2f01aaf2957004f4f136796147e11f7e52d84684`。
- 每个上游 revision 使用新的不可变 Dataset 名称或固定 Dataset version；不得在同名趋势序列中静默改 case/rubric。Dataset metadata 记录 upstream SHA、license、adapter/constraints、Python、LibreOffice 和 evaluator config。
- 不能上传的原始 Office 文件保存在外部缓存，Dataset item 只存 URI/id、checksum 和 revision；获许可且 SDK 支持的渲染图使用 Langfuse media。
- Git 只提交从已审核 Dataset Run 导出的结果快照和最小环境说明，不提交原始数据、完整 Trace 或大文件。

### profile 与审核

- `office-smoke`：OCB、OfficeBench Office subset、PresentBench 各 4 个固定分层 case，两个 Skill 同题；12 个 case 产生 24 个 Skill experiment traces，全部加入 Annotation Queue。
- `office-release`：OCB 全量、固定 OfficeBench Office subset；PresentBench 在 full/50%/25% 三档 token 估算后选档。换档使用不同 Dataset/series，不混合分母。
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
- common-task quality：OfficeBench official 或 OCB/PresentBench Mybot score、人工 Score、Token、LLM/tool latency、tool success 和 Agent steps。

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
| **S8 Benchmark CLI** | 19. 新建 `nanobot/cli/benchmark.py`，实现 `prepare/estimate/run/export` 子命令<br>20. `prepare`：校验 OCB/OfficeBench/PresentBench 资产、revision/SHA/license，创建 Langfuse Dataset（如果 enabled）<br>21. `estimate`：按 Agent/Judge input/output 分项计算预计 token | cli/benchmark.py | `nanobot benchmark prepare --profile ci` 成功，不依赖 Langfuse |
| **S9 Experiment Runner** | 22. `run` 子命令封装 `langfuse.run_experiment(data=dataset, task=_mybot_task_callback, evaluators=...)`<br>23. `_mybot_task_callback(item)` 执行 Luna Agent + Skill，返回 artifact 路径/内容<br>24. OfficeBench 官方 evaluator 作为 SDK evaluator function，返回 `Evaluation(value=score, comment=...)` | Experiment Runner 可用 | `nanobot benchmark run --profile office-smoke` 创建 Dataset Run，Langfuse 有 24 traces |
| **S10 LLM-as-a-Judge** | 25. 在 Langfuse UI 配置 Terra OpenAI-compatible LLM Connection（base_url/api_key/model=`gpt-5.6-terra`）<br>26. 配置 OCB/PresentBench Custom LLM-as-a-Judge（rubric/prompt template）<br>27. 设置 Dataset filter、scope、sampling | LLM Connection + Judge 配置 | Langfuse 对 Dataset Run 自动执行 Judge，生成 `mybot_score` + reasoning |
| **S11 SDK Evaluator** | 28. OpenXML/渲染/文件检查作为本地 SDK evaluator function（同步函数，返回 `Evaluation`）<br>29. 注册到 `run_experiment(evaluators=[officebench_official, openxml_check, ...])` | 本地 evaluator 集成 | Dataset Run 有 `official_score` + `openxml_valid` Score |
| **S12 Annotation Queue** | 30. `export` 子命令通过 Langfuse API 查询 Dataset Run 的 Scores、Annotation Queue 状态<br>31. 判断审核完整性（smoke 全部审核，release 抽样审核完成）<br>32. 生成去敏 JSON/Markdown 快照，更新 README 受控区块 | export 功能 | `nanobot benchmark export --dataset-run <id>` 生成 benchmarks/latest.md |

### P5.1 代码实现记录（2026-07-24）

- 已落地 `LangfuseConfig`、环境变量回退、默认关闭、masking 和进程级 client registry；启用后 JSONL `TraceHook` 与 Langfuse `LangfuseTraceHook` 互斥。
- 已在 AgentLoop、SubagentManager、AgentRunner、OpenAI-compatible Provider 和 facade 接入 agent/generation/tool observation、父子 OTel context、usage/error/status 和 flush/release。
- 已新增 `nanobot benchmark prepare/estimate/run/export`、三个固定 revision/license 的 adapter、独立 benchmark venv、OCB 固定行号 smoke、OfficeBench 官方 `evaluation.py` 原样 evaluator、Dataset item 去敏、隔离 testbed/reference、Annotation Queue 建立与 export 硬门。
- 已新增离线 contract 测试；`tests/cli/test_benchmark_contract.py`、`tests/runtime/test_langfuse_observability.py` 与 Office Skill/runtime 定向测试通过。
- 尚未产生可发布的真实质量数字：日本区 Cloud、Luna、Terra Connection、两个 Judge、licensed Dataset 和 token 估算已通过；首个 OCB/OfficeCLI Run 因两个本地引用资产缺失而停止，未创建审核 Queue。修复资产后仍须完成六组 Run、必需机器 Score、PresentBench media spike、人工 Queue 和 export；仓库不得填入占位分数。当前机器已安装稳定版 LibreOffice 并记录真实路径/版本，仍需在后续每次 `prepare` 中传入并通过一致性硬门。

### P5.1 用户配置与真实运行步骤（必须按顺序）

下面区分三套不同凭据，不能混用：

| 配置 | 保存位置 | 用途 | 是否进入仓库 |
| --- | --- | --- | --- |
| Luna 中转站 Key/Base URL | `~/.nanobot/config.json` 的 `providers.openai.*` | Mybot 被测 Agent 调用 `gpt-5.6-luna` | 否 |
| Langfuse Project Public/Secret Key | `~/.nanobot/config.json` 的 `observability.langfuse.*`，或 `LANGFUSE_*` 环境变量 | SDK 向日本区 Project 写 Trace、Dataset、Run、Score | 否 |
| Terra 中转站 Key/Base URL | Langfuse Project 的 LLM Connection | OCB/PresentBench Judge 调用 `gpt-5.6-terra` | 否；也不保存到 Mybot 前端 |

#### 换机与复用边界

以下项目属于**每台新电脑/新工作区必做**：安装项目依赖并激活 `venv`、准备 `~/.nanobot/config.json`（只从密码管理器注入 Luna 与 Langfuse Project Key）、设置 `chmod 600`、安装稳定版 LibreOffice 并记录绝对路径和完整版本输出、运行无 Key `ci`、重新建立 `~/.cache/nanobot/benchmarks/` 下的 benchmark venv/cache，并执行一次 `captureContent=false` 的 redacted prepare。若固定 smoke 仍包含需要 PDF 转 Office 的 OCB 资产，还须从密码管理器向当前 shell 重新注入 Adobe PDF Services Client ID/Secret；Terra Key 只保存在 Langfuse Project 的 Connection 中，新电脑不需要复制。不要复制旧电脑的完整 benchmark cache、原始 Office 文件或完整 Trace；新机器应按固定 revision/constraints 重新下载和校验。

以下项目属于**日本区 Langfuse Project 一次性或按变更复用**：Project/API Key Pair、Terra LLM Connection、OCB/PresentBench 两个 Judge、固定 revision 的许可审查结论、已创建的 Dataset/Run/Annotation Queue。换电脑时只需把对应 Key 安全注入新机器并复做本地 Cloud preflight；只有 Project/Key 轮换、Terra endpoint/model/prompt/rubric、benchmark revision/license、Langfuse 区域或数据许可结论变化时，才回到对应步骤重建或复核。

token 估算口径是仓库内可提交的公开配置：每台电脑都必须在 `estimate` 前确认当前 `profiles.json` 的 Agent/Judge input/output token 估算，实际 usage 以 Langfuse generation 为准；模型或 Judge 配置变更时必须重新 estimate 并重新确认运行规模。

#### 第 0 步：先验证无 Key 基线

```bash
cd /Users/wuhao.597/develop/Mybot
source venv/bin/activate
nanobot benchmark run --profile ci
```

当前预期是 `54 passed`。这一步不访问网络、不读取任何真实模型或 Langfuse Key；失败时先修本地代码或依赖，不继续真实 benchmark。

#### 第 1 步：配置被测 Agent 的 Luna Provider（每台新电脑）

只把以下字段合并进 `~/.nanobot/config.json`，不要覆盖已有配置，也不要把真实值写入仓库文档：

```json
{
  "providers": {
    "openai": {
      "apiKey": "<GPT-5.6 中转站 API Key>",
      "apiBase": "https://<GPT-5.6 中转站地址>/v1"
    }
  }
}
```

- Agent preset 固定为 `gpt-5-6-luna`，实际发送给 Provider 的 model 是 `gpt-5.6-luna`；两者已经内置，无需新建 model preset。
- Terra 可以复用同一中转站，但 Judge 凭据必须在第 7 步另存到 Langfuse LLM Connection；Langfuse Project Key 不能当作模型 Key。
- 配置文件只保存在用户目录，建议执行 `chmod 600 ~/.nanobot/config.json`。
- 手工修改配置后，benchmark CLI 的新进程会直接读取；已运行的 gateway 若要产生普通聊天 Trace，需要重启。

#### 第 2 步：在日本区创建 Langfuse Project 并配置 SDK Key（Project 一次性；每台电脑注入 Key）

1. 只打开 `https://jp.cloud.langfuse.com`，注册或登录日本区账号；不要使用欧盟/美国区 Project 的 Key。
2. 新建专用 Project，例如 `Mybot Public Benchmark`。公开 benchmark 与个人/公司数据不要混在同一 Project。
3. 在 Project Settings 的 API Keys 页面新建 Key Pair，记录 `public key` 和 `secret key`；Secret 只显示一次时立即存入密码管理器。
4. 先以 metadata-only 模式合并以下配置：

```json
{
  "observability": {
    "langfuse": {
      "enabled": true,
      "baseUrl": "https://jp.cloud.langfuse.com",
      "publicKey": "<日本区 Langfuse Project Public Key>",
      "secretKey": "<日本区 Langfuse Project Secret Key>",
      "captureContent": false
    }
  }
}
```

Key 也可通过 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 和 `LANGFUSE_BASE_URL` 提供，但 `enabled` 与 `captureContent` 仍必须在 config 中明确设置。Base URL 必须是 `https://jp.cloud.langfuse.com`；不要写默认欧盟地址。日志、截图、issue、commit 和导出文件都不得包含 Secret Key。

本轮工作材料中曾出现过一个真实外观的 Langfuse Secret；当前 `HEAD` 与工作区 diff 已确认不含该值，但不能据此认定凭据仍安全。下次 licensed prepare 前，用户必须在日本区 Project 的 API Keys 页面撤销旧 Key Pair、创建新 Pair、更新本机 `~/.nanobot/config.json`，保持 `captureContent=false` 重做第 6 步 redacted preflight；旧 Key 未撤销前不得继续真模型 Run。

#### 第 3 步：安装并锁定稳定版 LibreOffice（每台新电脑）

1. 从 LibreOffice 官方渠道安装稳定 release，不使用 Codex bundled runtime 中的 `LibreOfficeDev`、alpha、beta 或 nightly。
2. 找到真实 `soffice` 绝对路径。macOS 常见示例为 `/Applications/LibreOffice.app/Contents/MacOS/soffice`，实际以本机安装为准。
3. 运行并保留完整输出：

```bash
/absolute/path/to/soffice --version
```

4. 后续每次 `prepare` 同时传入相同的绝对路径和上述输出的完整原文。路径不可用或版本文本不完全一致时 CLI 会失败。

当前机器已通过 LibreOffice 官方稳定版渠道安装 Homebrew cask `libreoffice` 26.2.4。后续 `prepare` 固定传入：

```text
soffice path: /Applications/LibreOffice.app/Contents/MacOS/soffice
soffice --version: LibreOffice 26.2.4.2 0229ac93fcf0d7cbc6376066c6f35021cef002dc
```

Codex runtime 的开发版仍返回 `LibreOfficeDev 26.8.0.0.alpha0 2c87e51eeaa2b413ff4ae097b2705eea1995d8e5`；它只能用于开发排查，不能作为 smoke/release 发布证据。

#### 第 4 步：确认 Luna/Terra token 统计口径（每次 estimate 前复核）

`benchmarks/office/profiles.json` 的 `estimate_tokens_per_case` 固定保存单次 Luna Agent 与 Terra Judge 的 input/output token 估算：

```json
{
  "agent_input": 18000,
  "agent_output": 5000,
  "judge_input": 12000,
  "judge_output": 1500
}
```

`estimate` 按 profile case 数、两个 Skill 和需要 Judge 的 OCB/PresentBench item 分项计算预计 token，仅用于确认运行规模。无需填写或提交任何模型价格，CLI 不计算金额，也不以价格或金额确认阻止 `run`。真实 input/output/cache token 以 Langfuse generation usage 为准。

#### 第 5 步：完成数据许可与跨境上传确认（按 Project/revision 复核）

逐项审查 `benchmarks/office/profiles.json` 固定的 revision 和许可证：

- OCB code：MIT；Dataset：CDLA-Permissive-2.0。
- OfficeBench：Apache-2.0。
- PresentBench code：Apache-2.0；prompt/rubric 标记为 CC-BY-NC-4.0，源材料保留各自原始条款。

必须确认 prompt、expected output、rubric、引用文件和需要的渲染媒体均可上传日本区 Cloud。公司、客户、个人、受保密约束或许可不明确的数据一律不上传。

- 尚未全部确认：保持 `captureContent=false`，第 6 步只能执行 redacted prepare；不得传 `--allow-licensed-content`，也不得运行真模型。
- 全部确认：先记录审查人、日期、固定 revision 和结论，再将 `captureContent` 临时改为 `true`，并在 licensed prepare 中显式传入 `--allow-licensed-content`。

`captureContent=true` 是进程级内容开关，会让该配置下的普通 Agent Trace 也可能包含正文。开启期间不要运行个人/公司任务；benchmark 完成后必须在第 13 步恢复为 `false`。

2026-07-24 本轮执行按用户指示将许可确认视为不阻塞，仅限本计划固定 revision 的 OCB、OfficeBench、PresentBench 公开 benchmark 内容上传到日本区专用 Project。该指示不覆盖公司、客户、个人、保密或许可不明确的数据，也不是对后续 revision、其他区域或其他 Project 的永久法律结论；上述任一边界变化时必须重新执行本步骤。

#### 第 6 步：先做 metadata-only 日本区 Cloud preflight（每台新电脑）

保持 `captureContent=false`，不带 `--allow-licensed-content`：

```bash
nanobot benchmark prepare --profile office-smoke \
  --soffice /absolute/path/to/soffice \
  --soffice-version '<完整的 soffice --version 输出>'
```

首次执行会在 `~/.cache/nanobot/benchmarks/` 建立独立 venv、下载固定 revision、创建 `redacted` Dataset，并自动完成日本区写入、flush、API 回读和 deep link smoke。成功标准：

1. CLI 输出 `Prepared office-smoke`。
2. `~/.cache/nanobot/benchmarks/office-smoke.prepared.json` 包含 `cloud_smoke.trace_id` 和 `cloud_smoke.deep_link`。
3. deep link 能在当前 Project 打开 `mybot.benchmark.cloud_smoke` Trace。
4. redacted Dataset item 只有 hash/metadata，`expected_output.content_withheld=true`，没有 prompt、Office 原文件或 Secret。

认证失败、Project 区域错误、flush 后无法回读、deep link 404 或 redacted item 泄漏正文时立即停止并轮换已泄漏的 Key。

#### 第 7 步：创建 Terra LLM Connection 和两个 Judge（Project 一次性；配置变更时复核）

在同一个日本区 Langfuse Project 中：

1. 进入 Project Settings -> LLM Connections，新增 OpenAI-compatible/Custom OpenAI Connection。
2. Base URL 填 Terra 中转站的 OpenAI-compatible `/v1` 地址，API Key 填模型中转站 Key，model 填实际模型名 `gpt-5.6-terra`。不要填 Mybot preset 名 `gpt-5-6-terra`，也不要填 Langfuse Project Secret Key。
3. 连接名固定且可辨识，例如 `mybot-terra-judge-v1`；使用无敏感内容的最小 prompt 执行连接测试，确认响应、usage 和错误信息正常。
4. 新建 OCB Judge：只过滤 `metadata.benchmark=ocb` 且 `metadata.evaluation_source=langfuse_terra` 的 Experiment items；映射 Dataset `input`、task `output` 和 `expected_output`；Score 名必须精确为 `mybot_score`，数值范围 `0..1`，保留 reasoning/comment，sampling 为 100%。
5. 新建 PresentBench Judge：过滤条件改为 `metadata.benchmark=presentbench`，其余 Score 契约相同。不得把这两个 Judge 应用到 OfficeBench；OfficeBench 使用本地官方 evaluator 的 `official_score`。
6. 保存 evaluator 名称、版本、Connection 名、model、prompt/rubric 版本和 filter，后续不得在同一比较序列中静默修改。

若当前 Langfuse UI 必须先选择一个已有 Dataset/Run 才能映射 evaluator 字段，则本步先完成 1-3，执行第 8 步生成 licensed Dataset 后，再返回完成 4-6；真实 run 前两个 Judge 必须已经启用。

PresentBench 必须先检查 Langfuse Dataset item 和 evaluator 能否实际消费 rubric 与媒体。当前阶段只声明 media spike，不能用 `rubric_sha256`、本地文件路径或无图输入假装完成视觉评分。若 Cloud Judge 看不到真实 rubric/media，停止 PresentBench 发布，记录 `unscored`，后续只允许实现计划中的本地视觉 SDK evaluator fallback；不得人工补一个伪 `mybot_score` 绕过 export。

#### 第 8 步：执行 licensed prepare（每台新电脑/每个新 profile cache）

仅在第 5 步全部批准、`captureContent=true` 后执行：

OCB smoke 的 `science_mixed_long_1.pptx` 与 `winml_gdc.pptx` 上游是 PDF，固定 revision 的官方 `download_and_convert_files.py` 要通过 Adobe PDF Services 导出为 PPTX。每台没有这两个已校验缓存文件的新电脑都必须：

1. 在 Adobe Developer Console 创建或复用 PDF Services API 凭据，把 Client ID/Secret 存入密码管理器；不要写入仓库、`~/.nanobot/config.json`、shell history、截图或 benchmark cache。
2. 只在执行 `prepare` 的同一终端会话中注入：

```bash
export PDF_SERVICES_CLIENT_ID='<从密码管理器注入>'
export PDF_SERVICES_CLIENT_SECRET='<从密码管理器注入>'
```

3. 确认独立 benchmark venv 使用仓库锁定的 `pdfservices-sdk==4.2.0` 与 `python-dotenv==1.2.1`。`prepare` 会调用固定 OCB revision 的官方 downloader；不要用 LibreOffice 或其他转换器替换官方 Adobe 路径。
4. `winml_gdc` 来源下载若超时，直接重跑同一个 `prepare`；downloader 会跳过已经成功且大于 1 KiB 的文件，并重试仍缺失的文件。不得用空文件、改后缀或非官方同名文件绕过校验。

```bash
nanobot benchmark prepare --profile office-smoke \
  --soffice /absolute/path/to/soffice \
  --soffice-version '<完整的 soffice --version 输出>' \
  --allow-licensed-content
```

成功后应创建或复用名称含 `licensed-v1` 的三个不可变 Dataset，prepared 文件为 schema v2，记录三份 case manifest 的 SHA-256，且 `licensed_content_uploaded=true`。确认 OCB cache 同时存在 `Candy.xlsx`、`Data Access Plan Template _final.docx`、`science_mixed_long_1.pptx` 和 `winml_gdc.pptx`，四个 smoke row 的 `reference_sha256` 均非空。逐个抽查 Dataset item：内容与固定 revision 一致、没有本机绝对路径/Key/个人数据，OCB/PresentBench 只包含已经批准的内容；PresentBench `expected_output` 必须嵌入结构化 `rubric`，不得含 `judge_prompt_path`。redacted Dataset 与 licensed Dataset 名称不同，不能把旧 redacted Dataset 当作可运行输入。

本轮已完成 licensed Dataset 上传和路径去敏；随后已在 `captureContent=false` 下重跑 redacted prepare，当前本机 prepared 文件是 schema v2、包含三份 case manifest 摘要且 `licensed_content_uploaded=false`。必须在 Adobe 转换成功后按本步骤再次执行 licensed prepare，不能把当前 redacted prepared 文件用于真模型运行。

#### 第 9 步：估算并核对 smoke token 规模（每次 run）

```bash
nanobot benchmark estimate \
  --profile office-smoke \
  --model-preset gpt-5-6-luna
```

确认输出中的三个 case count、两个 Skill、`skill_runs`、`judge_runs`，以及 `estimated_tokens` 下的 Agent/Judge input/output 和 total。若 token 规模异常，先校正 profile 的单 case token 假设或样本档位，再运行真模型。

#### 第 10 步：运行真实 office-smoke（每个目标 Run）

```bash
nanobot benchmark run \
  --profile office-smoke \
  --model-preset gpt-5-6-luna
```

本轮首次尝试的 OCB/OfficeCLI Dataset Run ID 为 `8e63bb66-f505-4e08-b16a-70399467e074`：1 个 item 完成，3 个 item 因本地 OCB 引用文件缺失失败，且未创建 Annotation Queue。它只能作为基础设施失败证据，不能发布为质量结果。完成第 8 步后，先把整个 OCB/OfficeCLI 组合重跑为带父 Run 关联的 retry Run（当前 CLI 不做 item 级筛选）：

```bash
nanobot benchmark run \
  --profile office-smoke \
  --model-preset gpt-5-6-luna \
  --benchmark ocb \
  --skill officecli \
  --parent-run-id 8e63bb66-f505-4e08-b16a-70399467e074
```

retry 成功后，用过滤参数分别运行其余五个组合，避免再次执行已恢复的 OCB/OfficeCLI：

```bash
nanobot benchmark run --profile office-smoke --model-preset gpt-5-6-luna --benchmark ocb --skill office-python
nanobot benchmark run --profile office-smoke --model-preset gpt-5-6-luna --benchmark officebench --skill officecli
nanobot benchmark run --profile office-smoke --model-preset gpt-5-6-luna --benchmark officebench --skill office-python
nanobot benchmark run --profile office-smoke --model-preset gpt-5-6-luna --benchmark presentbench --skill officecli
nanobot benchmark run --profile office-smoke --model-preset gpt-5-6-luna --benchmark presentbench --skill office-python
```

完整 smoke 最终仍须得到 3 个 benchmark x 2 个 Skill = 6 个成功 Dataset Run、6 个 Annotation Queue，共 24 条成功 experiment traces；失败的父 Run 不计入这六组结果。保存 CLI 输出中的每个 `dataset_run=<id>`、`review_queue=<id>` 和 URL。成功运行后逐项确认：

- OCB 与 PresentBench 每个 item 最终都有 Terra Judge 产生的 `mybot_score` 和 reasoning；Judge 异步未完成时继续等待，不导出。
- OfficeBench 每个 item 有 `official_score`、`official_evaluator_ok=true` 和 `output_present=true`。
- 每个 Agent/Tool/Generation observation 层级正确，model、usage、latency 和错误可下钻；同一次 LLM 调用没有 drop-in/manual 双重 generation。
- 失败 item 不在原 Run 中伪装重试；新建带 `parent_run_id` 的 retry Dataset Run，并单独审核。

#### 第 11 步：完成 PresentBench media spike（首次/媒体或 Judge 配置变更时）

从两个 PresentBench smoke Run 各抽至少一个 item，确认 Judge 实际收到允许上传的渲染媒体，并能把视觉维度写入 reasoning/Score。只看到文件名、hash、本机路径或纯文本不算通过。平台不支持媒体时，把视觉维度标为 `unscored` 并停止 PresentBench 发布，直到本地 SDK evaluator fallback 有代码、测试和 Score 写回证据。

#### 第 12 步：在六个 Annotation Queue 完成人工审核（每个 smoke Run）

Queue 名固定为 `mybot-office-smoke-<benchmark>-<skill>-review`。smoke 的 24 条 trace 必须全部审核：

1. 打开 item 的 input/output、artifact hash、Judge/official Score 和 Trace。
2. 填写 `mybot-human-review`，数值范围 `0..1`。
3. 写明通过/失败原因；需要纠正时保留 comment/corrected output。
4. 将 Queue item 状态改为 `COMPLETED`，保留 reviewer 身份。

仅点完成但没有 `mybot-human-review` Score 不算审核完成。OCB/PresentBench Judge 与人工结论冲突、OfficeBench infrastructure error、视觉 `unscored` 均应先处理，不能靠人工 Queue 掩盖缺失的必需机器 Score。

#### 第 13 步：逐个导出并恢复安全默认值（每个已审核 Run）

每个 Dataset Run 独立导出：

```bash
nanobot benchmark export --dataset-run <一个已完成审核的 Dataset Run ID>
```

export 会检查 item 完整性、必需 Score、Queue 完成数量、`mybot-human-review` 和真实 deep link；失败信息必须修复，禁止手工编辑 README 绕过。成功后生成 `benchmarks/exports/<run-id>.{json,md}`，并把 README 受控区块更新为本次 Run。六个 Run 可依次留档，但 README 只展示最后一次成功导出的 Run；选择要公开展示的 Run 最后导出。

导出后检查快照没有 Key、正文、原始 Office 文件和本机敏感路径。随后把 `captureContent` 恢复为 `false`；若不需要普通任务持续上报，再把 `enabled` 恢复为 `false`。已运行 gateway 需重启后才应用观测开关。

#### 第 14 步：smoke 全部通过后再跑 release

release 必须重新 prepare，不能复用 smoke prepared 文件：

```bash
nanobot benchmark prepare --profile office-release \
  --soffice /absolute/path/to/soffice \
  --soffice-version '<完整的 soffice --version 输出>' \
  --allow-licensed-content

nanobot benchmark estimate \
  --profile office-release \
  --model-preset gpt-5-6-luna \
  --presentbench-sample 60

nanobot benchmark run \
  --profile office-release \
  --model-preset gpt-5-6-luna \
  --presentbench-sample 60
```

PresentBench 只允许 `60`（25%）、`119`（50%）或 `238`（full），estimate 与 run 必须使用同一档。release 每个 Queue 至少完成 CLI 固定的稳定 5% 样本，并手工补审高风险、Judge 异常、官方/Judge 分歧和视觉 `unscored`；全部必需 Score/审核齐全后才逐 Run export、检查 diff、提交和推送结果快照。

#### 停止条件

| 条件 | 必须动作 |
| --- | --- |
| `ci` 失败 | 不配置 Cloud，不跑真模型 |
| Langfuse 不是日本区、auth/readback/deep link 失败 | 修正 Project/Key/endpoint 后重做 preflight |
| 只有 LibreOfficeDev/alpha/beta/nightly | 不作为发布环境，安装稳定版 |
| OCB 官方 downloader 报 `PDF_SERVICES_CLIENT_ID / PDF_SERVICES_CLIENT_SECRET not set` | 从密码管理器向当前 shell 注入 Adobe PDF Services 凭据，重跑 licensed prepare；Key 不写入仓库/config/cache |
| OCB 来源 URL timeout 或两个 PPTX 仍缺失 | 重跑 prepare；四个引用资产与 SHA-256 未齐全前不调用模型 |
| token 估算与 profile case 数或样本档位不一致 | 校正统计口径后再运行真模型 |
| 任一数据许可或跨境上传未批准 | 只做 redacted prepare，保持 `captureContent=false` |
| Terra Connection 测试失败或 Judge filter/Score 名不正确 | 不跑或不导出 OCB/PresentBench |
| PresentBench rubric/media 未真正进入 Judge | 标为 `unscored`，停止 PresentBench 发布 |
| 必需 Score、Queue review 或 deep link 缺失 | 让 export fail closed，不手工填 README |
| 发现 Key、PII、公司内容或本机敏感路径泄漏 | 立即停用 Cloud、轮换 Key、删除受影响 Cloud 数据并重新审查 |

### 验证要求（全阶段）

- mock SDK 或真实 Cloud 验证每次 LLM/tool 恰好一条 observation，Agent/child 父子关系正确，error/cancel/timeout 全部闭合
- masking 在 SDK export 前生效，密钥、正文和原始 Office 文件不泄漏；enabled=false 时保留 JSONL 作为本地调试路径，enabled=true 时停写 JSONL
- Cloud 关闭/断网不阻塞普通 Runtime；smoke/release 的 Cloud preflight 必须失败
- `run_experiment()` 负责并发、error isolation、Dataset Run、item/run score；Mybot 没有平行的 experiment/status/score 数据模型
- OfficeBench 官方 evaluator 输出原样映射为 Score；OCB/PresentBench Terra LLM Connection、prompt/rubric 和非官方标签可追踪
- PresentBench 验证 media 到 Judge 的真实链路（如 spike 发现不支持，只允许视觉维度 evaluator fallback）
- Annotation Queue 的 Score/comment/reviewer 可直接供 export/CI 查询，不存在 `audit-sync`
- Langfuse Dashboard 能按 benchmark、Skill、model、release、score source 查看成功率、token、P50/P95 和质量趋势
- 本地 pytest/cassette/Policy/OCC/HITL/OpenXML 硬门在 Langfuse 关闭时独立通过
- P5.1 完成后，README benchmark 区块只能由已审核 Langfuse Dataset Run 导出
