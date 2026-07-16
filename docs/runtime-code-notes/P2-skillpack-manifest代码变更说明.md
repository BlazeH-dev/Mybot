# P2 Skill Pack Manifest 代码变更说明

> 当前状态：已执行（2026-07-16）。
> 对应计划：`docs/plans/runtime-steps/P2-skillpack-manifest.md`

## 1. 阶段目标

在不破坏 legacy `SKILL.md` 的前提下，为 Skill 增加机器可读的声明、局部校验、结构化可用性和统一开关。坏 Skill 只能隔离自己，不能拖垮网关、污染 Agent prompt 或通过显式 `/skill` 重新进入执行候选。

## 2. 代码变更

### 2.1 Typed manifest

- 新增 `nanobot/agent/skill_manifest.py`，使用 Pydantic 定义 manifest v1：`name`、`version`、`description`、`entrypoints`、`inputs`、`outputs`、`tools.required`、`providers.*`、`permissions.required`、`evals`。
- 所有层级均使用 `extra="forbid"`。schema/type/version 错误返回 `invalid_manifest` 和具体字段路径，不接受未知字段后继续运行。
- manifest 是可选文件；目录只有 `SKILL.md` 时继续使用原 frontmatter 和 `metadata.nanobot.requires`。

### 2.2 SkillsLoader / Registry 语义

- `nanobot/agent/skills.py` 在 workspace 优先、builtin 兜底的既有发现规则上旁路读取 `skill.yaml`；workspace 同名 Skill 的 markdown、manifest 和状态始终作为一个整体覆盖 builtin。
- manifest 存在但 YAML/schema/name 非法时局部 fail closed，不退化成“没有 manifest”。
- entrypoint/provider contract 必须是 Skill 目录内的安全相对路径；绝对路径、`..` 逃逸、缺文件和坏 JSON 都返回结构化原因。
- OfficeCLI provider contract 会校验 provider 标识，并通过 `select_officecli_asset()` 校验当前 OS/CPU 对应的固定资产声明。版本、平台资产名和 SHA-256 仍只来自 `references/officecli-runtime.json`，manifest 不复制第二份真相。
- legacy `requires.bins/env` 合并进统一状态，原因码包括 `disabled`、`invalid_manifest`、`missing_entrypoint`、`missing_contract`、`invalid_contract`、`missing_binary`、`missing_env`。
- 状态定义：配置禁用为 `disabled`；manifest/contract 结构失效为 `invalid`；声明合法但运行依赖缺失为 `unavailable`；只有 enabled、valid 且无原因时为 `available`。
- `list_skills()` 默认不返回 disabled，Agent summary、`load_skills_for_context()`、always Skill 和 `/skill` 只接受 available Skill；WebUI 可显式 `include_disabled=True` 获取完整 catalog。

### 2.3 Office Skill manifest 与打包

- 新增 `nanobot/skills/office-automation/skill.yaml`，声明 Python Office 工作流的入口、输入输出、工具、权限需求和 eval。
- 新增 `nanobot/skills/officecli/skill.yaml`，声明 OfficeCLI 入口、输入输出、工具、权限需求、eval，并只引用唯一 provider contract。
- `pyproject.toml` 增加 `nanobot/skills/**/*.yaml` 构建包含规则，换机重新安装项目后 manifest 会随 Python 包交付。
- OfficeCLI 的固定 binary 准备继续由 P1 后续补强的随包 `officecli` launcher 完成：首次执行只按 contract 下载 v1.0.135、验 SHA-256、原子缓存并关闭上游自更新；P2 catalog 校验不会调用 latest/install/update。

### 2.4 API 与 WebUI

- `nanobot/webui/skills_api.py` 的列表和详情返回 version、enabled、valid、available、status、结构化 reasons、legacy requirements、工具/权限声明和 provider 状态，同时继续隐藏本地绝对路径。
- `webui/src/components/settings/SkillsCatalogSettings.tsx` 展示 manifest 版本、disabled/invalid/unavailable 状态、逐条诊断、声明工具、声明权限和 provider 状态；中英文文案已补齐。
- permissions 仅进入只读 payload/UI。P2 没有把它接入 ToolRegistry、`WorkspaceScope` 或任何 policy allow 路径，因此声明不能授予权限、解除禁用或放宽 hard boundary。

### 2.5 即时开关与单轮显式路由（2026-07-16）

- Skills 设置详情页新增启用/禁用按钮。它仍只写入唯一的 `agents.defaults.disabledSkills`，但 WebUI 通过内部 `skills_reload` control 消息立即同步运行中的 `AgentLoop` 与 `SubagentManager`；当前正在执行的回合不改变，后续回合无需重启网关即可按新状态生效。
- `GET /api/webui/skills` 和详情接口按磁盘上的最新配置计算 catalog，前端收到成功结果后刷新对话框候选，避免已禁用 Skill 短暂仍可选择。
- 输入框 `@` 候选新增可用 Skill。选中或输入精确 `@skill-name` 会以 `selected_skills` 随 WebSocket 消息发送；AgentLoop 再以当前 loader 校验名称和可用性，禁用、未知或不可用项不能绕过开关。
- 有显式选择时，`ContextBuilder` 将所选 `SKILL.md` 正文放入本轮 `# Selected Skills`，声明其为必须遵循的路由契约，并不再暴露可自动选择的 Skill 摘要；未选择时完全保留原有渐进披露和模型自主选择行为。

## 3. 为什么这么做

- 可选 manifest 保留第三方/旧 Skill 的兼容性；“文件一旦存在就严格校验”避免配置损坏被静默忽略。
- 将 `enabled`、`valid`、`available` 分开后，UI 可以解释问题，Agent 又只看到真正能执行的候选。
- workspace 整包覆盖 builtin，避免 markdown 来自 workspace、manifest 却误读 builtin 的混合来源。
- provider contract 与 manifest 分工：manifest 描述依赖关系，contract 独占具体二进制版本、平台和 checksum，避免安全关键数据漂移。
- 权限声明不直接生效，为 P3 Policy 保留“需求输入”和“授权决策”之间的硬边界。

## 4. 运行路径

1. Loader 发现 `SKILL.md`，workspace 同名项先占位并屏蔽 builtin。
2. 若存在 `skill.yaml`，解析 typed schema、校验目录名和所有本地引用；若不存在，保留 legacy 行为。
3. 读取 frontmatter 的 CLI/ENV requirements，并合并 manifest/provider 诊断。
4. 计算 `enabled → valid → available → status`，但保留完整 reasons 供 API/UI 使用。
5. Agent 侧只消费 available 列表；WebUI 侧消费包含 disabled/invalid/unavailable 的完整 catalog。
6. 真正调用 OfficeCLI 时，随包 launcher 根据唯一 contract 准备并执行固定 runtime；准备失败只影响 `officecli`，不会改变 `office-automation` 的独立状态。

## 5. 验证方式

- `ruff check nanobot/ tests/agent/test_skill_manifest.py tests/webui/test_skills_api.py`
- manifest/loader/API/Office/command/WebSocket 定向 pytest：覆盖合法和 legacy manifest、坏 YAML/schema、字段路径、workspace 覆盖、路径逃逸、缺 entrypoint/contract、disabled catalog、无效 Skill 不进 summary/context/`/skill`、两个 Office manifest 和 API 隐私边界。
- WebUI `app-layout` 回归覆盖 catalog 状态、诊断和声明展示；TypeScript production build 通过。
- 即时开关/显式路由定向回归：覆盖 selected Skill 注入并抑制自动候选、disabled/unknown 过滤、WebSocket 名称归一化、热刷新同步主 Agent 与子代理、配置持久化；前端 production build 通过。
- 最终结果：后端 Skill/P2 定向回归 `101 passed`（含真实固定 OfficeCLI 集成）；WebUI 全量 `29 files / 401 tests passed`；production build 通过。
- `pip wheel . --no-deps` 构建成功，并从 wheel 清单确认 `office-automation/skill.yaml`、`officecli/skill.yaml` 与 `nanobot/officecli_runtime.py` 均已随包交付。

## 6. 后续影响

- P3 可以读取 permissions 作为“申请了什么”的输入，但必须由独立 policy 决定 allow/ask/deny，不能直接信任 manifest。
- P5 Eval 可直接消费 `evals` 标识和结构化 unavailable 原因。
- 后续新增 provider 时，应继续把可变版本/平台/checksum 放入 provider contract，而不是扩散到 manifest 或 Skill 文档。
