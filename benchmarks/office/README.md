# Office public benchmark adapter

This directory stores only adapter contracts. Public benchmark source/data, original Office files,
rendered media, virtual environments, traces, scores, and reviewer state live outside Git under the
cache selected by `nanobot benchmark prepare` and in Langfuse Japan Cloud.

`profiles.json` pins the OCB code and dataset revisions, license identities, smoke cases, the single
OfficeCLI Skill, the two compared Agent models, and per-case token assumptions. `estimate` reports
the expected Agent and Terra Judge input/output token volume; actual token usage remains available in
Langfuse generation metrics.
