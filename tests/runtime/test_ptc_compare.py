from nanobot.runtime.evals.ptc_compare import compare, deterministic_baseline, markdown


def test_ptc_comparison_reports_round_trip_and_context_deltas() -> None:
    native, ptc = deterministic_baseline()
    result = compare(native, ptc)
    assert result["delta"]["llm_round_trips"] == -2
    assert result["delta"]["model_visible_tool_result_chars"] == -2760
    assert "does not measure real-model quality" in markdown(result)
