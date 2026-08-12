# P1 Office 垂直切片代码说明

## 先记住一句话

P1 不是把 Office 逻辑塞进 Agent，而是把 Office 当作第一个 Skill：Agent 负责理解目标和调用工具，`officecli` Skill 负责 Office 操作，Runtime 负责输入快照、权限、产物、校验、恢复和 trace。

当前仓库只保留 `nanobot/skills/officecli/`。旧 `office-python`、`office-automation`、周报 DSL 和专用 workflow 已删除，文档不应再把它们当成可用能力。

## 1. 一次 Office 任务的调用链

```text
用户请求
  -> AgentRunner 选择 officecli Skill
  -> SkillLoader 校验 SKILL.md + skill.yaml + provider availability
  -> plan / Policy / WorkspaceScope 决定是否能执行
  -> OfficeCLI launcher 准备固定 binary
  -> officecli command 或 batch 执行
  -> .nanobot-runtime/artifacts/<task_id>/ 写入结果
  -> validate / OpenXML / visual / facts 校验
  -> ArtifactStore 登记 checksum、来源和 lineage
  -> TraceHook 记录摘要，AgentLoop 返回产物路径
```

关键点是“Skill 描述能力，Runtime 决定能否执行”。`SKILL.md` 中的 permissions 只是声明，不能替代 P3 policy，也不能扩大 workspace 或网络范围。

## 2. Skill 包的组成

| 文件 | 作用 |
| --- | --- |
| `SKILL.md` | 给模型的操作说明、事实口径、artifact 目录和验证要求 |
| `skill.yaml` | typed manifest：输入、输出、依赖工具、provider contract、声明权限、eval |
| `references/officecli-runtime.json` | OfficeCLI 1.0.135 的版本、平台资产、checksum、能力和 policy hints 唯一来源 |
| `scripts/officecli_backend.py` | 解析 provider、编译受控 batch、运行 command、收集 sidecar |
| `scripts/compile_officecli.py` | 可复用的结构化编译入口，不是第二套 Office 引擎 |
| `scripts/render_docx.py` / `render_pptx.py` | 需要时生成预览，供 visual sanity 使用 |
| `_shared/office_core` | `inspect_workbook.py` 和 `extract_facts.py` 等确定性公共能力 |

## 3. 固定 provider contract 与 launcher

`officecli-runtime.json` 同时记录：

- `validated_version=1.0.135`；
- 每个平台下载文件名和 SHA-256；
- 允许的 batch 动作（当前是 `add`、`set`）；
- `help/view/get/query/validate` 等低风险操作和 `raw/mcp/plugins/install/update` 等需要更严格审批的提示；
- 关闭上游自动更新、resident 和隐式环境继承的运行变量。

Mybot 的 `officecli` console launcher 在首次调用时按平台选择 contract 资产，下载到稳定缓存，使用文件锁和原子替换，校验 checksum 后才返回可执行路径。缓存被篡改或校验失败时删除无效文件并 fail closed；Agent 不能调用上游 `latest/install/update`。

因此“机器上恰好有一个叫 officecli 的命令”不等于 provider 可用。Loader 需要同时看到 launcher、contract、平台资产和正确版本。

## 4. OfficeCLI backend 的职责

`officecli_backend.py` 做四件事：

1. `get_officecli_info()` 解析固定 binary，记录实际版本和能力；
2. `compile_*_commands()` 把结构化报告/幻灯片操作转换成受限 `add/set` batch，避免模型直接拼接任意命令；
3. `_OfficeCliRunner` 以精确 argv、隔离环境、关闭自动更新的方式启动子进程，收集 stdout/stderr 和退出码；
4. `render_with_officecli()` 在任务 artifact 目录产生 validation/run sidecar 和预览路径。

编译器解决的是重复操作和命令可审计性，不是安全边界。最终是否允许执行仍经过 P3 `PolicyEngine`、`SandboxLauncher`、OCC 和 artifact path guard。

## 5. 事实层：为什么不能让模型直接算数字

定量任务先用共享脚本生成两类 artifact：

```text
workbook_schema.json   # sheet、列名、类型、行数和少量样例
verified_facts.json    # fact_id、值、单位、来源列/行、计算式、confidence
```

`inspect_workbook.py` 用 read-only `openpyxl` 读取每个 sheet，推断列类型并输出稳定 JSON。`extract_facts.py` 根据 metric spec 执行 sum、ratio、top-by-sum 等确定性计算，检查缺列、非数字、零分母和重复 `fact_id`，最后校验 facts schema。

报告中的数字必须引用 `fact_id`，不能由模型从原始表格自由估算。这样 evaluator 可以验证“数字对不对”和“数字能否追溯”是两个独立问题。

## 6. 输入、输出和验证

- 输入优先使用 P4 创建的 immutable snapshot；在 P4 不可用的旧入口中，Skill 也不能覆盖原 workbook。
- 新文件写到 `.nanobot-runtime/artifacts/<task_id>/`，并由 `ArtifactStore` 记录类型、checksum、tool call、source artifact、Skill 和 validation 状态。
- 修改已有用户文件属于高风险操作：先有 fresh-read snapshot，再经过 policy/approval 和 SHA-256 OCC；mtime 不变也不能跳过 hash 比较。
- `officecli validate` 检查 OfficeCLI 自身语义；Runtime evaluator 再检查 ZIP CRC、XML、Content Types、relationships、数字真值和预览非空。
- 预览进程必须在完成后被正确回收，不能因后台 descriptor/resident 进程让任务假成功或 hang 住。

## 7. 为什么选 OfficeCLI

它提供统一的 Word/Excel/PowerPoint 命令面和固定版本，减少每种文档一个私有 Python workflow 的维护面；同时 contract、batch、sidecar 和 validate 让执行可审计。代价是 provider binary、平台资产和上游版本都成为交付依赖，所以必须锁版本、校验 checksum、隔离缓存并在不可用时拒绝执行。

## 8. 当前边界

- P1 只证明 Office 垂直切片，不证明所有 Office 功能都能自动化。
- `raw`、MCP、plugins、install/update 等能力仍存在，但由 policy/sandbox 决定是否 ask 或 deny，Skill 不会偷偷删掉它们。
- P1 不承诺视觉质量等同人工；visual sanity 只先拦截空白/损坏渲染，语义质量由 benchmark evaluator 和人工审核补充。
- P1 不负责 Langfuse 的结果真相；trace/eval 编排属于 P5。

## 9. 验证证据

```bash
pytest tests/skills/test_officecli_runtime.py tests/agent/test_skill_manifest.py -q
pytest tests/evaluations/test_evaluation_contract.py tests/runtime/test_replay_trace_eval.py -q
ruff check nanobot/ tests/skills/test_officecli_runtime.py
```

测试覆盖 contract 平台选择、checksum fail closed、缓存复用/重建、自动更新关闭、子进程回收、manifest 可用性和 deterministic fixture。Runtime 测试进一步覆盖产物、OpenXML、red-team 和 checkpoint，避免只测“命令返回 0”。

## 面试怎么讲

### 30 秒回答

“我把 Office 做成可插拔 Skill，而不是改 Agent 核心。Skill manifest 描述输入输出和固定 provider contract，launcher 按版本和 checksum 准备 OfficeCLI；模型生成的操作先经过 Runtime 的 policy、sandbox、OCC，再把结果写入 task artifact。Excel 数字由共享脚本生成带来源的 verified facts，最终文件还要过 OpenXML、数字和视觉校验，所以结果既能交付，也能解释和复现。”

### 高频追问

**为什么 manifest 的 permissions 不等于授权？**

因为 Skill 包是不可信声明；真正的授权来自会话 `WorkspaceScope`、PolicyEngine、OS sandbox 和用户 approval。否则安装一个 Skill 就能绕过 Runtime 安全边界。

**为什么不让模型直接调用 LibreOffice 或 Python？**

那会把版本、参数、安全和验证散落到 prompt。固定 OfficeCLI contract 把 provider 依赖集中管理，Runtime 仍能对每次子进程做统一治理。

**checksum 校验能防什么？**

它能防缓存被替换、下载到错误版本或平台资产漂移；不能证明 OfficeCLI 本身没有 bug，所以还需要 OpenXML、facts 和 red-team 测试。

**为什么文件存在不代表任务成功？**

文件可能是空的、关系损坏、数字错误或预览空白。artifact registration 只证明产物可追踪，validator 才决定它是否满足交付契约。

## 2026-08-12：Office Case 空终态恢复

- Office benchmark 中，模型可能已完成文档读取和计算，却在最后一轮返回空内容；旧 finalization 提示过于宽泛，复杂或证据不完整的题目仍可能再次返回空。
- Runtime 的无工具 finalization 现在明确要求直接回答原请求、不得再调用工具；资料不足或相互冲突时需说明限制并基于显式假设给出最佳支持答案，而不是用空响应终止。
- 该变化不补充外部资料、不改变 Case prompt/evaluator，也不把 fallback 文案当作成功答案；有界 finalization 后持续为空仍以 `empty_final_response` fail-closed。
