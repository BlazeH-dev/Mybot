# P5 Trace 与 Eval Harness

> 状态：待执行。S5.0 紧随 P3；P5 Core 在 P4/P8 后收口。Judge/Verifier、多模型和 KV cache 为选做。
> 出口：关键 Agent 行为可回归，任务可追踪，安全/数字/文件硬门可量化。

## 1. 三层测试结构

1. 纯脚本/状态机单测：事实、schema、policy、OCC、artifact、预算。
2. 轻量 cassette Agent smoke：无 API key、无网络复现关键模型行为。
3. 真模型 benchmark：手动跑质量、成本、时长和模型差异，不进入 CI。

确定性硬失败不能被总体成功率或 LLM 软评分平均掉。

## 2. S5.0 轻量 Cassette（必做）

`runtime/replay.py` 包装 `LLMProvider`：

- record：规范化 request 摘要、response、工具调用和 InteractionRequest/control event，追加到 `tests/fixtures/cassettes/<case>.jsonl`。
- replay：按序返回 response，弱校验规范化 hash/关键字段；失配输出可读 diff。
- 剥离时间戳、随机 id、usage 等易变字段。
- 不做自动重录、多模型 cassette、复杂流式 chunk 对齐或全量会话 VCR。

首批只保留 3–4 条高价值路径：

- plan-only create→confirm→update→complete，以及普通 WebUI create→automatic activation→update→complete；
- required/auto_resolve/expire_and_deny 的回答或 deadline 恢复；
- approval/注入诱导越权副作用防护与文件冲突；
- checkpoint 恢复。Subagent 权限/预算优先用 fake provider 确定性测试。

验收：replay 不发网络请求，等待期间 provider 未被调用，prompt 行为变化可读失败，总 CI < 60 秒。

## 3. TraceHook（P5 Core）

`runtime/trace.py` 通过 `AgentHook`/`CompositeHook` 追加 JSONL，不重写 Runner。

每个 task 记录：

- trace/span/parent、actor、状态、耗时和错误；
- model、usage、缓存 token；
- plan、tool call/result；
- policy、InteractionRequest 策略/等待/回答/超时、approval；
- file conflict、input/artifact/checkpoint；
- Subagent 预算、child span、汇总与取消。

字段对齐 OpenTelemetry GenAI 语义约定；自有字段使用 `mybot.*`。默认脱敏输入输出摘要。人类等待时间与模型/工具执行时间分开。

提供 JSONL→OTLP 导出脚本或明确映射文档，使用 Jaeger/Langfuse 查看，不自研 trace 页面。

验收：一次计划任务 span 树完整；使用 child 时父子关联、usage、artifact 和 InteractionRequest 可核对。

## 4. 确定性 Eval 与报告（P5 Core）

`runtime/evals/` 使用注册式 metric：

```text
artifact_completion
file_openable
data_consistency
openxml_validation
visual_sanity
replayability
policy_compliance
file_conflict_safety
interaction_resume
approval_binding
subagent_governance
untrusted_content_safety
```

接口：`score(case_ctx) -> {passed, score, issues}`。输入为 artifacts、trace 和 P0 expected。

- Office 数字、权限、文件、OpenXML 和副作用使用确定性硬门。
- visual_sanity 首版只检查 screenshot、页数、空白和明显尺寸异常。
- report 输出 JSON + Markdown；固定 fixture 扩到至少 5 个不同数据/约束/异常 case。
- CI 跑确定性 smoke；真模型和完整红队由 benchmark 手动执行。

验收：≥5 个 case 产出可读报告；输入+facts+Skill 中间产物+引擎版本可重放语义等价成品。

## 5. 安全红队（P5 Core）

`tests/fixtures/redteam/` 至少覆盖：

- 会议纪要中的间接注入；
- xlsx 单元格中的数据载体注入；
- workspace 越界、覆盖源文件、删除诱导；
- 恶意 MCP 描述；
- 通过 child 绕过父策略。

验收对象是攻击后果，不是“识别率”：越权写入、敏感读取/泄漏、未确认外发和 child 绕过均为 0，攻击尝试进入 trace。选 1–2 条代表路径进入 cassette CI，完整集保留 benchmark。

## 6. 选做增强

### LLM Judge / Verifier

- 只评会议覆盖、文案、风格和轨迹合理性，不进入默认 Runtime。
- 从 trace 构造脱敏 `problem + trajectory`；Verifier backend 独立配置且要求 logprobs。
- 先对 20–30 个 case 离线 compare/track；select 只比较已经隔离生成、无副作用的候选。
- 记录与人工/确定性结果的相关性、误判、额外成本和 P95；无真实增益就删除。
- 软评分不能推翻数字、权限、文件、OpenXML 或副作用硬失败。

### 成本与多模型

- 价格表记录采集日期；trace 聚合每任务 token、成本和 P50/P95。
- 同一 case set 手动跑 DeepSeek V4 Pro/Flash 与 GPT-5.6 Sol/Terra/Luna 中至少两个能力档，输出模型×成功率×成本×时长矩阵。
- 不进入 CI；结论写入 metrics baseline。

### KV Cache

- 记录 prompt cache hit/miss tokens，区分会话内与跨任务命中。
- 先测基线，再最小调整 ContextBuilder：稳定 system/skills/tools 在前，动态状态在后。
- 优化后必须重跑同一 eval，成功率不得下降。

## 阶段出口

- [ ] 关键 cassette 无网络通过，三档 HITL、计划、冲突、恢复有回归。
- [ ] JSONL trace 对齐 OTel，父子 span 与人类等待时间可查。
- [ ] ≥5 个确定性 case 和红队硬门进入报告/CI。
- [ ] 数字、权限、文件、approval 超时、恢复和 replayability 有基线。
- [ ] 越权副作用、泄漏、未确认外发和 child 绕过为 0。
- [ ] 使用 Subagent 的任务有单/多 Agent 成功率、时长和 token 对比。
- [ ] trace 可导出标准查看器；eval 报告可读。
- [ ] 选做项未完成不影响 P5 Core 出口。
