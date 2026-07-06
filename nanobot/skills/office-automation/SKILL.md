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
- For tasks with at least three steps or at least two requested artifacts, create `plan.json`, show it to the user, and wait for explicit confirmation before running the workflow.
- If DSL validation fails twice in a row, stop self-repair and ask the user how to proceed.

## Workflow

Set a short stable `task_id`, then create:

```text
.nanobot-runtime/artifacts/<task_id>/
```

For complex tasks, first write `plan.json` using `references/plan.schema.json`. Steps should cover inspect, fact extraction, DSL drafting, validation, rendering, and final delivery checks. Show the plan and wait for the user to confirm before continuing.

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

5. Render the Word report:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/render_docx.py \
  --dsl .nanobot-runtime/artifacts/<task_id>/report_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --out .nanobot-runtime/artifacts/<task_id>/weekly_report.docx
```

6. Render the PowerPoint deck:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/render_pptx.py \
  --dsl .nanobot-runtime/artifacts/<task_id>/slide_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --constraints <constraints.json> \
  --out .nanobot-runtime/artifacts/<task_id>/weekly_review.pptx
```

7. Final delivery check.

Update `plan.json` step statuses to `done`, rerun `validate.py` with `--plan` and `--artifact-root`, and confirm that these artifacts exist:

- `plan.json`
- `workbook_schema.json`
- `verified_facts.json`
- `report_dsl.json`
- `slide_dsl.json`
- `quality_report.json`
- `weekly_report.docx`
- `weekly_review.pptx`

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
