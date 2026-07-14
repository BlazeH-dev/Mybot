---
name: office-automation
description: Original Python Office workflow for grounded Excel analysis, weekly reports, and PPT decks using verified facts, a constrained DSL, python-docx, and python-pptx. Use when the user explicitly asks for the Python/original Office skill.
---

# Office Automation — Python

Use this Skill when the user explicitly requests the original Python Office workflow, or when `officecli` is disabled/unavailable and this workflow still satisfies the request.

This Skill is independent from `officecli`. It owns its report/slide DSL and Python renderers, while sharing deterministic workbook inspection and verified-facts extraction with other Office skills.

## Hard rules

- Do not invent or rewrite quantitative claims. Important numbers must come from `verified_facts.json`.
- Put facts in prose or slides through `fact_ref` or `{{fact:<fact_id>.display_value}}` placeholders.
- Produce artifacts under `.nanobot-runtime/artifacts/<task_id>/`; do not overwrite user source files.
- For tasks with at least three steps or two requested deliverables, use the static `plan` tool, show the plan/hash, and wait for explicit confirmation.
- If DSL validation fails twice, stop self-repair and ask the user how to proceed.

## Workflow

1. Inspect the workbook through the shared Office core:

```bash
venv/bin/python nanobot/skills/_shared/office_core/scripts/inspect_workbook.py \
  --in <input.xlsx> \
  --out .nanobot-runtime/artifacts/<task_id>/workbook_schema.json
```

2. Extract deterministic facts:

```bash
venv/bin/python nanobot/skills/_shared/office_core/scripts/extract_facts.py \
  --in <input.xlsx> \
  --spec nanobot/skills/_shared/office_core/references/metric_spec.example.json \
  --out .nanobot-runtime/artifacts/<task_id>/verified_facts.json
```

3. Draft `report_dsl.json` and `slide_dsl.json` using this Skill's schemas. Meeting notes may supply qualitative conclusions, risks, and actions; numbers must reference facts.

4. Validate DSL and plan:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/validate.py \
  --dsl .nanobot-runtime/artifacts/<task_id>/report_dsl.json \
  --dsl .nanobot-runtime/artifacts/<task_id>/slide_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --constraints <constraints.json> \
  --plan .nanobot-runtime/artifacts/<task_id>/plan.json \
  --out .nanobot-runtime/artifacts/<task_id>/quality_report.json
```

5. Render with Python libraries:

```bash
venv/bin/python nanobot/skills/office-automation/scripts/render_docx.py \
  --dsl .nanobot-runtime/artifacts/<task_id>/report_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --out .nanobot-runtime/artifacts/<task_id>/weekly_report.docx

venv/bin/python nanobot/skills/office-automation/scripts/render_pptx.py \
  --dsl .nanobot-runtime/artifacts/<task_id>/slide_dsl.json \
  --facts .nanobot-runtime/artifacts/<task_id>/verified_facts.json \
  --constraints <constraints.json> \
  --out .nanobot-runtime/artifacts/<task_id>/weekly_review.pptx
```

6. Rerun validation with `--artifact-root`, update plan steps, and call `plan(action="complete")` only after all expected artifacts exist.

## Deliverables

- `plan.json`
- `workbook_schema.json`
- `verified_facts.json`
- `report_dsl.json`
- `slide_dsl.json`
- `quality_report.json`
- `weekly_report.docx`
- `weekly_review.pptx`

Report blocks support paragraphs, bullets, metrics, and tables. Slide layouts support title/metrics and bullets. Keep wording concise and fact-traceable.
