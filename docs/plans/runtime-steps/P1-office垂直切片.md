# P1 双 Office Skill 垂直切片

> 状态：P1 Core 与 P1.1 OfficePython 已完成（2026-07-24）。
> 目标：用两个独立 Office Skill 验证共享事实层、独立操作接口和 Runtime 治理，并为 P5.1 的公开 benchmark 建立公平 Python baseline。

## 1. 当前边界

### 共享 deterministic core

`nanobot/skills/_shared/office_core/` 负责 workbook 检查、事实抽取与
`verified_facts.json`；它没有 `SKILL.md`，不会作为第三个 Skill 被发现。

- 定量分析或生成定量结论时必须产出 verified facts。
- 纯格式、检查、提取和批注不强制运行事实层。
- `office-python` 与 `officecli` 可以共享 facts、输入快照和通用验证器，但各自保留操作接口。

### `office-python`

- 展示名 OfficePython，Skill id 固定为 `office-python`。
- 唯一入口是 `scripts/office.py --request ... --result ...`，不新增 Runtime typed tool。
- request 固定包含 `schema_version`、`operation`、`format`、输入/输出 artifact、
  中立 selector、payload 和 options；result 固定包含 status、matches/changes、artifact、
  validation、rendered assets、warnings 和 error。
- DOCX 使用 `python-docx + lxml`，XLSX 使用 `openpyxl`，PPTX 使用
  `python-pptx`；支持 `inspect/query/create/apply/validate/render`。
- 输入文件始终只读；create/apply/render 输出必须在 task artifact root。批量 apply 在同目录
  临时文件中全量执行，全部成功后用 `os.replace()` 原子发布。
- tracked changes、SmartArt、动画/timing 和复杂 PowerPoint master set 在无法可靠保真时
  返回 `status=unsupported`，不伪装成成功。
- LibreOffice 只负责 headless 转换/渲染；request 必须传外部可执行文件真实路径和精确版本。
  项目不下载或打包 LibreOffice，普通 CI 使用协议替身测试，不依赖系统安装。
- Python 包版本锁在 `references/constraints.txt`；request 结构在
  `references/request.schema.json`。

### `officecli`

- 通用 Office 请求默认优先 `officecli`；用户明确要求 Python 时选择 `office-python`。
- 固定 OfficeCLI v1.0.135 来源、平台资产和 checksum，首次使用由项目 launcher 准备；任务内
  不得跟随 latest/install/update。
- 保留 help/view/get/query、DOM、batch、validate、screenshot、raw、MCP、plugin、watch 等
  完整能力；高风险动作由 P3 Policy 按目标和参数治理。
- OfficeCLI 的历史 DSL compiler/render helper 只是兼容 helper，不进入 OfficePython 公平比较路径。

### 路由与迁移

- 路由只写在两个 Skill 的 description/正文，不在 AgentLoop 增加 Office 私有分支。
- `disabledSkills` 可分别禁用两个 Skill，OfficeCLI unavailable 不影响 OfficePython。
- 旧 `office-automation` 目录、manifest、id、显式路由、report/slide DSL、专用 renderer 和
  周报 workflow 已删除。
- 配置加载时只从 `disabledSkills` 删除旧 id，不把它迁移成 `office-python`，也不恢复旧会话路由。

## 2. 结构化接口契约

最小请求：

```json
{
  "schema_version": 1,
  "operation": "query",
  "format": "xlsx",
  "input_artifact": {"path": "/abs/input.xlsx"},
  "selector": {"kind": "cell", "sheet": "Data", "range": "A1:D20"},
  "payload": {},
  "options": {"artifact_root": "/abs/.nanobot-runtime/artifacts/task-id"}
}
```

selector 只表达 paragraph/table/cell/sheet/slide/shape 等中立概念，不复制 OfficeCLI DOM。
格式能力包括：

- DOCX：文本、段落、style/list、table/cell、图片、header/footer。
- XLSX：sheet、cell/range、formula、基础 bar/line chart。
- PPTX：slide、基础 layout、shape/text、图片。

`render` 输出 PDF，并把 LibreOffice 路径、精确版本、artifact hash 和 rendered asset 写入 result。

## 3. Fixture 与测试

旧 `tests/fixtures/office_weekly/` 和 `tests/skills/test_office_weekly.py` 已删除。当前使用：

```text
tests/fixtures/office_python/
  create_docx.json / sample.docx
  create_xlsx.json / sample.xlsx
  create_pptx.json / sample.pptx
  make_fixture.py

tests/skills/test_office_python.py
tests/skills/test_officecli_runtime.py
```

回归覆盖：

- 两个 Skill 可独立发现/禁用，`_shared` 不进入 summary，旧 id 不再被发现。
- 三种格式 create → inspect → query → apply → validate 的结构化闭环。
- 输入 hash 不变、workspace 外输出拒绝、后续 action 失败时既有输出不变。
- tracked changes 显式 unsupported。
- 外部 LibreOffice 精确版本匹配、render artifact 与版本审计。
- 中立 XLSX 继续通过 shared inspect/facts 和 P5 Core OpenXML hard gate。
- OfficeCLI launcher/provider contract case 归位且保持无网络单测。

## 4. 公平比较

P1.1 完成的是不偏置的执行接口和 fixture；真模型公平结果由 P5.1/P7 生成：

- 两个 Skill 使用相同 input snapshot、`gpt-5-6-luna`、Runtime Policy、任务约束和 evaluator。
- 一次 benchmark item 只启用一个被测 Skill；OfficePython 不调用 OfficeCLI compiler/backend。
- 分开报告 capability coverage 与共同任务质量、token、LLM/tool 耗时、工具成功率和 Agent steps。
- `unsupported` 单列为 coverage，不记作成功，也不混为基础设施执行错误。

## 5. 验证命令

```bash
source venv/bin/activate
pytest tests/skills/test_office_python.py tests/skills/test_officecli_runtime.py -q
pytest tests/runtime/test_replay_trace_eval.py -q
ruff check nanobot/ tests/skills/test_office_python.py tests/skills/test_officecli_runtime.py
```

## 6. P1.1 出口

- [x] `office-python` 独立覆盖 DOCX/XLSX/PPTX 的读取、创建、常用编辑、验证和外部渲染协议。
- [x] 旧 id、目录、显式路由和旧 DSL/周报链已删除；`disabledSkills` 清理有测试。
- [x] 原周报 fixture/test 已删除；OfficeCLI contract case 已归位，中立 fixture、shared facts、
  OpenXML/渲染验证器回归通过。
- [x] 公平比较的输入、模型、Policy、evaluator、coverage/quality 分母契约已固定；实际公开
  Dataset Run 与结果由 P5.1/P7 交付，不在 P1.1 伪造静态分数。
