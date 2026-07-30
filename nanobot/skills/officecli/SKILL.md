---
name: officecli
description: Office skill for creating, inspecting, validating, and modifying docx, xlsx, and pptx with the pinned OfficeCLI capability.
metadata:
  nanobot:
    requires:
      bins: [officecli]
---

# OfficeCLI

Use the pinned OfficeCLI capability for general Word, Excel, and PowerPoint work. It may use OfficeCLI's own commands, help system, DOM paths, batch format, validation, previews, raw XML, MCP, plugins, and resident mode when appropriate.

## Mybot rules

- OfficeCLI is the supported Office implementation for benchmark and general Office requests.
- Mybot provides an `officecli` launcher that downloads only the pinned contract asset on first use, verifies its checksum, caches it under the nanobot data directory, and disables upstream auto-update. Never invoke upstream `install`/`update` or follow `latest` from an Agent task.
- If the packaged launcher cannot provision the pinned binary, report the reason and use another enabled Skill only when that matches the request.
- Before a data analysis or quantitative reporting task, run the shared Office core to create `workbook_schema.json` and `verified_facts.json`. Pure formatting, inspection, comments, or text extraction do not need an empty facts workflow.
- Never invent quantitative claims. Values derived from user data must map to a fact id; numbers provided directly by the user should be recorded as user-provided facts when they become report claims.
- Write new outputs under `.nanobot-runtime/artifacts/<task_id>/`. Modifying an existing user file is a high-risk operation and must pass Runtime Policy, approval, and file freshness checks.
- OfficeCLI capabilities are not removed at the Skill layer. `raw`, MCP, plugins, install/update/config, watch, and existing-file mutation remain available but may require approval or be denied by hard workspace/network boundaries.
- Consult `officecli help ...` instead of guessing command names or property values. The pinned binary help is authoritative for its version.
- Check exit codes and structured JSON. Validate deliverables and use `view`/screenshot or HTML when visual verification matters.

## Shared facts for quantitative tasks

```bash
venv/bin/python nanobot/skills/_shared/office_core/scripts/inspect_workbook.py \
  --in <input.xlsx> \
  --out .nanobot-runtime/artifacts/<task_id>/workbook_schema.json

venv/bin/python nanobot/skills/_shared/office_core/scripts/extract_facts.py \
  --in <input.xlsx> \
  --spec nanobot/skills/_shared/office_core/references/metric_spec.example.json \
  --out .nanobot-runtime/artifacts/<task_id>/verified_facts.json
```

Use an input snapshot supplied by the Runtime when P4 is available. Until then, never overwrite the source workbook.

## OfficeCLI workflow

1. For an existing file, orient with `view outline`, `view stats`, `view issues`, `get`, or `query`.
2. For uncertain syntax, run `officecli help <format> [verb] [element] --json`.
3. Create or modify the document using the highest practical layer: read/view first, DOM operations next, raw XML only when necessary.
4. Use batch for repeatable groups of operations and inspect partial failures.
5. Flush/close before non-OfficeCLI tools inspect the file.
6. Run `officecli validate` and a visual view before delivery.
7. Record the actual binary version, commands/batch, validation, previews, facts, and final files as artifacts.

The existing `compile_officecli.py` and render helpers are an optional grounded-report compatibility path.

## Version source

The project contract is `references/officecli-runtime.json`. The upstream capability baseline is documented in `references/upstream-snapshot.md`. The installed `officecli` console script is Mybot's pinned launcher rather than upstream's latest-version installer. Do not follow `latest` during a task.
