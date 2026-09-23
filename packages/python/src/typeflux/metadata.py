from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from typeflux.core.errors import TypefluxError


class MetadataConflictError(TypefluxError, ValueError):
    """Raised when metadata contributors provide incompatible values."""


@dataclass(frozen=True)
class MetadataContribution:
    workflow_manifest: Mapping[str, Any] = field(default_factory=dict)
    workflow_metadata: Mapping[str, Any] = field(default_factory=dict)
    activity_metadata: Mapping[str, Any] = field(default_factory=dict)
    operation_metadata: Mapping[str, Any] = field(default_factory=dict)
    search_tags: Sequence[str] = ()
    redaction_exclusions: Sequence[str] = ()


@dataclass(frozen=True)
class WorkflowMetadataContext:
    workflow_name: str
    workflow_id: str
    task_queue: str
    temporal_run_id: str | None = None
    activities: Sequence[Any] = ()
    activity_rollup: Sequence[str | Mapping[str, Any]] | None = None
    input_value: Any | None = None
    user_metadata: Mapping[str, Any] | None = None
    user_tags: Sequence[str] = ()
    include_execution_manifest: bool = True
    execution_manifest: Any | None = None


@dataclass(frozen=True)
class ActivityMetadataContext:
    manifest: Any
    activity_execution_manifest: Any | None
    invocation_context: Any | None
    level: str
    validation_attempt: int | None = None
    registry_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class LifecycleOperationMetadataContext:
    operation_type: Literal["query", "signal"]
    operation_name: str
    workflow_name: str | None
    workflow_id: str
    run_id: str | None = None
    status: Any | None = None
    review_user_decision: str | None = None
    review_route_target: str | None = None
    #: The gate the decision addressed (#55 slice 4, multi-gate only; None is dropped,
    #: so single-gate operation metadata is byte-identical).
    review_gate: str | None = None
    cancellation_requested: bool = False


class MetadataContributor(Protocol):
    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution: ...

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution: ...

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution: ...


class NoOpMetadataContributor:
    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class CoreWorkflowContributor:
    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        typeflux: dict[str, Any] = {
            "manifest_version": "1",
            "level": "workflow",
            "workflow": {
                "workflow_name": context.workflow_name,
                "workflow_id": context.workflow_id,
                "task_queue": context.task_queue,
            },
            "activities": [_activity_name(activity) for activity in context.activities],
        }
        if context.include_execution_manifest and context.execution_manifest is not None:
            typeflux["execution_manifest"] = _plain_payload(context.execution_manifest)

        tags = {
            "typeflux",
            f"typeflux.workflow:{context.workflow_name}",
        }
        for activity in context.activities:
            if name := _activity_name(activity):
                tags.add(f"typeflux.activity:{name}")
            prompt_name = _activity_prompt_name(activity)
            if prompt_name:
                tags.add(f"typeflux.prompt:{prompt_name}")
        for item in context.activity_rollup or ():
            if isinstance(item, str):
                # Plain Temporal activities roll up as planned names only.
                tags.add(f"typeflux.activity:{item}")
                continue
            activity_name = item.get("activity_name")
            if isinstance(activity_name, str):
                tags.add(f"typeflux.activity:{activity_name}")
            prompt_name = _prompt_ref_name(item.get("prompt_ref"))
            if prompt_name:
                tags.add(f"typeflux.prompt:{prompt_name}")
            provider_model = item.get("provider_model")
            if isinstance(provider_model, str):
                tags.add(f"typeflux.model:{provider_model}")
        if environment := _workflow_environment_from_manifest(context.execution_manifest):
            tags.add(f"typeflux.env:{environment}")

        return MetadataContribution(
            workflow_metadata={"typeflux": typeflux},
            search_tags=tuple(sorted(tags)),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class YamlWorkflowContributor:
    def __init__(
        self,
        *,
        project: str | None,
        name: str | None,
        map_steps: Sequence[Mapping[str, Any]] = (),
        spec_digest: str | None = None,
        spec_digest_algorithm: str | None = None,
        generator_version: str | None = None,
        workflow_type: str | None = None,
        workflow_version_label: str | None = None,
    ) -> None:
        self.project = project
        self.name = name
        self.map_steps = tuple(dict(step) for step in map_steps)
        self.spec_digest = spec_digest
        self.spec_digest_algorithm = spec_digest_algorithm
        self.generator_version = generator_version
        self.workflow_type = workflow_type
        self.workflow_version_label = workflow_version_label

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        yaml_payload = _drop_none(
            {
                "name": self.name,
                "project": self.project,
                "spec_digest": self.spec_digest,
                "spec_digest_algorithm": self.spec_digest_algorithm,
                "generator_version": self.generator_version,
                "workflow_type": self.workflow_type,
                "workflow_version_label": self.workflow_version_label,
            }
        )
        if self.map_steps:
            yaml_payload["map_steps"] = [dict(step) for step in self.map_steps]
        manifest_payload = _drop_none(
            {
                "yaml_project": self.project,
                "yaml_name": self.name,
                "map_steps": [dict(step) for step in self.map_steps] or None,
                "contributions": {
                    "yaml": yaml_payload,
                }
                if yaml_payload
                else None,
            }
        )
        return MetadataContribution(
            workflow_manifest=manifest_payload,
            workflow_metadata={"typeflux": {"yaml": yaml_payload}} if yaml_payload else {},
            redaction_exclusions=(
                "typeflux.yaml.map_steps.*",
                "typeflux.yaml.spec_digest",
                "typeflux.yaml.spec_digest_algorithm",
                "typeflux.yaml.generator_version",
                "typeflux.yaml.workflow_type",
                "typeflux.yaml.workflow_version_label",
                "typeflux.execution_manifest.map_steps.*",
                "typeflux.execution_manifest.contributions.yaml.*",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class YamlOverrideContributor:
    def __init__(self, provenance: Any | None) -> None:
        self.payload = _yaml_override_payload(provenance)

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        if not self.payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_manifest={
                "contributions": {
                    "yaml_overrides": self.payload,
                }
            },
            workflow_metadata={
                "typeflux": {
                    "yaml_overrides": self.payload,
                }
            },
            redaction_exclusions=(
                "typeflux.yaml_overrides.source",
                "typeflux.yaml_overrides.project_name",
                "typeflux.yaml_overrides.environment_id",
                "typeflux.yaml_overrides.environment_name",
                "typeflux.yaml_overrides.workflow_id",
                "typeflux.yaml_overrides.override_paths.*",
                "typeflux.execution_manifest.contributions.yaml_overrides.*",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class ComponentProvenanceContributor:
    """Safe component-profile provenance (#215): kind/id/name/content hash only.

    Low-cardinality identifiers explain which component produced the effective
    provider/registry/runtime settings; no secrets, prompt text, or paths.
    """

    def __init__(self, components: Sequence[Mapping[str, Any]] = ()) -> None:
        self.payload = [
            {
                "kind": item.get("kind"),
                "id": item.get("id"),
                "name": item.get("name"),
                "content_hash": item.get("content_hash"),
            }
            for item in components
            if isinstance(item, Mapping)
        ]

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        if not self.payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_manifest={
                "contributions": {
                    "components": self.payload,
                }
            },
            workflow_metadata={
                "typeflux": {
                    "components": self.payload,
                }
            },
            redaction_exclusions=(
                "typeflux.components.*",
                "typeflux.execution_manifest.contributions.components.*",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class SecretReferenceContributor:
    def __init__(self, references: Sequence[Any] = ()) -> None:
        self.payload = _secret_references_payload(references)

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        if not self.payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_manifest={
                "contributions": {
                    "secret_references": self.payload,
                }
            },
            workflow_metadata={
                "typeflux": {
                    "secret_references": self.payload,
                }
            },
            redaction_exclusions=(
                "typeflux.secret_references.references.runtime_path",
                "typeflux.secret_references.references.source_kind",
                "typeflux.secret_references.references.source_name",
                "typeflux.secret_references.references.configured",
                "typeflux.execution_manifest.contributions.secret_references.references.runtime_path",
                "typeflux.execution_manifest.contributions.secret_references.references.source_kind",
                "typeflux.execution_manifest.contributions.secret_references.references.source_name",
                "typeflux.execution_manifest.contributions.secret_references.references.configured",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class PolicyContributor:
    def __init__(
        self,
        *,
        version: str | None = None,
        selected_policy_ids: Sequence[str] = (),
        applied_policy_ids: Sequence[str] = (),
        policy_names: Sequence[str] = (),
        policy_hash: str | None = None,
        enforcement_mode: str | None = None,
        admission_status: str | None = None,
    ) -> None:
        self.workflow_manifest_payload = _drop_none(
            {
                "version": version,
                "selected_policy_ids": list(selected_policy_ids) or None,
                "applied_policy_ids": list(applied_policy_ids) or None,
                "policy_names": list(policy_names) or None,
                "policy_hash": policy_hash,
                "admission_status": admission_status,
            }
        )
        self.workflow_metadata_payload = _drop_none(
            {
                **self.workflow_manifest_payload,
                "enforcement_mode": enforcement_mode,
            }
        )

    @classmethod
    def from_policy(
        cls,
        policy: Any | None,
        *,
        enforcement_mode: str = "runtime",
        admission_status: str = "passed",
    ) -> PolicyContributor:
        if policy is None:
            return cls()
        payload = getattr(policy, "payload", None)
        payload_mapping = payload if isinstance(payload, Mapping) else {}
        return cls(
            version=_string_or_none(payload_mapping.get("version")) or "1",
            selected_policy_ids=_string_sequence(
                getattr(policy, "selected_policy_ids", None)
                or payload_mapping.get("selected_policy_ids")
            ),
            applied_policy_ids=_string_sequence(
                getattr(policy, "applied_policy_ids", None)
                or payload_mapping.get("applied_policy_ids")
            ),
            policy_names=_string_sequence(
                getattr(policy, "policy_names", None) or payload_mapping.get("policy_names")
            ),
            policy_hash=_string_or_none(getattr(policy, "policy_hash", None))
            or _string_or_none(payload_mapping.get("policy_hash")),
            enforcement_mode=enforcement_mode,
            admission_status=admission_status,
        )

    @classmethod
    def from_guard(
        cls,
        guard: Any | None,
        *,
        enforcement_mode: str | None = None,
        admission_status: str | None = None,
    ) -> PolicyContributor:
        if guard is None:
            return cls()
        return cls.from_policy(
            getattr(guard, "policy", None),
            enforcement_mode=enforcement_mode
            or _string_or_none(getattr(guard, "enforcement_mode", None))
            or "runtime",
            admission_status=admission_status
            or _string_or_none(getattr(guard, "admission_status", None))
            or "passed",
        )

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        if not self.workflow_metadata_payload and not self.workflow_manifest_payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_manifest={
                "contributions": {
                    "policy": self.workflow_manifest_payload,
                }
            }
            if self.workflow_manifest_payload
            else {},
            workflow_metadata={
                "typeflux": {
                    "policy": self.workflow_metadata_payload,
                }
            }
            if self.workflow_metadata_payload
            else {},
            redaction_exclusions=(
                "typeflux.policy.version",
                "typeflux.policy.selected_policy_ids",
                "typeflux.policy.applied_policy_ids",
                "typeflux.policy.policy_names",
                "typeflux.policy.policy_hash",
                "typeflux.policy.enforcement_mode",
                "typeflux.policy.admission_status",
                "typeflux.execution_manifest.contributions.policy.version",
                "typeflux.execution_manifest.contributions.policy.selected_policy_ids",
                "typeflux.execution_manifest.contributions.policy.applied_policy_ids",
                "typeflux.execution_manifest.contributions.policy.policy_names",
                "typeflux.execution_manifest.contributions.policy.policy_hash",
                "typeflux.execution_manifest.contributions.policy.admission_status",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class AdmissionContributor:
    """Provenance for a spec that entered via ``admit_spec`` (#298 D298-4), mirroring
    ``PolicyContributor``. Stamps only SAFE identity — ``typeflux.admission.spec_origin``
    / ``status`` / ``policy_hash`` — never prompts, reviewer notes, or spec content, and
    only when admission actually ran (an operator filesystem flow that never called
    ``admit_spec`` contributes nothing, so its manifest is byte-unchanged)."""

    def __init__(
        self,
        *,
        spec_origin: str | None = None,
        status: str | None = None,
        policy_hash: str | None = None,
    ) -> None:
        self.payload = _drop_none(
            {
                "spec_origin": spec_origin,
                "status": status,
                "policy_hash": policy_hash,
            }
        )

    @classmethod
    def from_report(cls, report: Any | None) -> AdmissionContributor:
        """Build from an ``AdmissionReport`` (duck-typed to avoid a project→metadata
        import edge). ``None`` (admission never ran) yields an inert contributor."""
        if report is None:
            return cls()
        return cls(
            spec_origin=_string_or_none(getattr(report, "spec_origin", None)),
            status=(
                "admitted"
                if getattr(report, "admitted", None) is True
                else "rejected"
                if getattr(report, "admitted", None) is False
                else None
            ),
            policy_hash=_string_or_none(getattr(report, "policy_hash", None)),
        )

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        if not self.payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_manifest={"contributions": {"admission": self.payload}},
            workflow_metadata={"typeflux": {"admission": self.payload}},
            redaction_exclusions=(
                "typeflux.admission.spec_origin",
                "typeflux.admission.status",
                "typeflux.admission.policy_hash",
                "typeflux.execution_manifest.contributions.admission.spec_origin",
                "typeflux.execution_manifest.contributions.admission.status",
                "typeflux.execution_manifest.contributions.admission.policy_hash",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class RiskTierContributor:
    """Risk-tier evidence for a workflow governed by a ``risk_tiers`` policy (#300 D300-5).

    Mirrors ``PolicyContributor``: stamps only SAFE, low-cardinality governance
    identity — the declared/effective tier, what lifted the effective tier
    (``floor_source``), and the NAMES of the controls the effective tier is satisfied
    by — never prompt text, reviewer notes, or secrets. Contributes one 4-valued
    search tag ``typeflux.risk_tier:<effective>``. Stamped ONLY when a ``risk_tiers``
    dimension was in play (parity with ``AdmissionContributor``'s only-when-ran rule):
    an inert contributor (no data) contributes nothing, so a manifest for an
    ungoverned workflow is byte-unchanged.

    The tier here reflects the workflow's OWN declared+floor effective tier; the
    sub-workflow closure CASCADE (a parent lifted by a higher-tier child) is an
    admission-time fail-closed concern, evaluated by the closure walk.
    """

    def __init__(
        self,
        *,
        declared: str | None = None,
        effective: str | None = None,
        floor_source: str | None = None,
        satisfied_controls: Sequence[str] = (),
    ) -> None:
        self.effective = _string_or_none(effective)
        self.payload = _drop_none(
            {
                "declared": _string_or_none(declared),
                "effective": self.effective,
                "floor_source": _string_or_none(floor_source),
                "satisfied_controls": list(satisfied_controls) or None,
            }
        )

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        if not self.payload:
            return MetadataContribution()
        tags = (f"typeflux.risk_tier:{self.effective}",) if self.effective is not None else ()
        return MetadataContribution(
            workflow_manifest={"contributions": {"risk_tier": self.payload}},
            workflow_metadata={"typeflux": {"risk_tier": self.payload}},
            search_tags=tags,
            redaction_exclusions=(
                "typeflux.risk_tier.declared",
                "typeflux.risk_tier.effective",
                "typeflux.risk_tier.floor_source",
                "typeflux.risk_tier.satisfied_controls",
                "typeflux.execution_manifest.contributions.risk_tier.declared",
                "typeflux.execution_manifest.contributions.risk_tier.effective",
                "typeflux.execution_manifest.contributions.risk_tier.floor_source",
                "typeflux.execution_manifest.contributions.risk_tier.satisfied_controls",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class CompensationContributor:
    """Saga-compensation provenance for a workflow that declares ``compensate:`` steps
    (#299 D299-5), mirroring ``RiskTierContributor``.

    Stamps only SAFE, low-cardinality identity — the sorted ids of the steps that plan a
    compensation (``declared_steps``) and a static ``status`` marker — never prompts,
    compensation inputs, reviewer notes, or secrets. The step ids recorded are exactly
    the workflow's PLANNED graph steps (a ``compensate:`` is a plan-native command, not an
    ad-hoc client rollback), so this satisfies the #299 acceptance criterion "execution
    manifests do not record ad hoc client-side rollback actions unless they are planned
    workflow graph steps" BY CONSTRUCTION. Stamped ONLY when the workflow actually declares
    compensation (parity with ``RiskTierContributor``'s only-when-in-play rule): a workflow
    with no ``compensate:`` step contributes nothing, so its manifest is byte-unchanged.

    ``status`` is the DESIGN-time marker ``declared`` — the plan carries compensations. It
    is deliberately distinct from the RUNTIME terminal ``compensation_status``
    (``complete``/``partial``/``none``, on the status wire), which is only known after an
    unwind executes; the contributor records the plan, not the outcome.

    TS parity note: the TypeScript SDK has no metadata-contributor seam (like
    ``RiskTierContributor``, this is Python-only) — TS records compensation identity through
    the plan node + status wire, documented in the TS governance docs.
    """

    def __init__(self, *, declared_steps: Sequence[str] = ()) -> None:
        steps = sorted({step for step in declared_steps if isinstance(step, str) and step})
        self.declared_steps = tuple(steps)
        self.payload = _drop_none(
            {
                "declared_steps": list(steps) or None,
                "status": "declared" if steps else None,
            }
        )

    @classmethod
    def from_spec(cls, spec: Any) -> CompensationContributor:
        """Build from a resolved YAML spec by collecting every step id that declares a
        ``compensate:`` (walking parallel branches). An inert contributor (no compensating
        steps) contributes nothing."""
        return cls(declared_steps=_declared_compensation_steps(spec))

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        if not self.payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_manifest={"contributions": {"compensation": self.payload}},
            workflow_metadata={"typeflux": {"compensation": self.payload}},
            search_tags=("typeflux.compensation:declared",),
            redaction_exclusions=(
                "typeflux.compensation.declared_steps",
                "typeflux.compensation.status",
                "typeflux.execution_manifest.contributions.compensation.declared_steps",
                "typeflux.execution_manifest.contributions.compensation.status",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


def _declared_compensation_steps(spec: Any) -> list[str]:
    """Step ids that declare a ``compensate:`` across the full step tree (#299 D299-5).

    Local imports avoid a metadata→yaml.spec load-time edge (metadata is imported broadly).
    Recurses into parallel branches; a compensation is available on activity / map /
    sub-workflow steps and steps inside parallel branches (the shared step schema).
    """
    from typeflux.yaml.spec import WorkflowParallelStepSpec

    def _walk(steps: Sequence[Any]) -> list[str]:
        found: list[str] = []
        for step in steps:
            if isinstance(step, WorkflowParallelStepSpec):
                for branch in step.parallel.branches:
                    found.extend(_walk(branch.steps))
                continue
            if getattr(step, "compensate", None) is not None:
                found.append(step.id)
        return found

    workflow = getattr(spec, "workflow", None)
    steps = getattr(workflow, "steps", None)
    if not steps:
        return []
    return _walk(steps)


# Safe operational lifecycle fields; freeform values such as cancellation
# reasons or review notes must never be added here.
_SAFE_LIFECYCLE_FIELDS = (
    "state",
    "current_step",
    "completed_units",
    "total_units",
    "cancellation_requested",
    "waiting_checkpoint",
    "terminal_status",
    "status_event_limit",
    "review_after_step",
)


class YamlLifecycleContributor:
    def __init__(self, metadata: Mapping[str, Any] | None) -> None:
        self.metadata = dict(metadata or {})

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        return MetadataContribution(
            workflow_metadata={"typeflux": {"lifecycle": self.metadata}} if self.metadata else {},
            redaction_exclusions=tuple(
                f"{prefix}.{field}"
                for prefix in (
                    "typeflux.lifecycle",
                    "typeflux.execution_manifest.contributions.lifecycle",
                )
                for field in _SAFE_LIFECYCLE_FIELDS
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class TemporalConnectionContributor:
    def __init__(
        self,
        *,
        address: str | None = None,
        namespace: str | None = None,
        region: str | None = None,
        tls_enabled: bool | None = None,
        tls_mode: Literal["disabled", "boolean", "custom"] | None = None,
        api_key_configured: bool = False,
    ) -> None:
        self.address = address
        self.namespace = namespace
        self.region = region
        self.tls_enabled = tls_enabled
        self.tls_mode = tls_mode
        self.api_key_configured = api_key_configured

    @classmethod
    def from_env(
        cls,
        *,
        address: str | None = None,
        namespace: str | None = None,
        tls_enabled: bool | None = None,
        tls_mode: Literal["disabled", "boolean", "custom"] | None = None,
        api_key_configured: bool | None = None,
    ) -> TemporalConnectionContributor:
        resolved_tls_enabled = _env_flag("TEMPORAL_TLS") if tls_enabled is None else tls_enabled
        return cls(
            address=address or os.getenv("TEMPORAL_ADDRESS") or None,
            namespace=namespace or os.getenv("TEMPORAL_NAMESPACE") or None,
            region=os.getenv("TYPEFLUX_TEMPORAL_REGION") or None,
            tls_enabled=resolved_tls_enabled,
            tls_mode=tls_mode or ("boolean" if resolved_tls_enabled else "disabled"),
            api_key_configured=(
                bool(os.getenv("TEMPORAL_API_KEY"))
                if api_key_configured is None
                else api_key_configured
            ),
        )

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        payload = _drop_none(
            {
                "address": self.address,
                "namespace": self.namespace,
                "region": self.region,
                "tls_enabled": self.tls_enabled,
                "tls_mode": self.tls_mode,
                "api_key_configured": self.api_key_configured,
            }
        )
        if not payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_manifest={
                "contributions": {
                    "temporal_connection": payload,
                }
            },
            workflow_metadata={
                "typeflux": {
                    "temporal_connection": payload,
                }
            },
            redaction_exclusions=(
                "typeflux.temporal_connection.address",
                "typeflux.temporal_connection.namespace",
                "typeflux.temporal_connection.region",
                "typeflux.temporal_connection.tls_enabled",
                "typeflux.temporal_connection.tls_mode",
                "typeflux.temporal_connection.api_key_configured",
                "typeflux.execution_manifest.contributions.temporal_connection.address",
                "typeflux.execution_manifest.contributions.temporal_connection.namespace",
                "typeflux.execution_manifest.contributions.temporal_connection.region",
                "typeflux.execution_manifest.contributions.temporal_connection.tls_enabled",
                "typeflux.execution_manifest.contributions.temporal_connection.tls_mode",
                "typeflux.execution_manifest.contributions.temporal_connection.api_key_configured",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class RuntimePlacementContributor:
    def __init__(
        self,
        *,
        platform: str | None = None,
        k8s_namespace: str | None = None,
        k8s_pod_name: str | None = None,
        k8s_pod_uid: str | None = None,
        k8s_node_name: str | None = None,
        k8s_service_account: str | None = None,
        k8s_deployment_name: str | None = None,
        k8s_worker_name: str | None = None,
        container_image: str | None = None,
    ) -> None:
        self.platform = platform
        self.k8s_namespace = k8s_namespace
        self.k8s_pod_name = k8s_pod_name
        self.k8s_pod_uid = k8s_pod_uid
        self.k8s_node_name = k8s_node_name
        self.k8s_service_account = k8s_service_account
        self.k8s_deployment_name = k8s_deployment_name
        self.k8s_worker_name = k8s_worker_name
        self.container_image = container_image

    @classmethod
    def from_env(cls) -> RuntimePlacementContributor:
        return cls(
            platform=_env_value("TYPEFLUX_RUNTIME_PLATFORM"),
            k8s_namespace=_env_value("TYPEFLUX_K8S_NAMESPACE"),
            k8s_pod_name=_env_value("TYPEFLUX_K8S_POD_NAME"),
            k8s_pod_uid=_env_value("TYPEFLUX_K8S_POD_UID"),
            k8s_node_name=_env_value("TYPEFLUX_K8S_NODE_NAME"),
            k8s_service_account=_env_value("TYPEFLUX_K8S_SERVICE_ACCOUNT"),
            k8s_deployment_name=_env_value("TYPEFLUX_K8S_DEPLOYMENT_NAME"),
            k8s_worker_name=_env_value("TYPEFLUX_K8S_WORKER_NAME"),
            container_image=_env_value("TYPEFLUX_CONTAINER_IMAGE"),
        )

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        payload = self._payload()
        if not payload:
            return MetadataContribution()
        return MetadataContribution(
            workflow_metadata={
                "typeflux": {
                    "runtime_placement": payload,
                }
            },
            redaction_exclusions=self._redaction_exclusions(),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        del context
        payload = self._payload()
        if not payload:
            return MetadataContribution()
        return MetadataContribution(
            activity_metadata={
                "typeflux": {
                    "runtime_placement": payload,
                }
            },
            redaction_exclusions=self._redaction_exclusions(),
        )

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def _payload(self) -> dict[str, Any]:
        kubernetes_payload = _drop_none(
            {
                "namespace": self.k8s_namespace,
                "pod_name": self.k8s_pod_name,
                "pod_uid": self.k8s_pod_uid,
                "node_name": self.k8s_node_name,
                "service_account": self.k8s_service_account,
                "deployment_name": self.k8s_deployment_name,
                "worker_name": self.k8s_worker_name,
            }
        )
        payload = _drop_none(
            {
                "platform": self.platform,
                "kubernetes": kubernetes_payload or None,
                "container_image": self.container_image,
            }
        )
        return payload

    def _redaction_exclusions(self) -> tuple[str, ...]:
        return (
            "typeflux.runtime_placement.platform",
            "typeflux.runtime_placement.kubernetes.namespace",
            "typeflux.runtime_placement.kubernetes.pod_name",
            "typeflux.runtime_placement.kubernetes.pod_uid",
            "typeflux.runtime_placement.kubernetes.node_name",
            "typeflux.runtime_placement.kubernetes.service_account",
            "typeflux.runtime_placement.kubernetes.deployment_name",
            "typeflux.runtime_placement.kubernetes.worker_name",
            "typeflux.runtime_placement.container_image",
        )


class ActivityContextContributor:
    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        typeflux: dict[str, Any] = {}
        if temporal := _temporal_payload(context.invocation_context):
            typeflux["temporal"] = temporal
        if map_payload := _map_payload(context.invocation_context):
            typeflux["map"] = map_payload
        return MetadataContribution(
            activity_metadata={"typeflux": typeflux} if typeflux else {},
            redaction_exclusions=(
                "typeflux.map.*",
                "typeflux.temporal.*",
            ),
        )

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


class LifecycleOperationContributor:
    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        status_payload = _lifecycle_status_payload(context.status)
        operation_payload = _drop_none(
            {
                "operation_type": context.operation_type,
                "operation_name": context.operation_name,
                "workflow_name": context.workflow_name,
                "workflow_id": context.workflow_id,
                "run_id": context.run_id,
                "status": status_payload or None,
                "review_user_decision": context.review_user_decision,
                "review_route_target": context.review_route_target,
                "review_gate": context.review_gate,
                "cancellation_requested": (True if context.cancellation_requested else None),
            }
        )
        tags = {
            "typeflux",
            "typeflux.lifecycle",
            f"typeflux.lifecycle.{context.operation_type}:{context.operation_name}",
        }
        if context.workflow_name:
            tags.add(f"typeflux.workflow:{context.workflow_name}")
        return MetadataContribution(
            operation_metadata={
                "typeflux": {
                    "level": "lifecycle_operation",
                    "lifecycle_operation": operation_payload,
                }
            },
            search_tags=tuple(sorted(tags)),
            redaction_exclusions=(
                "typeflux.level",
                "typeflux.lifecycle_operation.operation_type",
                "typeflux.lifecycle_operation.operation_name",
                "typeflux.lifecycle_operation.workflow_name",
                "typeflux.lifecycle_operation.workflow_id",
                "typeflux.lifecycle_operation.run_id",
                "typeflux.lifecycle_operation.status.*",
                "typeflux.lifecycle_operation.review_user_decision",
                "typeflux.lifecycle_operation.review_route_target",
                "typeflux.lifecycle_operation.cancellation_requested",
            ),
        )


class ExtraTypefluxContributor:
    def __init__(self, extra_typeflux: Mapping[str, Any] | None) -> None:
        self.extra_typeflux = dict(extra_typeflux or {})

    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        yaml_payload = self.extra_typeflux.get("yaml")
        manifest_payload: dict[str, Any] = {}
        if isinstance(yaml_payload, Mapping):
            manifest_payload = _drop_none(
                {
                    "yaml_project": yaml_payload.get("project"),
                    "yaml_name": yaml_payload.get("name"),
                    "map_steps": _map_steps_payload(yaml_payload) or None,
                }
            )
        return MetadataContribution(
            workflow_manifest=manifest_payload,
            workflow_metadata={"typeflux": self.extra_typeflux} if self.extra_typeflux else {},
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        return MetadataContribution()

    def operation(self, context: LifecycleOperationMetadataContext) -> MetadataContribution:
        return MetadataContribution()


def merge_contributions(contributions: Sequence[MetadataContribution]) -> MetadataContribution:
    workflow_manifest: dict[str, Any] = {}
    workflow_metadata: dict[str, Any] = {}
    activity_metadata: dict[str, Any] = {}
    operation_metadata: dict[str, Any] = {}
    search_tags: set[str] = set()
    redaction_exclusions: set[str] = set()
    for contribution in contributions:
        workflow_manifest = _deep_merge(workflow_manifest, contribution.workflow_manifest)
        workflow_metadata = _deep_merge(workflow_metadata, contribution.workflow_metadata)
        activity_metadata = _deep_merge(activity_metadata, contribution.activity_metadata)
        operation_metadata = _deep_merge(operation_metadata, contribution.operation_metadata)
        search_tags.update(contribution.search_tags)
        redaction_exclusions.update(contribution.redaction_exclusions)
    return MetadataContribution(
        workflow_manifest=workflow_manifest,
        workflow_metadata=workflow_metadata,
        activity_metadata=activity_metadata,
        operation_metadata=operation_metadata,
        search_tags=tuple(sorted(search_tags)),
        redaction_exclusions=tuple(sorted(redaction_exclusions)),
    )


def workflow_contribution(
    contributors: Sequence[MetadataContributor],
    context: WorkflowMetadataContext,
) -> MetadataContribution:
    return merge_contributions([contributor.workflow(context) for contributor in contributors])


def activity_contribution(
    contributors: Sequence[MetadataContributor],
    context: ActivityMetadataContext,
) -> MetadataContribution:
    return merge_contributions([contributor.activity(context) for contributor in contributors])


def lifecycle_operation_contribution(
    contributors: Sequence[MetadataContributor],
    context: LifecycleOperationMetadataContext,
) -> MetadataContribution:
    contributions = []
    for contributor in contributors:
        operation = getattr(contributor, "operation", None)
        if operation is not None:
            contributions.append(operation(context))
    return merge_contributions(contributions)


def redaction_exclusions(contributors: Sequence[MetadataContributor]) -> tuple[str, ...]:
    values: set[str] = set()
    empty_workflow = WorkflowMetadataContext(
        workflow_name="",
        workflow_id="",
        task_queue="",
    )
    empty_activity = ActivityMetadataContext(
        manifest=None,
        activity_execution_manifest=None,
        invocation_context=None,
        level="activity",
    )
    empty_operation = LifecycleOperationMetadataContext(
        operation_type="query",
        operation_name="",
        workflow_name=None,
        workflow_id="",
    )
    for contributor in contributors:
        values.update(contributor.workflow(empty_workflow).redaction_exclusions)
        values.update(contributor.activity(empty_activity).redaction_exclusions)
        operation = getattr(contributor, "operation", None)
        if operation is not None:
            values.update(operation(empty_operation).redaction_exclusions)
    return tuple(sorted(values))


def _deep_merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, raw_value in right.items():
        value = _plain_payload(raw_value)
        if key not in merged:
            merged[key] = value
            continue
        existing = merged[key]
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
            continue
        if existing == value:
            continue
        raise MetadataConflictError(f"conflicting metadata contribution for {key!r}")
    return merged


def _plain_payload(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _plain_payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_payload(item) for item in value]
    if isinstance(value, list):
        return [_plain_payload(item) for item in value]
    return value


def _yaml_override_payload(provenance: Any | None) -> dict[str, Any]:
    if provenance is None:
        return {}
    override_paths = tuple(
        path for path in getattr(provenance, "override_paths", ()) if isinstance(path, str)
    )
    if not override_paths:
        return {}
    return _drop_none(
        {
            "source": _string_or_none(getattr(provenance, "source", None)),
            "project_name": _string_or_none(getattr(provenance, "project_name", None)),
            "environment_id": _string_or_none(getattr(provenance, "environment_id", None)),
            "environment_name": _string_or_none(getattr(provenance, "environment_name", None)),
            "workflow_id": _string_or_none(getattr(provenance, "workflow_id", None)),
            "override_paths": list(override_paths),
        }
    )


def _secret_references_payload(references: Sequence[Any]) -> dict[str, Any]:
    payload = []
    for reference in references:
        runtime_path = _string_or_none(getattr(reference, "runtime_path", None))
        source_kind = _string_or_none(getattr(reference, "source_kind", None))
        source_name = _string_or_none(getattr(reference, "source_name", None))
        if runtime_path is None or source_kind not in {"env", "file"} or source_name is None:
            continue
        payload.append(
            {
                "runtime_path": runtime_path,
                "source_kind": source_kind,
                "source_name": source_name,
                "configured": bool(getattr(reference, "configured", False)),
            }
        )
    if not payload:
        return {}
    return {"references": payload}


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _activity_name(activity: Any) -> str:
    name = getattr(activity, "name", None)
    return name if isinstance(name, str) else str(activity)


def _activity_prompt_name(activity: Any) -> str | None:
    prompt_ref = getattr(activity, "prompt_ref", None)
    name = getattr(prompt_ref, "name", None)
    return name if isinstance(name, str) else None


def _prompt_ref_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return value if isinstance(value, str) else None


def _workflow_environment_from_manifest(manifest: Any | None) -> str | None:
    if manifest is None:
        return None
    payload = _plain_payload(manifest)
    if not isinstance(payload, Mapping):
        return None
    code_provenance = payload.get("code_provenance")
    if not isinstance(code_provenance, Mapping):
        return None
    environment = code_provenance.get("environment")
    return environment if isinstance(environment, str) else None


def _temporal_payload(invocation_context: Any | None) -> dict[str, Any]:
    if invocation_context is None:
        return {}
    return _drop_none(
        {
            "namespace": getattr(invocation_context, "temporal_namespace", None),
            "workflow_type": getattr(invocation_context, "temporal_workflow_type", None),
            "workflow_id": getattr(invocation_context, "temporal_workflow_id", None),
            "run_id": getattr(invocation_context, "temporal_run_id", None),
            "task_queue": getattr(invocation_context, "temporal_task_queue", None),
            "activity_type": getattr(invocation_context, "temporal_activity_type", None),
            "activity_id": getattr(invocation_context, "temporal_activity_id", None),
            "activity_attempt": getattr(invocation_context, "temporal_activity_attempt", None),
        }
    )


def _map_payload(invocation_context: Any | None) -> dict[str, Any]:
    if invocation_context is None:
        return {}
    return _drop_none(
        {
            "map_step_id": getattr(invocation_context, "map_step_id", None),
            "map_index": getattr(invocation_context, "map_index", None),
            "map_size": getattr(invocation_context, "map_size", None),
            "map_concurrency": getattr(invocation_context, "map_concurrency", None),
        }
    )


def _lifecycle_status_payload(status: Any | None) -> dict[str, Any]:
    if status is None:
        return {}
    payload = _plain_payload(status)
    if not isinstance(payload, Mapping):
        return {}
    return _drop_none(
        {
            "state": payload.get("state"),
            "current_step": payload.get("current_step"),
            "completed_units": payload.get("completed_units"),
            "total_units": payload.get("total_units"),
            "waiting_checkpoint": payload.get("waiting_checkpoint"),
            "review_user_decision": payload.get("review_user_decision"),
            "review_route_target": payload.get("review_route_target"),
            "cancellation_requested": payload.get("cancellation_requested"),
            "terminal_status": payload.get("terminal_status"),
            "event_count": payload.get("event_count"),
            "events_truncated": payload.get("events_truncated"),
        }
    )


def _map_steps_payload(yaml_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    map_steps = yaml_payload.get("map_steps")
    if not isinstance(map_steps, list):
        return []
    return [dict(step) for step in map_steps if isinstance(step, Mapping)]


def _drop_none(data: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


__all__ = [
    "ActivityContextContributor",
    "ActivityMetadataContext",
    "CoreWorkflowContributor",
    "ExtraTypefluxContributor",
    "LifecycleOperationContributor",
    "LifecycleOperationMetadataContext",
    "MetadataConflictError",
    "MetadataContribution",
    "MetadataContributor",
    "NoOpMetadataContributor",
    "AdmissionContributor",
    "CompensationContributor",
    "PolicyContributor",
    "RiskTierContributor",
    "RuntimePlacementContributor",
    "SecretReferenceContributor",
    "TemporalConnectionContributor",
    "WorkflowMetadataContext",
    "YamlLifecycleContributor",
    "YamlOverrideContributor",
    "YamlWorkflowContributor",
    "activity_contribution",
    "lifecycle_operation_contribution",
    "merge_contributions",
    "redaction_exclusions",
    "workflow_contribution",
]
