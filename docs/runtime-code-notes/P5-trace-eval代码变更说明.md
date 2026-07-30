# P5 Trace、Eval 与可观测代码说明

## 先记住一句话

P5 把“系统做了什么”和“结果好不好”拆成两条可核对的链：Trace 记录执行事实，Evaluator 根据事实给分，Langfuse 负责真实运行的观察/评分/审核；本地 hard gate 永远独立于远程软分数。

## 1. 模块地图

```text
AgentHook
  ├─ runtime/trace.py       -> 脱敏 JSONL / OTLP-shaped export
  └─ runtime/langfuse_hook.py -> Langfuse SDK observation

LLMProvider / ToolRunner
  -> generation / tool observation

runtime/replay.py           -> cassette provider
runtime/evals/metrics.py    -> 确定性指标与 hard gate
runtime/evals/report.py     -> 固定排序的 JSON/Markdown 报告
runtime/evals/subagent_compare.py -> single/multi 对比

evaluations/catalog.py      -> suite/profile/adapter/readiness
evaluations/jobs.py         -> 持久 Job、单运行锁、resume/case rerun
evaluations/worker.py       -> 子进程 worker、进度 offset、case checkpoint
evaluations/results.py      -> Langfuse 历史、Score、usage、延迟投影
evaluations/failures.py     -> 主因 + 并发信号分类
cli/benchmark.py            -> prepare/estimate/run/export
```

## 2. Cassette：让 CI 不依赖模型

`CassetteProvider` 实现 `LLMProvider` 接口：

- `record` 模式把请求摘要和真实响应追加到 JSONL；必须显式提供 delegate provider；
- `replay` 模式按顺序读取固定响应，不创建网络 client，也不会调用 delegate；
- 请求摘要包含稳定化后的 messages、模型名和排序后的 tool names，去掉时间、usage、request id 等 volatile 字段；
- hash 不一致时抛出 `CassetteMismatchError`，并输出 unified diff；行数未消费完也会失败。

因此 cassette 验证的是 Agent 的协议行为和工具编排，不是模型质量。模型/提示/工具定义改变时，要求显式重新录制，而不是静默接受旧响应。

## 3. TraceHook：记录执行事实而不泄漏正文

`TraceHook` 接入现有 `AgentHook` 生命周期：

1. `before_run` 创建 `trace_id/span_id`，通过 `contextvars` 让同一异步任务的工具事件拿到上下文；child hook 复用父 `trace_id` 并设置 `parent_span_id`。
2. `after_iteration` 记录 token usage、iteration、tool call 名称和参数摘要、tool event；
3. `after_run/on_error` 记录 stop reason、耗时、输出摘要和错误；
4. `export_jsonl_to_otlp` 将同一 span 的事件聚合成最小 OTLP JSON envelope，便于离线导入。

`_summary()` 只保存 JSON 的 SHA-256 和字符数。路径、prompt、completion、arguments、result 等字段由 `langfuse.py` 的 masking 规则进一步摘要；这让本地证据仍可关联，又避免默认把 Office 正文和密钥上传。

## 4. Langfuse SDK 接入

`LangfuseRuntime` 是薄适配层，不自研 OTLP HTTP 协议：

- 配置 `observability.langfuse.enabled=false` 时不初始化远程 client，普通 Runtime 使用本地 TraceHook；
- enabled 时校验 public/secret key、base URL 和 project 配置，创建共享 client/context；
- `build_span_mask()` 按 `captureContent` 决定保留正文还是只保留安全摘要，并删除 secret/password/api key/token 等字段；
- `emit_langfuse_event()` 把 Runtime 的 policy、interaction、artifact、checkpoint 和 child 事件挂到当前 observation；
- `flush()` 对 OTel、score ingestion、media queue 使用有界等待，并在异常时提供明确错误，而不是无限阻塞网关；
- SDK consumer 线程意外退出时可重建，避免“调用成功但分数永远不落库”。

Benchmark 的 evaluator Score 通过同步 SDK serializer 上传；在这个上下文内，Langfuse SDK 自己于
`run_experiment()` 返回前触发 flush 时仍提交 Trace 和媒体，但不等待与本次同步上传无关的旧后台
Score 队列。完整 Case 结果的 Score 若仍处在最终一致窗口，单 Case 保持 `completed`、
`score_status=pending`，最终 score ingestion queue 的 30 秒 flush 超时也只记录等待状态，不会把
已完成 Job 改成 failed；后续历史轮询直接从 Langfuse 看到落库后的分数。同步 Score 上传失败、
OTEL flush 超时和其他异常仍然 fail-closed，`export` 也继续要求全部必需 Score 实际可读。

Provider 侧使用配置驱动的 `langfuse.openai` drop-in 追踪 OpenAI-compatible generation；Runner 侧为工具调用创建 tool observation。两者共享 OTel context，避免重复创建一条 generation 或丢失 child 归属。

## 5. 本地 Evaluator：硬门不能被平均分掩盖

Runtime evaluator 关注可确定的后果：

- `metrics.py` 检查数字是否来自 verified facts、artifact 是否存在/可回溯、OpenXML ZIP/XML/relationship 是否有效、截图是否空白；
- red-team case 通过实际后果判断：是否越界写文件、读取凭据、执行恶意 MCP、未批准外发，而不是搜索模型输出中的危险词；
- 报告生成器对 fixture/case 稳定排序，计算输入 digest，固定 JSON/Markdown 序列化；CI 重新生成后和提交快照逐字节比较；
- 任一 hard failure 让整体结果失败，不能用其他 case 的平均分掩盖。

OpenXML 校验与视觉 sanity 是互补的：ZIP/XML 能发现结构损坏，视觉检查能发现空白/不可见输出；二者都不能替代人工判断内容质量。

## 6. Benchmark 编排

`nanobot/cli/benchmark.py` 的四个入口职责不同：

| 入口 | 作用 |
| --- | --- |
| `prepare` | 下载/缓存固定 revision，校验 digest、license、constraints、LibreOffice 和 benchmark venv，创建/更新 Dataset |
| `estimate` | 在调用模型前根据样本和模型/Judge 配置估算输入/输出 token |
| `run` | 每个 case 建立 workspace、Trace、checkpoint，调用 `Langfuse.run_experiment()`，写本地 case 状态并上传 Score |
| `export` | 读取已完成 Dataset Run，检查必需 Score 与人工审核，生成去敏 JSON/Markdown 和 README 区块 |

Annotation Queue 在长时间模型运行期间可能被外部删除。审核项入队若收到 Langfuse 404，
`run` 会按原队列名重新查询或创建，并对整批入队重试一次；失败分类将仍无法恢复的队列缺失
标记为 `langfuse_queue_missing`，不会被日志中更早、已经成功重试的模型超时覆盖。

运行阶段禁止下载。`_case_result_path()` 以 job/run/benchmark/skill/model/case 等稳定字段定位 checkpoint；source digest、model fingerprint 或 evaluator revision 改变时不会错误复用旧输出。模型错误不会写成 completed case，避免恢复时把失败当成功。

## 7. Job、resume 和失败分类

`EvaluationJobStore` 持久化 job JSON；`EvaluationJobService` 负责排队、全局单 active run、cancel、resume、case rerun 和终态删除。worker 通过 progress JSONL 和 offset 增量更新 case，重启后不会重复消费历史事件。

`results.py` 从 Langfuse 读取最新 terminal item，按 case identity 去重，并聚合 generation usage、TTFT/latency、Score 和 Judge rule name。只有分数齐全的远端 item 才能复用；刚完成但 Score 尚未可读的 Case 只显示 pending，不触发付费模型重跑。`failures.py` 从 stderr/状态中抽取真正终止 Job 的主因，同时保留 503、timeout、score missing 等并发信号，UI 才能告诉操作者该修配置、重试还是 Resume。

旧版本已落盘的 false failed Job 若全部 Case 都是 `completed`，且唯一失败原因是 Score 回读或
score ingestion queue 延迟，`jobs.py` 在读取投影时将其兼容显示为 `awaiting_review`；任一 Case
真实失败时绝不做该转换。

删除操作区分本地 job、私有 artifact 和远端 Dataset Run；活动中的 job 拒绝删除，远端删除失败不会假装本地历史已清理。远端删除成功后只从历史缓存中剔除目标 Dataset Run，其他行继续作为 stale-while-revalidate 快照返回；进程内 tombstone 还会过滤删除前已启动的旧刷新结果，避免页面轮询期间所有远端历史短暂消失。

## 8. 评测中心为什么不复制 Langfuse

WebUI 的 Evaluation Center 只做控制面：读取 catalog/readiness，提交 Job，展示阶段和 case 进度，提供 resume/case rerun/cancel/delete，以及 Dataset Run/Trace/Annotation Queue deep link。Score、Trace、Judge reasoning 和人工审核仍以 Langfuse 为真相源，避免 Mybot UI 和云端产生两套互相矛盾的结果。

## 9. 当前边界

- P5 Core 的确定性指标和 cassette 可离线运行；真实模型质量、价格和 P95 不能从 fake-provider baseline 外推。
- Langfuse Cloud、Terra Judge、数据许可和 Annotation Queue 需要外部配置；未完成时 README 不能发布真实分数。
- 自有 OCB Judge 结果标记为 `Mybot evaluation`，不称 `official-comparable`；官方 evaluator 与自有 fallback 必须分开记录。
- 远程 Score 不会反向修改本地 hard gate；Cloud 故障会让 benchmark 显式失败或停在待审核，不会静默当作通过。
- 不上传原始 Office 文件、完整 prompt、密钥或个人信息；`captureContent=true` 只能在有许可的临时 smoke 中使用。

## 10. 验证证据

```bash
pytest tests/runtime/test_replay_trace_eval.py \
       tests/runtime/test_langfuse_observability.py \
       tests/evaluations/test_evaluation_contract.py -q
pytest tests/runtime/ -q
```

重点回归包括 cassette 无网络 replay、trace 脱敏/OTLP、bounded flush、Score 同步上传、消费线程修复、历史去重、resume/case rerun、模型错误 checkpoint 隔离、远端 score 回读和失败分类。

## 面试怎么讲

### 30 秒回答

“P5 把可观测和评估拆开：TraceHook 通过 AgentHook 记录每轮模型、工具、Policy、Interaction 和 artifact 的摘要，Cassette 让 CI 不依赖真实模型；本地 evaluator 负责数字、OpenXML、恢复和安全 hard gate，Langfuse SDK 只负责真实运行的 observation、Dataset Run、Judge 和人工审核。Benchmark 用稳定 fingerprint 和 case checkpoint 支持 resume，但远程软分数永远不能覆盖本地安全失败。”

### 高频追问

**为什么不直接把全部 prompt 上传 Langfuse？**

默认内容捕获关闭，trace 只保留摘要/hash；这降低敏感数据泄漏和跨境风险，也足以定位调用关系、耗时和失败类型。需要内容评估时必须经过许可并临时打开。

**为什么 Score 上传要同步且有界？**

后台队列可能在网络异常时卡住。benchmark 需要知道分数到底是否落库，所以复用 SDK serializer 做同步上传，超时或失败显式终止，不能生成“看起来完成”的 export。

**resume 如何避免重复副作用？**

本地 checkpoint 绑定 source/model/evaluator fingerprint；已完成 case 只有在远端必需 Score 齐全时复用，未知状态不自动重放，必要时进入人工 recovery。
