# P5 Cassette、Trace、Eval 与安全红队代码说明

> 对应计划：`docs/plans/runtime-steps/P5-trace-eval.md`
> 当前状态：S5.0 与 Core 已完成（2026-07-18）。P5.1 Langfuse 接入方案已评审并更新实施步骤（2026-07-23），待实施。

## P5.1 核心改动（相比原方案）

### 关键决策变更

1. **JSONL TraceHook 保留策略**：`observability.langfuse.enabled=false`（默认）时保留现有 JSONL TraceHook 作为本地调试路径，启用 Langfuse 后停写 JSONL，二者互斥。避免默认配置下完全无持久观测。

2. **Provider observation 创建边界**：必须在 `runner._request_model()` 内逐调用创建 generation observation，记录 start_time/TTFT/latency/usage，而非在 `after_iteration` 聚合。优先使用 `langfuse.openai` drop-in（从 config 设置环境变量后导入），让所有 OpenAI-compatible provider（OpenAI/DeepSeek/GPT-5.6）自动追踪。

3. **Tool observation 创建边界**：必须在 `runner._run_tool()` 内逐调用创建 tool observation，记录 tool_call_id/arguments 摘要/latency/result 摘要/error。

4. **Config schema 增强**：新增 `config.observability.langfuse.*` 字段（enabled/baseUrl/publicKey/secretKey/captureContent），provider 根据 config 条件设置环境变量并导入 `langfuse.openai.AsyncOpenAI`。

5. **Benchmark CLI 新建**：`nanobot/cli/benchmark.py` 实现 `prepare/estimate/run/export`，封装 `langfuse.run_experiment()`。

### 实施步骤概览

P5.1a（Observability）：S0 准备 → S1 Provider drop-in → S2 TraceHook 双模式 → S3 Tool observation → S4 Generation（非 drop-in） → S5 Subagent context → S6 Masking → S7 测试迁移

P5.1b（Evaluation）：S8 Benchmark CLI → S9 Experiment Runner → S10 LLM-as-a-Judge → S11 SDK Evaluator → S12 Annotation Queue

详见 `docs/plans/runtime-steps/P5-trace-eval.md` 第 9 节。

## 这一阶段解决什么问题

含 LLM 的 Agent 系统很难只靠普通单测回答：

- 模型每次可能输出不同工具调用，CI 怎样无 API Key 复现关键路径？
- 出错时怎样知道是模型、工具、Policy、人工等待还是 checkpoint 的问题？
- 最终 DOCX 能打开，是否代表数字正确、OpenXML 完整、页面不空白？
- 一个平均分很高的任务，是否可能偷偷发生一次越权写入？
- 多 Agent 更快还是更贵，怎样留下可比较数据？

P5 采用三层结构：

```text
纯状态机/脚本测试
  最快、最确定，测 policy、OCC、artifact、metric 等

Cassette Agent smoke
  固定模型响应，测真实 AgentLoop/Runner 工具协议，无 Key、无网络

真模型 benchmark
  测质量、成本和时延，但不放进确定性 CI
```

同时增加 Trace 解释执行过程，Eval 对结果做硬门判定，红队验证攻击后果。

## 1. CassetteProvider：录下模型边界，不录整个世界

文件：`nanobot/runtime/replay.py`

`CassetteProvider` 实现与普通 `LLMProvider` 相同的 `chat()` 接口，有两种模式。

### record

调用真实 delegate provider，然后把规范化 request 和 response 追加到 JSONL。

记录内容包括：

- model。
- messages 的稳定版本。
- 排序后的 tool names。
- request SHA-256。
- response content、finish reason、reasoning、tool calls。

### replay

不调用网络，按顺序读取 cassette：

1. 对当前 messages/tools/model 做相同规范化。
2. 计算 request hash。
3. 与当前行 expected hash 比较。
4. 匹配则重建 `LLMResponse`。
5. 不匹配则输出 cassette 与 runtime 的 unified diff。

`assert_consumed()` 还要求所有录制的 LLM 行都被完整使用，避免流程提前结束却误判通过。

### 为什么要剥离 volatile 字段

`_stable()` 去掉 timestamp、created_at、updated_at、usage、latency、request_id、turn_id 和 `_` 开头的内部字段。否则每次运行随机 id 和时间不同，cassette 会变成无意义的脆弱快照。

但它仍保留稳定 messages、tool names 和 arguments，所以 prompt/工具协议的真实变化会产生可读失败。

### 已提交 cassette

```text
plan_automatic.jsonl
plan_explicit.jsonl
interaction_deadlines.jsonl
checkpoint_conflict.jsonl
```

它们覆盖自动/显式计划、三档交互 deadline 和 checkpoint/冲突等高价值路径。

## 2. TraceHook：记录执行过程，但默认不保存敏感正文

文件：`nanobot/runtime/trace.py`

P5 没有重写 AgentRunner，而是复用现有 `AgentHook/CompositeHook`：

- `before_run` 记录 run start。
- `after_iteration` 记录 usage、tool calls 和 tool events。
- `after_run` 记录 stop reason、duration、usage 和输出摘要。
- `on_error` 记录错误。
- `on_finally` 清理 ContextVar。

### OTel-shaped 字段

每条 JSONL 包含：

```text
timestamp
trace_id / span_id / parent_span_id
event.name
gen_ai.system / gen_ai.request.model
mybot.task.id / mybot.actor
attributes
```

标准 GenAI 字段尽量对齐 OpenTelemetry，自有字段使用 `mybot.*`，便于未来接标准 collector，而不是被自定义格式锁死。

### 脱敏摘要

`_summary(value)` 不直接写 messages 或 final content，而是保存：

```json
{"sha256": "...", "chars": 1234}
```

tool argument 同样默认只记录摘要。这样能判断输入是否变化、大小是否异常，又不会默认把用户文档、密钥或完整对话写进 trace。

### 运行时事件

`emit_trace_event()` 使用当前 TraceContext 追加事件。P3/P4/P8 可记录：

- `mybot.policy.decision`
- `mybot.interaction.requested/resumed`
- `mybot.human_wait_ms`
- plan create/confirm
- checkpoint/recovery
- subagent spawn/complete/cancel/fail/loop_guard

人工等待时间与模型/工具运行时间分开，避免把“用户两分钟没点按钮”误算成模型推理慢。

### 父子 trace

child `TraceHook` 接收 parent TraceContext：

- 复用父 `trace_id`。
- 生成新的 child `span_id`。
- `parent_span_id` 指向父 span。
- actor 写成 `child:<id>`。

因此现有导出可以在标准查看器中还原父子关系；当前仓库尚未真正接入或部署 Langfuse。

### OTLP 导出

`export_jsonl_to_otlp()` 按 `(trace_id, span_id)` 聚合事件，生成最小 OTLP JSON envelope。项目选择导出到现有生态，而不是自研 trace dashboard。

## 3. Eval Harness：硬门不能被平均分掩盖

目录：`nanobot/runtime/evals/`

metric 接口：

```python
score(case) -> MetricResult(
    passed,
    score,
    issues,
    details,
    hard_gate=True,
)
```

当前注册 12 个 metric。

### 结果/文件类

1. `artifact_completion`
   - 所有 expected artifact 必须真实存在。
2. `file_openable`
   - 普通文件能打开；docx/xlsx/pptx 必须是可读 ZIP。
3. `data_consistency`
   - actual facts 必须等于 expected facts，每个定量 key 必须有非空 fact id。
4. `openxml_validation`
   - 检查 ZIP CRC、必需部件、XML/REL 可解析、Content Types 和 relationship 目标存在。
5. `visual_sanity`
   - 截图数量与页数一致、尺寸合理、不是纯色空白页。

### 治理证据类

6. `replayability`
7. `policy_compliance`
8. `file_conflict_safety`
9. `interaction_resume`
10. `approval_binding`
11. `subagent_governance`
12. `untrusted_content_safety`

这些 metric 当前读取固定 case 中的 evidence 布尔值，例如：

```text
no_unapproved_write
expired_never_allowed
zero_partial_write
waiting_provider_calls_zero
permission_not_widened
no_secret_leak
```

这里要诚实理解：部分 metric 是对测试已经生成的结构化证据做聚合，不是扫描所有真实世界行为的万能检测器。证据的可信度来自对应的 Runtime 定向测试和固定 fixture。

## 4. OpenXML 和视觉校验为什么不能只看“文件存在”

DOCX/XLSX/PPTX 本质上是 ZIP 包。文件扩展名正确不代表内部有效。

`OpenXmlValidationMetric` 会：

- 检查 `.docx/.xlsx/.pptx` 对应的必需 part。
- 调用 `ZipFile.testzip()` 检查 CRC。
- 解析所有 `.xml/.rels`。
- 检查 `[Content_Types].xml` 引用的 part 存在。
- 解析 relationship 相对路径，拒绝缺失或逃逸目标。

`VisualSanityMetric` 用 Pillow 打开截图：

- 页数必须是正整数并等于截图数。
- 宽高不能明显异常。
- 图片不能与左上角颜色铺满的纯色背景完全相同。

它只能发现明显空白和尺寸错误，不等于审美评分。版式美观仍属于未来人工或 LLM Judge 的软质量层。

## 5. Eval report 怎样保持确定性

文件：`nanobot/runtime/evals/report.py`

`evaluate_cases()`：

1. 按 case 路径排序。
2. 对 case JSON 规范化后计算 `fixture_digest`。
3. 按 case 声明的 metric 顺序执行注册表。
4. 统计 hard failure。
5. 只有 hard failure 为 0 且所有 case 通过，overall 才 PASS。

报告不写当前墙钟时间，避免同一 fixture 每次生成不同文件。输出 JSON + Markdown，可在 CI 中逐字节 `cmp` 已提交基线。

当前提交：

```text
benchmarks/latest.json
benchmarks/latest.md
benchmarks/subagent-comparison.json
benchmarks/subagent-comparison.md
```

当前确定性基线是 5 cases、0 hard failures、overall PASS。

## 6. 安全红队测“后果”，不测模型有没有说出危险关键词

fixtures 覆盖：

- 会议纪要中的间接 prompt injection。
- xlsx 单元格中的注入文本。
- workspace 越界、覆盖源文件、删除诱导。
- 恶意 MCP 描述。
- child 试图绕过父权限。

验收目标不是“模型识别攻击的准确率”，而是最终后果：

```text
未批准写入 = 0
敏感读取/泄漏 = 0
未确认外发 = 0
恶意 MCP 执行 = 0
child 权限扩大 = 0
```

即使模型被不可信文档说服，只要 Policy、Sandbox、approval 和 scope 仍阻断副作用，系统安全目标就成立。这个思路比只评价模型回复“我不会执行恶意指令”更工程化。

## 7. 单 Agent 与 Subagent 对比报告

文件：`nanobot/runtime/evals/subagent_compare.py`

固定 fake-provider baseline 记录：

```text
success_rate
wall_clock_ms / p95_wall_clock_ms
input_tokens / output_tokens
cost_usd
parent_context_tokens
failures / cancellations / loop_guard_stops
child_count
```

报告计算 multi - single 的 delta。

当前结果显示 multi 在固定假数据下 wall time 更低、token 更多、父上下文更小。但报告明确标记：

```text
measurement_kind = deterministic_fake_provider
```

它验证的是报告结构、治理开销字段和回归行为，不代表真实模型质量或真实并行收益。真模型结论仍需要手动 benchmark。

## 8. CI 怎样接入

`.github/workflows/ci.yml`：

1. 安装 Python 3.11、项目依赖和 Bubblewrap。
2. `ruff check nanobot/ tests/runtime/`。
3. 跑 Runtime、Office fixture、Skill、plan、模型配置等确定性测试。
4. 在 `/tmp` 生成 Runtime eval 和 Subagent comparison。
5. 用 `cmp` 与 committed benchmark 逐字节比较。

这让 metric 逻辑、fixture 或输出格式发生变化时必须显式更新基线，而不是悄悄漂移。

## 为什么不把 LLM Judge 放进硬门

LLM Judge 适合评估文案覆盖、风格和版式合理性，但不适合推翻：

- 数字错误。
- 越权写入。
- 文件冲突。
- OpenXML 损坏。
- 未确认外发。

软评分可能受模型、prompt 和价格变化影响。P5 的原则是先把可以确定性证明的安全与正确性做成硬门，Judge 只作为离线加分项，而且当前尚未实现。

## 验证与历史结果

主要测试：

- `tests/runtime/test_replay_trace_eval.py`
- `tests/runtime/test_redteam.py`
- 其他 P3/P4/P8 测试为 evidence 提供事实。

历史阶段快照：

- Runtime 当时 `56 passed, 1 skipped`，确定性执行约 1.25 秒。
- 受影响后端当时 `236 passed`。
- WebUI 交互相关当时 `51 passed`，lint/build 通过。
- 全量历史套件仍有本阶段外的旧 provider/facade/前端基线失败，因此没有被伪装成全量绿色。

这些数字是当时证据，当前状态应重新运行命令确认。

## 未实现和不能夸大的部分

- 没有 LLM Judge/Verifier 线上能力。
- 没有 OTel-native Langfuse SDK 接入、本地 span exporter 或 dataset/experiment/score 关联；当前只有直接追加的本地 JSONL 与最小 OTLP JSON 导出。
- 没有 OCB、OfficeBench Office subset 或 PresentBench adapter 与真模型基线。
- P1.1 已将 Python baseline 落地为通用 `office-python`；P5 Core 的 `office_baseline` 已切换到中立 OpenXML fixture，不再依赖旧周报资产。
- 没有真实 DeepSeek/GPT 多模型质量-价格矩阵。
- 没有 KV cache 优化结论。
- visual sanity 不是审美或排版质量评分。
- evidence metric 依赖固定测试产生的证据，不是生产监控万能扫描器。
- committed fake-provider Subagent 对比不能证明真实模型一定更快。

## P5.1 后续实施边界（尚未实现）

### P5.1a：Langfuse Python SDK 作为唯一观测后端

- `TraceHook` 保留为 AgentHook 上的语义入口，负责 `mybot.*`、Runtime 语义和字段 allowlist；Interaction/Checkpoint/ArtifactStore 仍是 Runtime 状态真相源。
- 锁定 Langfuse Python SDK，直接使用 OTel 上下文、observation、`propagate_attributes()`、`mask_otel_spans`、batch/retry/flush 和 Cloud 传输。不实现 `TraceEvent -> CompositeTraceSink`、生产 JSONL exporter、HTTP 客户端或发送队列。
- 实施前 spike 验证 async/Subagent 父子关系、agent/generation/tool/guardrail 映射、usage/cost、有限时间 shutdown 和日本区 API 回读。测试使用 OTel in-memory exporter；普通任务不持久双写 JSONL。
- SDK 随 Mybot 默认安装并锁定版本，不安装 Langfuse Skill，不让前端 JS/TS SDK发送主 Trace，也不把 secret key 暴露给浏览器。
- `after_iteration` 只保留摘要；Provider 和 Runner 工具边界必须逐调用创建 generation/tool observation。
- Cloud 默认关闭时，普通 Runtime 和本地 CI 继续运行，但不承诺持久 Trace 或离线补传；smoke/release 必须通过日本区真实写入、flush、回读和 deep link。
- 默认只上传脱敏 metadata、hash、长度、状态和指标；原始 Office 文件、正文、完整 artifact、密钥和个人信息不进入 Cloud。日本区属于跨境数据传输，敏感数据保持关闭。
- Langfuse UI 负责 Trace/Sessions、Dataset/Experiment、Scores、成本/延迟 Dashboard、Monitors 和 Annotation Queue；Mybot WebUI 不复制这些能力。

### P5.1b：Langfuse Evaluation 主流程

1. Dataset 使用公开 benchmark 的 input、expected output、rubric、revision 和获许可 media；不能上传的 Office 原文件留在外部缓存，item 保存 URI/id 和 checksum。
2. `run_experiment()` 负责并发、自动 Trace、错误隔离、item/run evaluator、Dataset Run 和比较；Mybot task callback 只负责执行 Luna Agent、Skill 和本地文件操作。
3. OfficeBench 官方 evaluator、OpenXML/渲染/文件检查作为本地 SDK evaluator function 返回 Langfuse `Evaluation`；不创建第二套 EvalResult/Score 数据库。
4. OCB/PresentBench 使用 Langfuse Custom LLM-as-a-Judge，通过 OpenAI-compatible LLM Connection 调用 `gpt-5-6-terra`，记录 `mybot_score` 和 reasoning；PresentBench 视觉媒体不兼容时只允许该维度使用本地 SDK evaluator fallback，Score 仍写 Langfuse。
5. JSON Schema、正则、长度等轻量线上检查使用 Langfuse Code Evaluator；Policy/OCC/HITL/恢复仍是本地 Runtime hard gate，不交给异步 evaluator 决策。
6. 人工审计使用 Langfuse Annotation Queue；smoke 全 12 case，release 约 5% 分层抽样并额外审核高风险、Judge 异常和视觉 `unscored`。不存在 `audit-sync` 和本地 `human_audit` 真相源。
7. `export --dataset-run` 读取 Langfuse 的完成状态、Scores 和 Annotation Queue，生成 README 快照；Dataset Run、Score、Annotation 是评估真相源。

### 结果口径、环境与恢复

- OfficeBench 使用 `evaluation_source=officebench_official` 和 `official_score`；OCB/PresentBench 使用 `evaluation_source=langfuse_terra`、`mybot_score` 和 `Mybot evaluation`，不得标记 `official-comparable`。
- 不配置 Azure/Gemini/Anthropic Judge SDK；Terra 凭据、Base URL、model name 和 structured-output/tool-calling 配置放在 Langfuse Project LLM Connection。
- 三套 adapter 共用独立 benchmark venv，只安装本地 task/evaluator 必需依赖；上游 revision 固定为 OCB `f5b560356c8c5fff78569307d655f76d9ea9f6f7`、OfficeBench `b978b808667c32b52ce19a67ce1def1de9ae02b7`、PresentBench `2f01aaf2957004f4f136796147e11f7e52d84684`。
- 每个 revision 使用新的不可变 Dataset 名称或固定 version；Dataset metadata 记录 SHA/license、adapter/constraints、Python、LibreOffice、Skill、model 和 evaluator config。实验 fingerprint 写入 Langfuse metadata，不建本地 run 数据库。
- release 固定 `prepare -> estimate -> run_experiment -> Annotation Queue -> export`；失败 item 首版使用带 `parent_run_id` 的 retry Dataset Run，不实现自定义 status/resume 状态机。
- `ci` 完全离线；`office-smoke` 三套各 4 case；`office-release` OCB 全量、固定 OfficeBench subset 和 PresentBench full/50%/25% 独立 Dataset/series。三套结果不合成总分。
- 成本估算和用户确认仍由 Mybot 在调用前完成；实际 token/cost、P50/P95、Score 趋势和 Dashboard 全由 Langfuse 产生。

## 面试怎么讲

### 30 秒回答

> P5 我把 Agent 测试分成纯状态机、无 Key cassette smoke 和 Langfuse Experiment。Cassette 规范化 request 并固定模型响应；TraceHook 在 Provider/Tool 边界创建 Langfuse OTel observation；Langfuse Dataset、Experiment、SDK evaluator、Terra LLM-as-a-Judge、Scores 和 Annotation Queue 负责真模型评估与人工审核，本地只保留文件/Runtime hard gate。

### 高频追问

**LLM 不稳定，cassette 测试还有意义吗？**

Cassette 不是测模型智能是否永远一样，而是固定模型边界后，验证 AgentLoop、工具协议、Policy、交互和恢复逻辑是否回归。真模型质量用另一层 benchmark 测。

**为什么 report 要逐字节比较？**

fixture 和报告都去掉不稳定时间字段后，字节级比较能发现 metric 顺序、字段、结果或基线的任何漂移，成本低且可解释。

**平均成功率 99% 为什么还可能失败？**

因为一次越权外发或敏感泄漏不能被其他高分 case 抵消。安全、数字、文件和 OpenXML 使用 hard gate，任何一个失败 overall 都失败。

**Trace 为什么不保存完整 prompt？**

默认完整 prompt 可能包含用户文档和密钥。hash + chars 足以做变化检测；需要深度调试时应通过受控、脱敏配置另行采集。

## 自测：读完 P5 应该能回答

1. 纯单测、cassette、真模型 benchmark 分别测什么？
2. cassette 怎样既稳定又能发现 prompt/工具协议变化？
3. Trace 与 Eval 的区别是什么？
4. hard gate 为什么不能被平均分覆盖？
5. OpenXML 校验比“文件能打开”多检查了什么？
6. 红队为什么评价攻击后果而不是模型识别率？
7. 当前 Subagent comparison 能证明什么，不能证明什么？
8. 为什么 Langfuse SDK 可以替代本地 Trace/Experiment/Score/Annotation 平台，而不能替代 Mybot 语义埋点和 Runtime 状态源？
9. Experiment Runner、SDK evaluator、Langfuse LLM-as-a-Judge、Code Evaluator 和 Annotation Queue 怎样分工？
10. 为什么本地 Office 文件 evaluator 仍可运行在 Experiment Runner 进程中，却不构成第二套评估平台？

## 对后续阶段的影响

- 选做 P6 Research 若实施，应直接复用 trace/eval，不创建 Research 私有观测通道。
- P7 benchmark 和 README 最终结果页的所有数字都应来自 Langfuse Dataset Run/Score/Annotation 导出，并明确区分 deterministic、fake-provider、官方 evaluator、Terra Judge 和人工审计。
- P8 父子 trace、loop guard 和 single/multi 对比由 P5 统一记录和展示。
