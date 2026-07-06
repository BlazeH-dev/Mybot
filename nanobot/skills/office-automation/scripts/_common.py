"""Shared helpers for the office automation skill scripts."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

FACT_PLACEHOLDER_RE = re.compile(
    r"\{\{fact:([A-Za-z0-9_-]+)\.(display_value|value|name|unit)\}\}"
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def json_ready(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def load_facts(path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(path)
    facts = payload.get("facts", [])
    if not isinstance(facts, list):
        raise ValueError("verified facts file must contain a facts list")

    by_id: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("each fact must be an object")
        fact_id = fact.get("fact_id")
        if not isinstance(fact_id, str) or not fact_id:
            raise ValueError("each fact must have a non-empty fact_id")
        by_id[fact_id] = fact
    return by_id


def replace_fact_placeholders(text: str, facts: dict[str, dict[str, Any]]) -> str:
    def replacement(match: re.Match[str]) -> str:
        fact_id, field = match.groups()
        fact = facts.get(fact_id)
        if fact is None:
            raise ValueError(f"unknown fact placeholder: {fact_id}")
        value = fact.get(field)
        if value is None:
            return ""
        return str(value)

    return FACT_PLACEHOLDER_RE.sub(replacement, text)


def collect_fact_refs(payload: Any) -> set[str]:
    refs: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            fact_ref = value.get("fact_ref")
            if isinstance(fact_ref, str):
                refs.add(fact_ref)
            fact_refs = value.get("fact_refs")
            if isinstance(fact_refs, list):
                refs.update(ref for ref in fact_refs if isinstance(ref, str))
            for child in value.values():
                visit(child)
            return

        if isinstance(value, list):
            for item in value:
                visit(item)
            return

        if isinstance(value, str):
            refs.update(match.group(1) for match in FACT_PLACEHOLDER_RE.finditer(value))

    visit(payload)
    return refs


def fact_display(facts: dict[str, dict[str, Any]], fact_id: str) -> str:
    fact = facts.get(fact_id)
    if fact is None:
        raise ValueError(f"unknown fact reference: {fact_id}")
    return str(fact.get("display_value", fact.get("value", "")))


def render_text_value(value: Any, facts: dict[str, dict[str, Any]]) -> str:
    if isinstance(value, dict):
        if "fact_ref" in value:
            return fact_display(facts, str(value["fact_ref"]))
        if "text" in value:
            return replace_fact_placeholders(str(value["text"]), facts)
    return replace_fact_placeholders(str(value), facts)
