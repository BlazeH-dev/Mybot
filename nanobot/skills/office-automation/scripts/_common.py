"""Compatibility imports from the shared deterministic Office core."""

from nanobot.skills._shared.office_core.common import (
    collect_fact_refs,
    fact_display,
    json_ready,
    load_facts,
    read_json,
    render_text_value,
    replace_fact_placeholders,
    write_json,
)

__all__ = [
    "collect_fact_refs",
    "fact_display",
    "json_ready",
    "load_facts",
    "read_json",
    "render_text_value",
    "replace_fact_placeholders",
    "write_json",
]
