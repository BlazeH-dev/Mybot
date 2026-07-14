# P3 Policy、可恢复审批与最小文件冲突保护 — 详细步骤

> 所属：`docs/plans/Mybot通用AgentRuntime与办公自动化SkillPack整合方案.md`
> 状态：仅规划，未执行。2026-07-14 按 grill-me 共识收缩 OCC、重构 HITL。
> 阶段出口：工具执行前统一 allow/ask/deny；ask 持久化后结束当前执行并可跨刷新/重启恢复；已有文件不会被过期读取静默覆盖。

---

## S3.1 Tool 风险元数据

- `Tool` 增加 `capability`、`risk_level`、`requires_approval`。
- 不使用模型可伪造的 `skill_scope` 作为授权依据。
- manifest permissions 仅是需求声明，最终权限由 Runtime、会话策略和一次性审批决定。
- 安全默认：已知本地只读可 allow；未知写入/执行/网络进入 ask；未知 MCP 默认 deny 或 require explicit trust。

## S3.2 PermissionDecision 纯函数

新增 `nanobot/runtime/policy.py`：

```python
PermissionDecision(
    action="allow|ask|deny",
    reason="...",
    matched_rules=[...],
    risk_level="...",
)
```

输入至少包含工具、规范化参数、request/task/plan 上下文、父任务约束和配置。

规则层级：

1. workspace、SSRF、敏感信息等硬边界 deny，配置和审批都不能放宽。
2. 配置 deny。
3. 已绑定参数的一次性 approval。
4. 配置 ask/allow 与默认策略。

## S3.3 工具执行前拦截

- 在 `ToolRegistry.prepare_call` 成功解析参数后执行 policy。
- deny 返回稳定、不可通过重试绕过的结构化错误。
- allow 正常执行。
- ask 不执行工具，转 S3.4 创建 pending approval。
- 每次决策先写本地 audit，P5 再接完整 trace。

## S3.4 `pending_approval` 持久化

新增 `nanobot/runtime/approvals.py`，记录：

```text
approval_id
task_id / plan_hash / step_id
tool_name
normalized_params_hash
target summary
risk / reason
created_at / expires_at
status: pending|approved|denied|expired|consumed
```

流程：

1. policy 返回 ask。
2. Runtime 原子写 pending approval，发送 WebUI runtime event。
3. 当前 Agent 执行进入 `awaiting_approval` 并结束，不保持 Runner coroutine 长时间等待。
4. 用户批准/拒绝形成新 inbound control event。
5. Runtime 恢复原任务；只有工具名、plan hash 和规范化参数 hash 完全一致时消费一次性 approval。
6. 参数改变、计划替换、过期或已消费都必须重新审批。

WebSocket 只负责展示和传递决定，不是审批状态真相源。

## S3.5 OfficeCLI 能力分级

OfficeCLI 能力完整保留，不在 Skill 层删除：

- `help/view/get/query/validate/screenshot`：通常 allow。
- 任务 artifact 目录中新文件的常规 `add/set/batch`：可 allow。
- 修改用户已有文件：ask，并执行文件新鲜度检查。
- `raw/raw-set/add-part`：默认 ask。
- `MCP/plugin/install/update/config/watch`：根据安装、网络、长期进程和配置副作用 ask/deny。
- workspace 越界、敏感路径和 SSRF：硬 deny。

不声称通过 shell 字符串分类形成绝对安全；最终安全依赖硬边界、目标路径、参数 hash 和审批。

## S3.6 最小文件 OCC

复用 `agent/tools/file_state.py`：

- 已有文件在 `write_file`、`edit_file`、`apply_patch` 前必须有本会话读取快照。
- 即使 mtime 未变化也比较 SHA-256；内容变化返回 `file_conflict:modified_since_read`。
- 未读已有文件返回 `file_conflict:not_read`。
- 多文件 patch 在第一处写入前统一检查全部已有目标；任一冲突则零写入。
- 成功写入后刷新 read state。
- 错误提示明确要求“重新读取 → 重新生成 patch → 再提交”。

冻结前不承诺：

- 新文件 expected-absent 竞态完全消除。
- fsync/数据库式事务。
- 对 shell/外部进程任意写盘的拦截。
- 最终 hash 校验后的极小 TOCTOU 窗口消失。

## S3.7 不可信内容安全口径

- 网页、文档、表格、邮件和 MCP 描述统一视为 untrusted content。
- 不承诺检测全部提示词注入。
- 安全验收改为：注入不能造成越权写入、敏感信息泄漏或未确认外发。
- 启发式 injection signal 只用于审计，不是安全边界。

## 定向测试

- 硬边界不能被配置 allow 或用户 approval 覆盖。
- ask 后 Runner 结束，pending approval 可跨重新实例化读回。
- approve/deny/expire/params hash mismatch/plan hash mismatch。
- approval 只消费一次。
- OfficeCLI L1 allow、已有文件修改 ask、越界 deny。
- 未读写入、读取后修改、mtime 不变但 hash 改变、多文件第 N 个冲突均硬失败。

## 阶段出口检查

- [ ] allow/ask/deny 在工具执行前统一生效。
- [ ] ask 不阻塞 Runner，审批可跨刷新/断线/重启恢复。
- [ ] approval 与精确参数和计划绑定且一次性消费。
- [ ] 配置和审批不能突破硬边界。
- [ ] OfficeCLI 完整能力由 Policy 分级治理。
- [ ] 已有文件冲突拦截率 100%，多文件冲突时零写入。
- [ ] 安全报告使用“越权副作用为 0”，不宣称万能注入检测。
