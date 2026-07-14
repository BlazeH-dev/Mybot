# OfficeCLI upstream Skill snapshot

- Repository: https://github.com/iOfficeAI/OfficeCLI
- Release/tag: `v1.0.135`
- Upstream Skill: https://github.com/iOfficeAI/OfficeCLI/blob/v1.0.135/SKILL.md
- Upstream file SHA: `0b110eab23229c3b2f507b1802f3bcd37e44a8dd`
- License: Apache-2.0

The upstream Skill describes OfficeCLI as a general Office capability rather than a renderer-only backend. Its capability model includes:

- L1 read/inspect: help, view, get, query, validate, screenshot/HTML.
- L2 DOM operations: create, add, set, remove, move, swap, batch, merge.
- L3/raw and extension paths: raw, raw-set, add-part, plugins, MCP.
- Stateful helpers: resident open/save/close and watch.
- Specialized docx/pptx/xlsx workflows.

Mybot keeps this capability surface but overlays its own verified-facts, workspace, approval, artifact, checkpoint, trace, and eval rules. The upstream install instructions are not copied into the active workflow because Mybot is responsible for preparing the pinned binary.
