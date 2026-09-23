from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from typeflux.observability.inspect import (
    _DTO,
    ActivityExecutionManifestView,
    TraceInspection,
    TraceReader,
    WorkflowExecutionManifestView,
    _drop_none,
    inspect_trace,
)


class ManifestFieldDiff(_DTO):
    field: str
    left: Any | None = None
    right: Any | None = None


class WorkflowManifestDiff(_DTO):
    changed_fields: tuple[ManifestFieldDiff, ...] = ()


class ActivityManifestDiff(_DTO):
    activity_name: str
    status: Literal["added", "removed", "changed", "unchanged"]
    changed_fields: tuple[ManifestFieldDiff, ...] = ()
    left: ActivityExecutionManifestView | None = None
    right: ActivityExecutionManifestView | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "activity_name": self.activity_name,
                "status": self.status,
                "changed_fields": [field.field for field in self.changed_fields],
            }
        )


class TraceDiff(_DTO):
    left_trace_id: str
    right_trace_id: str
    workflow: WorkflowManifestDiff
    activities: tuple[ActivityManifestDiff, ...]
    left_span_counts: dict[str, int]
    right_span_counts: dict[str, int]
    warnings: tuple[str, ...] = ()

    def to_summary_dict(self) -> dict[str, Any]:
        activity_groups = {
            "added": [],
            "removed": [],
            "changed": [],
            "unchanged": [],
        }
        for activity in self.activities:
            activity_groups[activity.status].append(activity.to_summary_dict())
        return {
            "left_trace_id": self.left_trace_id,
            "right_trace_id": self.right_trace_id,
            "workflow_changes": [field.field for field in self.workflow.changed_fields],
            "activities": activity_groups,
            "span_counts": {
                "left": self.left_span_counts,
                "right": self.right_span_counts,
            },
            "warnings": list(self.warnings),
        }

    def to_json_dict(self) -> dict[str, Any]:
        return self.to_public_dict()


def diff_traces(
    reader: TraceReader,
    left_trace_id: str,
    right_trace_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_detail_pages: int | None = None,
) -> TraceDiff:
    left = inspect_trace(
        reader,
        left_trace_id,
        since=since,
        until=until,
        max_detail_pages=max_detail_pages,
    )
    right = inspect_trace(
        reader,
        right_trace_id,
        since=since,
        until=until,
        max_detail_pages=max_detail_pages,
    )
    return TraceDiff(
        left_trace_id=left_trace_id,
        right_trace_id=right_trace_id,
        workflow=_diff_workflows(left.workflow_manifest, right.workflow_manifest),
        activities=_diff_activities(left.activity_manifests, right.activity_manifests),
        left_span_counts=_span_counts(left),
        right_span_counts=_span_counts(right),
        warnings=tuple((*left.warnings, *right.warnings)),
    )


def _diff_workflows(
    left: WorkflowExecutionManifestView | None,
    right: WorkflowExecutionManifestView | None,
) -> WorkflowManifestDiff:
    left_payload = {} if left is None else left.to_public_dict()
    right_payload = {} if right is None else right.to_public_dict()
    # "policy" is surfaced as its own diff field; strip it from the catch-all
    # "contributions" blob so a single policy change is reported once, not twice.
    left_payload = _without_contribution(left_payload, "policy")
    right_payload = _without_contribution(right_payload, "policy")
    fields = (
        "workflow_name",
        "task_queue",
        "yaml_project",
        "yaml_name",
        "sdk_version",
        "workflow_contract_hash",
        "manifest_hash",
        "map_steps",
        "policy",
        "contributions",
        "code_provenance",
    )
    return WorkflowManifestDiff(
        changed_fields=tuple(
            ManifestFieldDiff(
                field=field, left=left_payload.get(field), right=right_payload.get(field)
            )
            for field in fields
            if left_payload.get(field) != right_payload.get(field)
        )
    )


def _without_contribution(payload: dict[str, Any], key: str) -> dict[str, Any]:
    contributions = payload.get("contributions")
    if not isinstance(contributions, dict) or key not in contributions:
        return payload
    pruned = {name: value for name, value in contributions.items() if name != key}
    return {**payload, "contributions": pruned}


def _diff_activities(
    left: Sequence[ActivityExecutionManifestView],
    right: Sequence[ActivityExecutionManifestView],
) -> tuple[ActivityManifestDiff, ...]:
    left_by_name = {activity.activity_name: activity for activity in left}
    right_by_name = {activity.activity_name: activity for activity in right}
    diffs = []
    for name in sorted(set(left_by_name) | set(right_by_name)):
        left_activity = left_by_name.get(name)
        right_activity = right_by_name.get(name)
        if left_activity is None:
            diffs.append(
                ActivityManifestDiff(activity_name=name, status="added", right=right_activity)
            )
            continue
        if right_activity is None:
            diffs.append(
                ActivityManifestDiff(activity_name=name, status="removed", left=left_activity)
            )
            continue
        changed = _activity_field_diffs(left_activity, right_activity)
        diffs.append(
            ActivityManifestDiff(
                activity_name=name,
                status="changed" if changed else "unchanged",
                changed_fields=changed,
                left=left_activity,
                right=right_activity,
            )
        )
    return tuple(diffs)


def _activity_field_diffs(
    left: ActivityExecutionManifestView,
    right: ActivityExecutionManifestView,
) -> tuple[ManifestFieldDiff, ...]:
    left_payload = left.to_public_dict()
    right_payload = right.to_public_dict()
    fields = (
        "activity_manifest_hash",
        "manifest_hash",
        "input_schema",
        "output_schema",
        "prompt_ref",
        "resolved_prompt_version",
        "prompt_messages_hash",
        "rendered_messages_hash",
        "provider_model",
        "temperature",
        "provider_params",
        "hook_name",
        "definition_source",
    )
    return tuple(
        ManifestFieldDiff(field=field, left=left_payload.get(field), right=right_payload.get(field))
        for field in fields
        if left_payload.get(field) != right_payload.get(field)
    )


def _span_counts(inspection: TraceInspection) -> dict[str, int]:
    return {
        "activities": len(inspection.activity_manifests),
        "generations": len(inspection.generation_spans),
        "hooks": len(inspection.hook_spans),
        "providers": len(inspection.provider_spans),
        "temporal": len(inspection.temporal_spans),
    }


__all__ = [
    "ActivityManifestDiff",
    "ManifestFieldDiff",
    "TraceDiff",
    "WorkflowManifestDiff",
    "diff_traces",
]
