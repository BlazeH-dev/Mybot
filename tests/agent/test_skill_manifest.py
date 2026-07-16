from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


def _write_skill(root: Path, name: str, *, manifest: str | None = None) -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Legacy {name}.\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    if manifest is not None:
        (skill_dir / "skill.yaml").write_text(manifest, encoding="utf-8")
    return skill_dir


def _loader(tmp_path: Path, *, disabled: set[str] | None = None) -> SkillsLoader:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    return SkillsLoader(tmp_path, builtin_skills_dir=builtin, disabled_skills=disabled)


def test_valid_manifest_overrides_description_and_exposes_declarations(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "alpha",
        manifest="""
name: alpha
version: 1
description: Typed alpha.
entrypoints: [scripts/run.py]
inputs: [text]
outputs: [summary]
tools:
  required: [read_file]
providers: {}
permissions:
  required: [filesystem:read]
evals: [alpha-smoke]
""",
    )
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("", encoding="utf-8")
    loader = _loader(tmp_path)

    status = loader.get_skill_status("alpha")

    assert status["status"] == "available"
    assert status["manifest_present"] is True
    assert status["manifest"]["version"] == 1
    assert status["manifest"]["tools"]["required"] == ["read_file"]
    assert status["manifest"]["permissions"]["required"] == ["filesystem:read"]
    assert loader._get_skill_description("alpha") == "Typed alpha."


def test_legacy_skill_without_manifest_remains_available(tmp_path: Path) -> None:
    _write_skill(tmp_path, "legacy")
    loader = _loader(tmp_path)

    status = loader.get_skill_status("legacy")

    assert status["available"] is True
    assert status["manifest_present"] is False
    assert status["manifest"] is None


def test_invalid_yaml_is_locally_fail_closed(tmp_path: Path) -> None:
    _write_skill(tmp_path, "broken", manifest="name: [\n")
    _write_skill(tmp_path, "healthy")
    loader = _loader(tmp_path)

    broken = loader.get_skill_status("broken")
    available = {entry["name"] for entry in loader.list_skills(filter_unavailable=True)}
    summary = loader.build_skills_summary()

    assert broken["valid"] is False
    assert broken["status"] == "invalid"
    assert broken["reasons"][0]["code"] == "invalid_manifest"
    assert available == {"healthy"}
    assert "broken" not in summary
    assert "healthy" in summary


def test_invalid_manifest_cannot_be_loaded_directly_into_context(tmp_path: Path) -> None:
    _write_skill(tmp_path, "broken", manifest="name: [\n")
    loader = _loader(tmp_path)

    assert loader.load_skills_for_context(["broken"]) == ""


def test_manifest_schema_errors_include_field_path(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "broken",
        manifest="""
name: broken
version: 2
unexpected: true
""",
    )
    status = _loader(tmp_path).get_skill_status("broken")

    fields = {reason.get("field") for reason in status["reasons"]}
    assert status["valid"] is False
    assert {"version", "unexpected"}.issubset(fields)


def test_manifest_name_must_match_directory(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", manifest="name: beta\nversion: 1\n")

    status = _loader(tmp_path).get_skill_status("alpha")

    assert status["status"] == "invalid"
    assert status["reasons"][0]["field"] == "name"


def test_workspace_manifest_shadows_builtin_skill(tmp_path: Path) -> None:
    workspace_skill = _write_skill(
        tmp_path,
        "alpha",
        manifest="name: alpha\nversion: 1\ndescription: Workspace alpha.\n",
    )
    builtin_root = tmp_path / "builtin"
    builtin_skill = builtin_root / "alpha"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Builtin alpha.\n---\n",
        encoding="utf-8",
    )
    (builtin_skill / "skill.yaml").write_text("name: [\n", encoding="utf-8")
    loader = SkillsLoader(tmp_path, builtin_skills_dir=builtin_root)

    entries = loader.list_skills(filter_unavailable=False)
    status = loader.get_skill_status("alpha")

    assert entries == [
        {
            "name": "alpha",
            "path": str(workspace_skill / "SKILL.md"),
            "source": "workspace",
        }
    ]
    assert status["status"] == "available"
    assert status["manifest"]["description"] == "Workspace alpha."


@pytest.mark.parametrize(
    ("manifest", "field"),
    [
        ("name: alpha\nversion: 1\nentrypoints: [../escape.py]\n", "entrypoints.0"),
        (
            """
name: alpha
version: 1
providers:
  demo:
    required: true
    contract: ../escape.json
""",
            "providers.demo.contract",
        ),
    ],
)
def test_manifest_rejects_paths_outside_skill_directory(
    tmp_path: Path,
    manifest: str,
    field: str,
) -> None:
    _write_skill(tmp_path, "alpha", manifest=manifest)

    status = _loader(tmp_path).get_skill_status("alpha")

    assert status["status"] == "invalid"
    assert status["valid"] is False
    assert status["reasons"][0]["code"] == "invalid_manifest"
    assert status["reasons"][0]["field"] == field


def test_missing_entrypoint_makes_only_target_unavailable(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "alpha",
        manifest="name: alpha\nversion: 1\nentrypoints: [scripts/missing.py]\n",
    )
    _write_skill(tmp_path, "beta")
    loader = _loader(tmp_path)

    status = loader.get_skill_status("alpha")

    assert status["valid"] is True
    assert status["status"] == "unavailable"
    assert status["reasons"][0]["code"] == "missing_entrypoint"
    assert {entry["name"] for entry in loader.list_skills()} == {"beta"}


def test_missing_provider_contract_is_structured_and_local(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "alpha",
        manifest="""
name: alpha
version: 1
providers:
  demo:
    required: true
    contract: references/missing.json
""",
    )
    status = _loader(tmp_path).get_skill_status("alpha")

    assert status["status"] == "invalid"
    assert status["valid"] is False
    assert status["reasons"][0]["code"] == "missing_contract"
    assert status["providers"][0]["available"] is False


def test_invalid_non_office_provider_contract_is_locally_fail_closed(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "alpha",
        manifest="""
name: alpha
version: 1
providers:
  demo:
    required: true
    contract: references/demo.json
""",
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "demo.json").write_text("{", encoding="utf-8")
    _write_skill(tmp_path, "beta")
    loader = _loader(tmp_path)

    status = loader.get_skill_status("alpha")

    assert status["status"] == "invalid"
    assert status["valid"] is False
    assert status["reasons"][0]["code"] == "invalid_contract"
    assert {entry["name"] for entry in loader.list_skills()} == {"beta"}


def test_manifest_permissions_are_declarative_and_cannot_enable_skill(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "alpha",
        manifest="""
name: alpha
version: 1
permissions:
  required: [workspace:full-access, network:any]
""",
    )
    loader = _loader(tmp_path, disabled={"alpha"})

    status = loader.get_skill_status("alpha")

    assert status["enabled"] is False
    assert status["available"] is False
    assert status["status"] == "disabled"
    assert status["manifest"]["permissions"]["required"] == [
        "workspace:full-access",
        "network:any",
    ]


def test_disabled_skill_can_be_listed_for_catalog_but_not_agent(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha")
    loader = _loader(tmp_path, disabled={"alpha"})

    assert loader.list_skills(filter_unavailable=False) == []
    catalog = loader.list_skills(filter_unavailable=False, include_disabled=True)

    assert [entry["name"] for entry in catalog] == ["alpha"]
    assert loader.get_skill_status("alpha")["status"] == "disabled"


@pytest.mark.parametrize("name", ["office-automation", "officecli"])
def test_packaged_office_manifests_are_valid(name: str, tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)

    status = loader.get_skill_status(name)

    assert status["manifest_present"] is True
    assert status["valid"] is True
    assert status["available"] is True


def test_officecli_manifest_references_single_provider_contract(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)

    status = loader.get_skill_status("officecli")

    assert status["manifest"]["providers"] == {
        "officecli": {
            "required": True,
            "contract": "references/officecli-runtime.json",
        }
    }
    assert status["providers"][0]["available"] is True
