# P2 SkillPack manifest 与开关 — 详细步骤

> 所属：`docs/plans/Mybot通用AgentRuntime与办公自动化SkillPack整合方案.md`
> 状态：仅规划，未执行。2026-07-14 按双 Office Skill 边界重写。
> 阶段出口：Skill 可声明、可校验、可诊断、可启用/禁用；损坏的单个 Skill 不拖垮网关，也不能绕过 manifest 治理。

---

## S2.1 读取可选 `skill.yaml`

- 在 workspace 优先、builtin 兜底的 Skill 根目录旁路读取 `skill.yaml`。
- 没有 manifest：兼容旧 Skill，继续按 SKILL.md frontmatter 加载。
- 存在 manifest：必须进入 schema 校验，不能在解析失败后退化为“没有 manifest”。
- 为 `office-automation` 与 `officecli` 分别新增 manifest。
- `_shared/office_core` 没有 SKILL.md/manifest，不参与发现。

## S2.2 最小 typed schema

首版字段：

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

约束：

- manifest permissions 只表达能力需求，不授予权限。
- OfficeCLI 版本、checksum、平台、命令与能力信息只存在 provider contract；manifest 只保存相对引用，避免重复配置漂移。
- schema 错误必须返回字段路径和可读原因。

## S2.3 局部 fail closed

- 无 manifest：兼容加载。
- 合法 manifest：正常加载并展示治理信息。
- manifest 存在但解析/校验失败：该 Skill 标记 `invalid/unavailable`，禁止进入 summary 和执行候选。
- 网关、聊天和其他 Skill 继续工作。
- 核心 Mybot 配置损坏才阻止网关启动。

## S2.4 依赖与可用性

- `office-automation` 检查 Python 依赖与共享 core。
- `officecli` 检查固定 binary、版本、平台和 provider contract。
- OfficeCLI unavailable 不影响 Python Skill。
- availability 返回结构化原因，例如 `missing_binary`、`version_mismatch`、`invalid_manifest`。
- 不在 Agent 任务中静默安装或自动切换 Skill。

## S2.5 启用/禁用开关

- 复用现有 `config.agents.defaults.disabled_skills` / WebUI `disabledSkills`，不新建平行配置。
- 两个 Office Skill 默认启用。
- 被禁用的 Skill 不进入 summary、候选和执行路径。
- 用户提及已禁用 Skill 时只提示打开开关，不自动修改配置。
- 未显式指定时 Office 请求默认优先 `officecli`；OfficeCLI disabled/unavailable 时才选择 Python Skill并说明原因。

## S2.6 WebUI/API 最小展示

API 返回：

- name/version/source
- enabled/disabled
- valid/invalid/unavailable
- 缺失依赖原因
- provider 名称与验证版本摘要
- permissions requirements 摘要

WebUI 只需提供开关和诊断文本，不做复杂权限管理后台。

## 定向测试

- 两个合法 Office manifest 可读。
- 缺 manifest 的旧 Skill 行为不变。
- 坏 YAML/坏 schema → 该 Skill unavailable，其他 Skill 正常。
- provider contract 路径不存在 → 精确错误。
- 禁用任一 Office Skill 后 summary 不含它。
- OfficeCLI binary 缺失只影响 `officecli`。

## 阶段出口检查

- [ ] 两个 Office Skill 均有独立 manifest。
- [ ] OfficeCLI manifest 只引用 provider contract，不重复配置。
- [ ] 非法 manifest 局部 fail closed。
- [ ] permissions 不会授予或放宽 Runtime 权限。
- [ ] `disabledSkills` WebUI/API/loader 端到端生效。
- [ ] 旧 Skill 兼容行为不回归。
