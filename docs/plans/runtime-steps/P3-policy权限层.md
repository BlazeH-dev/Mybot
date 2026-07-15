# P3 Policy、三档 HITL 与最小文件 OCC

> 状态：待执行。文件租约不属于本阶段必做。
> 出口：工具执行前统一 allow/ask/deny；等待可持久化恢复；危险审批超时不放行；已有文件冲突拦截率 100%。

## 1. Policy Gate

### 元数据与决策

`Tool` 增加 `capability`、`risk_level`、`requires_approval`。`runtime/policy.py` 提供纯函数：

```python
PermissionDecision(
    action="allow|ask|deny",
    reason="...",
    matched_rules=[...],
    risk_level="...",
)
```

输入包含工具、规范化参数、request/task/plan、父任务约束和配置。规则优先级：

1. workspace、SSRF、敏感信息 hard deny，任何配置和审批都不能放宽。
2. 配置 deny。
3. 精确参数绑定的一次性 approval。
4. 配置 ask/allow 与安全默认值。

### 接线

- `ToolRegistry.prepare_call` 只做同步解析、转换和 schema 校验。
- Runner/Runtime 在实际执行前调用异步 policy gate，负责持久化和事件 I/O。
- deny 返回稳定结构化错误；allow 执行；ask 不执行工具，创建 approval。
- 每次决策先写本地 audit，P5 接入完整 trace。

## 2. InteractionRequest

`runtime/interactions.py` 统一承接：

- `question`：`request_user_input` 的单选、多选和自由文本。
- `approval`：高风险工具批准/拒绝。
- `plan_confirmation`：只用于 plan-only、手动计划或其他明确要求人工确认的计划，绑定 plan hash；普通 WebUI 自动激活计划不创建该请求。
- `recovery_decision`：uncertain 副作用人工决定。

最小记录：

```text
request_id / revision / kind
task_id / turn_id / plan_hash / step_id / child_id
continuation / tool_call_id
payload / questions / target summary
strategy: required|auto_resolve|expire_and_deny
created_at / expires_at
status: pending|answered|approved|denied|timed_out|expired|cancelled|superseded|consumed
```

首版可按 request 原子写入 `.nanobot-runtime/interactions/<request_id>.json`；session/task metadata 只保存活动引用。

### 三档等待

- `required`：必要参数、文件、不可推断选择、需要人工确认的 plan、uncertain recovery。无回答就不继续，只能回答、取消或 `/stop`。
- `auto_resolve`：非阻塞偏好问题，deadline 建议 60–240 秒。到期优先用声明的确定性默认值，否则返回 `timed_out` 让模型最佳判断；不得伪造用户答案。
- `expire_and_deny`：修改用户原文件、消息/邮件、高风险 shell、远程写操作。到期 expired/denied，原工具不得执行。

普通聊天不能隐式消费 approval。客户端响应必须包含 request id、expected revision 和幂等键。

### Suspension 与恢复

1. 模型调用 `request_user_input`，或 policy 返回 ask。
2. Runtime 原子保存请求并推送 WebUI 卡片。
3. Runner 返回 typed `awaiting_question|approval|plan_confirmation|recovery_decision`，不是 tool error。
4. 当前 LLM 调用结束，释放 Runner 资源；task/turn 不发送 completed，等待期 token 为 0。
5. 用户响应或 deadline 恢复原 task/turn 和 tool call；原子竞争只允许一次消费。
6. 回答、`timed_out`、denied/expired 作为匹配原 `tool_call_id` 的结构化 tool result 注入，不重复添加用户文本。

`InteractionManager` 启动时扫描 overdue pending 请求；运行中维护最近 deadline 的 timer，不为每个请求建立永久 cron。刷新、断线和重启后 WebUI 必须重放未处理卡片。

## 3. 参数绑定 Approval

`runtime/approvals.py` 是 `InteractionRequest(kind="approval")` 的安全专用逻辑，额外保存：

```text
tool_name / normalized_params_hash
task_id / plan_hash / step_id
target / risk / reason / expires_at
```

- approval 固定 `expire_and_deny`，只能消费一次。
- 工具名、plan hash、参数 hash 任一改变，或请求过期/已消费，都必须重新审批。
- plan 的自动激活状态不能替代工具 approval；自动 plan-and-execute 仍逐次经过 policy gate。
- WebSocket 不是状态真相源。

OfficeCLI 基线：只读 help/view/get/query/validate 通常 allow；任务目录新产物的常规 DOM 操作可 allow；修改用户文件、raw、MCP/plugin/install/update/config/watch 按参数 ask/deny；硬边界始终 deny。

## 4. 最小文件 OCC

复用 `agent/tools/file_state.py`，但 read snapshot 必须按 actor 隔离：

- 已有文件在 `write_file`、`edit_file`、`apply_patch` 前必须由当前 actor 读取。
- 未读返回 `file_conflict:not_read`；SHA-256 变化返回 `file_conflict:modified_since_read`，即使 mtime 未变也失败。
- 多文件 patch 在第一次写入前检查全部目标；任一冲突则零写入。
- 成功写入后刷新当前 actor read state；提示模型重新读取、重新生成 patch。

冻结前不承诺：新文件完整竞态消除、数据库式事务/fsync、shell 任意写盘拦截、最终微小 TOCTOU、Subagent 文件租约或跨进程锁。

## 5. 安全口径

- 网页、文档、表格、邮件和 MCP 描述都是 untrusted content。
- 不承诺检测全部注入；验收的是越权写入、敏感泄漏和未确认外发为 0。
- 启发式 injection signal 只用于审计，不是安全边界。

## 测试与出口

- hard deny 不能被配置、Skill、approval 或 child 放宽。
- required 不回答不继续；auto_resolve 到期恢复；expire_and_deny 到期不执行。
- 等待期间 provider 不被调用；回答/deadline 只恢复一次，重复、迟到和错误 revision 被拒。
- approval 的 approve/deny/expire、参数/计划 hash mismatch 和一次性消费通过。
- 刷新、断线、重启后卡片和 continuation 可恢复。
- OfficeCLI allow/ask/deny 分级通过。
- 未读、读后修改、mtime 不变但 hash 变化、多文件第 N 个冲突均硬失败且零部分写入。
