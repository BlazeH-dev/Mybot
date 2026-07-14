# P1 双 Office Skill 垂直切片 — 详细步骤

> 所属：`docs/plans/Mybot通用AgentRuntime与办公自动化SkillPack整合方案.md`
> 状态：2026-07-14 已按新边界完成。原事实/DSL/Python 渲染链已恢复为独立 `office-automation`，OfficeCLI batch/validate/screenshot 已迁入独立 `officecli`，静态 `plan` 工具继续复用。
> 阶段出口：`office-automation` 与 `officecli` 可独立发现、启用和执行；两者共享 verified facts 与公共约束，但不强制共享 DSL；固定 fixture 下两条路径均可生成并验证产物。

主题：**独立 Skill 边界 + 共享确定性事实层 + 计划契约**。P1 不把 OfficeCLI 当作 Python renderer 的替代开关，而是用两套完整 Office 能力验证 Runtime 能否加载、治理和比较不同 Skill。

---

## S1.1 抽取共享 Office deterministic core

### 目标

让两个 Office Skill 使用同一输入检查、事实抽取和公共约束，避免复制“数字真相”逻辑。

### 目录

```text
nanobot/skills/_shared/office_core/
├── __init__.py
├── common.py
├── scripts/
│   ├── inspect_workbook.py
│   └── extract_facts.py
└── references/
    ├── metric_spec.example.json
    └── verified_facts.schema.json
```

`_shared/office_core` 不含 `SKILL.md`，不会被 `SkillsLoader` 当成第三个 Skill，也不会进入模型上下文。

### 规则

- 定量分析、指标计算和生成定量结论时必须产出 `verified_facts.json`。
- 纯格式调整、文档检查、批注或内容提取不强制运行事实层。
- 用户明确提供且无需计算的数字可登记为 `source=user_provided` 的 fact。
- 两个 Skill 可以拥有不同的中间表示和渲染流程，但最终关键数字必须回溯到相同 fact id。

### 验收

- 共享脚本可独立执行。
- 原 fixture 指标保持完全一致。
- `_shared` 不出现在 Skill summary。

---

## S1.2 恢复 `office-automation` Python Skill

### 目标

恢复 2026-07-07 已落地的 Python Office 工作流，让它不依赖 OfficeCLI。

### 保留能力

- `office-automation/SKILL.md`：Excel/CSV + 会议纪要 → grounded report/deck。
- 自有 `report_dsl.schema.json`、`slide_dsl.schema.json` 与 `validate.py`。
- `python-docx` 渲染 Word，`python-pptx` 渲染 PowerPoint。
- 计划确认、facts 引用、质量报告和任务 artifact 目录约束。

### 调整

- 删除 `--backend`、`--officecli-bin`、`MYBOT_OFFICE_BACKEND` 等后端切换参数。
- `render_docx.py` / `render_pptx.py` 只负责 Python Skill。
- SKILL.md 不再提 OfficeCLI、raw/MCP/plugin 或 OfficeCLI sidecar。
- `inspect_workbook.py` / `extract_facts.py` 改为调用共享 core 路径。

### 验收

- 未安装 OfficeCLI 时 `office-automation` 全链仍可运行。
- 原 deterministic artifact chain 通过。
- 产物中的关键数字全部来自 shared verified facts。

---

## S1.3 新增独立 `officecli` Skill

### 目标

把 OfficeCLI 作为完整、独立、默认优先匹配的 Office 能力包，而不是 `office-automation` 的默认 renderer。

### 目录

```text
nanobot/skills/officecli/
├── SKILL.md
├── references/
│   ├── officecli-runtime.json
│   └── upstream-snapshot.md
└── scripts/
    ├── officecli_backend.py
    ├── compile_officecli.py
    ├── render_docx.py
    └── render_pptx.py
```

### Skill 基线

- 来源固定到 OfficeCLI v1.0.135 官方 Skill/帮助语义，记录来源 URL、tag 与 snapshot hash。
- Mybot 的 SKILL.md 保留 OfficeCLI 的 help-first、view/get/query、DOM、batch、validate、screenshot、raw XML、MCP、plugin、watch 等能力说明。
- 不在 Skill 文本中硬删除高能力操作；是否执行由 P3 Runtime Policy 根据命令、目标和参数裁决。
- Mybot 负责准备固定 binary，Agent 任务不应在无批准时自行安装或更新。

### Grounded 数据任务

- 涉及计算或定量结论时，先运行共享 inspect/extract，消费 verified facts。
- OfficeCLI 可使用自己的命令/batch 和中间表示，不强制消费 Python Skill 的 report/slide DSL。
- 当前已实现的 `DSL → OfficeCLI batch` 编译器保留为可选 grounded-report helper，用于回归现有能力；它不代表 OfficeCLI Skill 的全部能力。

### 验收

- `SkillsLoader` 可独立发现 `officecli`。
- OfficeCLI 缺失只影响 `officecli`，不影响 `office-automation` 与网关启动。
- 设置 `OFFICECLI_TEST_BIN` 时，固定 binary 集成 case 产出 batch、validation、run metadata 与 preview。

---

## S1.4 两个 Skill 的路由与开关语义

### 目标

避免两个宽泛 Office Skill 产生不可解释的选择冲突。

### 规则

- 未显式指定时，普通 Office 请求优先使用 `officecli`。
- 用户明确要求 Python/原 Office Skill 时使用 `office-automation`。
- 用户要求比较时可运行两个 Skill，并共享相同输入快照、verified facts 和输出约束。
- `officecli` 被 `disabledSkills` 禁用或 binary 不可用时，使用 `office-automation` 并说明原因。
- 已禁用 Skill 不因用户提及而自动启用；提示用户先修改开关。
- 路由提示放在 Skill description/manifest，不在 Runtime 写死 Office 私有 if/else。

### 验收

- summary 同时展示两个 Skill，描述能体现差异。
- 禁用任一 Skill 后不会出现在可选 summary。
- `_shared/office_core` 永远不出现在 summary。

---

## S1.5 OfficeCLI binary 与能力契约

### 目标

固定可复现的 OfficeCLI 运行环境，同时保留完整能力面。

### 契约

- 固定验证版本、各平台 asset 名称与 SHA-256。
- 记录官方 Skill snapshot 来源和 hash；版本升级建议整体完成并重跑集成测试，但不把自动升级做进 P1。
- `help/view/get/query/validate/screenshot` 属低风险候选。
- 对任务目录新产物的普通 `add/set/batch` 可由策略允许。
- 修改用户已有文件、raw/raw-set/add-part、MCP/plugin/install/update/config/watch 等进入 P3 参数级 ask/deny。
- workspace、SSRF 和敏感信息硬边界不可被配置或审批放宽。

### 验收

- contract 只有一个真相源；P2 `skill.yaml` 只引用它。
- binary 版本不匹配时给出清晰 unavailable/diagnostic，不静默切换 Skill。
- 分发 binary 时补齐 Apache-2.0 NOTICE 与第三方声明。

---

## S1.6 质量校验与产物

### Python Skill

- `workbook_schema.json`
- `verified_facts.json`
- `report_dsl.json`
- `slide_dsl.json`
- `quality_report.json`
- `weekly_report.docx`
- `weekly_review.pptx`

### OfficeCLI Skill

- `workbook_schema.json` / `verified_facts.json`（仅数据任务）
- Skill 自有命令或 batch 记录
- docx/xlsx/pptx 成品
- `*.officecli-validation.json`
- `*.officecli-run.json`
- preview PNG/HTML（任务需要时）

两个 Skill 的产物文件名可以不同，但必须登记实际 Skill、引擎版本、facts 来源和验证结果。

---

## S1.7 定向测试

### 确定性测试

- shared inspect/extract 与 expected metrics 一致。
- `office-automation` Python docx/pptx 可打开，facts 无占位泄漏。
- 两个 Skill 均可发现，`_shared` 不可发现。
- OfficeCLI contract、snapshot 来源和 checksum 字段完整。
- OfficeCLI 可选 compiler 仍只输出预期 batch，真实 binary case 由 `OFFICECLI_TEST_BIN` 触发。

### 测试组织

- 共享 facts 测试可以保留在 `tests/skills/test_office_weekly.py`。
- Python Skill 与 OfficeCLI Skill 的测试使用各自脚本目录，不能再通过同一 `--backend` 参数切换。
- 普通 CI 不动态下载 OfficeCLI；固定 binary 集成环境单独运行。

---

## S1.8 WebUI Artifact 展示

现有 `OfficeArtifactsPanel` 继续识别 docx/pptx、OfficeCLI batch/validation/run sidecar 和 preview。P1 不新增复杂对比页面，只确保两个 Skill 的产物均能列出、预览或下载。

---

## S1.9 静态 `plan` 工具【已完成，继续复用】

- 固定 action：`create/get/confirm/update_step/complete`。
- plan hash 只覆盖不可变契约；确认后才激活。
- expected artifacts、步骤依赖和完成检查由工具执行。
- P3 将计划确认与高风险审批统一接入可持久化 approval 语义。
- P4 durable checkpoint 仅服务已确认计划任务。

---

## 阶段出口检查

- [x] `office-automation` 是独立 Python Skill，不依赖 OfficeCLI。
- [x] `officecli` 是独立 Skill，保留官方完整能力并受 Runtime Policy 治理。
- [x] 两个 Skill 共享 facts/constraints，但不强制共享 DSL。
- [x] 默认优先 OfficeCLI，`disabledSkills` 可单独禁用任一 Skill。
- [x] shared core 不进入 Skill summary。
- [x] Python deterministic chain 通过；固定 binary OfficeCLI 集成 case 可单独运行。
- [x] 现有 plan tool 行为不回归。
- [x] P1 代码说明、总方案、P2-P8 计划与 `docs/修改记录.md` 同步更新。
