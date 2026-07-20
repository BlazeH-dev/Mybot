# P5 Cassette、Trace、Eval 与安全红队代码说明

> 对应计划：`docs/plans/runtime-steps/P5-trace-eval.md`
> 当前状态：S5.0 与 Core 已完成（2026-07-18）。LLM Judge/Verifier、真模型多模型矩阵和 KV cache 优化未实现。

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

因此可以在 Jaeger/Langfuse 等标准查看器里还原父子关系。

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
- 没有真实 DeepSeek/GPT 多模型质量-价格矩阵。
- 没有 KV cache 优化结论。
- visual sanity 不是审美或排版质量评分。
- evidence metric 依赖固定测试产生的证据，不是生产监控万能扫描器。
- committed fake-provider Subagent 对比不能证明真实模型一定更快。

## 面试怎么讲

### 30 秒回答

> P5 我把 Agent 测试分成纯状态机、无 Key cassette smoke 和手动真模型 benchmark。Cassette 规范化 request 并固定模型响应，prompt 或工具协议变化会给可读 diff；TraceHook 复用现有 Hook 输出脱敏 OTel-shaped JSONL，并支持父子 span 和 OTLP 导出；Eval 注册 12 个 metric，对数字、文件、OpenXML、权限、OCC、HITL 和 Subagent 做硬门，红队看越权副作用是否为零，而不是只看模型有没有识别攻击。

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

## 对后续阶段的影响

- P6 Research 应直接复用 trace/eval，不创建 Research 私有观测通道。
- P7 benchmark、README 和 demo 的所有数字都应来自这里的可复现报告。
- P8 父子 trace、loop guard 和 single/multi 对比由 P5 统一记录和展示。
