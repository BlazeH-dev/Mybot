# P2 Skill Pack Manifest 与开关

> 状态：已完成。Skill 已支持声明、校验、诊断和禁用，同时兼容现有 `SKILL.md`。

## 实施契约

### 发现与校验

- 在 workspace 优先、builtin 兜底的 Skill 根目录旁路读取可选 `skill.yaml`。
- 无 manifest：继续按 `SKILL.md` frontmatter 兼容加载。
- manifest 存在：必须通过 typed schema；解析失败不能退化成“无 manifest”。
- 损坏 manifest 只使该 Skill `invalid/unavailable`，不得拖垮网关或其他 Skill。
- `_shared/office_core` 不参与 Skill 发现。

### 最小 schema

```yaml
name: officecli
version: 1
description: ...
entrypoints: []
inputs: []
outputs: []
tools:
  required: []
providers:
  officecli:
    required: true
    contract: references/officecli-runtime.json
permissions:
  required: []
evals: []
```

- permissions 只表达需求，不能授予或放宽 Runtime 权限。
- OfficeCLI 版本、平台和 checksum 只存在 provider contract；manifest 只引用相对路径。
- schema 错误返回字段路径和可读原因。

### Availability 与开关

- `officecli` 检查随包 launcher、固定 binary、版本、平台和 contract；launcher 可按 contract 准备缺失缓存。
- availability 返回结构化原因，如 `missing_binary`、`version_mismatch`、`invalid_manifest`。
- 复用 `agents.defaults.disabled_skills` / WebUI `disabledSkills`，不建立平行开关。
- 禁用或 unavailable 的 Skill 不进入 summary、候选和执行路径；Registry/Agent 不得调用上游 latest/install/update、自动启用或静默切换。随包 provider launcher 按 contract 准备固定 runtime 不视为动态扩权安装。

API/WebUI 只展示 name/version/source、enabled、valid/available、缺失依赖和权限需求摘要，不做权限管理后台。

## 实际落地

- 新增 `nanobot.agent.skill_manifest` 的 Pydantic v1 schema，未知字段、错误类型、name 与目录不一致、危险相对路径均局部 fail closed，并返回字段路径。
- `SkillsLoader` 作为当前 Skill Registry：workspace 同名 Skill 覆盖 builtin；无 `skill.yaml` 继续走 legacy frontmatter；manifest 存在但无效时不得回退。
- availability 统一返回 `enabled`、`valid`、`available`、`status`、结构化 `reasons`、provider 与 legacy requirements；disabled/invalid/unavailable Skill 不进入 Agent summary、显式上下文加载或 `/skill`。
- WebUI catalog 仍展示 disabled/invalid/unavailable Skill 及诊断、工具/权限声明和 provider 状态；权限字段仅展示，不接入 ToolRegistry 或 policy 决策。
- `officecli` 已随包提供 manifest。manifest 只引用 `references/officecli-runtime.json`；当前平台资产选择、固定版本和 checksum 校验继续由该唯一 contract 与随包 launcher 负责。
- wheel/sdist 构建包含 `nanobot/skills/**/*.yaml`。

## 测试与出口

- OfficeCLI manifest 合法可读，旧 Skill 无 manifest 行为不变。
- 坏 YAML/schema、缺 contract 只隔离目标 Skill并给精确错误。
- OfficeCLI launcher/固定资产准备失败不影响 Python Skill。
- `disabledSkills` 在 loader、API 和 WebUI 端到端生效。
- manifest permissions 无法改变 policy 结果。
