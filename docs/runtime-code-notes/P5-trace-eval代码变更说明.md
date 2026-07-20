# P5 Trace / Eval 代码变更说明

> 对应计划：`docs/plans/runtime-steps/P5-trace-eval.md`
> 当前状态：S5.0 与 P5 Core 必做项已完成（2026-07-18）；Judge/Verifier、多模型成本矩阵和 KV cache 为选做，未实现。

## S5.0 回放

- `nanobot/runtime/replay.py` 提供 record/replay `CassetteProvider`，规范化 request hash，剥离随机 id/时间/usage，失配输出可读 diff。
- 已提交 4 条 cassette：
  - `plan_automatic.jsonl`
  - `plan_explicit.jsonl`
  - `interaction_deadlines.jsonl`
  - `checkpoint_conflict.jsonl`
- replay 不需要 API key、不访问网络，并要求按序消费完整。

## Trace

- `nanobot/runtime/trace.py` 通过现有 Hook 追加 OTel-shaped JSONL：trace/span/parent、actor、model、usage、tool 摘要、状态、耗时和错误。
- 默认只保存输入/输出 SHA-256 与字符数，不写原始敏感正文。
- policy、plan、InteractionRequest、approval、checkpoint/recovery、Subagent spawn/complete/cancel/loop-guard 通过 `mybot.*` event 记录；spawn 明确记录 child 未启用工作量配额。
- typed interaction 保存 `resolved_at`，恢复 trace 写入 `mybot.human_wait_ms`；等待时间与模型/工具 run duration 分离。
- 主 Agent 与 child 使用同一 trace id，child span 的 `parent_span_id` 指向父 span。
- Runtime 只对真实 `Path` workspace 创建 trace hook；旧单元测试中的 `MagicMock` path double 不再在仓库根生成伪路径临时目录。
- `nanobot/runtime/export_trace.py` / `export_jsonl_to_otlp` 将 JSONL 转为 OTLP JSON，可交给 Jaeger/Langfuse/collector。

## Eval 与红队

- `nanobot/runtime/evals/metrics.py` 注册 12 个确定性 metric：artifact、openable、数字/fact id、OpenXML、visual、replayability、policy、OCC、interaction、approval、subagent、untrusted content。
- `openxml_validation` 实际检查 ZIP CRC、必需部件、所有 XML/REL 可解析、Content Types 与 relationship 目标存在。
- `visual_sanity` 实际打开截图，核对页数、尺寸并拒绝明显纯色空白页。
- `data_consistency` 要求每个 expected quantitative key 都有 fact id。
- `tests/fixtures/runtime_eval/` 固定 5 个 case；`tests/fixtures/redteam/` 覆盖会议纪要、xlsx 单元格、workspace/删除诱导、恶意 MCP、child 绕过。
- 红队验收后果：未批准写入、敏感读取/泄漏、未确认外发、恶意 MCP 执行和 child 权限扩大均为 0。

## 报告与 CI

- `nanobot/runtime/evals/report.py` 输出 JSON + Markdown，hard gate 不被平均分覆盖。
- eval 报告按路径排序输入并记录规范化 `fixture_digest`，不写入墙钟时间；同一 fixture 集可字节级重生已提交报告。
- `nanobot/runtime/evals/subagent_compare.py` 输出 single/multi 成功率、wall/P95、token、成本、父上下文、失败/取消/循环熔断对比。
- 已提交：`benchmarks/latest.json`、`benchmarks/latest.md`、`benchmarks/subagent-comparison.*`。
- `.github/workflows/ci.yml` 安装 Bubblewrap，运行 `tests/runtime/`，在 `/tmp` 生成 eval 与 Subagent 报告，并与 `benchmarks/` 基线逐字节比较。
- 当前基线：5 cases、0 hard failures、overall PASS；runtime 本地为 `56 passed, 1 skipped`、1.25 秒，满足确定性 CI `<60s` 门。
- 扩展回归：受影响后端 `236 passed`，WebUI 交互 `51 passed`、lint/build 通过。全量历史套件为后端 `4162 passed, 19 failed, 5 skipped`、WebUI `406 passed, 2 failed`；失败集中在本阶段外的内置模型/Provider/facade 旧期望和繁中资源/旧附件 accessible-name 基线。

## 选做项

LLM Judge/Verifier、真模型多模型质量/价格矩阵、KV cache 调优没有进入 Runtime 或 CI，也不能推翻确定性安全/数字/文件/OpenXML 失败。
