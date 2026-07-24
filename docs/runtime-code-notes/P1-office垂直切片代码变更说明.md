# P1 Office 垂直切片代码变更说明

> 对应计划：`docs/plans/runtime-steps/P1-office垂直切片.md`
> 当前状态：P1 Core 与 P1.1 OfficePython 已完成（2026-07-24）。

## 1. 这一阶段解决什么问题

Office 文件任务不是“让模型写一段文字”就结束。它同时需要：

1. 从输入文件获得可核对的结构和数字。
2. 让模型负责规划与内容组织，让确定性代码负责文件操作。
3. 不覆盖用户输入，批量编辑失败时不留下半成品。
4. 对 Python 库无法保真的 OOXML 特性诚实返回 unsupported。
5. 用两个独立 Skill 在相同条件下比较，而不是把 OfficeCLI 藏进 Python baseline。

最终结构是：

```text
SkillsLoader
  ├── officecli
  ├── office-python
  └── _shared/office_core（代码依赖，不是 Skill）

office-python request JSON
  -> scripts/office.py
  -> python-docx / openpyxl / python-pptx
  -> 临时 OpenXML 文件
  -> validate
  -> os.replace() 原子发布
  -> result JSON
```

## 2. 双 Skill 边界

### OfficePython

`nanobot/skills/office-python/` 是通用 Python baseline：

```text
SKILL.md
skill.yaml
references/
  constraints.txt
  request.schema.json
scripts/
  office.py
```

它只暴露一个脚本入口，不依赖 OfficeCLI、OfficeCLI compiler 或 OfficeCLI backend。支持的传输
操作固定为 `inspect/query/create/apply/validate/render`，格式固定为 `docx/xlsx/pptx`。

### OfficeCLI

`nanobot/skills/officecli/` 继续承担默认通用 Office 路由，使用固定 v1.0.135 contract 和项目
launcher。OfficeCLI 的 DOM/query/batch/view/validate/raw 等能力没有被 Python 接口裁剪；P3 在
运行时按实际工具与参数做 allow/ask/deny。

### Shared facts

`nanobot/skills/_shared/office_core/` 保留 workbook inspect 与 verified facts：定量任务必须先把
关键值写成带 fact id、来源、计算和展示值的结构化事实；纯格式和提取任务不创建形式化空 facts。

共享的是事实、输入快照和验证器，不是某一方的操作 DSL。这样两个 Skill 面对同一数据，但不会因
共享 OfficeCLI action space 而破坏公平性。

## 3. `office.py` 的接口怎样工作

### Request/result

request 必须包含：

```text
schema_version
operation / format
input_artifact / output_artifact
selector
payload
options.artifact_root
```

result 无论成功、失败还是 unsupported，都有稳定字段：

```text
status
matches / changes
artifact / validation / rendered_assets
warnings
error
duration_ms
```

CLI 把错误写进 result JSON 后返回非零 exit code，Agent 不需要从 stderr 猜语义。

### 中立 selector

selector 使用 `paragraph/table/cell/sheet/slide/shape`、index、name、text_contains、range 等通用
概念。例如：

```json
{"kind":"paragraph","text_contains":"Draft"}
{"kind":"cell","sheet":"Data","range":"A1:D20"}
{"kind":"shape","slide":1,"text_contains":"Old title"}
```

它没有 OfficeCLI DOM path，也不会把 Python baseline 变成 OfficeCLI 的另一层 wrapper。

## 4. 三种格式的实现

### DOCX

`python-docx` 负责 paragraph、heading/style、list、table/cell、inline image、header/footer。inspect
额外给出段落、表格、图片、section 和 style 摘要；query 返回可序列化匹配项。

编辑现有 DOCX 前直接检查 OOXML ZIP：出现 `<w:ins>`、`<w:del>`、move 标记或启用 tracked
revisions 时返回 `unsupported_features`。这样不会让 python-docx 保存时静默丢失修订语义。

### XLSX

`openpyxl` 负责 sheet、cell/range、formula 和基础 bar/line chart。query 可以选择 sheet、cell、
formula 或 chart；apply 支持 set/clear cell、append row、add/rename sheet 和 add chart。

读取时保留 formula；`options.data_only=true` 只影响 query 视图，不改变源文件。

### PPTX

`python-pptx` 负责 slide、title/content/blank 基础 layout、shape text、textbox 和 picture。编辑前
检查 `ppt/diagrams/`、多个 slide master 和 `<p:timing>`，分别把 SmartArt、复杂母版和动画/timing
标成 unsupported，避免“文件能保存”被误解为“语义完整保真”。

## 5. 为什么输入只读、输出原子

`_validate_request()` 先做以下检查：

- 输入存在且扩展名与 format 一致。
- output 不能等于 input。
- create/apply/render 的 output 必须位于 `options.artifact_root` 下。
- create/apply 输出扩展名与 Office 格式一致，render 输出 `.pdf`。

执行前记录输入 SHA-256，结束后再次计算。若输入变化，结果返回
`readonly_input_violated`。

create/apply 使用 `_atomic_office_write()`：

1. 在最终输出同一目录创建临时文件。
2. 加载源文件并顺序执行整个 actions 数组。
3. 保存并验证临时文件非空。
4. 全部成功后 `os.replace(temp, output)`。
5. 任一步失败都删除临时文件，既有 output 保持不变。

同目录临时文件保证最终 rename 在同一文件系统内，避免跨设备 rename 破坏原子性。

## 6. LibreOffice 为什么是外部锁定依赖

Python 库不负责完整排版渲染。`render` 因此调用外部 `soffice --headless --convert-to pdf`，但要求
request 同时提供：

```json
{
  "libreoffice": {
    "path": "/absolute/path/to/soffice",
    "expected_version": "LibreOffice 24.2.7.2"
  }
}
```

脚本先执行 `--version` 精确匹配，再在 artifact 目录的临时文件夹转换，最后原子发布 PDF。result
记录真实 path/version/hash。项目不下载、打包或暗中选择 LibreOffice；普通 CI 用小型可执行替身验证
协议，正式 smoke/release 必须在 P5.1 prepare manifest 中锁真实外部安装。

## 7. 旧工作流怎样退出

P1.1 直接删除：

- `nanobot/skills/office-automation/`
- report/slide DSL schema 与 validator
- DOCX/PPTX 周报专用 renderer
- `tests/fixtures/office_weekly/`
- `tests/skills/test_office_weekly.py`

没有保留旧 id alias、旧 session 路由或输出迁移。`nanobot/config/loader.py` 只从
`agents.defaults.disabledSkills` 删除 `office-automation`，不会把“用户曾禁用旧 Skill”解释为“自动
禁用新 Skill”。

OfficeCLI 的有效 launcher/provider contract 测试移动到 `test_officecli_runtime.py`；历史
OfficeCLI compatibility helper 仍存在，但不使用旧周报 fixture，也不进入公平 benchmark。

## 8. Fixture 与确定性证据

`tests/fixtures/office_python/` 提供三份 request template、三份可重生 OpenXML 文件和
`make_fixture.py`。样本只验证通用接口，不携带周报业务数据、固定叙事或偏向某个 Skill 的断言。

`tests/skills/test_office_python.py` 覆盖：

- Skill 发现、独立禁用、单入口与精确 constraints。
- 三格式 create → inspect → query → apply → validate。
- source hash 不变和 artifact root 拒绝。
- 多 action 后段失败时不覆盖既有 output。
- tracked changes 返回 unsupported。
- LibreOffice 版本锁与 rendered asset 审计。
- shared workbook inspect/facts 继续可用。

P5 Core 的 `tests/fixtures/runtime_eval/office_baseline.json` 也改用中立 `sample.xlsx`，因此删除旧
周报 fixture 不会削弱 OpenXML、file-openable、artifact-completion 与 data-consistency hard gate。

## 9. 验证结果

P1.1 定向验证：

```text
tests/skills/test_office_python.py
tests/skills/test_officecli_runtime.py
tests/config/test_config_migration.py
tests/agent/test_skill_manifest.py

57 passed
```

中立 fixture 与 P5 Core 联合回归：

```text
tests/runtime/test_replay_trace_eval.py + tests/skills/test_office_python.py
21 passed
```

全量 `ruff check nanobot/` 通过。全量后端套件为 `4177 passed, 19 failed, 6 skipped`；19 个失败
仍集中在本阶段未改动的内置 model preset 旧断言、Ant Ling/Novita/Skywork provider 自动路由和
Nanobot facade 无 Key 配置，P1/Runtime 定向集合没有失败。

## 10. 已知边界与后续

- OfficePython 不承诺 tracked changes、SmartArt、动画或复杂母版的可编辑保真。
- LibreOffice 真实版本未由仓库提供；没有外部安装时仍可 create/query/apply/validate，但不能真实 render。
- P1.1 只固定公平比较契约，不伪造静态质量分。P5.1 用相同 Luna、Dataset、Policy 和 evaluator
  分别运行两个 Skill，P7 只发布 Langfuse Dataset Run 导出的结果。

## 面试怎么讲

> P1 我把 Python Office 从周报专用 DSL 改成一个真正通用的 JSON 执行接口，同时保留独立
> OfficeCLI Skill。Python 侧对 DOCX/XLSX/PPTX 使用各自成熟库，输入只读，批量编辑先写临时
> OpenXML 再原子发布；无法保真的 tracked changes、SmartArt 和动画明确返回 unsupported。两个
> Skill 只共享事实和治理，不共享 action space，所以后续可以在同一 Dataset 上公平比较质量、成本
> 和步骤数。

## 自测

1. 为什么 result JSON 和进程 exit code 要同时存在？
2. 为什么 output 必须和临时文件在同一目录？
3. 为什么 `unsupported` 不能合并成普通 tool error？
4. 旧 disabled id 为什么只删除而不映射到新 id？
5. LibreOffice 版本为什么由 release manifest 锁定而不是项目下载？
