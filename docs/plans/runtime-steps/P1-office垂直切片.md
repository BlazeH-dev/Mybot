# P1 双 Office Skill 垂直切片

> 状态：已完成。目标是用两个独立 Office Skill 验证共享事实层、独立工作流和静态计划契约。

## 已落地边界

### 共享 deterministic core

`nanobot/skills/_shared/office_core/` 负责 workbook 检查、事实抽取和公共约束；不含 `SKILL.md`，不能被发现为 Skill。

- 定量分析或生成定量结论时必须产出 `verified_facts.json`。
- 纯格式、检查、提取和批注不强制运行事实层。
- 用户直接提供的数字可登记为 `source=user_provided`。
- 两个 Skill 的关键数字都必须回溯到相同 fact id。

### `office-automation`

- 独立 Python Skill，不依赖 OfficeCLI。
- 保留 report/slide DSL、validator、`python-docx`、`python-pptx` 和 quality report。
- 不再包含 backend 切换、OfficeCLI 参数或 OfficeCLI 能力说明。

### `officecli`

- 独立 Skill，固定 OfficeCLI v1.0.135 来源、snapshot 和 provider contract。
- 保留 help/view/get/query、DOM、batch、validate、screenshot、raw、MCP、plugin、watch 等完整能力说明。
- 不在 Skill 层删除高风险能力；P3 按目标和参数 allow/ask/deny。
- Mybot 随 Python 包安装同名 launcher，首次使用时按 contract 准备固定 binary；任务内不得调用上游 latest/install/update。
- 数据任务消费 shared facts；OfficeCLI 可使用自己的命令和中间表示。现有 DSL→batch 仅是可选兼容 helper。

### 路由与开关

- 普通 Office 请求默认优先 `officecli`。
- 用户明确要求 Python 时使用 `office-automation`。
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
