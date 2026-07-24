# P2 Skill Pack Manifest 与运行时开关代码说明

> 对应计划：`docs/plans/runtime-steps/P2-skillpack-manifest.md`
> 当前状态：已完成（2026-07-16）。

## 这一阶段解决什么问题

P1 当前有 `office-python` 和 `officecli` 两个 Skill，但只有 `SKILL.md` 还不够解决工程治理问题：

- 程序怎样知道 Skill 的版本、入口、输入输出和 provider 依赖？
- Skill 写坏了，是拖垮整个网关，还是只隔离自己？
- “被用户禁用”“声明非法”“依赖缺失”是不是同一种状态？
- WebUI 怎样告诉用户具体缺什么，而不是只显示“不可用”？
- 第三方旧 Skill 没有 manifest，是否会被全部破坏？

P2 的答案是：在保留 `SKILL.md` 人类/模型指令的同时，增加一个可选但严格的 `skill.yaml`，让程序可以机器化校验和诊断。

## 最重要的分层

```text
SKILL.md
  负责：教模型什么时候使用、按什么步骤使用

skill.yaml
  负责：声明名称、版本、入口、输入输出、工具需求、provider、权限需求、eval

provider contract
  负责：具体外部运行时版本、平台资产、下载地址和 checksum

Runtime Policy
  负责：真正决定一次工具调用 allow / ask / deny
```

这四层不能合并：

- Skill 文档不能自己授权。
- manifest 的 `permissions.required` 只能表达“我需要什么”，不能表达“我已获准”。
- OfficeCLI 的固定二进制版本不能复制到多个 manifest 和脚本里。
- Runtime 不能因为 Skill 自称安全就跳过 Policy。

## 实际做了什么

### 1. 新增 typed manifest schema

文件：`nanobot/agent/skill_manifest.py`

`SkillManifest` 使用 Pydantic 定义 v1 字段：

```text
name / version / description
entrypoints / inputs / outputs
tools.required
providers.<name>.required / contract
permissions.required
evals
```

所有 model 都设置 `extra="forbid"`。这意味着拼错字段不会被静默忽略。例如把 `entrypoints` 写成 `entrypoint`，Skill 会得到带字段路径的 `invalid_manifest`，而不是看似加载成功、运行时才莫名失败。

`version` 当前限定为字面量 `1`，便于以后升级 schema 时显式迁移，而不是让 Loader 猜格式。

### 2. 在 SkillsLoader 中实现兼容加载

文件：`nanobot/agent/skills.py`

发现顺序仍是：

```text
workspace/skills/<name>  优先
nanobot/skills/<name>    builtin 兜底
```

workspace 同名 Skill 会整包覆盖 builtin。这里的“整包”很重要：不能出现 workspace 的 `SKILL.md` 配 builtin 的 `skill.yaml`，否则说明文字和机器声明可能属于两个版本。

加载规则是：

1. 目录必须先有 `SKILL.md`，它仍是 Skill 的发现入口。
2. 没有 `skill.yaml`：按 legacy frontmatter 继续工作。
3. 存在 `skill.yaml`：必须成功解析和校验。
4. manifest 存在但损坏：只隔离这个 Skill，不退化为 legacy 模式。

第 4 点叫“局部 fail closed”：

- fail closed：有风险或状态不明时拒绝使用。
- 局部：只影响目标 Skill，不让一个坏插件拖垮整个 Agent。

### 3. 校验安全相对路径与 provider contract

manifest 中的 entrypoint 和 contract 必须满足：

- 非空。
- 不是绝对路径。
- 不包含 `..`。
- resolve 后仍位于 Skill 目录内。
- 声明的文件真实存在。

这是为了防止一个 workspace Skill 用 manifest 指向 `~/.ssh`、其他 Skill 或任意宿主文件。

对于 OfficeCLI，Loader 还会：

1. 读取 `references/officecli-runtime.json`。
2. 确认 `provider` 确实是 `officecli`。
3. 调用 `select_officecli_asset()`。
4. 按当前 OS/CPU 检查是否存在平台资产、版本和合法 SHA-256。

注意：catalog 校验只检查 contract，不在列表页擅自运行上游 install/update。

### 4. 把状态拆成 enabled、valid、available

`get_skill_status()` 返回四个关键结果：

| 字段 | 含义 | 例子 |
| --- | --- | --- |
| `enabled` | 用户是否允许候选 | `disabledSkills` 中存在该名称时为 false |
| `valid` | manifest/contract 结构是否合法 | YAML 损坏、name 不匹配、contract 非法时为 false |
| `available` | 当前机器能否真正运行 | enabled + valid + 没有缺 binary/env 等原因 |
| `status` | 给 UI 的归一化状态 | disabled / invalid / unavailable / available |

为什么不只用一个布尔值？因为修复方式不同：

- disabled：用户点“启用”。
- invalid：开发者修 manifest。
- unavailable：安装依赖或配置环境。
- available：可以进入 Agent 候选。

结构化 `reasons` 还包含稳定 code、message 和可选 field，例如：

```text
disabled
invalid_manifest
missing_entrypoint
missing_contract
invalid_contract
missing_binary
missing_env
```

### 5. Agent 与 WebUI 消费不同视图

Agent 只应看到真正可执行的 Skill：

- `build_skills_summary()` 只列 available Skill。
- `load_skills_for_context()` 再次检查 availability。
- always Skill 和 `/skill` 不能绕过 disabled/invalid/unavailable。

WebUI 则需要看到完整 catalog，才能帮助用户修复问题：

- 展示来源、版本和状态。
- 展示逐条 availability diagnostics。
- 展示 required tools、permissions 和 providers。
- 不向浏览器暴露本地绝对路径等隐私信息。

这个设计体现了“执行面最小化、管理面可解释”。

### 6. 为两个 Office Skill 添加 manifest

新增：

```text
nanobot/skills/office-python/skill.yaml
nanobot/skills/officecli/skill.yaml
```

Python Skill 声明自己的脚本入口、xlsx/会议纪要输入、facts/docx/pptx/quality report 输出，以及文件读写和进程执行需求。

OfficeCLI Skill 声明 Office 文档输入、成品/validation/run/preview 输出，并引用唯一的 `references/officecli-runtime.json`。

`pyproject.toml` 把 `nanobot/skills/**/*.yaml` 纳入包构建，否则开发目录能用、安装 wheel 后 manifest 却会消失。

### 7. 复用唯一开关并支持热刷新

P2 没有新造 `skill_enabled.json`，继续使用：

```text
agents.defaults.disabledSkills
```

设置页修改后：

1. API 持久化 disabled list。
2. WebUI 发送内部 `skills_reload` control message。
3. 运行中的 AgentLoop 更新 `context.skills.disabled_skills`。
4. SubagentManager 同步相同 disabled set。
5. 当前正在执行的回合不突变，从下一轮开始生效。

这样既避免重启网关，也避免执行到一半工具集合突然变化。

### 8. 支持单轮显式 Skill 路由

WebUI 输入框可以选择 `@skill-name`，通过 WebSocket 的 `selected_skills` 字段发送，而不是把它当普通聊天文字。

运行时处理：

1. WebSocket 只做名称格式、数量上限等基础归一化。
2. AgentLoop 用最新 Loader 再检查存在性、enabled 和 availability。
3. 合法选择进入本轮 `# Selected Skills` 上下文。
4. 有显式选择时，隐藏自动候选 summary，减少模型改路由的可能。
5. 未选择时保留原来的渐进披露和自主路由。

这是“用户指定本轮方法”，不是永久修改全局配置。

## 一次加载的完整路径

```text
扫描 workspace Skill
  -> 同名名称占位，屏蔽 builtin
  -> 扫描剩余 builtin Skill
  -> 检查 disabledSkills
  -> 读取可选 skill.yaml
       ├── 不存在：legacy frontmatter
       └── 存在：Pydantic 严格校验
  -> 校验 entrypoint / provider contract / bins / env
  -> 计算 enabled / valid / available / status / reasons
       ├── Agent：只保留 available
       └── WebUI：保留完整 catalog 和诊断
```

## 为什么这样设计

### 为什么 manifest 是可选的

nanobot 原有 Skill 和第三方 Skill 只依赖 `SKILL.md`。如果强制所有旧 Skill 一次迁移，会产生很大的兼容成本。可选 manifest 让新能力逐步增强。

### 为什么“存在就严格”

如果坏 manifest 被当成不存在，开发者会误以为声明生效，实际却偷偷走 legacy 路径。安全和运行依赖都可能与预期不一致，因此必须显式失败。

### 为什么 permissions 只展示不授权

插件是潜在不可信输入。允许插件通过自己的 manifest 获得文件、网络或命令权限，相当于让申请人自己审批自己。P3 必须基于真实工具参数、workspace scope 和 hard boundary 独立判断。

### 为什么版本/checksum 放 provider contract

manifest 描述 Skill 依赖哪个 provider；contract 描述 provider 的具体可执行资产。两者变化频率和责任不同，分开后不会在多个位置复制安全关键版本信息。

## 验证与证据

主要测试覆盖：

- 合法 manifest 与无 manifest legacy Skill。
- YAML/schema/name 错误及准确字段路径。
- workspace 整包覆盖 builtin。
- 绝对路径、`..` 和 contract 路径逃逸。
- 缺 entrypoint、contract、binary、env。
- disabled/invalid/unavailable 不进入 summary、context 和 `/skill`。
- WebUI catalog 保留诊断但隐藏本地敏感路径。
- 即时开关同步主 Agent 与 Subagent。
- `selected_skills` 的名称归一化和运行时复核。
- wheel 中包含两个 `skill.yaml`、contract 和 launcher。

历史阶段验收记录为：后端定向回归 `101 passed`，WebUI 当时全量 `401 tests passed`，production build 通过。它们是当次快照，当前结果应以重新运行测试为准。

## 边界与未做内容

- P2 没有实现权限管理后台。
- manifest permissions 不会注册或解锁工具。
- P2 没有自动安装任意 provider。
- P2 没有把 Skill 变成复杂插件进程或工作流引擎。
- availability 是当前环境诊断，不代表具体一次高风险调用会被 Policy 放行。

## 面试怎么讲

### 30 秒回答

> P2 我给 Skill 增加了可选的 typed manifest，但保留原 `SKILL.md` 兼容。manifest 一旦存在就严格校验，损坏只隔离当前 Skill；Loader 把状态拆成 enabled、valid、available，并向 Agent 和 WebUI提供不同视图。版本和 checksum 放独立 provider contract，permissions 只声明不授权，真正权限仍由 P3 Policy 决定。

### 高频追问

**fail closed 会不会让系统太脆弱？**

这里是局部 fail closed：坏 Skill 自己不可执行，但网关和其他 Skill 正常运行。相比全局崩溃和静默忽略，这个边界兼顾安全与可用性。

**为什么 Agent 看不到 unavailable Skill，WebUI 却要看到？**

Agent 看到它只会产生无意义调用；用户需要看到它才能知道如何修复。执行候选和诊断 catalog 的目标不同。

**显式 `@skill` 能否绕过禁用？**

不能。浏览器提交只是意图，AgentLoop 会用当前 Loader 再检查 availability。客户端输入永远不能作为授权事实。

**workspace Skill 为什么优先？**

允许项目定制或覆盖 builtin，但必须整包覆盖，避免说明和 manifest 混源。

## 自测：读完 P2 应该能回答

1. `SKILL.md`、`skill.yaml`、provider contract、Policy 的职责分别是什么？
2. enabled、valid、available 有什么区别？
3. 为什么 manifest 可选，但存在后不能回退？
4. 为什么 permissions 声明不能直接授权？
5. 显式选择 Skill 的消息怎样防止绕过 disabled 状态？
6. 为什么包构建必须显式包含 YAML 文件？

## 对后续阶段的影响

- P3 可以把 permissions 当成风险提示输入，但不能信任它做授权。
- P5 可以用 `evals` 标识和 availability reason 做结构化评测。
- P6 Research Skill 若落地，应直接复用同一 manifest、开关和诊断机制。
- P8 子 Agent 与主 Agent 共享最新 disabled set，避免派生后重新暴露已禁用 Skill。
