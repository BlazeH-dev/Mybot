# P5 Trace / Eval 代码变更说明

> 当前状态：仅规划，尚未执行；不表示 trace/eval 代码已落地。
> 对应计划：`docs/plans/runtime-steps/P5-trace-eval.md`

2026-07-16 精简后的 P5 Core 为：轻量 cassette、OTel 风格 JSONL trace、确定性 metric/report、代表性安全红队。Judge/Verifier、多模型成本矩阵和 KV cache 优化仍为选做，不能覆盖确定性硬失败或阻塞 Core 出口。

计划回放必须分别覆盖普通 WebUI 的 create→automatic activation→update→complete，以及 plan-only 的 create→explicit confirm→update→complete；trace 记录 execution mode、activation mode、plan hash 和独立工具 approval，不能把自动计划激活误记为安全审批。

实现后必须补充真实 schema、导出路径、case set、指标基线和 CI 结果。
