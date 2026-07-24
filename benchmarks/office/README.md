# Office public benchmark adapter

This directory stores only adapter contracts. Public benchmark source/data, original Office files,
rendered media, virtual environments, traces, scores, and reviewer state live outside Git under the
cache selected by `nanobot benchmark prepare` and in Langfuse Japan Cloud.

`profiles.json` pins code and dataset revisions, license identities, smoke cases, compared Skills,
models, and pre-run pricing assumptions. Fill non-zero Luna/Terra pricing before relying on
`estimate`; actual tokens and cost remain Langfuse generation metrics.

PresentBench source materials retain their original licenses. `prepare` validates the pinned
metadata but does not upload material files. Only content explicitly cleared by the operator may be
added to Langfuse.
