from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from typeflux.manifests._common import canonical_json, drop_none
from typeflux.manifests.provenance import (
    CodeProvenance,
    collect_code_provenance,
    package_version_or_none,
)

ActivityRollupEntry = str | dict[str, Any]

_ACTIVITY_CONTRACT_FIELDS = (
    "activity_name",
    "activity_manifest_hash",
    "definition_source",
    "input_schema",
    "output_schema",
    "prompt_ref",
    "resolved_prompt_version",
    "prompt_messages_hash",
    "provider_model",
    "temperature",
    "hook_name",
    "artifact_inputs",
)

_PROVIDER_PARAM_CONTRACT_FIELDS = (
    "max_tokens",
    "top_p",
    "top_k",
    "stop",
    "seed",
    "frequency_penalty",
    "presence_penalty",
)


@dataclass(frozen=True)
class WorkflowExecutionManifest:
    workflow_name: str
    workflow_id: str
    task_queue: str
    activities: tuple[ActivityRollupEntry, ...]
    code_provenance: CodeProvenance
    map_steps: tuple[dict[str, Any], ...] = ()
    contributions: dict[str, Any] = field(default_factory=dict)
    temporal_run_id: str | None = None
    yaml_project: str | None = None
    yaml_name: str | None = None
    sdk_version: str | None = None
    manifest_version: str = "1"
    workflow_contract_hash: str = ""
    manifest_hash: str = ""

    def with_temporal_run_id(self, temporal_run_id: str | None) -> WorkflowExecutionManifest:
        if temporal_run_id is None or temporal_run_id == self.temporal_run_id:
            return self
        return build_workflow_execution_manifest(
            workflow_name=self.workflow_name,
            workflow_id=self.workflow_id,
            task_queue=self.task_queue,
            activities=self.activities,
            code_provenance=self.code_provenance,
            map_steps=self.map_steps,
            contributions=self.contributions,
            temporal_run_id=temporal_run_id,
            yaml_project=self.yaml_project,
            yaml_name=self.yaml_name,
            sdk_version=self.sdk_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "manifest_version": self.manifest_version,
                "manifest_hash": self.manifest_hash,
                "workflow_contract_hash": self.workflow_contract_hash,
                "workflow_name": self.workflow_name,
                "workflow_id": self.workflow_id,
                "temporal_run_id": self.temporal_run_id,
                "task_queue": self.task_queue,
                "activities": list(self.activities),
                "map_steps": list(self.map_steps) or None,
                "contributions": self.contributions or None,
                "code_provenance": self.code_provenance.to_dict(),
                "yaml_project": self.yaml_project,
                "yaml_name": self.yaml_name,
                "sdk_version": self.sdk_version,
            }
        )


def build_workflow_execution_manifest(
    *,
    workflow_name: str,
    workflow_id: str,
    task_queue: str,
    activities: Sequence[str | Mapping[str, Any]],
    code_provenance: CodeProvenance | None = None,
    map_steps: Sequence[Mapping[str, Any]] = (),
    contributions: Mapping[str, Any] | None = None,
    temporal_run_id: str | None = None,
    yaml_project: str | None = None,
    yaml_name: str | None = None,
    sdk_version: str | None = None,
) -> WorkflowExecutionManifest:
    provenance = code_provenance or collect_code_provenance()
    resolved_sdk_version = sdk_version or package_version_or_none()
    resolved_activities = [
        dict(activity) if isinstance(activity, Mapping) else activity for activity in activities
    ]
    resolved_map_steps = [dict(step) for step in map_steps]
    workflow_contract_hash = _workflow_contract_hash(
        workflow_name=workflow_name,
        activities=resolved_activities,
        map_steps=resolved_map_steps,
        yaml_project=yaml_project,
        yaml_name=yaml_name,
    )
    base = {
        "manifest_version": "1",
        "workflow_name": workflow_name,
        "workflow_contract_hash": workflow_contract_hash,
        "workflow_id": workflow_id,
        "temporal_run_id": temporal_run_id,
        "task_queue": task_queue,
        "activities": resolved_activities,
        "map_steps": resolved_map_steps or None,
        "contributions": dict(contributions) if contributions else None,
        "code_provenance": provenance.to_dict(),
        "yaml_project": yaml_project,
        "yaml_name": yaml_name,
        "sdk_version": resolved_sdk_version,
    }
    manifest_hash = sha256(canonical_json(drop_none(base)).encode("utf-8")).hexdigest()
    return WorkflowExecutionManifest(
        workflow_name=workflow_name,
        workflow_id=workflow_id,
        temporal_run_id=temporal_run_id,
        task_queue=task_queue,
        activities=tuple(
            dict(activity) if isinstance(activity, Mapping) else activity for activity in activities
        ),
        map_steps=tuple(dict(step) for step in map_steps),
        contributions=dict(contributions or {}),
        code_provenance=provenance,
        yaml_project=yaml_project,
        yaml_name=yaml_name,
        sdk_version=resolved_sdk_version,
        workflow_contract_hash=workflow_contract_hash,
        manifest_hash=manifest_hash,
    )


def _workflow_contract_hash(
    *,
    workflow_name: str,
    activities: Sequence[str | Mapping[str, Any]],
    map_steps: Sequence[Mapping[str, Any]],
    yaml_project: str | None,
    yaml_name: str | None,
) -> str:
    base = {
        "contract_version": "1",
        "workflow_name": workflow_name,
        "activities": [_activity_contract_payload(activity) for activity in activities],
        "map_steps": [dict(step) for step in map_steps] or None,
        "yaml_project": yaml_project,
        "yaml_name": yaml_name,
    }
    return sha256(canonical_json(drop_none(base)).encode("utf-8")).hexdigest()


def _activity_contract_payload(activity: str | Mapping[str, Any]) -> str | dict[str, Any]:
    if isinstance(activity, str):
        return activity
    payload = drop_none(
        {field: activity.get(field) for field in _ACTIVITY_CONTRACT_FIELDS if field in activity}
    )
    provider_params = activity.get("provider_params")
    if isinstance(provider_params, Mapping):
        contract_params = drop_none(
            {
                field: provider_params.get(field)
                for field in _PROVIDER_PARAM_CONTRACT_FIELDS
                if field in provider_params
            }
        )
        if contract_params:
            payload["provider_params"] = contract_params
    return payload


__all__ = ["ActivityRollupEntry", "WorkflowExecutionManifest", "build_workflow_execution_manifest"]
