# Office public benchmark adapter

This directory stores only adapter contracts. Public benchmark source/data, original Office files,
rendered media, virtual environments, traces, scores, and reviewer state live outside Git under the
cache selected by `nanobot benchmark prepare` and in Langfuse Japan Cloud.

`profiles.json` pins code and dataset revisions, license identities, smoke cases, compared Skills,
models, and per-case token assumptions. `estimate` reports the expected Luna Agent and Terra Judge
input/output token volume; actual token usage remains available in Langfuse generation metrics.

PresentBench source materials retain their original licenses. `prepare` validates the pinned
metadata but does not upload material files. Only content explicitly cleared by the operator may be
added to Langfuse.
