# P5 Trace、Eval 与可观测

> 状态：P5 Core 已完成；P5.1 代码已完成，真实 Langfuse Cloud smoke、人工审核和公开结果发布待外部凭据与许可配置。
> 本文是实施计划和验收标准。历史决策、废弃方案和每次运行日志不放在这里。

## 1. 阶段目标

P5 要回答两个问题：Runtime 是否按安全契约执行，Office 任务的结果是否可重复、可解释、可比较。

硬门与真模型质量分开：本地确定性测试负责安全、文件、数字、OpenXML、恢复等不可妥协的行为；Langfuse 负责真实模型的 trace、Dataset Run、Judge、人工审核和趋势分析。远程评分不能覆盖本地 hard failure。

## 2. 固定职责

| 层 | 负责内容 | 事实来源 |
| --- | --- | --- |
| `runtime/replay.py` | 无网络 cassette record/replay | cassette 文件 |
| `runtime/trace.py` | Mybot 事件、脱敏摘要、父子 span、本地 JSONL/OTLP | Runtime task |
| `runtime/langfuse.py` | Langfuse SDK 初始化、masking、flush、score 上传 | Langfuse SDK |
| `runtime/langfuse_hook.py` | AgentHook 到 SDK observation 的适配 | AgentHook |
| `runtime/evals/*` | hard gate、报告、单/多 Agent 对比 | 本地 evaluator |
| `evaluations/*` | benchmark catalog、Job、断点、失败分类、结果投影 | 本地 Job 状态 + Langfuse 只读结果 |
| `cli/benchmark.py` | `prepare/estimate/run/export` 编排入口 | CLI 参数与 Langfuse |
| WebUI Evaluation Center | 选择、进度、case 状态、恢复/删除、deep link | HTTP API，不复制 trace 真相 |

## 3. 实施步骤

### S5.0：确定性回放与 hard gate（已完成）

1. 为代表性 Agent 行为录制固定 cassette，replay 时禁止网络和 delegate provider。
2. 在 `TraceHook` 中记录 run、iteration、tool、policy、interaction、artifact、checkpoint 和 parent/child 关联；正文、参数和结果只保留摘要/hash。
3. 固定五个 runtime eval case，验证数字、文件存在性、OpenXML ZIP/XML/relationship、视觉非空、安全红队和恢复语义。
4. 报告按固定 fixture 排序生成，和仓库快照做字节级比较；任一 hard gate 失败即整体失败，不用平均分掩盖。

### S5.1a：Langfuse 可观测（代码已完成）

1. 从配置创建 Langfuse client；默认关闭远程 sink，保留本地 JSONL 调试路径。
2. 通过 OTel context 关联 task/session、Agent/child、generation、tool、Policy、Interaction、artifact 和 checkpoint。
3. 对 OpenAI-compatible provider 使用配置驱动的 `langfuse.openai` drop-in；Runner 对每次模型请求和工具调用创建一条 observation，避免只记录粗粒度总 span。
4. 对输入、输出、密钥、路径和敏感字段执行 allowlist/摘要化；SDK 负责批处理、重试和 flush，Mybot 为 flush 设置有界等待。
5. Cloud 故障只降级普通 Runtime；benchmark 的 score 上传、必需结果回读和 preflight 失败必须显式报错。

### S5.1b：Benchmark 与评估（代码已完成）

1. `EvaluationCatalog` 描述 suite、profile、benchmark、Skill、model preset、runtime profile 和可用性原因。
2. `prepare` 锁定外部 revision、资产 digest、LibreOffice 版本和 benchmark venv；运行阶段禁止下载。
3. `estimate` 在调用模型前输出样本数和输入/输出 token 估算；`run` 通过 SDK `run_experiment()` 执行每个 case。
4. 每个 case 以本地 checkpoint 原子写入；成功 case 可复用，模型错误、配置错误和未完成 case 不能伪装成成功结果。
5. evaluator 只写确定性 hard gate 和约定的 Score；OCB/需要语义判断的结果由固定 Terra Judge 或人工审核单列，不能和 hard gate 合并成一个不可解释总分。
6. `resume` 只重跑未完成 case；`case rerun` 只重跑指定 case 并创建清晰的尝试记录；`export` 只读取已完成且审核齐全的 Dataset Run。

### S5.1c：评测中心（代码已完成）

1. HTTP API 暴露 catalog、readiness、Job、case、resume、cancel 和删除操作。
2. Job 状态机区分 queued、preflight、preparing、estimating、running、remote_scoring、awaiting_review、completed、failed、cancelled。
3. 进度从持久化 JSONL offset 增量消费，历史 case 去重并保留最新 terminal attempt；单 active run 锁防止同一 profile 并发污染。
4. 失败分类保留主因和并发信号，UI 展示原始错误、可重试性和恢复入口。

## 4. Benchmark 运行顺序

```text
ci (离线硬门)
  -> prepare (锁定 revision/资产)
  -> readiness + estimate (凭据、许可、token)
  -> run (Agent + evaluator)
  -> remote scoring / human review
  -> export (去敏快照与 README 结果区块)
```

`ci` 不需要 API key、网络或 Langfuse。`office-smoke` 用少量固定 case 验证完整链路；`office-release` 只在 smoke 通过后运行。OCB 结果必须标记 evaluator 来源（官方或 `Mybot evaluation`），不能把自有 Judge 写成官方可比成绩。

## 5. 外部配置前置

真实 smoke 前必须确认：

- Mybot 被测模型 provider 可调用，Judge 使用的 Terra connection 可调用；
- Langfuse 项目、区域 endpoint、SDK key 和 dataset/score 配置正确；默认 `captureContent=false`，只有经过许可审查才允许临时打开；
- benchmark 资产 revision、许可证、缓存 fingerprint 和 LibreOffice 版本固定；
- 结果需要的 Score、Annotation Queue 和导出权限可回读；
- 导出内容已去除 API key、原始 Office 文件、个人信息和不允许跨境的数据。

任何一项不满足都停在 preflight/prepare，不调用真模型，不发布 README 数字。

## 6. 阶段出口

- [x] cassette 可在无网络环境重放，request mismatch 有可读 diff。
- [x] 本地 runtime hard gate、Trace、OpenXML/视觉校验、红队和报告重生通过 CI。
- [x] Langfuse observation、masking、bounded flush、Score 上传和失败可见性有离线测试。
- [x] benchmark 可 prepare/estimate/run/resume/case-rerun/export，Job 与 WebUI 状态一致。
- [ ] 至少一个真实 Dataset Run 完成 Score 回读、人工审核和去敏 export；完成前 P7 只能展示“外部证据待配置”。

## 7. 明确不做

- 不自建第二套 Trace、Dataset、Score 或标注数据库。
- 不把 LLM Judge 放进本地安全硬门，不用软分数覆盖权限、数字或文件失败。
- 不在 benchmark 运行中隐式下载资产、安装 latest binary 或修改输入。
- 不把旧 Python Office Skill、旧周报 DSL、已放弃的独立评测 UI 和历史命令恢复回来。
