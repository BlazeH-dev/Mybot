"""Typed optional manifest models for agent skills."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillToolsManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[str] = Field(default_factory=list)


class SkillProviderManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = True
    contract: str | None = None


class SkillPermissionsManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[str] = Field(default_factory=list)


class SkillManifest(BaseModel):
    """P2 v1 skill manifest. Unknown fields fail closed."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: Literal[1] = 1
    description: str = ""
    entrypoints: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    tools: SkillToolsManifest = Field(default_factory=SkillToolsManifest)
    providers: dict[str, SkillProviderManifest] = Field(default_factory=dict)
    permissions: SkillPermissionsManifest = Field(default_factory=SkillPermissionsManifest)
    evals: list[str] = Field(default_factory=list)
