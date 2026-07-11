---
name: office-automation
description: Use this skill for Excel analysis, weekly reports, PPT decks, or turning meeting notes into grounded Office artifacts such as docx, pptx, verified_facts.json, and quality reports.
---

# Office Automation

Use this skill when the user asks for a report, weekly review, PPT/deck, or other Office artifact based on Excel data, CSV-like tables, or meeting notes.

Hard rules:

- Do not invent or rewrite numbers. All important numbers must come from `verified_facts.json`.
- Put numbers in prose or slides by using `fact_ref` fields or `{{fact:<fact_id>.display_value}}` placeholders.
- Produce artifacts under `.nanobot-runtime/artifacts/<task_id>/`; do not overwrite the user's source files.
- Use the pinned OfficeCLI backend for docx/pptx rendering. Do not install, update, configure, or expose OfficeCLI MCP/plugins during a task.
- Never generate raw OfficeCLI shell commands or use `raw-set`/`add-part`. Compile the DSL to the bounded batch JSON through `compile_officecli.py` or the render scripts.
- For tasks with at least three steps or at least two requested artifacts, use the statically registered `plan` tool. Create the plan, show the returned plan and hash, wait for explicit confirmation, then call `plan(action="confirm")` with the exact hash before running the workflow.
- If DSL validation fails twice in a row, stop self-repair and ask the user how to proceed.

## Workflow

Set a short stable `task_id`, then create:

```text
.nanobot-runtime/artifacts/<task_id>/
```

For complex tasks, first call the static `plan` tool with `action="create"`. Steps should cover inspect, fact extraction, DSL drafting, validation, rendering, and final delivery checks, with `expected_artifacts` matching the files below. The tool persists `.nanobot-runtime/artifacts/<task_id>/plan.json` and returns a stable `plan_hash`. Show the plan to the user and wait. Only after explicit confirmation call `plan(action="confirm", task_id=..., expected_plan_hash=...)`.

The validated OfficeCLI version, release checksums, allowed command subset, and denied operations are declared in `references/officecli-runtime.json`. If the installed binary does not match that contract, stop and report the dependency mismatch instead of installing or silently using another renderer.

1. Inspect the workbook:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/inspect_workbook.py \
  --in <input.xlsx> \
  --out .nanobot-runtime/artifacts/<task_id>/workbook_schema.json
```

2. Extract verified facts. If the user did not provide a metric spec, create one from the workbook schema, or adapt `references/metric_spec.example.json`.

```bash
venv/bin/python nanobot/skills/office-automation/scripts/extract_facts.py \
  --in <input.xlsx> \
  --spec <metric_spec.json> \
  --out .nanobot-runtime/artifacts/<task_id>/verified_facts.json
```

3. Draft `report_dsl.json` and `slide_dsl.json`.

Use `references/report_dsl.schema.json` and `references/slide_dsl.schema.json`. Use the meeting notes for qualitative conclusions, risks, and actions. Use `verified_facts.json` for every metric.

4. Validate the DSL and plan:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/validate.py \
  --dsl .nanobot-runtime/artifacts/<task_id>/report_dsl.json \
  --dsl .nanobot-runtime/artifacts/<task_id>/slide_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --constraints <constraints.json> \
  --plan .nanobot-runtime/artifacts/<task_id>/plan.json \
  --out .nanobot-runtime/artifacts/<task_id>/quality_report.json
```

If validation fails, read `quality_report.json`, fix the DSL, and rerun validation. Do this at most twice before asking the user.

5. Compile and render the Word report through OfficeCLI:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/render_docx.py \
  --backend officecli \
  --dsl .nanobot-runtime/artifacts/<task_id>/report_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --preview-dir .nanobot-runtime/artifacts/<task_id>/previews \
  --out .nanobot-runtime/artifacts/<task_id>/weekly_report.docx
```

The renderer records a replayable batch, validation result, run metadata, and preview PNGs next to the output. OfficeCLI must already be installed at the validated project version; the task must not download it dynamically.

6. Compile and render the PowerPoint deck through OfficeCLI:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/render_pptx.py \
  --backend officecli \
  --dsl .nanobot-runtime/artifacts/<task_id>/slide_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --constraints <constraints.json> \
  --preview-dir .nanobot-runtime/artifacts/<task_id>/previews \
  --out .nanobot-runtime/artifacts/<task_id>/weekly_review.pptx
```

7. Final delivery check.

Use `plan(action="update_step")` whenever a step starts or finishes. At delivery, rerun `validate.py` with `--plan` and `--artifact-root`, then call `plan(action="complete")`; the tool refuses completion while steps or expected artifacts are missing.

- `plan.json`
- `workbook_schema.json`
- `verified_facts.json`
- `report_dsl.json`
- `slide_dsl.json`
- `quality_report.json`
- `weekly_report.docx`
- `weekly_review.pptx`
- `weekly_report.docx.officecli-batch.json`
- `weekly_report.docx.officecli-validation.json`
- `weekly_report.docx.officecli-run.json`
- `weekly_review.pptx.officecli-batch.json`
- `weekly_review.pptx.officecli-validation.json`
- `weekly_review.pptx.officecli-run.json`
- `previews/*.png`

The Python renderer remains available only as an explicit `--backend python` compatibility path for deterministic differential tests. It is not the default delivery backend.

## DSL Notes

Report blocks support `paragraph`, `bullets`, `metrics`, and `table`.

Slide layouts support `title_metrics` and `bullets`.

Use concise text. Prefer traceable facts:

```json
{
  "type": "paragraph",
  "text": "Weekly GMV reached {{fact:total_gmv_cny.display_value}}.",
  "fact_refs": ["total_gmv_cny"]
}
```
