from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class TypefluxProjectDefaultsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_filename: str = "typeflux.yaml"
    #: Project-wide runtime defaults applied beneath every workflow YAML
    #: (engine defaults < these < workflow YAML < profiles < environment).
    runtime: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workflow_filename")
    @classmethod
    def _validate_workflow_filename(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("defaults.workflow_filename must be non-empty")
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("defaults.workflow_filename must be a file name")
        return value


class ProjectWorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    path: str | None = None
    directory: str | None = None
    #: Component profile selection by kind, e.g. {"provider": "anthropic-prod"}.
    profiles: dict[str, str] = Field(default_factory=dict)
    resolved_path: Path | None = Field(default=None, exclude=True)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("workflow id must be non-empty")
        return value

    @field_validator("path", "directory")
    @classmethod
    def _validate_path_value(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("workflow path and directory values must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_location(self) -> ProjectWorkflowSpec:
        if (self.path is None) == (self.directory is None):
            raise ValueError("workflow must configure exactly one of path or directory")
        return self


class ProjectValidationTargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflows: list[str] = Field(default_factory=list)
    environment: str | None = None
    policies: list[str] = Field(default_factory=list)

    @field_validator("workflows", "policies")
    @classmethod
    def _validate_ref_list(cls, value: list[str]) -> list[str]:
        for item in value:
            _validate_ref_name(item, field="validation target reference")
        return value

    @field_validator("environment")
    @classmethod
    def _validate_environment_ref(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_ref_name(value, field="validation target environment")
        return value


class ProjectValidationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: dict[str, ProjectValidationTargetSpec] = Field(default_factory=dict)

    @field_validator("targets")
    @classmethod
    def _validate_target_names(
        cls,
        value: dict[str, ProjectValidationTargetSpec],
    ) -> dict[str, ProjectValidationTargetSpec]:
        for name in value:
            _validate_ref_name(name, field="validation target")
        return value


class ProjectProfilesSpec(BaseModel):
    """File-referenced component profiles, keyed by kind then id (#214)."""

    model_config = ConfigDict(extra="forbid")

    provider: dict[str, str] = Field(default_factory=dict)
    registry: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)

    @field_validator("provider", "registry", "runtime")
    @classmethod
    def _validate_profile_refs(cls, value: dict[str, str]) -> dict[str, str]:
        for name, path in value.items():
            _validate_ref_name(name, field="profile id")
            if not path or path.strip() != path:
                raise ValueError(f"profile path for {name!r} must be non-empty")
        return value


class TypefluxProjectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    version: Literal["1"] = "1"
    name: str
    defaults: TypefluxProjectDefaultsSpec = Field(default_factory=TypefluxProjectDefaultsSpec)
    workflows: list[ProjectWorkflowSpec]
    environments: dict[str, str] = Field(default_factory=dict)
    policies: dict[str, str] = Field(default_factory=dict)
    profiles: ProjectProfilesSpec | None = None
    validation: ProjectValidationSpec = Field(default_factory=ProjectValidationSpec)
    manifest_path: Path = Field(default=Path("typeflux.project.yaml"), exclude=True)

    @property
    def project_dir(self) -> Path:
        return self.manifest_path.parent

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("project name must be non-empty")
        return value

    @field_validator("manifest_path", mode="before")
    @classmethod
    def _validate_manifest_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @field_validator("environments", "policies")
    @classmethod
    def _validate_reference_map(cls, value: dict[str, str]) -> dict[str, str]:
        for name, path in value.items():
            _validate_ref_name(name, field="reference id")
            if not path or path.strip() != path:
                raise ValueError(f"reference path for {name!r} must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_workflows(self) -> TypefluxProjectSpec:
        if not self.workflows:
            raise ValueError("project workflows must contain at least one workflow")
        seen: set[str] = set()
        duplicates: set[str] = set()
        for workflow in self.workflows:
            if workflow.id in seen:
                duplicates.add(workflow.id)
            seen.add(workflow.id)
        if duplicates:
            raise ValueError(f"duplicate project workflow id(s): {', '.join(sorted(duplicates))}")
        return self


class ProjectValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    reference: str | None = None
    path: str | None = None


class ProjectWorkflowSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    yaml_project: str | None = None
    yaml_name: str | None = None
    workflow_name: str | None = None
    task_queue: str | None = None


class ProjectValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    status: Literal["passed", "failed", "skipped"]
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ProjectResolvedWorkflowValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    environment_id: str
    ok: bool
    workflow_path: str | None = None
    environment_profile_path: str | None = None
    yaml_project: str | None = None
    yaml_name: str | None = None
    workflow_name: str | None = None
    task_queue: str | None = None
    checks: tuple[ProjectValidationCheck, ...] = ()


class ProjectValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str
    manifest_path: str
    ok: bool
    issues: tuple[ProjectValidationIssue, ...] = ()
    workflows: tuple[ProjectWorkflowSummary, ...] = ()
    resolved_workflows: tuple[ProjectResolvedWorkflowValidation, ...] = ()

    def to_dict(self) -> dict:
        payload = self.model_dump(mode="json", exclude_none=True)
        if not self.resolved_workflows:
            payload.pop("resolved_workflows", None)
        return payload


def _validate_ref_name(value: str, *, field: str) -> None:
    if not value or value.strip() != value:
        raise ValueError(f"{field} must be non-empty")


__all__ = [
    "ProjectResolvedWorkflowValidation",
    "ProjectValidationCheck",
    "ProjectValidationIssue",
    "ProjectValidationReport",
    "ProjectValidationSpec",
    "ProjectValidationTargetSpec",
    "ProjectWorkflowSpec",
    "ProjectWorkflowSummary",
    "TypefluxProjectDefaultsSpec",
    "TypefluxProjectSpec",
]
