from pathlib import Path

from nanobot.webui.skills_api import webui_skill_detail_payload, webui_skills_payload


def _write_skill(workspace: Path, name: str, manifest: str | None = None) -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill.\n---\n",
        encoding="utf-8",
    )
    if manifest is not None:
        (skill_dir / "skill.yaml").write_text(manifest, encoding="utf-8")


def test_catalog_includes_disabled_skill_with_structured_status(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", "name: alpha\nversion: 1\n")

    payload = webui_skills_payload(tmp_path, disabled_skills={"alpha"})
    alpha = next(skill for skill in payload["skills"] if skill["name"] == "alpha")

    assert alpha["enabled"] is False
    assert alpha["valid"] is True
    assert alpha["available"] is False
    assert alpha["status"] == "disabled"
    assert alpha["availability_reasons"][0]["code"] == "disabled"


def test_detail_exposes_manifest_providers_tools_and_permissions(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "alpha",
        """
name: alpha
version: 1
tools:
  required: [read_file]
providers:
  demo:
    required: false
permissions:
  required: [filesystem:read]
""",
    )

    detail = webui_skill_detail_payload(tmp_path, "alpha")

    assert detail is not None
    assert detail["version"] == 1
    assert detail["tools_required"] == ["read_file"]
    assert detail["permissions_required"] == ["filesystem:read"]
    assert detail["providers"] == [
        {
            "name": "demo",
            "required": False,
            "contract": None,
            "available": True,
            "reasons": [],
        }
    ]
    assert detail["manifest"]["name"] == "alpha"


def test_invalid_manifest_is_visible_but_not_available(tmp_path: Path) -> None:
    _write_skill(tmp_path, "broken", "name: [\n")

    payload = webui_skills_payload(tmp_path)
    broken = next(skill for skill in payload["skills"] if skill["name"] == "broken")

    assert broken["valid"] is False
    assert broken["available"] is False
    assert broken["status"] == "invalid"
    assert broken["availability_reasons"][0]["code"] == "invalid_manifest"
