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

## 2026-08-11：Plan DAG Trace 与回归门

- `PlanTool` 和 `AgentLoop` 发出 `mybot.plan.*` DAG 生命周期事件；完成后不再创建额外 Reviewer trace。
- `tests/runtime/test_plan_dag.py` 覆盖 DAG cycle/layers、计划 Markdown、确定性 complete、child parallel dispatch、artifact tamper 和 orphan child recovery。
- `tests/runtime/test_interactions_approvals.py` 覆盖参数绑定审批、recovery 回填和审批原因翻译键；真实 `SubagentManager` lifecycle 测试覆盖 completion callback 的 node/result 传递。
- WebUI interaction 回归验证中文 HITL 展示与英文协议值提交相互独立；plan/artifact checksum 和节点终态仍由 Runtime 硬判定。

## 2026-08-11：Subagent 用户可见 activity 投影

- child 的脱敏 TraceHook/Langfuse observation 仍是诊断和评估真相源；新增 WebUI `subagent_activity` transcript 只服务用户查看实时执行过程。
- activity 绑定 task/hash/node/child，记录 phase、iteration、elapsed、usage、reasoning、tool start/end 和终态；WebUI replay 合并为 refresh-safe snapshot。
- 回归覆盖 child activity 不进入主消息、并行 child 独立聚合、工具阶段合并、终态恢复和 stale revision 过滤。

## 2026-08-11：WebUI 本轮 Trace 面板

- `AgentLoop` 将 WebUI 稳定 `webui_turn_id` 与 session key 传入 `TraceHook` / `LangfuseTraceHook`；本地每条 JSONL event 增加 `mybot.webui.turn.id`、`mybot.session.id`，Langfuse 写入同名 observation metadata，因此 UI 不再依赖时间猜测本轮。
- `runtime/trace_reader.py` 是只读投影层：本地模式只扫描当前 session workspace 的 `.nanobot-runtime/trace/*.jsonl`，Langfuse 模式按 session 和 turn metadata 查询远端 trace/observation；两种真相源仍互斥，不复制远端正文到本地。
- `/api/sessions/<key>/trace?turn_id=<id>` 要求 gateway API token，校验 session/turn 格式并使用当前 session workspace；返回 span tree、event、usage、duration、状态和可选 Langfuse deep link。
- API 投影在已有 Trace/Langfuse masking 之外再次过滤 secret/password/api key/token，并统一遮蔽 content/prompt/completion/message/input/output/result 字符串；WebUI 不展示完整 prompt/response。
- WebUI `TracePanel` 由聊天页右上角 Activity 图标打开；运行中每 1.5 秒刷新，历史轮次打开时读取最新 user turn id，展示本地或 Langfuse 来源、span/event、token、耗时和错误状态。
- 设置页“系统 → 可观测性”提供 `observability.langfuse.enabled` 开关；设置响应仅投影 enabled、凭据是否完整和 base URL，密钥不返回浏览器。开启前必须已有 public/secret key，保存后以独立 `observability` section 标记引擎重启；重启后在 Langfuse 与本地 JSONL 两个互斥真相源之间切换。
- 回归覆盖 session/turn 隔离、短文本二次脱敏、非法 turn id 拒绝、API URL 编码、面板加载和已有 ThreadShell 行为；定向后端 27 项、前端 55 项通过，Python/WebUI lint 与生产构建通过。

## 2026-08-11：Office smoke prepare 状态提示

- 评测 preflight 将“prepared 文件不存在”和“prepared 已存在但许可内容未上传”作为互斥状态。
- 未准备的 profile 只提示先执行 prepare；只有读取到 `licensed_content_uploaded=false` 时才提示 licensed prepare，避免同一次 readiness 返回互相矛盾的两个原因。
- 该修复只校准状态投影，不放宽 licensed content、Langfuse `captureContent`、模型凭据、LibreOffice 或 Dataset 完整性硬门。

## 2026-08-11：prepare 下载有界恢复与阶段进度

- 已缓存的 benchmark source 只有在 HEAD 等于 pinned revision、工作树干净且 LICENSE digest 匹配时才直接复用；满足条件时不再无条件 `git fetch`，避免 GitHub 不可达阻塞重复 prepare。
- Hugging Face 官方 endpoint 使用有界连接、下载和子进程超时；单次连接/下载保持 10/120 秒超时，整批 snapshot 总时限为 2 小时，避免 release 多 GB 许可资产的正常传输被 smoke 级总时限中断。未显式配置 `HF_ENDPOINT` 时，官方源连续失败后自动回退 `https://hf-mirror.com`，仍按固定 dataset revision 下载并由后续 manifest digest/fingerprint 校验。
- benchmark constraints 固定加入 `pandas==2.3.2`，满足上游 OCB `download_and_convert_files.py` 的 parquet manifest 读取依赖；依赖仍只安装在外部 benchmark venv。
- benchmark constraints 固定加入 `curl_cffi==0.13.0`；licensed prepare 通过运行时 wrapper 仅替换 pinned 上游 downloader 的 HTTP GET 实现，以 Chrome TLS 指纹访问会拦截普通 Python 客户端的官方来源。原始 URL、SEC 联系标识、Adobe 转换、文件格式校验、manifest 与 fingerprint 口径保持不变。
- blocked source 会继续尝试 Safari、Firefox 与 HTTP/1.1 指纹；`pypdf==6.15.0` 只处理空密码可解的 PDF 容器加密，再调用同一 Adobe Export PDF API。真实 404、不可解密 PDF 或全部网络策略失败仍由资产完整性硬门拒绝。
- 标准 `requests` 仍优先处理正常响应，只有明确的 403/503、拦截 HTML 或 TLS 错误才进入浏览器指纹路径；缺失资产按文件隔离到独立有界子进程，单个站点触发原生崩溃时其余文件仍可继续缓存，最终完整性校验仍 fail-closed。
- Office release 正式口径改为原固定分层 255 候选中的可执行子集：固定排除受 36 个失效/受限来源影响的 41 个 Case，并追加排除任务/参考证据契约或稳定产出异常的 Case 0、4、7，不补入其他 Case，最终每模型 211、双模型 422。Prepare manifest、Dataset、fingerprint、Job 预估和运行 checkpoint 均以同一 211 条身份集合为准；已有 DeepSeek 其余 211 条完成 checkpoint 必须复用，不得因口径收缩而重跑。
- Adobe 转换通过运行时 wrapper 向官方 SDK 注入 `ClientConfig(connect_timeout=30000, read_timeout=120000)`，对仍缺失的目标文件最多重试 3 次；不修改 pinned OCB checkout，也不把本地替代转换混入 benchmark。
- Adobe 运行环境优先保留显式 `CHROMIUM_PATH`，否则查找 PATH 内的 Edge/Chrome/Chromium，并在 macOS 自动识别 `/Applications` 下的标准安装，确保 HTML 许可源仍使用上游规定的 Chromium→Adobe 转换链。
- 许可源下载/Adobe 子进程将 `getaddrinfo` 结果中的 IPv4 候选排在 IPv6 之前，但不删除 IPv6 候选；这避免本机无路由的 IPv6 CloudFront 在 123 个 release 直接下载项上重复耗尽连接超时，仍保留 IPv6-only 源站的回退能力。
- `prepare_stage` 进度覆盖 pinned source、benchmark venv、Dataset metadata、smoke manifest、licensed references、Langfuse Dataset、Cloud smoke 和最终 fingerprint；worker 将阶段投影到 Job `phase/current_variant`，页面不再把网络挂起伪装成无信息的 `preparing`。
- 镜像只解决公共 OCB 资产传输，不改变 licensed upload、Adobe 转换、Langfuse Japan Cloud 或模型评测硬门。

## 2026-08-11：本地 Trace 测试隔离

- `tests/agent/test_task_cancel.py` 的最小 AgentLoop 改为接收 pytest `tmp_path` 作为真实 workspace，不再用 `MagicMock` 伪造路径；本地 `TraceHook` 写入 `.nanobot-runtime/trace/` 时因此始终位于 pytest 临时目录，不会将 mock 的字符串表示物化为项目根目录下的 `MagicMock/`。
- `.gitignore` 排除 nanobot 本地 workspace 模板、cron、memory 及 `.nanobot-runtime` 状态。网关被临时指向源码仓库时，工作区初始化和本地 trace 不会进入 Git 待提交列表。

## 2026-08-12：benchmark 媒体队列延迟恢复

- Langfuse 的 Office 媒体附件由独立后台队列上传；模型 Case、结构化输出和 Dataset Run item 已完成后，该队列仍可能因网络吞吐在 30 秒 flush 窗口内保留大量任务。
- `_flush_benchmark_runtime()` 将 `media upload queue` timeout 与已有的 `score ingestion queue` timeout 同样视为远端最终一致延迟，打印明确告警并允许后续 variant 继续；恢复时仍复用同一 Job、稳定 Dataset Run 和 Case checkpoint。
- OTEL trace flush timeout、同步 Score 上传错误和未分类异常继续 fail-closed，避免把 Trace 或评分真相源丢失误报为成功。
- `tests/cli/test_benchmark_contract.py` 覆盖 score/media 队列可恢复、OTEL flush 仍抛错的分类契约。

## 2026-08-12：双模型历史结果独立投影

- `/api/evaluations/runs` 从 Job Case 和已关联 Langfuse Dataset Run 投影 `model_runs`，每个模型独立统计成功、失败、剩余、状态、得分、用量和链接。
- 运行中的模型即使尚未获得 Dataset Run ID 也会立即出现；远端 Run 可用后按 `model_preset` 绑定，不改变 Job、Run 或 checkpoint。
- 已被本地模型行关联的 Langfuse Run 不再重复显示，模型行展开只返回该模型的 Case 视图。
- 模型投影只适用于 `action=run`；prepare 只记录准备状态，不再产生没有 Case/Score 的模型结果行。
- worker 实时事件只携带原始 Job 时，前端合并更新并保留轮询接口已有的 `model_runs`，因此运行中的 DeepSeek/Luna 独立行不会在进度事件到达时消失。
