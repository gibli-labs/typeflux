"""Project-local activity catalog for control-plane composition (#217).

The catalog re-projects a resolved workflow's discovered activities into a
UI-friendly, secret-free shape: schema identities plus JSON Schemas for
inspection, AI vs plain Temporal kinds clearly distinguished, and static
type-compatibility edges (`compatible_next`) so a control plane can reason
about valid step chains before execution.

AI-specific metadata (prompt reference, provider params, artifact inputs)
stays separate from plain Temporal activity metadata and never includes raw
prompt text or secret values. Discovery runs through ``collect_activities``,
so import-policy boundaries and duplicate-name failures apply unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from typeflux.core.contracts import AIActivity, YamlWorkflowActivity
from typeflux.manifests._common import schema_identity
from typeflux.project.environment import resolve_project_workflow
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.workflow import flatten_call_specs

CATALOG_VERSION: Literal["1"] = "1"


class CatalogSchema(BaseModel):
    """Schema identity plus a UI-renderable JSON Schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    hash: str
    json_schema: dict[str, Any]


class CatalogActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: Literal["ai", "temporal"]
    input_schema: CatalogSchema
    output_schema: CatalogSchema
    definition_source: dict[str, Any]
    task_queue: str | None = None
    start_to_close_timeout_seconds: float | None = None
    #: AI-only fields stay None/empty for plain Temporal activities.
    prompt_ref: dict[str, Any] | None = None
    provider_params: dict[str, Any] = Field(default_factory=dict)
    validation_retries: int | None = None
    artifact_inputs: tuple[dict[str, Any], ...] = ()
    #: Steps in the resolved workflow that schedule this activity.
    used_by_steps: tuple[str, ...] = ()
    #: Activities whose input type matches this activity's output type —
    #: statically valid successors for composition.
    compatible_next: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ActivityCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: Literal["1"] = CATALOG_VERSION
    project: str
    workflow_id: str
    environment_id: str
    activities: tuple[CatalogActivity, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def resolve_activity_catalog(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
) -> ActivityCatalog:
    """Build the activity catalog for one resolved workflow/environment."""
    from typeflux.project.environment import create_workflow_with_subworkflows

    resolved = resolve_project_workflow(
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
    )
    workflow_cls, _subworkflows, activities = create_workflow_with_subworkflows(project, resolved)
    used_by: dict[str, list[str]] = {}
    # Leaf calls, depth-first (#55): nested branch steps use activities like any other.
    for call in flatten_call_specs(getattr(workflow_cls, "__typeflux_activity_calls__")):
        used_by.setdefault(call.activity_name, []).append(call.step_id)
    entries = tuple(
        _catalog_activity(
            activities[name],
            all_activities=activities,
            used_by_steps=tuple(used_by.get(name, ())),
        )
        for name in sorted(activities)
    )
    return ActivityCatalog(
        project=project.name,
        workflow_id=workflow_id,
        environment_id=environment_id,
        activities=entries,
    )


def _catalog_activity(
    activity: YamlWorkflowActivity,
    *,
    all_activities: dict[str, YamlWorkflowActivity],
    used_by_steps: tuple[str, ...],
) -> CatalogActivity:
    is_ai = isinstance(activity, AIActivity)
    compatible_next = tuple(
        sorted(
            name
            for name, candidate in all_activities.items()
            if name != activity.name and candidate.input_type is activity.output_type
        )
    )
    return CatalogActivity(
        name=activity.name,
        kind="ai" if is_ai else "temporal",
        input_schema=_catalog_schema(activity.input_type),
        output_schema=_catalog_schema(activity.output_type),
        definition_source=activity.definition_source.to_dict(),
        task_queue=activity.task_queue,
        start_to_close_timeout_seconds=(
            activity.start_to_close_timeout.total_seconds()
            if activity.start_to_close_timeout is not None
            else None
        ),
        prompt_ref=activity.prompt_ref.to_dict() if is_ai else None,
        provider_params=activity.provider_params.to_dict() if is_ai else {},
        validation_retries=activity.validation_retries if is_ai else None,
        artifact_inputs=(
            tuple(item.safe_definition() for item in activity.artifact_inputs) if is_ai else ()
        ),
        used_by_steps=used_by_steps,
        compatible_next=compatible_next,
    )


def _catalog_schema(model_type: type) -> CatalogSchema:
    identity = schema_identity(model_type).to_dict()
    return CatalogSchema(
        name=identity["name"],
        hash=identity["hash"],
        json_schema=model_type.model_json_schema(),
    )


__all__ = [
    "CATALOG_VERSION",
    "ActivityCatalog",
    "CatalogActivity",
    "CatalogSchema",
    "resolve_activity_catalog",
]
