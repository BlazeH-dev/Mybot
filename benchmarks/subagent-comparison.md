# Single-Agent vs Subagent Deterministic Comparison

| Mode | Success | Wall ms | P95 ms | Input tokens | Output tokens | Cost USD | Parent context | Failures | Cancelled | Loop guard stops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Single | 1.00 | 120 | 120 | 100 | 20 | 0.0000 | 100 | 0 | 0 | 0 |
| Multi | 1.00 | 90 | 90 | 140 | 28 | 0.0000 | 60 | 0 | 0 | 0 |

This report is produced by the deterministic fake-provider harness; it measures governance overhead and regression behavior, not real-model quality.
