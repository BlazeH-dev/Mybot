# Runtime Metrics Baseline

> 最新确定性报告：`benchmarks/latest.json` / `benchmarks/latest.md`
> 采集日期：2026-07-30

## P5 Core 硬门

| 指标 | 当前值 | 口径 |
| --- | ---: | --- |
| 确定性 case | 5 | `tests/fixtures/runtime_eval/*.json` |
| Hard failures | 0 | 任一安全/数字/文件/OpenXML hard gate 失败即整体失败 |
| Overall | PASS | 不使用平均分掩盖 hard failure |
| Runtime suite | 76 passed, 1 skipped | skip 为 macOS 本机无法运行 Linux Bubblewrap real smoke；CI 安装 Bubblewrap 后执行 |
| Runtime suite 本地耗时 | 4.41s | Python 3.11，本机定向运行；CI 门为 `<60s` |
| 未批准 workspace 外写入 | 0 | policy/OCC/red-team 后果测试 |
| 未批准命令网络访问 | 0 | restricted 默认断网；批准 curl 绑定 command/domain/port/DNS IP/expiry |
| 未确认外发 / 恶意 MCP 执行 | 0 | external/MCP 默认 ask，红队测试验证未执行 |
| 已有文件冲突拦截 | 100% | 未读、读后变化、mtime 不变 hash 变化、多文件 preflight |
| `expire_and_deny` 自动放行 | 0 | deadline 只产生 expired/denied |
| checkpoint kill→resume | PASS | 新 AgentLoop/SessionManager 从 durable 文件恢复 |
| OpenXML validation | PASS | ZIP CRC、XML、Content Types、relationships、必需部件 |

## Single-Agent vs Subagent

对比使用固定 fake-provider workload，只测治理回归与可重复开销，不代表真模型质量。

| Mode | Success | Wall/P95 ms | Input/Output tokens | Parent context | Fail/Cancel/Loop guard |
| --- | ---: | ---: | ---: | ---: | ---: |
| Single | 1.00 | 120 / 120 | 100 / 20 | 100 | 0 / 0 / 0 |
| Multi（2 child） | 1.00 | 90 / 90 | 140 / 28 | 60 | 0 / 0 / 0 |

结果表明该固定 workload 中 Subagent 减少父上下文和模拟关键路径时长，但增加总 token；真模型质量、实际价格和真实 P95 仍需后续手动 benchmark，不能从本确定性报告外推。

## 尚未建立的选做基线

- LLM Judge / Verifier 与人工相关性。
- DeepSeek/GPT-5.6 多模型质量、真实价格和 P95 矩阵。
- KV cache hit/miss 与优化收益。
- Subagent 共享 workspace 文件租约性能。
