---
name: office-python
description: General Python baseline for inspecting, querying, creating, editing, validating, and rendering DOCX, XLSX, and PPTX through a neutral JSON interface. Use when the user explicitly asks for Python Office automation.
---

# OfficePython

Use this Skill when the user explicitly requests Python-based Office automation. General Office
requests prefer `officecli`. OfficePython is an independent baseline and must never call OfficeCLI or
its compiler/backend.

## Interface

Call only `scripts/office.py` with request and result JSON files:

```bash
venv/bin/python nanobot/skills/office-python/scripts/office.py \
  --request .nanobot-runtime/artifacts/<task_id>/office-request.json \
  --result .nanobot-runtime/artifacts/<task_id>/office-result.json
```

The request always contains:

- `schema_version: 1`
- `operation`: `inspect`, `query`, `create`, `apply`, `validate`, or `render`
- `format`: `docx`, `xlsx`, or `pptx`
- `input_artifact` and `output_artifact` objects with absolute `path` values when required
- a format-neutral `selector` object and `payload` object
- `options.artifact_root`; render also requires the locked external LibreOffice path/version

The result always contains `status`, `matches`, `changes`, `artifact`, `validation`,
`rendered_assets`, `warnings`, and `error`. Treat `status=unsupported` as a capability boundary, not
an execution success or generic failure.

## Safety and grounding

- Inputs are read-only. `create`, `apply`, and `render` publish only below
  `.nanobot-runtime/artifacts/<task_id>/` and never overwrite their input.
- An `apply` batch is all-or-none: every action runs against a temporary file before atomic publish.
- Tracked changes, complex PowerPoint master sets, animations/timing, and SmartArt return
  `unsupported` when Python libraries cannot preserve them safely.
- LibreOffice is external and version-locked by each render request. Do not download or bundle it.
- For quantitative analysis, create `verified_facts.json` with the shared Office core before writing
  claims. Pure inspection, formatting, extraction, and comments do not require empty facts.
- Use the static `plan` tool for multi-step work and keep every output under the task artifact root.

Selector examples:

```json
{"kind":"paragraph","text_contains":"Draft"}
{"kind":"cell","sheet":"Summary","range":"A1:D20"}
{"kind":"shape","slide":0,"text_contains":"Old title"}
```

See `references/request.schema.json` for the transport contract. The locked Python packages are in
`references/constraints.txt`.
