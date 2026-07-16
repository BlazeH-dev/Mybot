# P2 Skill Pack Manifest 代码变更说明

> 当前状态：仅规划，尚未执行；不表示 manifest 代码已落地。
> 对应计划：`docs/plans/runtime-steps/P2-skillpack-manifest.md`

2026-07-16 精简后的必做边界不变：可选 typed `skill.yaml`、损坏 Skill 局部 fail closed、结构化 availability、复用 `disabledSkills`、permissions 只声明需求且不能授予权限。OfficeCLI manifest 只引用唯一 provider contract。

2026-07-16 已先落地 OfficeCLI provider 准备层：`pyproject.toml` 注册同名 console script，`nanobot.officecli_runtime` 只读取现有 provider contract，完成平台选择、固定资产下载、SHA-256 校验、原子缓存、并发锁和禁用自更新。该准备层不新增 manifest 版本/checksum 真相源；P2 typed `skill.yaml` 和通用 Registry 仍保持“仅规划”。

实现后必须补充真实文件、接线、测试和验证结果。
