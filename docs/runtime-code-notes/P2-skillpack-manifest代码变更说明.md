# P2 Skill Pack Manifest 代码变更说明

> 当前状态：仅规划，尚未执行；不表示 manifest 代码已落地。
> 对应计划：`docs/plans/runtime-steps/P2-skillpack-manifest.md`

2026-07-16 精简后的必做边界不变：可选 typed `skill.yaml`、损坏 Skill 局部 fail closed、结构化 availability、复用 `disabledSkills`、permissions 只声明需求且不能授予权限。OfficeCLI manifest 只引用唯一 provider contract。

实现后必须补充真实文件、接线、测试和验证结果。
