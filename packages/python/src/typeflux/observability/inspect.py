from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from typeflux.manifests import (
    ReconstructedExecutionManifest,
    reconstruct_execution_manifest,
)


class _DTO(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    def to_public_dict(self) -> dict[str, Any]:
        return _public_value(self.model_dump(mode="python", exclude_none=True))

    def to_json_dict(self) -> dict[str, Any]:
        return self.to_public_dict()


class PromptRefView(_DTO):
    name: str
    # Manifests written before the version/label split stored version as a
    # label-like string; the reader keeps accepting both shapes.
    version: int | str | None = None
    label: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> PromptRefView:
        if isinstance(payload, PromptRefView):
            return payload
        if is_dataclass(payload):
            payload = asdict(payload)
        if isinstance(payload, str):
            payload = {"name": payload}
        if not isinstance(payload, dict):
            raise ValueError("prompt_ref must be a mapping or string")
        return cls.model_validate(payload)


class SchemaIdentityView(_DTO):
    name: str
    hash: str
    module: str | None = None
    module_status: str | None = None
    module_warning: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> SchemaIdentityView:
        if isinstance(payload, SchemaIdentityView):
            return payload
        if not isinstance(payload, dict):
            raise ValueError("schema identity must be a mapping")
        return cls.model_validate(payload)


class CodeProvenanceView(_DTO):
    available: bool | None = None
    source: str | None = None
    repo_url: str | None = None
    git_ref: str | None = None
    git_sha: str | None = None
    dirty: bool | None = None
    dirty_hash: str | None = None
    deployment_id: str | None = None
    environment: str | None = None
    package_version: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> CodeProvenanceView | None:
        if payload is None:
            return None
        if isinstance(payload, CodeProvenanceView):
            return payload
        if is_dataclass(payload):
            payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("code provenance must be a mapping")
        return cls.model_validate(payload)


class TemporalConnectionView(_DTO):
    address: str | None = None
    namespace: str | None = None
    region: str | None = None
    tls_enabled: bool | None = None
    tls_mode: Literal["disabled", "boolean", "custom"] | None = None
    api_key_configured: bool | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> TemporalConnectionView | None:
        if payload is None:
            return None
        if isinstance(payload, TemporalConnectionView):
            return payload
        if is_dataclass(payload):
            payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("temporal connection must be a mapping")
        if not payload:
            return None
        return cls.model_validate(payload)


class KubernetesRuntimePlacementView(_DTO):
    namespace: str | None = None
    pod_name: str | None = None
    pod_uid: str | None = None
    node_name: str | None = None
    service_account: str | None = None
    deployment_name: str | None = None
    worker_name: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> KubernetesRuntimePlacementView | None:
        if payload is None:
            return None
        if isinstance(payload, KubernetesRuntimePlacementView):
            return payload
        if is_dataclass(payload):
            payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("kubernetes runtime placement must be a mapping")
        if not payload:
            return None
        return cls.model_validate(payload)


class RuntimePlacementView(_DTO):
    platform: str | None = None
    kubernetes: KubernetesRuntimePlacementView | None = None
    container_image: str | None = None

    @field_validator("kubernetes", mode="before")
    @classmethod
    def _coerce_kubernetes(cls, value: Any) -> KubernetesRuntimePlacementView | None:
        return KubernetesRuntimePlacementView.from_payload(value)

    @classmethod
    def from_payload(cls, payload: Any) -> RuntimePlacementView | None:
        if payload is None:
            return None
        if isinstance(payload, RuntimePlacementView):
            return payload
        if is_dataclass(payload):
            payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("runtime placement must be a mapping")
        if not payload:
            return None
        return cls.model_validate(payload)


class PolicyView(_DTO):
    version: str | None = None
    selected_policy_ids: tuple[str, ...] = ()
    applied_policy_ids: tuple[str, ...] = ()
    policy_names: tuple[str, ...] = ()
    policy_hash: str | None = None
    enforcement_mode: str | None = None
    admission_status: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> PolicyView | None:
        if payload is None:
            return None
        if isinstance(payload, PolicyView):
            return payload
        if is_dataclass(payload):
            payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("policy must be a mapping")
        if not payload:
            return None
        return cls.model_validate(payload)


class ActivityDefinitionSourceView(_DTO):
    kind: Literal["yaml", "python", "unknown"] = "unknown"
    module: str | None = None
    export: str | None = None
    yaml_project: str | None = None
    yaml_name: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> ActivityDefinitionSourceView:
        if payload is None:
            return cls()
        if isinstance(payload, ActivityDefinitionSourceView):
            return payload
        if is_dataclass(payload):
            if hasattr(payload, "to_dict"):
                payload = payload.to_dict()
            else:
                payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("activity definition source must be a mapping")
        return cls.model_validate(payload)


class ActivityExecutionManifestView(_DTO):
    activity_name: str
    activity_manifest_hash: str | None = None
    definition_source: ActivityDefinitionSourceView = Field(
        default_factory=ActivityDefinitionSourceView
    )
    input_schema: SchemaIdentityView
    output_schema: SchemaIdentityView
    prompt_ref: PromptRefView
    manifest_hash: str | None = None
    manifest_version: str | None = None
    resolved_prompt_version: str | None = None
    prompt_messages_hash: str | None = None
    rendered_messages_hash: str | None = None
    provider_model: str | None = None
    temperature: float | None = None
    provider_params: dict[str, Any] = Field(default_factory=dict)
    hook_name: str | None = None
    artifact_inputs: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    validation_attempt: int | None = None

    @field_validator("input_schema", "output_schema", mode="before")
    @classmethod
    def _coerce_schema(cls, value: Any) -> SchemaIdentityView:
        return SchemaIdentityView.from_payload(value)

    @field_validator("prompt_ref", mode="before")
    @classmethod
    def _coerce_prompt_ref(cls, value: Any) -> PromptRefView:
        return PromptRefView.from_payload(value)

    @field_validator("definition_source", mode="before")
    @classmethod
    def _coerce_definition_source(cls, value: Any) -> ActivityDefinitionSourceView:
        return ActivityDefinitionSourceView.from_payload(value)

    @classmethod
    def from_payload(cls, payload: Any) -> ActivityExecutionManifestView:
        if isinstance(payload, ActivityExecutionManifestView):
            return payload
        if is_dataclass(payload) and hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        elif is_dataclass(payload):
            payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("activity execution manifest must be a mapping")
        return cls.model_validate(payload)

    def to_summary_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "activity_name": self.activity_name,
                "prompt_ref": self.prompt_ref.name,
                "resolved_prompt_version": self.resolved_prompt_version,
                "input_schema": self.input_schema.to_public_dict(),
                "output_schema": self.output_schema.to_public_dict(),
                "provider_model": self.provider_model,
                "provider_params": self.provider_params or None,
                "hook_name": self.hook_name,
                "artifact_inputs": list(self.artifact_inputs) or None,
                "artifacts": list(self.artifacts) or None,
                "definition_source": self.definition_source.to_public_dict(),
                "activity_manifest_hash": self.activity_manifest_hash,
            }
        )


class WorkflowExecutionManifestView(_DTO):
    workflow_name: str
    workflow_id: str
    task_queue: str
    activities: tuple[ActivityExecutionManifestView, ...] = ()
    map_steps: tuple[dict[str, Any], ...] = ()
    contributions: dict[str, Any] = Field(default_factory=dict)
    code_provenance: CodeProvenanceView | None = None
    temporal_connection: TemporalConnectionView | None = None
    policy: PolicyView | None = None
    temporal_run_id: str | None = None
    yaml_project: str | None = None
    yaml_name: str | None = None
    sdk_version: str | None = None
    manifest_version: str | None = None
    workflow_contract_hash: str | None = None
    manifest_hash: str | None = None

    @field_validator("activities", mode="before")
    @classmethod
    def _coerce_activities(cls, value: Any) -> tuple[ActivityExecutionManifestView, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("activities must be a sequence")
        return tuple(
            ActivityExecutionManifestView.from_payload(item)
            for item in value
            if isinstance(item, dict) or isinstance(item, ActivityExecutionManifestView)
        )

    @field_validator("map_steps", mode="before")
    @classmethod
    def _coerce_map_steps(cls, value: Any) -> tuple[dict[str, Any], ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("map_steps must be a sequence")
        return tuple(dict(item) for item in value if isinstance(item, dict))

    @field_validator("code_provenance", mode="before")
    @classmethod
    def _coerce_code_provenance(cls, value: Any) -> CodeProvenanceView | None:
        return CodeProvenanceView.from_payload(value)

    @field_validator("temporal_connection", mode="before")
    @classmethod
    def _coerce_temporal_connection(cls, value: Any) -> TemporalConnectionView | None:
        return TemporalConnectionView.from_payload(value)

    @field_validator("policy", mode="before")
    @classmethod
    def _coerce_policy(cls, value: Any) -> PolicyView | None:
        return PolicyView.from_payload(value)

    @classmethod
    def from_payload(cls, payload: Any) -> WorkflowExecutionManifestView:
        if isinstance(payload, WorkflowExecutionManifestView):
            return payload
        if is_dataclass(payload) and hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        elif is_dataclass(payload):
            payload = asdict(payload)
        if not isinstance(payload, dict):
            raise ValueError("workflow execution manifest must be a mapping")
        if "temporal_connection" not in payload:
            contributions = payload.get("contributions")
            if isinstance(contributions, dict):
                temporal_connection = contributions.get("temporal_connection")
                if isinstance(temporal_connection, dict):
                    payload = {**payload, "temporal_connection": temporal_connection}
        if "policy" not in payload:
            contributions = payload.get("contributions")
            if isinstance(contributions, dict):
                policy = contributions.get("policy")
                if isinstance(policy, dict):
                    payload = {**payload, "policy": policy}
        return cls.model_validate(payload)

    def to_summary_dict(self) -> dict[str, Any]:
        provenance = None
        if self.code_provenance is not None:
            provenance = _drop_none(
                {
                    "source": self.code_provenance.source,
                    "git_ref": self.code_provenance.git_ref,
                    "git_sha": self.code_provenance.git_sha,
                    "dirty": self.code_provenance.dirty,
                    "deployment_id": self.code_provenance.deployment_id,
                    "environment": self.code_provenance.environment,
                }
            )
        return _drop_none(
            {
                "workflow_name": self.workflow_name,
                "workflow_id": self.workflow_id,
                "temporal_run_id": self.temporal_run_id,
                "task_queue": self.task_queue,
                "workflow_contract_hash": self.workflow_contract_hash,
                "manifest_hash": self.manifest_hash,
                "map_steps": list(self.map_steps) or None,
                "contributions": self.contributions or None,
                "code_provenance": provenance,
                "temporal_connection": (
                    None
                    if self.temporal_connection is None
                    else self.temporal_connection.to_public_dict()
                ),
                "policy": None if self.policy is None else self.policy.to_public_dict(),
            }
        )


class ReconstructedExecutionManifestView(_DTO):
    workflow: WorkflowExecutionManifestView | None = None
    activities: tuple[ActivityExecutionManifestView, ...] = ()
    missing_activities: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_reconstructed(
        cls,
        reconstructed: ReconstructedExecutionManifest,
    ) -> ReconstructedExecutionManifestView:
        return cls(
            workflow=(
                WorkflowExecutionManifestView.from_payload(reconstructed.workflow)
                if reconstructed.workflow is not None
                else None
            ),
            activities=tuple(
                ActivityExecutionManifestView.from_payload(activity)
                for activity in reconstructed.activities
            ),
            missing_activities=reconstructed.missing_activities,
            warnings=reconstructed.warnings,
        )

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "workflow": None if self.workflow is None else self.workflow.to_public_dict(),
            "activities": [activity.to_public_dict() for activity in self.activities],
            "missing_activities": list(self.missing_activities),
            "warnings": list(self.warnings),
        }


class ObservationRecord(_DTO):
    observation_id: str | None = None
    name: str | None = None
    type: str | None = None
    level: str | None = None
    input: Any | None = None
    output: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    start_time: datetime | None = None
    end_time: datetime | None = None
    raw: Any | None = Field(default=None, exclude=True)


class TraceRetrievalInfo(_DTO):
    backend: str
    complete: bool
    pages_read: int
    observations_read: int
    page_size: int
    max_pages: int
    next_cursor: str | None = None
    warnings: tuple[str, ...] = ()


class TraceRecord(_DTO):
    trace_id: str
    name: str | None = None
    timestamp: datetime | None = None
    input: Any | None = None
    output: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    observations: tuple[ObservationRecord, ...] = ()
    retrieval: TraceRetrievalInfo | None = None
    raw: Any | None = Field(default=None, exclude=True)


class TracePage(_DTO):
    traces: tuple[TraceRecord, ...]
    next_cursor: str | None = None
    complete: bool = True
    warnings: tuple[str, ...] = ()

    def to_summary_dict(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "traces": [
                TraceSummaryView.from_trace(trace).to_public_dict() for trace in self.traces
            ],
            "next_cursor": self.next_cursor,
        }
        if self.warnings:
            summary["warnings"] = list(self.warnings)
        return summary


#: The audit-honesty caveat every subject-trace deletion report carries (#715
#: slice 2). The two query channels — the ``typeflux.subject:{id}`` tag and the
#: native ``userId`` — only see traces STAMPED with the subject index, which
#: began with slice 1. Traces written before subject tagging carry neither
#: carrier and are invisible to both channels: this report is complete for
#: post-slice-1 traces only, never a proof that a subject has no older traces.
SUBJECT_TRACE_INDEX_COVERAGE = (
    "Only traces stamped with the subject index (native userId + the "
    "typeflux.subject:<id> tag, emitted since #715 slice 1) are visible to the "
    "tag and user_id query channels; traces written before subject tagging carry "
    "neither carrier and are invisible here. This report is complete for "
    "post-slice-1 traces only — not proof that no older traces exist."
)


class SubjectTraceDeletionFailure(_DTO):
    """One trace the delete call could not remove — id plus the failure reason.

    Bulk deletes run in batches; a batch that raises attributes its whole span
    of ids here (the provider bulk API does not report per-id status), so a
    partial failure is visible without pretending finer granularity exists.
    """

    trace_id: str
    reason: str


class SubjectTraceDeletionConflict(_DTO):
    """A matched trace EXCLUDED from deletion because it is not solely this
    subject's (#715 review round).

    Slice 1 stamps a trace with ALL of a run's subjects, so a multi-subject
    trace matched while erasing ONE subject would, if deleted wholesale,
    silently destroy the OTHER subjects' audit trails. Such traces are excluded
    and surfaced here for deliberate operator handling (slice 5's CLI).

    ``other_subject_count`` is the number of OTHER ``typeflux.subject:`` markers
    on the trace — a COUNT, never the other subject ids: listing them would leak
    other subjects' presence into this subject's erasure report (the report is a
    per-subject compliance artifact and must not become a subject directory).
    ``None`` means the listing row carried no readable ``tags`` field, so the
    other-subject question is UNANSWERABLE — excluded fail-safe as
    conflicted-unknown rather than deleted on a guess.
    """

    trace_id: str
    other_subject_count: int | None = None


class SubjectTraceDeletionReport(_DTO):
    """The outcome of a subject-scoped trace deletion — the Langfuse surface's
    erasure primitive (#715 slice 2), carrying ids/counts only, never trace PII.

    ``matched_by_tag`` / ``matched_by_user_id`` are the raw per-channel hits (a
    trace may appear in both); ``trace_ids`` is their order-preserving,
    de-duplicated UNION **minus** the ``conflicted`` traces — exactly what a dry
    run WOULD delete. ``conflicted`` lists matched traces excluded because they
    also carry OTHER subjects (or their tags were unreadable) — see
    :class:`SubjectTraceDeletionConflict`. On execute, ``deleted_count`` counts
    successful removals and ``failures`` records the rest. ``index_coverage`` is
    the always-present audit caveat (:data:`SUBJECT_TRACE_INDEX_COVERAGE`);
    ``warnings`` carries scan-completeness caveats (e.g. a page-cap truncation).
    """

    subject_id: str
    dry_run: bool
    matched_by_tag: tuple[str, ...] = ()
    matched_by_user_id: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    deleted_count: int = 0
    failures: tuple[SubjectTraceDeletionFailure, ...] = ()
    conflicted: tuple[SubjectTraceDeletionConflict, ...] = ()
    index_coverage: str = SUBJECT_TRACE_INDEX_COVERAGE
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict for the erasure receipt (slice 5) and ``--json`` output."""

        return self.model_dump(mode="json")


@dataclass(frozen=True)
class TraceListQuery:
    limit: int = 20
    cursor: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    workflow_name: str | None = None
    workflow_id: str | None = None
    status: Literal["ok", "error"] | None = None


@dataclass(frozen=True)
class TraceSearchQuery:
    limit: int = 20
    cursor: str | None = None
    scan_pages: int = 5
    since: datetime | None = None
    until: datetime | None = None
    status: Literal["ok", "error"] | None = None
    workflow_name: str | None = None
    workflow_id: str | None = None
    activity_name: str | None = None
    prompt_ref: str | None = None
    resolved_prompt_version: str | None = None
    input_schema_hash: str | None = None
    output_schema_hash: str | None = None
    activity_manifest_hash: str | None = None
    execution_manifest_hash: str | None = None
    workflow_contract_hash: str | None = None
    provider_model: str | None = None
    git_sha: str | None = None
    deployment_id: str | None = None
    environment: str | None = None
    temporal_address: str | None = None
    temporal_namespace: str | None = None
    temporal_region: str | None = None
    runtime_platform: str | None = None
    k8s_namespace: str | None = None
    k8s_deployment_name: str | None = None
    k8s_pod_name: str | None = None
    container_image: str | None = None
    policy_id: str | None = None
    policy_name: str | None = None
    policy_hash: str | None = None
    include_untagged_fallback: bool = True
    backend_filter: Any | None = None


class TraceReader(Protocol):
    def get_trace(
        self,
        trace_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_detail_pages: int | None = None,
    ) -> TraceRecord: ...

    def list_traces(self, query: TraceListQuery) -> TracePage: ...

    def search_traces(self, query: TraceSearchQuery) -> TracePage: ...

    def delete_traces_for_subject(
        self,
        subject_id: str,
        *,
        dry_run: bool = True,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> SubjectTraceDeletionReport:
        """Erase every trace attributed to ``subject_id`` (#715 slice 2).

        The deletion primitive for the Langfuse surface: it queries by BOTH the
        ``typeflux.subject:{id}`` tag and the native ``userId`` and UNIONs the
        results (either carrier may exist), then — unless ``dry_run`` — removes
        them. ``dry_run`` (default True; safe by default) lists what WOULD be
        deleted without mutating; a real run reports what was deleted and what
        failed. ``since``/``until`` bound the trace timestamp window scanned.
        Backends without a deletable trace store raise a clear not-supported
        error rather than silently no-op.
        """
        ...


class TraceSummaryView(_DTO):
    trace_id: str
    timestamp: datetime | None = None
    status: Literal["ok", "error"]
    workflow_name: str | None = None
    workflow_id: str | None = None
    temporal_run_id: str | None = None
    task_queue: str | None = None
    workflow_contract_hash: str | None = None
    manifest_hash: str | None = None
    git_sha: str | None = None
    git_ref: str | None = None
    deployment_id: str | None = None
    environment: str | None = None
    temporal_connection: TemporalConnectionView | None = None
    runtime_placement: RuntimePlacementView | None = None
    policy: PolicyView | None = None
    activities: tuple[str, ...] = ()
    prompt_refs: tuple[str, ...] = ()
    provider_models: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_trace(cls, trace: TraceRecord) -> TraceSummaryView:
        reconstructed = reconstruct_execution_manifest(_trace_payload(trace))
        workflow = reconstructed.workflow or {}
        code = (
            workflow.get("code_provenance")
            if isinstance(workflow.get("code_provenance"), dict)
            else {}
        )
        temporal_connection = _temporal_connection(workflow)
        runtime_placement = _runtime_placement_for_trace(trace)
        policy = _policy_for_trace(trace, workflow)
        activities = tuple(
            _unique(
                activity.get("activity_name")
                for activity in reconstructed.activities
                if isinstance(activity.get("activity_name"), str)
            )
        )
        prompt_refs = tuple(
            _unique(
                _prompt_ref_name(activity.get("prompt_ref"))
                for activity in reconstructed.activities
                if _prompt_ref_name(activity.get("prompt_ref")) is not None
            )
        )
        provider_models = tuple(
            _unique(
                activity.get("provider_model")
                for activity in reconstructed.activities
                if isinstance(activity.get("provider_model"), str)
            )
        )
        return cls(
            trace_id=trace.trace_id,
            timestamp=trace.timestamp,
            status=_trace_status(trace),
            workflow_name=_workflow_name(workflow) or trace.name,
            workflow_id=_workflow_id(workflow),
            temporal_run_id=workflow.get("temporal_run_id")
            if isinstance(workflow.get("temporal_run_id"), str)
            else None,
            task_queue=workflow.get("task_queue")
            if isinstance(workflow.get("task_queue"), str)
            else None,
            workflow_contract_hash=workflow.get("workflow_contract_hash")
            if isinstance(workflow.get("workflow_contract_hash"), str)
            else None,
            manifest_hash=workflow.get("manifest_hash")
            if isinstance(workflow.get("manifest_hash"), str)
            else None,
            git_sha=code.get("git_sha") if isinstance(code.get("git_sha"), str) else None,
            git_ref=code.get("git_ref") if isinstance(code.get("git_ref"), str) else None,
            deployment_id=code.get("deployment_id")
            if isinstance(code.get("deployment_id"), str)
            else None,
            environment=code.get("environment")
            if isinstance(code.get("environment"), str)
            else None,
            temporal_connection=TemporalConnectionView.from_payload(temporal_connection),
            runtime_placement=RuntimePlacementView.from_payload(runtime_placement),
            policy=PolicyView.from_payload(policy),
            activities=activities,
            prompt_refs=prompt_refs,
            provider_models=provider_models,
            warnings=reconstructed.warnings,
        )

    def to_row_dict(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "status": self.status,
            "workflow": self.workflow_name or "",
            "workflow_id": self.workflow_id or "",
            "git": _short(self.git_sha),
            "contract": _short(self.workflow_contract_hash),
            "manifest": _short(self.manifest_hash),
            "activities": ",".join(self.activities),
            "prompts": ",".join(self.prompt_refs),
        }


class TraceInspection(_DTO):
    trace: TraceRecord
    reconstructed_manifest: ReconstructedExecutionManifest
    workflow: dict[str, Any] | None
    activities: tuple[dict[str, Any], ...]
    workflow_manifest: WorkflowExecutionManifestView | None = None
    activity_manifests: tuple[ActivityExecutionManifestView, ...] = ()
    manifest_view: ReconstructedExecutionManifestView
    provider_spans: tuple[ObservationRecord, ...]
    generation_spans: tuple[ObservationRecord, ...]
    hook_spans: tuple[ObservationRecord, ...]
    temporal_spans: tuple[ObservationRecord, ...]
    runtime_placement: RuntimePlacementView | None = None
    warnings: tuple[str, ...]

    def to_summary_dict(self) -> dict[str, Any]:
        workflow = (
            None if self.workflow_manifest is None else self.workflow_manifest.to_summary_dict()
        )
        if workflow is not None and self.runtime_placement is not None:
            workflow = {
                **workflow,
                "runtime_placement": self.runtime_placement.to_public_dict(),
            }
        return {
            "trace_id": self.trace.trace_id,
            "workflow": workflow,
            "activities": [activity.to_summary_dict() for activity in self.activity_manifests],
            "spans": {
                "activities": len(self.activity_manifests),
                "generations": len(self.generation_spans),
                "hooks": len(self.hook_spans),
                "providers": len(self.provider_spans),
                "temporal": len(self.temporal_spans),
            },
            "warnings": list(self.warnings),
        }

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "trace": self.trace.to_public_dict(),
            "workflow_manifest": (
                None if self.workflow_manifest is None else self.workflow_manifest.to_public_dict()
            ),
            "runtime_placement": (
                None if self.runtime_placement is None else self.runtime_placement.to_public_dict()
            ),
            "activity_manifests": [
                activity.to_public_dict() for activity in self.activity_manifests
            ],
            "reconstructed_manifest": self.manifest_view.to_manifest_dict(),
            "spans": {
                "provider": [span.to_public_dict() for span in self.provider_spans],
                "generation": [span.to_public_dict() for span in self.generation_spans],
                "hook": [span.to_public_dict() for span in self.hook_spans],
                "temporal": [span.to_public_dict() for span in self.temporal_spans],
            },
            "warnings": list(self.warnings),
        }

    def to_manifest_dict(self) -> dict[str, Any]:
        return self.manifest_view.to_manifest_dict()


def inspect_trace(
    reader: TraceReader,
    trace_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_detail_pages: int | None = None,
) -> TraceInspection:
    trace = reader.get_trace(
        trace_id,
        since=since,
        until=until,
        max_detail_pages=max_detail_pages,
    )
    payload = _trace_payload(trace)
    reconstructed = reconstruct_execution_manifest(payload)
    manifest_view = ReconstructedExecutionManifestView.from_reconstructed(reconstructed)
    warnings = tuple((*_retrieval_warnings(trace), *reconstructed.warnings))
    manifest_view = manifest_view.model_copy(update={"warnings": warnings})
    return TraceInspection(
        trace=trace,
        reconstructed_manifest=reconstructed,
        workflow=reconstructed.workflow,
        activities=reconstructed.activities,
        workflow_manifest=manifest_view.workflow,
        activity_manifests=manifest_view.activities,
        manifest_view=manifest_view,
        provider_spans=tuple(
            observation for observation in trace.observations if _is_provider_span(observation)
        ),
        generation_spans=tuple(
            observation for observation in trace.observations if _is_generation_span(observation)
        ),
        hook_spans=tuple(
            observation for observation in trace.observations if _is_hook_span(observation)
        ),
        temporal_spans=tuple(
            observation for observation in trace.observations if _is_temporal_span(observation)
        ),
        runtime_placement=RuntimePlacementView.from_payload(_runtime_placement_for_trace(trace)),
        warnings=warnings,
    )


def list_traces(reader: TraceReader, query: TraceListQuery) -> TracePage:
    return reader.list_traces(query)


def search_traces(reader: TraceReader, query: TraceSearchQuery) -> TracePage:
    return reader.search_traces(query)


def export_execution_manifest(
    reader: TraceReader,
    trace_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_detail_pages: int | None = None,
) -> dict[str, Any]:
    inspection = inspect_trace(
        reader,
        trace_id,
        since=since,
        until=until,
        max_detail_pages=max_detail_pages,
    )
    return inspection.to_manifest_dict()


def _trace_payload(trace: TraceRecord) -> dict[str, Any]:
    return {
        "metadata": trace.metadata,
        "observations": [
            {
                "metadata": observation.metadata,
                "name": observation.name,
                "type": observation.type,
                "level": observation.level,
            }
            for observation in trace.observations
        ],
    }


def _retrieval_warnings(trace: TraceRecord) -> tuple[str, ...]:
    if trace.retrieval is None:
        return ()
    return trace.retrieval.warnings


def _public_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        if hasattr(value, "to_public_dict"):
            return value.to_public_dict()
        return _public_value(value.model_dump(mode="json"))
    if is_dataclass(value):
        if hasattr(value, "to_dict"):
            return _public_value(value.to_dict())
        return _public_value(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    return value


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _as_utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp_matches_window(
    timestamp: datetime | None,
    *,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    normalized_timestamp = _as_utc_aware(timestamp)
    if since is not None and normalized_timestamp is not None and normalized_timestamp < since:
        return False
    if until is not None and normalized_timestamp is not None and normalized_timestamp > until:
        return False
    return True


def _apply_list_filters(
    traces: Sequence[TraceRecord],
    query: TraceListQuery,
) -> tuple[TraceRecord, ...]:
    since = _as_utc_aware(query.since)
    until = _as_utc_aware(query.until)
    filtered = []
    for trace in traces:
        workflow = _workflow_manifest(trace)
        if query.workflow_name and _workflow_name(workflow) != query.workflow_name:
            continue
        if query.workflow_id and not _trace_workflow_id_matches(
            trace,
            workflow,
            query.workflow_id,
            include_observation_metadata=True,
        ):
            continue
        if query.status == "error" and not _trace_has_error(trace):
            continue
        if query.status == "ok" and _trace_has_error(trace):
            continue
        if not _timestamp_matches_window(trace.timestamp, since=since, until=until):
            continue
        filtered.append(trace)
    return tuple(filtered)


def _trace_matches_search(trace: TraceRecord, query: TraceSearchQuery) -> bool:
    since = _as_utc_aware(query.since)
    until = _as_utc_aware(query.until)
    workflow = _workflow_manifest(trace)
    activities = _activity_manifests(trace)
    provider_activities = (
        _provider_activity_candidates(trace) if query.include_untagged_fallback else ()
    )
    if query.status and _trace_status(trace) != query.status:
        return False
    if not _timestamp_matches_window(trace.timestamp, since=since, until=until):
        return False
    if query.workflow_name:
        if workflow:
            if _workflow_name(workflow) != query.workflow_name:
                return False
        elif not any(
            _provider_context_matches(activity, workflow_name=query.workflow_name)
            for activity in provider_activities
        ):
            return False
    if query.workflow_id:
        if not _trace_workflow_id_matches(
            trace,
            workflow,
            query.workflow_id,
            include_observation_metadata=query.include_untagged_fallback,
        ):
            return False
    if (
        query.execution_manifest_hash
        and workflow.get("manifest_hash") != query.execution_manifest_hash
    ):
        return False
    if (
        query.workflow_contract_hash
        and workflow.get("workflow_contract_hash") != query.workflow_contract_hash
    ):
        return False
    code = (
        workflow.get("code_provenance") if isinstance(workflow.get("code_provenance"), dict) else {}
    )
    if query.git_sha and code.get("git_sha") != query.git_sha:
        return False
    if query.deployment_id and code.get("deployment_id") != query.deployment_id:
        return False
    if query.environment and code.get("environment") != query.environment:
        return False
    temporal_connection = _temporal_connection(workflow)
    if query.temporal_address and temporal_connection.get("address") != query.temporal_address:
        return False
    if (
        query.temporal_namespace
        and temporal_connection.get("namespace") != query.temporal_namespace
    ):
        return False
    if query.temporal_region and temporal_connection.get("region") != query.temporal_region:
        return False
    runtime_placement = _runtime_placement_for_trace(trace)
    kubernetes = runtime_placement.get("kubernetes")
    kubernetes = kubernetes if isinstance(kubernetes, dict) else {}
    if query.runtime_platform and runtime_placement.get("platform") != query.runtime_platform:
        return False
    if query.k8s_namespace and kubernetes.get("namespace") != query.k8s_namespace:
        return False
    if query.k8s_deployment_name and kubernetes.get("deployment_name") != query.k8s_deployment_name:
        return False
    if query.k8s_pod_name and kubernetes.get("pod_name") != query.k8s_pod_name:
        return False
    if query.container_image and runtime_placement.get("container_image") != query.container_image:
        return False
    policy = _policy_for_trace(trace, workflow)
    if query.policy_hash and policy.get("policy_hash") != query.policy_hash:
        return False
    if query.policy_id and query.policy_id not in {
        *_string_items(policy.get("selected_policy_ids")),
        *_string_items(policy.get("applied_policy_ids")),
    }:
        return False
    if query.policy_name and query.policy_name not in _string_items(policy.get("policy_names")):
        return False
    activity_filters = {
        "activity_name": query.activity_name,
        "activity_manifest_hash": query.activity_manifest_hash,
        "resolved_prompt_version": query.resolved_prompt_version,
        "provider_model": query.provider_model,
    }
    if (
        any(activity_filters.values())
        or query.prompt_ref
        or query.input_schema_hash
        or query.output_schema_hash
    ):
        return any(_activity_matches(activity, query) for activity in activities) or any(
            _activity_matches(activity, query) for activity in provider_activities
        )
    return True


def _activity_matches(activity: dict[str, Any], query: TraceSearchQuery) -> bool:
    if query.activity_name and activity.get("activity_name") != query.activity_name:
        return False
    if (
        query.activity_manifest_hash
        and activity.get("activity_manifest_hash") != query.activity_manifest_hash
    ):
        return False
    if (
        query.resolved_prompt_version
        and activity.get("resolved_prompt_version") != query.resolved_prompt_version
    ):
        return False
    if query.provider_model and activity.get("provider_model") != query.provider_model:
        return False
    if query.prompt_ref and _prompt_ref_name(activity.get("prompt_ref")) != query.prompt_ref:
        return False
    if (
        query.input_schema_hash
        and _schema_hash(activity.get("input_schema")) != query.input_schema_hash
    ):
        return False
    if (
        query.output_schema_hash
        and _schema_hash(activity.get("output_schema")) != query.output_schema_hash
    ):
        return False
    return True


def _workflow_manifest(trace: TraceRecord) -> dict[str, Any]:
    typeflux = trace.metadata.get("typeflux")
    if not isinstance(typeflux, dict):
        return {}
    execution_manifest = typeflux.get("execution_manifest")
    return execution_manifest if isinstance(execution_manifest, dict) else {}


def _activity_manifests(trace: TraceRecord) -> tuple[dict[str, Any], ...]:
    reconstructed = reconstruct_execution_manifest(_trace_payload(trace))
    return reconstructed.activities


def _workflow_name(workflow: dict[str, Any]) -> str | None:
    return workflow.get("workflow_name") or workflow.get("name")


def _workflow_id(workflow: dict[str, Any]) -> str | None:
    return workflow.get("workflow_id")


def _trace_workflow_id_matches(
    trace: TraceRecord,
    workflow: dict[str, Any],
    workflow_id: str,
    *,
    include_observation_metadata: bool,
) -> bool:
    if _workflow_id(workflow) == workflow_id:
        return True
    if not include_observation_metadata:
        return False
    for metadata in (trace.metadata, *(observation.metadata for observation in trace.observations)):
        if workflow_id in _workflow_ids_from_metadata(metadata):
            return True
    return False


def _workflow_ids_from_metadata(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in (
        # The bare key is the TS runtime's grouped-parent trace identity
        # (`runtime.ts` `recordWorkflowRun` metadata, #681): without it a
        # cross-edition correlation lookup (#686) would miss TS-traced runs.
        "workflow_id",
        "temporal.workflow_id",
        "typeflux.temporal.workflow_id",
        "typeflux.lifecycle_operation.workflow_id",
    ):
        _append_string(values, metadata.get(key))
    typeflux = metadata.get("typeflux")
    if isinstance(typeflux, Mapping):
        execution_manifest = typeflux.get("execution_manifest")
        if isinstance(execution_manifest, Mapping):
            _append_string(values, execution_manifest.get("workflow_id"))
        # The per-activity manifest carries the run's workflow_id in both
        # editions (#682); a WORKER-only traced run (a control-plane start,
        # #686) has no caller-side parent metadata, so the activity spans are
        # the only join surface the trace offers.
        activity_manifest = typeflux.get("activity_execution_manifest")
        if isinstance(activity_manifest, Mapping):
            _append_string(values, activity_manifest.get("workflow_id"))
        temporal = typeflux.get("temporal")
        if isinstance(temporal, Mapping):
            _append_string(values, temporal.get("workflow_id"))
        lifecycle_operation = typeflux.get("lifecycle_operation")
        if isinstance(lifecycle_operation, Mapping):
            _append_string(values, lifecycle_operation.get("workflow_id"))
    return tuple(values)


def _append_string(values: list[str], value: Any) -> None:
    if isinstance(value, str):
        values.append(value)


def _temporal_connection(workflow: dict[str, Any]) -> dict[str, Any]:
    direct = workflow.get("temporal_connection")
    if isinstance(direct, dict):
        return direct
    contributions = workflow.get("contributions")
    if not isinstance(contributions, dict):
        return {}
    temporal_connection = contributions.get("temporal_connection")
    return temporal_connection if isinstance(temporal_connection, dict) else {}


def _policy(workflow: dict[str, Any]) -> dict[str, Any]:
    direct = workflow.get("policy")
    if isinstance(direct, dict):
        return direct
    contributions = workflow.get("contributions")
    if not isinstance(contributions, dict):
        return {}
    policy = contributions.get("policy")
    return policy if isinstance(policy, dict) else {}


def _policy_for_trace(trace: TraceRecord, workflow: dict[str, Any]) -> dict[str, Any]:
    policy = dict(_policy(workflow))
    direct = _trace_policy(trace)
    if direct:
        policy.update(direct)
    return policy


def _runtime_placement_for_trace(trace: TraceRecord) -> dict[str, Any]:
    placement = _runtime_placement_from_metadata(trace.metadata)
    if placement:
        return placement
    for observation in trace.observations:
        placement = _runtime_placement_from_metadata(observation.metadata)
        if placement:
            return placement
    return {}


def _runtime_placement_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    typeflux = metadata.get("typeflux") if isinstance(metadata, dict) else None
    if not isinstance(typeflux, dict):
        return {}
    placement = typeflux.get("runtime_placement")
    return placement if isinstance(placement, dict) else {}


def _trace_policy(trace: TraceRecord) -> dict[str, Any]:
    typeflux = trace.metadata.get("typeflux") if isinstance(trace.metadata, dict) else None
    if not isinstance(typeflux, dict):
        return {}
    policy = typeflux.get("policy")
    return policy if isinstance(policy, dict) else {}


def _string_items(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _prompt_ref_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    if isinstance(value, str):
        return value
    return None


def _schema_hash(value: Any) -> str | None:
    return value.get("hash") if isinstance(value, dict) else None


def _provider_activity_candidates(trace: TraceRecord) -> tuple[dict[str, Any], ...]:
    candidates = []
    for metadata in (trace.metadata, *(observation.metadata for observation in trace.observations)):
        candidate = _provider_activity_candidate(metadata)
        if candidate:
            candidates.append(candidate)
    return tuple(candidates)


def _provider_activity_candidate(metadata: dict[str, Any]) -> dict[str, Any]:
    typeflux = metadata.get("typeflux")
    if not isinstance(typeflux, dict):
        return {}
    level = typeflux.get("level")
    if level not in {"provider", "generation"}:
        return {}
    activity = typeflux.get("activity") if isinstance(typeflux.get("activity"), dict) else {}
    provider = typeflux.get("provider") if isinstance(typeflux.get("provider"), dict) else {}
    join = typeflux.get("join") if isinstance(typeflux.get("join"), dict) else {}
    temporal = typeflux.get("temporal") if isinstance(typeflux.get("temporal"), dict) else {}
    return _drop_none(
        {
            "activity_name": typeflux.get("activity_name")
            or metadata.get("typeflux.activity_name"),
            "activity_manifest_hash": (
                activity.get("activity_manifest_hash")
                or join.get("activity_manifest_hash")
                or metadata.get("typeflux.manifest_hash")
            ),
            "manifest_hash": activity.get("manifest_hash")
            or join.get("activity_execution_manifest_hash"),
            "input_schema": activity.get("input_schema"),
            "output_schema": activity.get("output_schema"),
            "prompt_ref": activity.get("prompt_ref"),
            "resolved_prompt_version": activity.get("resolved_prompt_version")
            or provider.get("resolved_prompt_version"),
            "provider_model": activity.get("provider_model"),
            "provider_params": activity.get("provider_params"),
            "_workflow_name": temporal.get("workflow_type"),
            "_workflow_id": temporal.get("workflow_id"),
            "_run_id": temporal.get("run_id"),
        }
    )


def _provider_context_matches(
    activity: dict[str, Any],
    *,
    workflow_name: str | None = None,
    workflow_id: str | None = None,
) -> bool:
    if workflow_name and activity.get("_workflow_name") != workflow_name:
        return False
    if workflow_id and activity.get("_workflow_id") != workflow_id:
        return False
    return True


def _trace_has_error(trace: TraceRecord) -> bool:
    return any((observation.level or "").upper() == "ERROR" for observation in trace.observations)


def _trace_status(trace: TraceRecord) -> Literal["ok", "error"]:
    return "error" if _trace_has_error(trace) else "ok"


def _unique(values: Iterable[Any]) -> tuple[Any, ...]:
    seen = set()
    result = []
    for value in values:
        if value is None or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _short(value: str | None, length: int = 8) -> str:
    if not value:
        return ""
    return value[:length]


def _is_provider_span(observation: ObservationRecord) -> bool:
    typeflux = observation.metadata.get("typeflux")
    return isinstance(typeflux, dict) and typeflux.get("level") == "provider"


def _is_generation_span(observation: ObservationRecord) -> bool:
    typeflux = observation.metadata.get("typeflux")
    if isinstance(typeflux, dict) and typeflux.get("level") == "generation":
        return True
    return observation.type == "generation"


def _is_hook_span(observation: ObservationRecord) -> bool:
    typeflux = observation.metadata.get("typeflux")
    return isinstance(typeflux, dict) and typeflux.get("level") == "hook"


def _is_temporal_span(observation: ObservationRecord) -> bool:
    metadata = observation.metadata
    if any(str(key).startswith("temporal.") for key in metadata):
        return True
    return bool(observation.name and observation.name.startswith("Run"))


__all__ = [
    "ActivityDefinitionSourceView",
    "ActivityExecutionManifestView",
    "CodeProvenanceView",
    "ObservationRecord",
    "PolicyView",
    "PromptRefView",
    "ReconstructedExecutionManifestView",
    "SchemaIdentityView",
    "TemporalConnectionView",
    "TraceInspection",
    "TraceListQuery",
    "TracePage",
    "TraceReader",
    "TraceRecord",
    "TraceRetrievalInfo",
    "TraceSearchQuery",
    "TraceSummaryView",
    "WorkflowExecutionManifestView",
    "export_execution_manifest",
    "inspect_trace",
    "list_traces",
    "search_traces",
]
