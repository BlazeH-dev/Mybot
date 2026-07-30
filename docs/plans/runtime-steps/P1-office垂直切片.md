# P1 Office 垂直切片

> 状态：已完成。当前只保留 `officecli` Skill；原 Python Office Skill、周报 DSL 和旧显式路由已删除。

## 1. 目标和边界

用一个真实领域验证 Agent Runtime 的输入、计划、工具、artifact、验证和恢复链路，同时让 Office 能力保持在 Skill 内，不把业务分支写进 AgentLoop。

- `nanobot/skills/officecli/` 负责 DOCX、XLSX、PPTX 的 inspect/query/create/apply/validate/render。
- `_shared/office_core/` 只放事实抽取和通用校验，不作为独立 Skill 或权限入口。
- Workspace、Policy、输入快照、artifact、OpenXML hard gate 和 checkpoint 由 Runtime 统一负责。
- OfficeCLI 版本、平台 binary 和 checksum 只由 `references/officecli-runtime.json` 与 launcher 管理。

## 2. 实施步骤

1. 固定中立 XLSX fixture 和可复算的 verified facts，先验证数字真值与 OpenXML 读取。
2. 提供唯一的 `officecli` manifest、SKILL.md、provider contract 和随包 launcher。
3. 将用户请求编译成受控 OfficeCLI command batch，禁止 Agent 自己选择 latest binary 或绕过验证。
4. 在 Runtime 中登记输入 snapshot、facts、命令结果、validation sidecar、渲染媒体和最终 artifact。
5. 对 DOCX/XLSX/PPTX 分别做 ZIP/XML/relationship、表格数值和非空截图校验。
6. 将 Office 任务接入 plan hash、Policy/HITL/OCC、checkpoint 和 trace；任何外部副作用仍走 P3。
7. 用同一 Dataset、输入、Skill、Policy 和 evaluator 比较 Luna 与 DeepSeek V4 Flash；Terra 只作固定 Judge。

## 3. 验收

```bash
source venv/bin/activate
pytest tests/skills/test_officecli_runtime.py tests/agent/test_skill_manifest.py -q
pytest tests/evaluations/test_evaluation_contract.py tests/runtime/test_replay_trace_eval.py -q
ruff check nanobot/ tests/skills/test_officecli_runtime.py
```

- binary 缺失、版本错误、checksum 不匹配时 fail closed，并可安全重建缓存。
- 同一输入可重算 facts；产物包含校验结果且可追溯到 snapshot。
- 生成文件可被对应 OpenXML 库打开，关系损坏、空渲染和错误数字会被 hard gate 拦截。
- 两个模型使用独立 Dataset Run、workspace 和 checkpoint，不能复用对方输出。

## 4. 明确不做

- 不恢复已删除的旧 Python Office 路由、旧周报 DSL 或专用 report/slide workflow。
- 不在 Agent 核心添加 Office 私有分支。
- 不把 Office benchmark 的模型质量分数当成 Runtime 安全 hard gate。
