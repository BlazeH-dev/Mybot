# P1 双 Office Skill 垂直切片代码变更说明

> 对应计划：`docs/plans/runtime-steps/P1-office垂直切片.md`
> 当前状态：双 Skill 边界、共享确定性事实层、Python 渲染链、OfficeCLI 独立能力包、静态 Plan Tool、pytest 回归与 WebUI artifact 面板均已落地。
> 2026-07-14 修订：撤销“单 Skill + OfficeCLI 默认后端 + legacy Python 后端”的结构，改为两个地位独立、可分别启用或禁用的 Skill。
> 2026-07-16 修复：新增随 Python 包安装的固定版本 launcher，首次调用按 provider contract 下载、校验并缓存 v1.0.135，不再要求用户手工准备 binary。
> 2026-07-16：阶段计划仅合并重复背景与步骤，双 Skill、facts、路由、contract、测试和 plan 契约不变。

## 阶段目标

P1 要验证的不是某一个 Office 库，而是一条可治理、可测试、可替换的 Office 任务链：

```text
输入文件 / 用户要求
  -> 独立 Skill 路由
     ├── office-automation（Python 工作流）
     └── officecli（OfficeCLI 完整能力）
  -> 共享输入检查与 verified facts（仅定量任务必需）
  -> Skill 自有工作流 / 中间表示
  -> docx / xlsx / pptx 与质量、验证、预览产物
```

两个 Skill 的关系是：

- `office-automation`：原有 Python Office 工作流，拥有自己的 report/slide DSL、validator、`python-docx` 和 `python-pptx` 渲染器。
- `officecli`：基于 OfficeCLI v1.0.135 官方能力快照的独立 Skill，可使用其 help、view、DOM、batch、validate、raw、MCP、plugin 等能力。
- 两者共享确定性输入检查、`verified_facts.json` 和 Runtime 公共治理，但不要求共享 DSL。
- 未显式指定时，通用 Office 请求优先匹配 `officecli`；用户明确要求原 Python 方案时使用 `office-automation`。
- 两者都通过现有 `disabledSkills` 独立开关；OfficeCLI binary 缺失只会使 `officecli` unavailable，不影响 Python Skill。

P1 继续复用现有 Agent 工具与 Skill 渐进披露机制，没有为 Office 在 `loop.py` 或 `runner.py` 中写私有分支。

## 代码变更

### 1. 共享 Office 确定性核心

新增：

```text
nanobot/skills/_shared/office_core/
├── __init__.py
├── common.py
├── references/
│   ├── metric_spec.example.json
│   └── verified_facts.schema.json
└── scripts/
    ├── __init__.py
    ├── inspect_workbook.py
    └── extract_facts.py
```

这个目录没有 `SKILL.md`，因此不会被 `SkillsLoader` 当成第三个 Skill，也不会进入模型的 Skills summary。

共享层只负责可以确定性复用的事实工作：

- 只读检查工作簿，生成紧凑 `workbook_schema.json`。
- 按 metric spec 计算指标，生成 `verified_facts.json`。
- 提供 JSON 读写、fact 格式化等公共函数。

`office-automation/scripts/inspect_workbook.py`、`extract_facts.py` 与 `_common.py` 保留兼容入口，内部转发到共享实现，避免旧脚本路径立即失效。

### 2. 恢复独立 Python Skill：`office-automation`

当前目录：

```text
nanobot/skills/office-automation/
├── SKILL.md
├── assets/
├── references/
│   ├── plan.schema.json
│   ├── report_dsl.schema.json
│   └── slide_dsl.schema.json
└── scripts/
    ├── _common.py
    ├── extract_facts.py
    ├── inspect_workbook.py
    ├── render_docx.py
    ├── render_pptx.py
    └── validate.py
```

主要调整：

- `SKILL.md` 明确这是原 Python Office 工作流，不再把 OfficeCLI 描述成内部默认后端。
- `render_docx.py` 只使用 `python-docx`。
- `render_pptx.py` 只使用 `python-pptx`，并继续执行 PPT 页数约束。
- 删除 `--backend`、`--officecli-bin`、`MYBOT_OFFICE_BACKEND` 等后端切换语义。
- 保留 Skill 自己的 DSL、validator、plan 校验与 artifact 约定。

Python 路径的确定性链路为：

```text
Excel
  -> shared inspect_workbook.py
  -> shared extract_facts.py
  -> verified_facts.json
  -> report_dsl.json / slide_dsl.json
  -> office-automation/validate.py
  -> python-docx / python-pptx
  -> weekly_report.docx / weekly_review.pptx
```

### 3. 新增独立 OfficeCLI Skill：`officecli`

当前目录：

```text
nanobot/skills/officecli/
├── SKILL.md
├── references/
│   ├── officecli-runtime.json
│   └── upstream-snapshot.md
└── scripts/
    ├── compile_officecli.py
    ├── officecli_backend.py
    ├── render_docx.py
    └── render_pptx.py
```

版本与来源固定为：

- 官方仓库：`https://github.com/iOfficeAI/OfficeCLI`
- tag：`v1.0.135`
- 上游 `SKILL.md` SHA：`0b110eab23229c3b2f507b1802f3bcd37e44a8dd`
- license：Apache-2.0

`SKILL.md` 和 `officecli-runtime.json` 保留 OfficeCLI 的完整能力面，包括：

- help、create、view、get、query、validate。
- add、set、remove、move、swap、batch。
- raw、raw-set、add-part。
- plugins、MCP、watch、install、config、update。

这些能力不会在 Skill 层被删除。P3 Runtime Policy 将按操作、参数、目标路径和网络边界分类为 allow / ask / deny。manifest 或 Skill 只能声明需求，不能自行授予权限；workspace、网络和敏感信息硬边界不可被审批放宽。

`compile_officecli.py`、`render_docx.py`、`render_pptx.py` 是现有 grounded report/deck 的兼容 helper：

```text
verified facts + 原固定 DSL
  -> compile_officecli.py
  -> 可重放 add/set batch
  -> 固定版本 OfficeCLI
  -> Office 文件 + validation/run/preview sidecar
```

它们不是 OfficeCLI Skill 的唯一工作流，也不代表两个 Skill 必须共享 DSL。`render_pptx.py` 会在调用 OfficeCLI 前检查公共 `pptx_max_pages` 约束，避免兼容 helper 绕过页数限制。

### 4. OfficeCLI 运行契约

`nanobot/skills/officecli/references/officecli-runtime.json` 记录：

- 固定验证版本与 release URL。
- 各平台 release asset 名称与 SHA-256。
- 完整能力列表。
- 兼容 batch helper 当前允许的 `add/set` 操作。
- Runtime Policy 的 allow/ask 提示，而不是静态删减能力。
- `OFFICECLI_SKIP_UPDATE=1`、`OFFICECLI_NO_AUTO_RESIDENT=1` 与 `OFFICECLI_RESIDENT_FLUSH=each` 等隔离运行设置；兼容 helper 使用一次性进程，截图文件已完整落盘但浏览器仍不退出时终止对应进程组并记录 `timed_out_after_output`；上游成功生成有效 PNG 却返回纯路径而非 JSON 时记录 `unstructured_output`，其他命令继续严格要求结构化 JSON。

`officecli_backend.py` 负责：

- 查找 binary 并严格检查 v1.0.135；测试或显式诊断可使用允许非固定版本的参数。
- 使用 argv 数组执行，不经过 shell。
- 关闭自动更新并使用隔离 HOME，避免污染用户插件和配置。
- 执行 batch 后显式 flush/close。
- 同时检查进程退出码、结构化 JSON 的 success 和 unsupported-property warning。
- 生成 batch、validation、run metadata 和可选 preview。

P1 不允许 Agent 任务调用上游 `latest`、`install` 或 `update`。Mybot Python 包提供同名 `officecli` launcher：首次调用时从唯一 provider contract 选择平台资产，下载 v1.0.135、校验 SHA-256、原子缓存并强制 `OFFICECLI_SKIP_UPDATE=1`；后续直接复用缓存。平台/版本/checksum 不在 launcher 内重复维护。

launcher 缓存目录在进入 OfficeCLI 临时 `HOME` 隔离前显式固定，避免每个 DOM 子命令重复下载；`SkillsLoader` 除 PATH 外还检查当前 Python 的 scripts 目录，因此直接执行 `venv/bin/nanobot gateway` 时也能发现同目录 console script。

### 5. Skill 路由、可用性与开关

现有 `SkillsLoader` 已支持两类独立控制：

- `metadata.nanobot.requires.bins`：`officecli` 声明需要 `officecli` 命令；正常安装 Mybot 时 console script 与 `nanobot` 一起生成，首次执行再准备固定 binary。
- `agents.defaults.disabledSkills`：可按 Skill 名分别禁用 `officecli` 或 `office-automation`。

路由偏好写在两个 Skill 的 description 和正文中，不在 Runtime 写 Office 专用 if/else：

- 通用 Office 请求默认优先 `officecli`。
- 明确要求 Python/原 Office Skill 时使用 `office-automation`。
- 对比任务可以同时运行两个 Skill，使用同一输入快照、facts 和输出约束。
- OfficeCLI unavailable/disabled 时，只有请求语义允许才回退到 Python Skill，并应向用户说明原因。

### 6. 静态 Plan Tool

P1 继续复用已落地的静态计划工具：

```text
nanobot/agent/tools/plan.py
nanobot/session/plan_state.py
tests/agent/tools/test_plan_tool.py
```

`plan` 工具固定提供 `create/get/confirm/update_step/complete`：

- create 规范化计划并计算不可变 contract hash。
- 普通 WebUI 回合发送 `execution_mode=default`；复杂任务调用 create 后计划自动进入 active，模型可直接执行并持续更新步骤。
- 输入框“计划”按钮发送 `execution_mode=plan_only`；AgentLoop 只暴露 plan、文件读取/搜索和网页读取工具，禁止 Shell、写文件、CLI、MCP 与其他副作用。
- plan-only create 保持 `awaiting_confirmation`；同一回合禁止 confirm，必须由用户在后续回合确认。
- confirm 必须提交精确 hash，并记录确认消息 id；计划卡片的“执行计划”按钮会发送带 task id 与 hash 的显式确认消息。
- update_step 检查依赖顺序。
- complete 核对计划产物与实际交付路径。
- 完整计划写入 `.nanobot-runtime/artifacts/<task_id>/plan.json`，上下文只追加紧凑摘要。

WebUI 新增 `PlanProgressCard`，直接消费现有结构化 tool progress event，因此实时流和 transcript 重放使用同一数据源。卡片只显示最新计划快照，展示 pending / in_progress / done / skipped，并在待确认状态提供执行按钮。

P3 计划用持久化 `InteractionRequest` 承接通用问题、需要人工确认的 plan-only/手动计划、恢复决定和高风险审批：`required` 必须回答，`auto_resolve` 到 deadline 后按默认值或最佳判断继续，安全 approval 固定 `expire_and_deny`。普通 WebUI 自动激活计划不创建 plan confirmation，但其每个工具调用仍经过独立 policy/approval。等待时当前 LLM 调用已结束，task/turn 逻辑挂起并释放 Runner 资源；回答或 deadline 恢复同一执行链。P4 checkpoint 只服务 active/completed 且 `approved_plan_hash` 绑定当前 hash 的计划任务。

### 7. WebUI artifact 面板

现有实现继续复用：

```text
webui/src/components/OfficeArtifactsPanel.tsx
webui/src/components/MessageBubble.tsx
webui/src/components/thread/PlanProgressCard.tsx
webui/src/tests/office-artifacts-panel.test.tsx
webui/src/tests/message-bubble.test.tsx
webui/src/tests/plan-progress-card.test.tsx
```

面板从 assistant 文本中识别 `.nanobot-runtime/artifacts/<task_id>/...`，展示 plan、quality report、facts、DSL、docx、pptx、OfficeCLI batch/validation/run sidecar 和 preview。P1 不新增复杂后端协议；P4/P5 后再接正式 artifact/trace API。

### 8. 测试调整

`tests/skills/test_office_weekly.py` 现在显式区分：

- `PYTHON_SKILL_DIR`
- `OFFICECLI_SKILL_DIR`
- `SHARED_CORE_DIR`

测试不再用同一脚本的 `--backend` 参数切换实现，覆盖：

- 两个 Skill 均可发现，`_shared` 不可发现。
- OfficeCLI 缺失不隐藏 Python Skill；两个 Skill 可通过 `disabledSkills` 独立关闭。
- shared inspect/extract 与固定 expected metrics 一致。
- 缺列、空值、未知 fact、PPT 超页数等失败路径。
- Python docx/pptx 可重新打开、无 fact 占位泄漏、关键数字来自 facts。
- OfficeCLI contract 版本、能力、policy hints 和 asset checksum 完整。
- OfficeCLI 兼容 compiler 只生成预期的可重放 `add/set` batch。
- OfficeCLI PPT helper 在执行 binary 前强制页数约束。
- 设置 `OFFICECLI_TEST_BIN` 时运行固定 binary 集成测试，检查 Office 文件、validation/run/batch sidecar 和 preview。

## 为什么采用双 Skill，而不是融合为一个 Skill

### 1. 能力模型不同

Python Skill 是一个窄而确定的 grounded report/deck 工作流；OfficeCLI 是通用 Office DOM/CLI 能力。把两者塞进同一个 Skill 并用 `--backend` 切换，会让“选择工作流”和“选择渲染引擎”混在一起，也无法准确表达 OfficeCLI 的 xlsx、inspect、raw、MCP、plugin 等独立能力。

### 2. 可以公平比较

两个 Skill 独立后，可以在相同输入快照、facts 和约束下比较：

- 生成质量与可打开性。
- 命令/产物可重放程度。
- token、耗时和工具调用成本。
- 对 Runtime Policy、artifact、trace 的接入复杂度。

即使 OfficeCLI 的某项结果不如 Python Skill，也保留并如实记录；比较结果不影响它作为独立附加能力存在。

### 3. 治理边界更清晰

独立 Skill 可以分别声明依赖、可用性和开关。Runtime 共享的是权限、输入快照、facts、artifact、checkpoint、trace 和 eval，而不是强迫所有 Skill 使用同一个私有 DSL。

### 4. Grounding 只在需要时启用

定量分析和定量结论必须使用 `verified_facts.json`。纯格式调整、查看、批注、文本抽取等任务不需要为了形式完整而创建空 facts 文件。

## 验证方式

### P1 Skill 回归

```bash
source venv/bin/activate
pytest tests/skills/test_office_weekly.py -q
```

2026-07-14 双 Skill 重构后的本地结果：

```text
11 passed, 1 skipped
```

普通 CI 不访问 GitHub，覆盖 launcher 的平台映射、checksum、缓存复用、篡改恢复和禁更环境；真实 binary 集成 case 可使用 launcher 已验证的缓存路径或显式 `OFFICECLI_TEST_BIN`。

2026-07-16 launcher 修复回归：

```text
tests/skills/test_officecli_runtime.py: 10 passed
OFFICECLI_TEST_BIN=venv/bin/officecli 的 P1/SkillLoader/WebUI 路由相关回归: 80 passed
ruff check nanobot/: passed
```

### Plan Tool 回归

```bash
source venv/bin/activate
pytest tests/agent/test_execution_mode.py tests/agent/tools/test_plan_tool.py -q
```

覆盖 plan-only 工具收敛、默认 WebUI 自动激活、同回合禁止确认、hash 确认、依赖与 artifact 校验。

```text
11 passed
```

### Lint

```bash
source venv/bin/activate
ruff check nanobot/skills tests/skills/test_office_weekly.py
```

### 固定 OfficeCLI binary 集成

```bash
OFFICECLI_TEST_BIN=/absolute/path/to/officecli \
  venv/bin/python -m pytest \
  tests/skills/test_office_weekly.py::test_officecli_backend_real_binary -q
```

也可直接使用随包安装的 launcher：

```bash
officecli --version
OFFICECLI_TEST_BIN="$(venv/bin/python -c 'from nanobot.officecli_runtime import ensure_officecli; print(ensure_officecli())')" \
  venv/bin/python -m pytest \
  tests/skills/test_office_weekly.py::test_officecli_backend_real_binary -q
```

launcher 只下载 contract 固定的 v1.0.135 并校验 checksum，不跟随 latest。

### 前端回归

```bash
cd webui
bun run test -- \
  src/tests/plan-progress-card.test.tsx \
  src/tests/thread-composer-attach.test.tsx \
  src/tests/nanobot-client.test.ts \
  src/tests/useNanobotStream.test.tsx \
  src/tests/i18n.test.tsx
bun run lint
bun run build
```

计划 UI 复用结构化 tool event 与 transcript，不从 assistant 自然语言猜测步骤状态。

## 阶段验收情况

已完成：

- `office-automation` 恢复为独立 Python Skill，不依赖 OfficeCLI。
- `officecli` 成为独立 Skill，保留 OfficeCLI 完整能力说明和固定版本契约。
- 两个 Skill 共享 deterministic facts，但不强制共享 DSL。
- 普通 Office 请求默认优先 `officecli`，明确 Python 请求使用 `office-automation`。
- 两个 Skill 可通过 `disabledSkills` 独立禁用。
- OfficeCLI binary 缺失只影响 `officecli` availability。
- shared core 不进入 Skills summary。
- Python deterministic artifact chain 通过。
- OfficeCLI compiler、contract、约束和真实 binary 集成入口保留。
- 静态 Plan Tool、artifact 面板和现有测试未回归。
- WebUI 仅规划按钮、默认复杂任务自动 plan-and-execute、计划步骤卡片和执行入口已落地。

后续阶段接续：

- P2：为两个 Skill 增加正式 manifest/Registry；本地存在但无效的 manifest 必须 fail closed，缺失 manifest 保持兼容。
- P3：实现参数级 allow/ask/deny、三档持久化 `InteractionRequest`、参数绑定 `expire_and_deny` approval、文件 fresh-read hash 和不可放宽的 workspace/network/sensitive 边界。
- P4：实现不可变输入快照、artifact/lineage 和已激活、hash 绑定计划任务的 checkpoint/resume；恢复状态使用 completed/pending/uncertain。
- P5：记录 Skill、引擎、facts、policy、artifact 和成本时长 trace，安全目标是“不可信内容无法诱导未授权副作用或泄漏”。
- P8：支持最多 5 个直接子 Agent、禁止嵌套、权限只收紧、隔离上下文/产物、父 Agent 汇总事实与结果，并比较单 Agent 顺序执行和双 Agent 并行执行的成本与时长；共享 workspace 文件租约仅作选做增强。

## 维护约定

以后只要 P1 相关代码、方案、Skill 边界、测试、artifact 形状或执行语义发生变化，必须同时更新：

1. `docs/plans/runtime-steps/P1-office垂直切片.md`
2. 本代码变更说明
3. `docs/修改记录.md`

该规则已写入仓库根目录 `AGENTS.md`，并同样适用于 P0-P8 其他阶段的对应 runtime code notes。
