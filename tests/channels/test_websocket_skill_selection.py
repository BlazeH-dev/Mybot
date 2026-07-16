"""WebSocket envelope normalization for explicit Skill routing."""

from nanobot.channels.websocket import _normalize_selected_skills


def test_normalize_selected_skills_keeps_unique_safe_names() -> None:
    assert _normalize_selected_skills(["officecli", "officecli", "bad/name", 7, "alpha-beta"]) == [
        "officecli",
        "alpha-beta",
    ]


def test_normalize_selected_skills_limits_the_envelope() -> None:
    assert _normalize_selected_skills([f"skill-{index}" for index in range(10)]) == [
        f"skill-{index}" for index in range(8)
    ]
