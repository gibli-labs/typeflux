from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReconstructedExecutionManifest:
    workflow: dict[str, Any] | None
    activities: tuple[dict[str, Any], ...]
    missing_activities: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "activities": list(self.activities),
            "missing_activities": list(self.missing_activities),
            "warnings": list(self.warnings),
        }


def reconstruct_execution_manifest(
    trace_payload: Mapping[str, Any],
) -> ReconstructedExecutionManifest:
    metadata_candidates = _metadata_candidates(trace_payload)
    workflow = None
    root_activities: list[dict[str, Any]] = []
    child_activities: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for metadata in metadata_candidates:
        typeflux = metadata.get("typeflux")
        if not isinstance(typeflux, dict):
            continue
        execution_manifest = typeflux.get("execution_manifest")
        if isinstance(execution_manifest, dict):
            candidate_activities = _workflow_activity_rollup(execution_manifest)
            if workflow is None or len(candidate_activities) > len(root_activities):
                workflow = execution_manifest
                root_activities = candidate_activities
        activity_manifest = typeflux.get("activity_execution_manifest")
        if isinstance(activity_manifest, dict):
            activity_name = activity_manifest.get("activity_name")
            if isinstance(activity_name, str):
                child_activities.setdefault(activity_name, activity_manifest)

    expected = []
    expected_activity_hashes: dict[str, str] = {}
    if workflow is not None:
        raw_expected = workflow.get("activities")
        if isinstance(raw_expected, list):
            expected = [
                item if isinstance(item, str) else item.get("activity_name")
                for item in raw_expected
                if isinstance(item, str) or isinstance(item, dict)
            ]
            expected = [name for name in expected if isinstance(name, str)]
            expected_activity_hashes = {
                item["activity_name"]: item["activity_manifest_hash"]
                for item in raw_expected
                if isinstance(item, dict)
                and isinstance(item.get("activity_name"), str)
                and isinstance(item.get("activity_manifest_hash"), str)
            }
    activities = list(root_activities)
    observed_names = {
        activity.get("activity_name")
        for activity in activities
        if isinstance(activity.get("activity_name"), str)
    }
    for name in expected:
        if name not in observed_names and name in child_activities:
            activities.append(child_activities[name])
            observed_names.add(name)
    if not activities:
        activities = list(child_activities.values())

    observed = {activity.get("activity_name") for activity in activities}
    missing = tuple(name for name in expected if name not in observed)
    if workflow is None:
        warnings.append("missing workflow execution manifest")
    if missing:
        warnings.append("missing activity execution manifest(s): " + ", ".join(missing))
    mismatches = []
    for name, expected_hash in expected_activity_hashes.items():
        child = child_activities.get(name)
        if child is None:
            continue
        if child.get("activity_manifest_hash") != expected_hash:
            mismatches.append(name)
    if mismatches:
        warnings.append("activity manifest hash mismatch(es): " + ", ".join(sorted(mismatches)))

    return ReconstructedExecutionManifest(
        workflow=workflow,
        activities=tuple(activities),
        missing_activities=missing,
        warnings=tuple(warnings),
    )


def _metadata_candidates(value: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict):
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(metadata)
        for item in value.values():
            candidates.extend(_metadata_candidates(item))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(_metadata_candidates(item))
    return candidates


def _workflow_activity_rollup(workflow_manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    activities = []
    for item in workflow_manifest.get("activities") or ():
        if isinstance(item, dict) and "activity_name" in item:
            activities.append(item)
    return activities


__all__ = ["ReconstructedExecutionManifest", "reconstruct_execution_manifest"]
