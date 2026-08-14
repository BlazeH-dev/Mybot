# PTC Code Mode：用模型生成程序编排多轮工具调用

## 1. 能力定位

Mybot 的 PTC（Programmatic Tool Calling）不是把多个 JSON tool call 简单合并，而是增加一种
工具呈现与执行协议。原生模式由模型逐轮选择工具，每个中间结果都会进入下一次 LLM 请求；Code
Mode 只向模型暴露 `run_code` 和按当前工具注册表生成的 Python SDK，模型提交一个 async Python
函数体，在独立子进程中完成循环、分支、并发读取和结果筛选。

只有程序的 `print()` 和顶层 `return` 进入模型历史。内部工具调用仍回到宿主 Runner，经过原有
参数校验、Policy、HITL、workspace、网络授权、文件 OCC 和工具实现，不能绕过 Runtime 治理。

## 2. 配置与线协议

`ToolsConfig.mode` 支持：

- `native`：默认值，保持原有完整工具 Schema。
- `code`：线协议只包含保留工具 `run_code`。
- `both`：同时暴露原生工具和 `run_code`，用于灰度对比。

配置示例：

```json
{
  "tools": {
    "mode": "code",
    "ptc": {
      "maxParallelSubCalls": 10,
      "computeTimeoutSeconds": 60,
      "wallTimeoutSeconds": 600,
      "maxOutputChars": 65536,
      "sandbox": "auto"
    }
  }
}
```

`run_code` 参数固定为 `code` 和 `description`。`code` 是 async 函数体，不是完整模块；运行时
注入 `tools`、`ToolCallError`、只提供 `gather`/`sleep` 的 `asyncio` facade，以及安全的
`math`、`json`、单参数 `type()` 和有界结构摘要 `shape()`。SDK 从 `Tool.to_schema()` 稳定排序
生成；工具可额外提供只进入 PTC SDK、不进入 Provider 原生工具协议的 `output_schema`，把返回
对象生成为嵌套 `TypedDict`。不合法的 Python 工具名通过 `tools["name"](...)` 调用。

WebUI 在“设置 → 系统 → 工具调用”提供 Native / Code / Both 三态切换。保存仍走统一的
`PATCH /api/settings`，后端在写配置前校验枚举值；切到 Code 或 Both 时还会预检当前主机能否
构造隔离 worker 的 `LaunchSpec`。预检失败则不落盘。AgentLoop 在每条新消息开始前只刷新
`mode + ptc` 工具呈现快照，因此保存后从下一轮模型请求生效，无需重启，也不重建工具注册表。
正在执行的轮次继续使用启动时快照，避免中途改变工具协议；已存在的会话数据不需要迁移。

## 3. 调用链

```text
AgentRunner
  -> 按 native/code/both 投影 Provider tools
  -> 临时向 system message 追加稳定 Python SDK
  -> 模型调用 run_code
  -> PtcRuntime 启动新 Python 子进程
  -> worker 发送 tool.call JSON Lines
  -> PtcToolScheduler 按 concurrency_safe/exclusive 调度
  -> AgentRunner.execute_embedded_tool
  -> _run_tool_unobserved（原 Policy/HITL/workspace/工具流水线）
  -> tool.result 返回 worker
  -> 仅 logs + returned 成为外层 run_code tool result
```

子调用 ID 为 `<outer_call_id>:ptc:<sequence>`。每个子调用形成 `ptc_subcall` 工具事件，携带父子
ID、参数、状态、耗时、结果摘要和 `cache_hit`；事件通过 WebSocket 实时发送并进入 Trace，但
不会形成 `role=tool` 模型历史。外层事件附带逻辑/实际子调用数、同轮缓存命中、并发峰值、输出
字符和失败类型。

主 Agent 的 `sandbox: auto` 会继承本轮有效的 read-only、workspace-write 或 full-access 模式；
子 Agent 继续按原治理约束收紧为 workspace-write。配置为 `sandbox: none` 时才显式跳过 OS
sandbox，该选项只面向测试和受信环境。

## 4. 并发、审批和恢复

调度器按提交顺序处理：连续 `concurrency_safe` 调用在上限内并发，写入和 exclusive 工具形成
barrier。`policy_gate` 仍对每个子调用独立执行，但不会仅因 gate 存在就把已声明并发安全的读取
退化为串行；写入、exclusive 和需要审批的调用仍通过 barrier/中止语义收口。

一次 `AgentRunner.run()` 内，成功的“同工具名 + 完全相同 JSON 参数”只读并发安全调用可以跨
多个 `run_code` 重试复用。缓存不跨 Agent 运行，不缓存失败、写入或 exclusive 调用；任何写入或
exclusive 调用一提交就推进 mutation epoch 并清空已有读取缓存；即使 barrier 前已启动的读稍后
完成，也只能写入旧 epoch，不能被后续调用命中。它减少程序生成失败后的重复 I/O，不改变 worker
无状态和审批后重新生成程序的契约。

程序正常提前 `return` 时，未启动的排队调用被取消，已启动调用会在外层 `run_code` 返回前完成；
墙钟超时、任务取消、审批中断或 worker 异常时，排队及活跃宿主调用都会取消，并终止、等待整个
worker 进程组。这样不会在模型已经看到 PTC 失败后继续补执行旧程序中的写操作。

工具返回 `ask` 或 `ToolSuspensionResult` 时，PTC 本轮结算为 `approval_required`，沿用现有
`awaiting_*` checkpoint 和 InteractionRequest。首版不序列化 Python 栈或局部变量；批准后由
模型重新提交程序。这避免不可重放的持久 kernel，也明确不承诺程序级 exactly-once。

## 5. 隔离与安全边界

每次运行使用新 Python 进程、`-I` isolated mode、最小环境变量、独立进程组、CPU/墙钟/输出
上限。AST 只兼容白名单 `math/json` import，并改写为预注入模块引用；其他 import、动态执行、
直接文件入口、private/dunder 访问继续拒绝。`sandbox: auto` 通过现有 `SandboxLauncher` 请求
`workspace_write` Seatbelt；不可用时 Code Mode 启动失败，不回退到进程内 `exec`。
`sandbox: none` 只用于测试或明确受信环境。

这些措施是 containment，不是强多租户安全边界。Python 对象模型不是能力安全 VM；对不可信
租户需要容器或 microVM 后端，并同时覆盖 Bash、MCP 等其他执行面。

## 6. 失败分类

运行时区分 `syntax_error`、`exception`、`timeout`、`cancelled`、`output_limit`、`worker_exit`、
`invalid_json` 和 `approval_required`。参数、子调用参数/结果和最终返回值必须是 lossless JSON；
tuple、非字符串字典键、NaN、对象实例或其他会被 Python JSON 编码器强制转换或无法精确编码的
值都会失败关闭。`print()` 使用独立的小日志预算，超长内容会带标记截断，让紧凑最终结果仍可
返回；超大的顶层 `return` 仍以 `output_limit` 失败，并返回有界 `Return shape` 提示模型在同一
程序内聚合。最终渲染文本也受 `maxOutputChars` 硬上限约束。

## 7. 测试证据

- `test_ptc_sdk.py`：三态配置、稳定 SDK、特殊工具名、保留名和输出 `TypedDict`。
- `test_ptc_runtime.py`：async/RPC、null 返回、白名单 import、`type/shape`、print 截断、return
  上限、超时和 sandbox launch。
- `test_ptc_runner.py`：投影、防直呼绕过、并发读取、写 barrier、取消传播、单一模型可见结果、
  同轮读取缓存、策略检查下并发、嵌套事件和内容无关 Trace 摘要。
- `test_settings_api.py`、`test_runtime_refresh.py` 与 `settings-view.test.tsx`：WebUI 三态投影、保存、
  非法值、runtime 预检、下一轮热刷新和无障碍选中状态。
- 原 Runner、ToolRegistry、配置和 WebUI activity 测试继续通过，证明 native 默认行为未回归。

## 8. 真实模型回归

### 8.1 评测问题

模型必须先发现所有日志文件，再读取全部记录并完成确定性聚合。实际提示词约束为：

> 使用工具发现所有 service log，共 8 个服务、640 条请求。对每个服务计算
> `error_rate_pct = status >= 500 的数量 / 请求数 * 100`，并计算 nearest-rank P95：将延迟排序，
> 取一基索引 `ceil(0.95 * n)`。只保留 `error_rate_pct >= 5` 且 `p95_ms >= 300` 的服务，按错误率
> 降序、服务名升序排列。只返回包含 `total_requests` 和 `services` 的紧凑 JSON，不要解释文字。

固定数据如下，每个服务 80 条记录；前 `errors` 条状态为 503，最后 5 条延迟为 `p95_ms`，其余
延迟使用稳定的低位基线序列：

| Service | Errors | Error rate | P95 ms | 是否保留 |
| --- | ---: | ---: | ---: | --- |
| auth | 8 | 10.00% | 420 | 是 |
| billing | 4 | 5.00% | 360 | 是 |
| search | 2 | 2.50% | 510 | 否，错误率不足 |
| profile | 6 | 7.50% | 250 | 否，P95 不足 |
| checkout | 10 | 12.50% | 650 | 是 |
| notifications | 1 | 1.25% | 180 | 否 |
| catalog | 5 | 6.25% | 340 | 是 |
| analytics | 3 | 3.75% | 290 | 否 |

期望结果固定为：

```json
{
  "total_requests": 640,
  "services": [
    {"service": "checkout", "error_rate_pct": 12.5, "p95_ms": 650},
    {"service": "auth", "error_rate_pct": 10.0, "p95_ms": 420},
    {"service": "catalog", "error_rate_pct": 6.25, "p95_ms": 340},
    {"service": "billing", "error_rate_pct": 5.0, "p95_ms": 360}
  ]
}
```

这道题适合验证 PTC，不是因为计算困难，而是因为它同时覆盖“先发现、批量读取、并发、循环、
结构判断、确定性计算、过滤、排序、紧凑返回”。Native 必须让 8 份工具结果进入模型上下文；PTC
可以让 640 条记录只留在 worker 内部。

### 8.2 修复前的失败轨迹

最初三轮评测中，Native 和 PTC 最终都得到 3/3 正确，但 PTC 中位数为 20.07 秒，比 Native 的
15.70 秒慢 27.9%；PTC 中位数需要 6 次 LLM round-trip、5 个外层 `run_code` 和 25 次子调用。
它虽然已减少约 40.8% 总 token 和 94.8% 模型可见中间字符，却被程序失败与重读抵消了时延收益。

一条典型失败链是：

1. 第一个 `run_code` 只发现文件，没有完成读取和聚合。
2. 第二个程序读取全部日志后同时 `print(out)` 和 `return out`，约 109K 中间结果触发
   `output_limit`。
3. 下一次程序使用当时被禁止的 `import math`，得到 `syntax_error`。
4. 程序错误地把读取结果当作列表；真实结构是 `{"service": ..., "records": [...]}`。
5. 为检查结构又调用当时未注入的 `type()`，再次失败。
6. 模型单独提交结构探测程序，再重新读取全部日志。
7. 最后又提交一个独立过滤程序，才得到正确答案。

根因不是“模型不会写 Python”，而是运行时契约和 SDK 提示不足：模型把 `run_code` 当成 REPL；
输出形状没有机器可读契约；常见的安全计算/检查入口缺失；失败后的相同只读调用没有复用；真实
会话总是存在 `policy_gate`，旧条件又让所有安全读取退化为串行。

### 8.3 针对性修复

修复与失败原因一一对应：

| 失败 | 修复 |
| --- | --- |
| 多个短程序逐步探测 | `run_code` Schema 和 system prompt 强制强调“一次完成发现、读取、计算、过滤和返回”，并说明 worker 无状态 |
| 大量 `print` 击穿程序 | `print()` 使用独立小预算并截断；大 `return` 仍失败，错误附带有界 `Return shape` |
| `import math/json` 失败 | 预注入安全模块，AST 只兼容 `math/json` 白名单 import；其他 import 继续拒绝 |
| 裸 `import math` 出现 `math = math` 局部遮蔽 | 裸 import 直接移除并使用注入全局；仅别名/from-import 生成安全赋值 |
| `type()` 不可用 | 注入只接受一个参数的安全 `type()`，不开放三参数动态类构造 |
| 不知道工具返回结构 | `Tool.output_schema` 只进入 PTC SDK，生成嵌套 `TypedDict`；无 Schema 时提供有界 `shape()` |
| 失败后重复读取 | 一次 Agent 运行内缓存成功的完全相同只读调用；写入/exclusive 推进 mutation epoch 并清缓存 |
| Policy 存在时读取串行 | 每次子调用仍逐次经过 Policy，但 `concurrency_safe` 读取可以继续有界并发 |

修复过程中的第一次真实 smoke 又发现了裸 `import math` 的 AST 遮蔽问题。该轮已经做到 8 个日志读取峰值并发 8、真实读取仅 8 次，但首段程序在调用工具前因 `UnboundLocalError` 失败，导致总共 2 个 `run_code`、3 轮 LLM、8.88 秒。修复 import normalization 后再进入正式三轮评测。

### 8.4 正式测试方法

- 模型：`deepseek-v4-flash`，temperature 0，最多 4096 completion tokens。
- 模式：Native 与 Code 各 3 次，按 Native → Code 交替运行，降低服务端时延漂移偏差。
- 工具：一次日志发现延迟 80ms；每次日志读取延迟 180ms；读取工具声明 `read_only` 和
  `output_schema`。
- 策略：两种模式使用同一个 allow 型 `policy_gate`；PTC 仍实际经过嵌入式工具执行流水线。
- PTC：`maxParallelSubCalls=10`，测试环境显式使用 `sandbox=none`，墙钟上限 120 秒。
- 正确性：最终文本解析为 JSON 后与上述期望对象做精确相等比较，不以包含关键词代替。
- 统计：墙钟、LLM 轮次、prompt/completion/total token、模型可见 tool result 字符、外层工具数、PTC 逻辑/实际子调用、缓存命中、并发峰值和失败类型。

### 8.5 三轮原始结果

| Mode | Run | 正确 | Wall | LLM rounds | Prompt | Completion | Total token | 可见工具字符 | 调用结构 | Peak |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| Native | 1 | 是 | 48.913s | 4 | 21,564 | 8,455 | 30,019 | 23,295 | 9 native | - |
| Native | 2 | 是 | 25.653s | 3 | 11,429 | 3,925 | 15,354 | 23,295 | 9 native | - |
| Native | 3 | 是 | 21.394s | 3 | 11,444 | 3,065 | 14,509 | 23,295 | 9 native | - |
| PTC | 1 | 是 | 6.157s | 2 | 3,335 | 789 | 4,124 | 291 | 1 outer / 9 executed | 8 |
| PTC | 2 | 是 | 4.708s | 2 | 3,226 | 551 | 3,777 | 408 | 1 outer / 9 executed | 8 |
| PTC | 3 | 是 | 5.800s | 2 | 3,116 | 539 | 3,655 | 291 | 1 outer / 9 executed | 1 |

Native 第一次出现一次 Provider 空响应恢复，因此范围明显更大。报告中保留该值，不删除“难看”的
样本；中位数可以降低单次异常的影响，但仍同时给出范围。PTC 第三次模型选择顺序读取，峰值并发
只有 1，说明 PTC 的核心收益不只来自并发，还来自少一次 LLM round-trip 和不回传原始日志。

### 8.6 中位数对比

| 指标 | Native 中位数（范围） | PTC 中位数（范围） | 中位数变化 |
| --- | ---: | ---: | ---: |
| 正确率 | 3/3 | 3/3 | 持平 |
| 墙钟时间 | 25.653s（21.394-48.913） | 5.800s（4.708-6.157） | -77.4% |
| LLM round-trip | 3（3-4） | 2（2-2） | -33.3% |
| Prompt token | 11,444（11,429-21,564） | 3,226（3,116-3,335） | -71.8% |
| Completion token | 3,925（3,065-8,455） | 551（539-789） | -86.0% |
| Total token | 15,354（14,509-30,019） | 3,777（3,655-4,124） | -75.4% |
| 模型可见工具结果 | 23,295 chars | 291 chars（291-408） | -98.8% |
| 实际工具执行 | 9 | 9 | 相同 |
| 外层工具调用 | 9 native | 1 `run_code` | 模型工具轮次收敛 |
| PTC 程序失败 | - | 0 | 三轮均一次成功 |

与修复前 PTC 的 20.07 秒、6 轮、5 个 outer/25 个 subcall 相比，修复后 PTC 中位数为 5.80 秒、
2 轮、1 个 outer/9 个实际 subcall，墙钟下降约 71%。但修复前后来自不同采样时段，Provider 延迟、缓存 token 和早期 fixture 序列化长度存在波动；严格性能结论应优先使用同一时段交替运行的正式
Native/PTC 三轮，而把修复前数据用于证明失败链和调用收敛。

### 8.7 结论与边界

这次结果支持三个结论：

1. 对“批量读取后确定性聚合”的任务，PTC 能在不减少真实工具执行次数的情况下，大幅减少模型
   round-trip、上下文字符和 token；收益不是少读数据，而是不让模型反复接收数据。
2. PTC 性能高度依赖程序一次成功率。输出 Schema、清晰的单程序契约和常见安全计算能力不是体验
   优化，而是时延优化的核心组成部分。
3. 同轮只读缓存是失败恢复保险。本次正式三轮均为 0 cache hit，不能把缓存包装成正常路径收益；
   它解决的是模型偶发重试时不重复 I/O。

不能由这一个 Case 推导出“PTC 永远优于 Native”。需要模型观察每个中间结果后重新决策、单工具
调用、强交互审批、长任务 durable recovery 或高副作用工作流时，Native、Plan DAG 或 Subagent
可能更合适。后续应把该 Case 固化为可重复基准，并增加工具失败后恢复、读写依赖链、审批中断和
不同模型的稳定性测试。

## 9. 关键设计取舍

| 选择 | 原因 | 代价 |
| --- | --- | --- |
| 每次新建 worker | 状态隔离、易取消和回收 | 有进程启动成本，不能恢复 Python 栈 |
| 子调用回到 Runner | 复用原生 Policy/HITL/workspace/OCC | RPC 和调度逻辑更复杂 |
| 只返回 print/return | 压缩模型上下文 | 模型不再自然看到每个中间结果 |
| lossless JSON | 避免跨进程类型静默变化 | 不支持 tuple、NaN 和任意 Python 对象 |
| 审批后重新提交程序 | 避免持久化不可重放的 kernel 状态 | 审批前的纯计算可能重做 |

## 10. 与 Native、Plan DAG 和 Subagent 的边界

PTC 解决“一次模型决策内如何高效组合多个工具调用”；Plan DAG 解决“复杂任务的持久依赖和
恢复”；Subagent 解决“独立上下文的并行语义工作”。PTC 程序不是 durable plan，worker 也不是 child Agent。

一个实用决策表：

| 任务特征 | 首选 |
| --- | --- |
| 单次调用或需要模型观察每个结果后重新决策 | Native |
| 批量读取、过滤、循环、简单依赖链，中间结果不值得进模型上下文 | PTC |
| 跨审批、跨进程、需要产物闸门的长任务 | Plan DAG + Checkpoint |
| 可分解且需要独立推理上下文的并行工作 | Subagent |

## 11. 调度器不变量

调度器不是简单 semaphore。它要同时保证：子调用按提交顺序入队；连续安全读最多并发
`maxParallelSubCalls`；写入/exclusive 等待前面读取完成并单独执行；barrier 后的调用不得
越过它；程序结束、审批、超时或取消时，队列与活跃任务必须收口。

这些语义必须与 Native Runner 一致。否则同一工具在 `native` 安全、在 `code` 下却竞态，会让
工具 Schema 的 `concurrency_safe`/exclusive 声明失去意义。

## 12. 性能模型与面试表达

PTC 的理论收益主要来自两处：多个工具之间少了 LLM round-trip，大量中间结果不再重复进入
后续 prompt。成本则包括 worker 启动、RPC、程序生成错误和可观察性复杂度。因此不能只报 token
降低，必须同时比较成功率、质量、round-trip、墙钟时间、token 和失败分布。

30 秒讲法：

> 我把模型多轮工具循环压缩成一次 `run_code`：根据当前工具 Schema 生成稳定 Python SDK，程序在
> 每次新建的受限子进程中执行，子调用通过 RPC 回到 Runner 复用原有 Policy/HITL/OCC。读调用
> 有界并发，写入形成 barrier，中间结果不进模型历史。它是性能优化和 containment，不是权限旁路或强多租户沙箱。

## 13. 自测

1. 为什么 PTC 子调用必须回到 Runner，而不能在 worker 里直接实例化工具？
2. 为什么调度器不能只用一个 semaphore？
3. 审批后为什么不恢复旧 Python 状态？这带来什么重放风险？
4. PTC 和 Plan DAG、Subagent 各自管理哪一层编排？
5. 什么任务不适合 Code Mode？怎样用评测证明选择？
