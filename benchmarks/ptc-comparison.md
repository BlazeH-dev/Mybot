# Native vs PTC Deterministic Comparison

| Mode | Success | Wall ms | LLM round-trips | Tool calls | Visible result chars | Input tokens | Output tokens | Failures |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Native | 1.00 | 150 | 4 | 3 | 3000 | 900 | 180 | 0 |
| PTC | 1.00 | 70 | 2 | 3 | 240 | 620 | 130 | 0 |

This fixed fake-provider workload validates reporting and protocol expectations; it does not measure real-model quality or production savings.
