# P1 双 Office Skill 垂直切片

> 状态：P1 Core 已完成；P1.1 `OfficePython` 通用化与改名待实施。
> 目标：用两个独立 Office Skill 验证共享事实层、独立工作流和静态计划契约，并为后续公开 benchmark 建立公平的 Python 基线。

## 已落地边界

### 共享 deterministic core

`nanobot/skills/_shared/office_core/` 负责 workbook 检查、事实抽取和公共约束；不含 `SKILL.md`，不能被发现为 Skill。

- 定量分析或生成定量结论时必须产出 `verified_facts.json`。
- 纯格式、检查、提取和批注不强制运行事实层。
- 用户直接提供的数字可登记为 `source=user_provided`。
- 两个 Skill 的关键数字都必须回溯到相同 fact id。

### 当前 `office-automation`

- 当前是独立 Python Skill，不依赖 OfficeCLI。
- 当前仍保留 report/slide DSL、validator、`python-docx`、`python-pptx` 和 quality report；这些窄工作流只属于 P1 Core 的历史实现，不是 P1.1 的目标接口。
- 不再包含 backend 切换、OfficeCLI 参数或 OfficeCLI 能力说明。

### `officecli`

- 独立 Skill，固定 OfficeCLI v1.0.135 来源、snapshot 和 provider contract。
- 保留 help/view/get/query、DOM、batch、validate、screenshot、raw、MCP、plugin、watch 等完整能力说明。
- 不在 Skill 层删除高风险能力；P3 按目标和参数 allow/ask/deny。
- Mybot 随 Python 包安装同名 launcher，首次使用时按 contract 准备固定 binary；任务内不得调用上游 latest/install/update。
- 数据任务消费 shared facts；OfficeCLI 可使用自己的命令和中间表示。现有 DSL→batch 仅是可选兼容 helper。

### 路由与开关

- 普通 Office 请求默认优先 `officecli`。
- 用户明确要求 Python 时当前使用 `office-automation`；P1.1 完成后只使用 `office-python`。
- 比较任务可运行两个 Skill，共享输入和 facts。
- `disabledSkills` 可分别禁用；OfficeCLI unavailable 只影响 `officecli`，回退时必须说明原因。
- 路由写在 Skill description/manifest，不在 Runtime 写 Office 私有分支。

### 静态 `plan`

- 固定 `create/get/confirm/update_step/complete`。
- WebUI 输入框提供单轮“仅规划”模式：本轮只允许 plan 与只读检查工具，计划停在待确认状态。
- 普通 WebUI 模式下复杂任务可自动创建并激活 plan，随后持续更新执行步骤。
- plan hash 绑定不可变契约；仅规划模式可通过计划卡片显式确认后执行。
- 依赖和 expected artifacts 由工具校验。
- WebUI 计划卡片展示 pending / in progress / done / skipped，并提供“执行计划”入口。
- P3 接入三档 InteractionRequest；P4 仅为 active/completed 且 `approved_plan_hash` 绑定当前 hash 的计划落 durable checkpoint。

## 产物与验证

Python Skill：schema、facts、report/slide DSL、quality report、docx、pptx。

OfficeCLI Skill：必要时的 schema/facts、命令或 batch、Office 成品、validation/run sidecar、preview。

必须验证：

- 两个 Skill 可独立发现、禁用和运行，`_shared` 不进入 summary。
- OfficeCLI 固定资产首次下载失败时 Python 链仍通过，并明确报告 unavailable 原因。
- shared facts 与 P0 expected metrics 一致，产物关键数字无占位泄漏。
- OfficeCLI contract、来源、版本和 checksum 完整；launcher 按平台首次下载并校验 v1.0.135，普通单元测试不访问网络，真实 binary case 可由 launcher 或 `OFFICECLI_TEST_BIN` 触发。
- 按需下载 runtime 时维护 Apache-2.0 第三方声明。
- WebUI `OfficeArtifactsPanel` 能列出、预览或下载两条路径的产物。
- “仅规划”消息不能获得写文件、Shell、CLI、MCP 等执行型工具；普通复杂任务的 plan 状态可在 UI 中实时更新。

## 阶段出口

- [x] 两个 Office Skill 独立，facts/constraints 共享但 DSL 不强制共享。
- [x] 默认路由、availability 和 `disabledSkills` 语义明确。
- [x] Python deterministic chain 与 plan tool 不回归。
- [x] WebUI 支持仅规划、自动 plan-and-execute 与结构化步骤卡片。
- [x] 固定 OfficeCLI 集成环境可验证 sidecar、preview 和成品。

## P1.1 OfficePython 通用化（待实施）

当前代码中的 `office-automation` 是面向 Excel facts -> 周报/PPT 的窄工作流。P1.1 直接将其改名为展示名 `OfficePython`、Skill id `office-python`，删除旧 id 和旧窄工作流，扩展成不依赖 OfficeCLI 的通用 Python Office 基线；实施完成前，README、benchmark 和阶段出口不得宣称新能力已实现。

### 能力与接口

- DOCX 使用 `python-docx + lxml`，XLSX 使用 `openpyxl`，PPTX 使用 `python-pptx`；LibreOffice headless 只负责打开、重算、转换和渲染验证。
- Skill 只调用独立 Python 入口 `scripts/office.py`，通过 request/result JSON 文件通信；不新增 Runtime typed tool。
- request 固定包含 `schema_version`、`operation`、`format`、输入/输出 artifact 引用、中立 `selector`、payload 和 options；result 固定包含 `status`、matches/changes、artifact、validation、rendered assets、warnings 和 error。
- 提供统一的 `inspect/query/create/apply/validate/render` JSON CLI，覆盖 DOCX 文本/段落/样式/列表/表格/图片/页眉页脚、XLSX 工作表/单元格/公式/图表和 PPTX 幻灯片/基础布局。
- selector 保持格式中立，不模仿 OfficeCLI DOM；输入文件始终只读，`create/apply/render` 只能写 task artifact。批量 apply 在临时目录中全量执行，全部成功后原子发布，失败不发布输出。
- 删除 report/slide DSL、专用 renderer、周报 workflow 和原 `tests/fixtures/office_weekly/`；不迁移旧周报数据、固定 DSL、expected metrics/constraints 或业务回归。保留 `_shared/office_core`、verified facts schema、通用验证器和不进入公平比较路径的兼容 helper；P1.1 另建只覆盖新 JSON 接口与三种格式的最小中立 fixture。
- 删除依赖旧 fixture/DSL 的周报专用测试；仍有效的 OfficeCLI launcher/provider contract case 移入现有 `test_officecli_runtime.py`，OfficePython 新接口使用独立中立测试，不继续沿用 `test_office_weekly.py` 的业务命名和断言。
- tracked changes、复杂母版/动画、SmartArt 和库本身无法可靠保真的 OOXML 特性必须显式返回 `unsupported`，不能静默降级或伪造成功。
- 直接将目录、展示名和 Skill id 改为 `office-python`；旧 `office-automation` id、显式路由和历史兼容迁移代码删除。启动时仅从 `disabledSkills` 删除旧 id，不迁移成新 id，也不恢复旧会话路由。

### 运行环境

- Python 依赖使用独立、精确的 pip constraints；不引入 uv/Poetry，不把 OfficeBench 的完整旧依赖表安装进 Mybot 主环境。
- LibreOffice 不由项目下载或打包。`prepare`/release 环境锁记录 `soffice` 的真实路径、版本和校验信息；smoke/release 缺失或版本漂移即失败，CI 不依赖 LibreOffice。
- 当前机器可见的 `LibreOfficeDev 26.8.0.0.alpha0` 来自 Codex runtime，不能作为项目交付资产或跨机器基线；正式 release 必须使用外部预装且显式锁定的版本。

### 公平比较

- `office-python` 与 `officecli` 使用相同输入快照、`gpt-5-6-luna`、Runtime policy、任务约束和 evaluator；一次 run 只启用一个被测 Skill。
- 两者可共享 facts、OpenXML/渲染验证器和 benchmark adapter，但 `office-python` 不调用 OfficeCLI 或其 compiler/backend。
- 分开报告 capability coverage 与共同支持任务的质量、token、LLM/工具耗时、工具成功率和 Agent 循环步数；`unsupported` 不冒充执行错误。
- 不在 prompt、case 路由或评分中人为偏袒 OfficeCLI；其统一跨格式接口、DOM/query/batch/view/validate/raw 等优势必须由真实结果证明。

### P1.1 出口

- [ ] `office-python` 可独立完成 DOCX/XLSX/PPTX 的读取、创建、常用编辑、验证和渲染。
- [ ] 旧 id、目录、显式路由和旧 DSL/周报链已删除；`disabledSkills` 清理行为有测试。
- [ ] 原周报 fixture 和专用测试已删除；有效 OfficeCLI contract case 已归位，新建的最小中立 fixture、shared facts、OpenXML/渲染验证器回归通过。
- [ ] 两个 Skill 在固定 smoke 上生成 capability coverage 与共同任务比较报告。
