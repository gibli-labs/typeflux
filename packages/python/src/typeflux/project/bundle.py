from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from typeflux.core.contracts import AIActivity, YamlWorkflowActivity
from typeflux.manifests._common import schema_identity
from typeflux.observability.semantic import workflow_trace_name
from typeflux.project.environment import (
    ProjectResolvedWorkflow,
    create_workflow_with_subworkflows,
    project_environment_context,
    resolve_project_workflow,
)
from typeflux.project.policy import (
    ComposedProjectPolicy,
    ProjectPolicyError,
    compose_project_policies,
)
from typeflux.project.policy_enforcement import (
    evaluate_workflow_risk_tier,
    select_project_policy_ids_for_workflow,
)
from typeflux.project.spec import (
    ProjectValidationCheck,
    ProjectValidationIssue,
    TypefluxProjectSpec,
)
from typeflux.project.validation import validate_project_bundle
from typeflux.yaml.identity import GENERATOR_VERSION, SPEC_DIGEST_ALGORITHM
from typeflux.yaml.secrets import secret_reference_records
from typeflux.yaml.spec import TypefluxYamlSpec
from typeflux.yaml.workflow import (
    MapCallSpec,
    MapSubworkflowCallSpec,
    ParallelCallSpec,
    SubworkflowCallSpec,
    flatten_call_specs,
    render_when_gate,
)

BUNDLE_VERSION: Literal["1"] = "1"

_logger = logging.getLogger(__name__)

#: Explicit allowlist of environment variables that may surface as external
#: console links. URLs only — never credentials; nothing outside this map is
#: ever read into the bundle.
_EXTERNAL_LINK_ENV_VARS = {
    "temporal_ui": "TEMPORAL_UI_URL",
    "langfuse_project": "LANGFUSE_PROJECT_URL",
}


class BundleProject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    manifest_path: str


class BundleEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    profile_path: str
    env_files: tuple[dict[str, Any], ...] = ()
    profile_variable_names: tuple[str, ...] = ()


class BundleWorkflowIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    path: str
    yaml_project: str
    yaml_name: str
    workflow_name: str
    workflow_type: str
    version_label: str | None = None
    spec_digest: str
    spec_digest_algorithm: str
    generator_version: str
    task_queue: str
    #: The exact Langfuse trace name this workflow's runs are recorded under
    #: (TypefluxWorkflow:<name>), so the console can deep-link precisely.
    observability_trace_name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class BundlePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_policy_ids: tuple[str, ...]
    applied_policy_ids: tuple[str, ...]
    policy_names: tuple[str, ...]
    policy_hash: str


class BundleRiskTierRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    satisfied: bool


class BundleRiskTierCascade(BaseModel):
    """The sub-workflow closure LIFT of this workflow's effective tier (#300 D300-3):
    present only when a higher-tier closure member raises the effective tier above the
    workflow's own declared+floor effective. ``lifted_by`` is the member workflow id;
    ``requirements`` is the parent's macro re-expanded at the lifted tier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lifted_by: str
    effective: str
    requirements: tuple[BundleRiskTierRequirement, ...] = ()


class BundleRiskTier(BaseModel):
    """The workflow's risk-tier posture under the composed policy (#300 slice 2).

    ``effective`` is ALWAYS the tier admission enforces: the workflow's own
    ``max(declared, floor)``, lifted by the sub-workflow closure cascade when a
    higher-tier child raises it. ``floor_source`` records what set it — ``declared`` /
    ``policy_floor`` / ``cascade:<member workflow id>`` — and ``requirements`` is the
    ENFORCED tier's macro expansion with satisfaction (plus a ``require_declared`` entry
    when the policy demands an explicit declaration). The ``cascade`` block explains a
    lift (the lifting member + the lifted tier's re-expansion). Omitted entirely
    (exclude_none) when the composed policy declares no ``risk_tiers`` dimension.
    Redaction-safe: tier names and control names only, never prompt text, reviewer
    notes, or secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    declared: str
    effective: str
    floor: str
    floor_source: str
    requirements: tuple[BundleRiskTierRequirement, ...] = ()
    cascade: BundleRiskTierCascade | None = None


class BundleRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_attempts: int
    initial_interval_seconds: float | None = None
    maximum_interval_seconds: float | None = None
    backoff_coefficient: float | None = None


class BundleActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: Literal["ai", "temporal"] = "ai"
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # AI-only fields stay None for plain Temporal activities: the bundle
    # records them as planned activity names plus schema identity without
    # inventing prompt/provider metadata.
    prompt_ref: dict[str, Any] | None = None
    definition_source: dict[str, Any]
    task_queue: str | None = None
    start_to_close_timeout_seconds: float | None = None
    retry: BundleRetryPolicy | None = None
    validation_retries: int | None = None
    artifact_inputs: tuple[dict[str, Any], ...] = ()
    used_by_steps: tuple[str, ...] = ()


class BundleMapShape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    over: str
    concurrency: int
    collect_output_schema: dict[str, Any]
    collect_field: str


class BundleStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: Literal["activity", "map"]
    activity: str
    map: BundleMapShape | None = None
    effective_start_to_close_timeout_seconds: float
    effective_retry: BundleRetryPolicy


class BundleTopologyNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: Literal["activity", "map", "parallel", "workflow"]
    #: Present exactly when the node calls an activity (kind ``activity``/``map``);
    #: a ``parallel`` block or a ``workflow`` sub-workflow node calls no activity of
    #: its own (#55).
    activity: str | None = None
    #: Present exactly on a ``workflow`` node (a ``workflow:`` step or a
    #: ``map.workflow`` fan-out): the child's MANIFEST workflow id (#55 §3.4/§7).
    workflow: str | None = None


class BundleTopologyEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    target: str
    #: ``branch``: a parallel node to its branch's first step; ``collect``: a
    #: branch's last step back to the parallel node (where the collected value
    #: materializes); ``conditional``: a sequential edge into a ``when``-gated
    #: step (#55).
    kind: Literal["sequential", "review", "branch", "collect", "conditional"]
    #: Review edges carry the user decision that routes execution to the
    #: target step; branch/conditional edges carry the rendered ``when``
    #: predicate; sequential and collect edges have no condition.
    condition: str | None = None


class BundleTopology(BaseModel):
    """Workflow structure as a nodes+edges DAG (read-only projection).

    Vocabulary: activity/map nodes and sequential edges in declared step order
    (also the ``invalid_user_decision: warn`` fall-through path); review edges
    from the review checkpoint to each routed decision target; and the #55
    composition shapes — ``parallel`` nodes with ``branch``/``collect`` edges
    embedding each branch's chain, plus ``conditional`` sequential edges into
    ``when``-gated steps. The Python generator emits the composition shapes in
    #55 slice 2; the DTO accepts them now so both editions share one contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[BundleTopologyNode, ...] = ()
    edges: tuple[BundleTopologyEdge, ...] = ()


class BundleLifecycleReviewTimeout(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seconds: int
    on_timeout: str
    route: str | None = None


class BundleLifecycleReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    after_step: str
    invalid_user_decision: str
    user_decisions: dict[str, str]
    #: Bounded-wait config for the gate (#297); None when the gate waits
    #: indefinitely. Distinguishes a fail-closed gate from an unbounded one.
    timeout: BundleLifecycleReviewTimeout | None = None


class BundleLifecycleGate(BaseModel):
    """One named review gate in the bundle projection (#55 slice 4): the
    ``BundleLifecycleReview`` shape plus its ``id``. Emitted only for multi-gate
    (``lifecycle.gates``) workflows — single-``review`` bundles keep ``review``
    byte-identical and carry no ``gates`` key (exclude_none)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    after_step: str
    invalid_user_decision: str
    user_decisions: dict[str, str]
    timeout: BundleLifecycleReviewTimeout | None = None


class BundleLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    progress: bool
    cancellation: bool
    status_event_limit: int
    review: BundleLifecycleReview | None = None
    #: The named gates (#55 slice 4); None (excluded) for single-``review``/gateless specs.
    gates: tuple[BundleLifecycleGate, ...] | None = None


class BundleLinks(BaseModel):
    """External UI base URLs for the deep-link-out affordances (#250).

    Resolved per environment from the allowlisted variables; the console
    hides any affordance whose base URL is not configured.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Temporal Web UI base URL (``TEMPORAL_UI_URL``).
    temporal_ui: str | None = None
    #: Langfuse *project* base URL (``LANGFUSE_PROJECT_URL``) — Langfuse
    #: URLs are project-scoped and the project id is not derivable here, so
    #: the operator provides the project base.
    langfuse_project: str | None = None


class BundleRuntimeEffective(BaseModel):
    """One effective runtime knob with its source (#265).

    ``source`` is computed by comparing the resolved value to the engine
    default and checking the project defaults layer: ``project_default``
    when the path is set in ``defaults.runtime``; ``engine_default`` when
    the value equals the engine default and is not project-set; otherwise
    ``configured`` (set in workflow YAML, a profile, or the environment).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    #: Defaults to ``None`` so the DTO round-trips its own wire encoding (#642):
    #: both editions serialize with exclude_none, so a null effective value is
    #: legitimately ABSENT on the wire — a required field would reject the
    #: subprocess resolver's (and our own) serialized bundles.
    value: Any = None
    source: Literal["engine_default", "project_default", "configured"]


class BundleCode(BaseModel):
    """Git provenance for the resolving checkout (#252) — never contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str
    branch: str | None = None
    dirty: bool = False
    #: Normalized https remote (origin); absent without a remote.
    repo_url: str | None = None
    #: Repo-relative paths for source deep-links.
    manifest_path: str | None = None
    workflow_path: str | None = None


class BundleErasureCache(BaseModel):
    """The cache-erasure contract this deployment gets (#795). ``declared`` is the spec's
    ``runtime.cache_erasure`` (absent ≡ ``any``); ``behavior`` states the resolution rule
    honestly — the store itself is code-injected, so the bundle names the CONTRACT and the
    erasure RECEIPT records which behavior actually ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    declared: Literal["targeted", "any"]
    behavior: str


class BundleErasure(BaseModel):
    """Erasure posture (#795): present when the workflow declares subject selectors or a
    cache-erasure requirement — the declarations that make erasure behavior load-bearing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_selectors: int
    cache: BundleErasureCache


class BundleValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    issues: tuple[ProjectValidationIssue, ...] = ()
    checks: tuple[ProjectValidationCheck, ...] = ()


class ResolvedWorkflowBundle(BaseModel):
    """Immutable, secret-safe control-plane view of one resolved workflow.

    Composes the existing project resolution, validation, policy, identity,
    and deployment surfaces; it performs no resolution of its own.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_version: Literal["1"] = BUNDLE_VERSION
    project: BundleProject
    environment: BundleEnvironment
    workflow: BundleWorkflowIdentity
    runtime: dict[str, Any]
    policy: BundlePolicy | None = None
    #: Risk-tier posture under the composed policy (#300 slice 2); omitted when the
    #: policy declares no ``risk_tiers`` dimension (exclude_none). Redaction-safe.
    risk_tier: BundleRiskTier | None = None
    activities: tuple[BundleActivity, ...] = ()
    steps: tuple[BundleStep, ...] = ()
    #: Workflow structure as a nodes+edges DAG — a display/monitoring
    #: projection of the steps and review routes, never an authoring surface.
    topology: BundleTopology = Field(default_factory=BundleTopology)
    lifecycle: BundleLifecycle | None = None
    secret_references: tuple[dict[str, Any], ...] = ()
    #: Erasure posture (#795); omitted (exclude_none) when the workflow declares neither
    #: subject selectors nor a cache-erasure requirement.
    erasure: BundleErasure | None = None
    validation: BundleValidation
    deployment_preview: dict[str, Any] | None = None
    deployment_preview_reference: str | None = None
    #: External UI base URLs (Temporal Web, Langfuse project) when the
    #: environment configures them; URLs only, never secrets.
    links: BundleLinks | None = None
    #: Git provenance of the resolving checkout when available.
    code: BundleCode | None = None
    #: Effective runtime knobs (materialized defaults) with per-key source.
    runtime_effective: tuple[BundleRuntimeEffective, ...] = ()
    #: Safe component-profile provenance (#214/#215): kind, id, name,
    #: content hash, project-local source path, and the override paths the
    #: profile set — never secrets or prompt text.
    components: tuple[dict[str, Any], ...] = Field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


_CACHE_ERASURE_BEHAVIOR: dict[str, str] = {
    # Cross-edition EXACT text (the TS control plane emits the byte-identical strings).
    "targeted": (
        "targeted per-subject invalidation (declared REQUIRED: wiring a store without "
        "SubjectErasableCacheStore fails runtime assembly, an erase run on the cache "
        "surface without a wired store fails loudly, and a deployment that wires no "
        "cache store satisfies the requirement vacuously — an empty cache has nothing "
        "to erase)"
    ),
    "any": (
        "wired-store dependent: targeted per-subject invalidation when the injected "
        "CacheStore implements SubjectErasableCacheStore, else the documented "
        "full-cache-flush fallback (the erasure receipt records which behavior ran)"
    ),
}


def _bundle_erasure(spec: Any) -> BundleErasure | None:
    """#795: erasure posture, present only when a spec declaration makes it load-bearing
    (subject selectors or a cache-erasure requirement) — existing bundles are unchanged."""
    selectors = len(spec.workflow.subjects)
    declared = spec.runtime.cache_erasure
    if selectors == 0 and declared is None:
        return None
    effective = declared or "any"
    return BundleErasure(
        subject_selectors=selectors,
        cache=BundleErasureCache(declared=effective, behavior=_CACHE_ERASURE_BEHAVIOR[effective]),
    )


def resolve_workflow_bundle(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    policy_ids: Sequence[str] = (),
    deployment_image: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> ResolvedWorkflowBundle:
    # ``base_env`` (#760): the hermetic interpolation base for the workflow's `${VAR}`
    # spec references — retained on the resolved artifact, so the sub-workflow closure
    # build below resolves children under the same base and the bundle's spec digest is
    # machine-independent. Scope note: the bundle's VALIDATION checks (policy env
    # references, connection probes) still read the process environment by design —
    # they are runtime gates, not artifact bytes.
    resolved = resolve_project_workflow(
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
        base_env=base_env,
    )
    summary = resolved.summary()
    report = validate_project_bundle(
        project,
        environment_id=environment_id,
        workflow_ids=(workflow_id,),
        policy_ids=tuple(policy_ids),
    )
    resolved_checks: tuple[ProjectValidationCheck, ...] = ()
    for workflow_validation in report.resolved_workflows:
        if (
            workflow_validation.workflow_id == workflow_id
            and workflow_validation.environment_id == environment_id
        ):
            resolved_checks = workflow_validation.checks
            break

    workflow_cls, _subworkflows, activities = create_workflow_with_subworkflows(project, resolved)
    with project_environment_context(resolved.application):
        secret_references = secret_reference_records(resolved.spec)
        links = _bundle_links(resolved.spec)

    return ResolvedWorkflowBundle(
        project=BundleProject(
            name=project.name,
            manifest_path=str(project.manifest_path),
        ),
        environment=_bundle_environment(resolved),
        workflow=_bundle_workflow_identity(resolved, workflow_cls),
        runtime={
            "temporal": summary.temporal,
            "registry": summary.registry,
            "provider": _provider_runtime(resolved.spec, summary.provider),
            "observability": summary.observability,
        },
        policy=_bundle_policy(project, workflow_id, environment_id, policy_ids),
        risk_tier=_bundle_risk_tier(
            project, resolved, environment_id=environment_id, policy_ids=policy_ids
        ),
        components=tuple(component.to_dict() for component in resolved.components),
        activities=_bundle_activities(activities, workflow_cls),
        steps=_bundle_steps(workflow_cls),
        topology=_bundle_topology(workflow_cls, resolved.spec),
        lifecycle=_bundle_lifecycle(resolved.spec),
        links=links,
        code=_bundle_code(project, resolved),
        runtime_effective=_bundle_runtime_effective(resolved.spec, project),
        secret_references=tuple(
            {
                "runtime_path": record.runtime_path,
                "source_kind": record.source_kind,
                "source_name": record.source_name,
                "configured": record.configured,
            }
            for record in secret_references
        ),
        erasure=_bundle_erasure(resolved.spec),
        validation=BundleValidation(
            ok=report.ok,
            issues=report.issues,
            checks=resolved_checks,
        ),
        deployment_preview=_deployment_preview(
            project,
            spec=resolved.spec,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=policy_ids,
            deployment_image=deployment_image,
        ),
        deployment_preview_reference=(
            None
            if deployment_image is not None
            else (
                "uv run typeflux-project deploy "
                f"{project.manifest_path} --environment {environment_id} "
                f"--workflow {workflow_id} --image <digest-pinned-image>"
            )
        ),
    )


def _bundle_environment(resolved: ProjectResolvedWorkflow) -> BundleEnvironment:
    application = resolved.application.safe_summary()
    return BundleEnvironment(
        id=resolved.environment_id,
        name=resolved.environment.name,
        profile_path=str(resolved.environment.profile_path),
        env_files=tuple(application.get("env_files", ())),
        profile_variable_names=tuple(application.get("profile_variable_names", ())),
    )


def _bundle_workflow_identity(
    resolved: ProjectResolvedWorkflow,
    workflow_cls: type,
) -> BundleWorkflowIdentity:
    spec = resolved.spec
    return BundleWorkflowIdentity(
        id=resolved.workflow_id,
        path=str(resolved.workflow_path),
        yaml_project=spec.project,
        yaml_name=spec.name,
        workflow_name=spec.workflow.name,
        workflow_type=getattr(workflow_cls, "__typeflux_workflow_type__"),
        version_label=getattr(workflow_cls, "__typeflux_workflow_version_label__"),
        spec_digest=getattr(workflow_cls, "__typeflux_spec_digest__"),
        spec_digest_algorithm=SPEC_DIGEST_ALGORITHM,
        generator_version=GENERATOR_VERSION,
        task_queue=spec.task_queue,
        observability_trace_name=workflow_trace_name(spec.workflow.name),
        input_schema=_workflow_io_schema(workflow_cls, "input_value"),
        output_schema=_workflow_io_schema(workflow_cls, "return"),
    )


def _workflow_io_schema(workflow_cls: type, annotation: str) -> dict[str, Any]:
    run = getattr(workflow_cls, "run")
    model = run.__annotations__[annotation]
    identity = schema_identity(model).to_dict()
    # Carry the JSON Schema so a console can render a typed input form and
    # validate against it (#269). Additive; no prompt text or secrets.
    if hasattr(model, "model_json_schema"):
        identity["json_schema"] = model.model_json_schema()
    return identity


def _provider_runtime(spec: TypefluxYamlSpec, provider_summary: dict[str, Any]) -> dict[str, Any]:
    provider = spec.runtime.provider
    payload = dict(provider_summary)
    payload["allow_prompt_model_override"] = provider.allow_prompt_model_override
    params = provider.provider_params().to_dict()
    if params:
        payload["params"] = params
    return payload


def _bundle_policy(
    project: TypefluxProjectSpec,
    workflow_id: str,
    environment_id: str,
    policy_ids: Sequence[str],
) -> BundlePolicy | None:
    selected = select_project_policy_ids_for_workflow(
        project,
        environment_id=environment_id,
        workflow_id=workflow_id,
        explicit_policy_ids=tuple(policy_ids),
    )
    if not selected:
        return None
    try:
        policy: ComposedProjectPolicy = compose_project_policies(project, selected)
    except ProjectPolicyError:
        # Composition conflicts surface as failed validation checks; the
        # bundle stays inspectable with the selection recorded.
        return BundlePolicy(
            selected_policy_ids=selected,
            applied_policy_ids=(),
            policy_names=(),
            policy_hash="",
        )
    return BundlePolicy(
        selected_policy_ids=policy.selected_policy_ids,
        applied_policy_ids=policy.applied_policy_ids,
        policy_names=policy.policy_names,
        policy_hash=policy.policy_hash,
    )


def _bundle_risk_tier(
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    *,
    environment_id: str,
    policy_ids: Sequence[str],
) -> BundleRiskTier | None:
    # Same selection as `_bundle_policy` (auto-bound + explicit), so the risk-tier surface
    # tracks the policy the workflow is actually governed by — even without explicit
    # policy_ids in the request. Derived from the SHARED `evaluate_workflow_risk_tier`, so
    # the bundle shows the SAME posture the admission checks compute (no second path).
    selected = select_project_policy_ids_for_workflow(
        project,
        environment_id=environment_id,
        workflow_id=resolved.workflow_id,
        explicit_policy_ids=tuple(policy_ids),
    )
    if not selected:
        return None
    try:
        policy = compose_project_policies(project, selected)
    except ProjectPolicyError:
        # A composition conflict surfaces as a failed validation check (like `_bundle_policy`);
        # there is no composed policy to derive a tier posture from.
        return None
    posture = evaluate_workflow_risk_tier(
        project, resolved=resolved, policy=policy, environment_id=environment_id
    )
    if posture is None:
        return None

    def _requirements(
        evaluation: Any,
    ) -> tuple[BundleRiskTierRequirement, ...]:
        return tuple(
            BundleRiskTierRequirement(name=req.name, satisfied=req.satisfied)
            for req in evaluation.requirements
        )

    # `effective` is ALWAYS what admission enforces: a cascade lift promotes the
    # top-level posture (effective / floor_source / requirements) to the LIFTED
    # evaluation; the `cascade` block explains it. `require_declared` is a real
    # admission requirement too, so it surfaces as a requirement entry (first — it
    # is checked before the macro expansion) instead of silently reading satisfied.
    enforced = posture.cascade.evaluation if posture.cascade is not None else posture.base
    requirements: list[BundleRiskTierRequirement] = []
    if enforced.require_declared:
        requirements.append(
            BundleRiskTierRequirement(name="require_declared", satisfied=not enforced.undeclared)
        )
    requirements.extend(_requirements(enforced))
    return BundleRiskTier(
        declared=posture.base.declared,
        effective=enforced.effective,
        floor=posture.base.floor,
        floor_source=(
            f"cascade:{posture.cascade.lifted_by}"
            if posture.cascade is not None
            else posture.base.floor_source
        ),
        requirements=tuple(requirements),
        cascade=(
            BundleRiskTierCascade(
                lifted_by=posture.cascade.lifted_by,
                effective=posture.cascade.evaluation.effective,
                requirements=_requirements(posture.cascade.evaluation),
            )
            if posture.cascade is not None
            else None
        ),
    )


def _bundle_activities(
    activities: dict[str, YamlWorkflowActivity],
    workflow_cls: type,
) -> tuple[BundleActivity, ...]:
    # Leaf calls, depth-first (#55): nested branch steps use activities like any other.
    calls = flatten_call_specs(getattr(workflow_cls, "__typeflux_activity_calls__"))
    used_by: dict[str, list[str]] = {}
    for call in calls:
        used_by.setdefault(call.activity_name, []).append(call.step_id)
    descriptors = []
    for name in sorted(activities):
        activity = activities[name]
        is_ai = isinstance(activity, AIActivity)
        descriptors.append(
            BundleActivity(
                name=name,
                kind="ai" if is_ai else "temporal",
                input_schema=schema_identity(activity.input_type).to_dict(),
                output_schema=schema_identity(activity.output_type).to_dict(),
                prompt_ref=activity.prompt_ref.to_dict() if is_ai else None,
                definition_source=activity.definition_source.to_dict(),
                task_queue=activity.task_queue,
                start_to_close_timeout_seconds=(
                    activity.start_to_close_timeout.total_seconds()
                    if activity.start_to_close_timeout is not None
                    else None
                ),
                retry=_bundle_retry_policy(activity.retry_policy),
                validation_retries=activity.validation_retries if is_ai else None,
                artifact_inputs=(
                    tuple(item.safe_definition() for item in activity.artifact_inputs)
                    if is_ai
                    else ()
                ),
                used_by_steps=tuple(used_by.get(name, ())),
            )
        )
    return tuple(descriptors)


def _bundle_steps(workflow_cls: type) -> tuple[BundleStep, ...]:
    # LEAF calls, depth-first (#55): `steps` carries per-activity effective options,
    # so a parallel node — which calls no activity — contributes its branches'
    # steps, not itself; the block structure lives in the topology projection.
    steps = []
    for call in flatten_call_specs(getattr(workflow_cls, "__typeflux_activity_calls__")):
        is_map = isinstance(call, MapCallSpec)
        steps.append(
            BundleStep(
                id=call.step_id,
                kind="map" if is_map else "activity",
                activity=call.activity_name,
                map=(
                    BundleMapShape(
                        over=call.over,
                        concurrency=call.concurrency,
                        collect_output_schema=schema_identity(call.collect.output_type).to_dict(),
                        collect_field=call.collect.field,
                    )
                    if is_map
                    else None
                ),
                effective_start_to_close_timeout_seconds=(
                    call.start_to_close_timeout.total_seconds()
                ),
                effective_retry=_bundle_retry_policy(call.retry_policy)
                or BundleRetryPolicy(maximum_attempts=0),
            )
        )
    return tuple(steps)


def _git(project_dir: Any, *args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed args, no shell.
            ["git", *args],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _normalize_remote(url: str) -> str | None:
    if url.startswith("git@") and ":" in url:
        host, _, path = url[len("git@") :].partition(":")
        url = f"https://{host}/{path}"
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if not url.startswith(("http://", "https://")):
        return None
    # Strip userinfo: tokens embedded in https remotes must never reach the
    # bundle JSON or console links.
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    return f"{parsed.scheme}://{host}{parsed.path}"


_MISSING = object()


def _path_get(data: Any, path: str) -> Any:
    node = data
    for segment in path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return _MISSING
        node = node[segment]
    return node


def _bundle_runtime_effective(
    spec: TypefluxYamlSpec, project: TypefluxProjectSpec
) -> tuple[BundleRuntimeEffective, ...]:
    from typeflux.execution.controls import ProviderRetryPolicy
    from typeflux.yaml.spec import DEFAULT_PROVIDER_MODELS

    defaults = project.defaults.runtime or {}
    entries: list[tuple[str, Any, Any]] = []  # (path, effective, engine_default)

    provider = spec.runtime.provider
    entries.append(("provider.model", provider.model, DEFAULT_PROVIDER_MODELS.get(provider.type)))

    engine_retry = ProviderRetryPolicy()
    retry = spec.runtime.provider_retry
    for field in (
        "max_attempts",
        "initial_backoff_seconds",
        "max_backoff_seconds",
        "backoff_multiplier",
        "jitter_ratio",
        "retry_rate_limits",
    ):
        engine_value = getattr(engine_retry, field)
        effective = getattr(retry, field) if retry is not None else engine_value
        entries.append((f"provider_retry.{field}", effective, engine_value))

    if spec.runtime.registry.type in ("langfuse", "langsmith"):
        label = getattr(spec.runtime.registry, "label", None) or "production"
        entries.append(("registry.label", label, "production"))

    lifecycle = spec.workflow.lifecycle
    if lifecycle is not None and lifecycle.enabled:
        entries.append(
            ("lifecycle.history.status_event_limit", lifecycle.history.status_event_limit, 50)
        )

    result = []
    for path, effective, engine_default in entries:
        project_value = _path_get(defaults, path)
        # project_default only when the effective value actually came from
        # the project defaults — a higher-precedence layer (workflow YAML,
        # profile, environment) that overrode it shows as configured.
        if project_value is not _MISSING and effective == project_value:
            source: str = "project_default"
        elif effective == engine_default:
            source = "engine_default"
        else:
            source = "configured"
        result.append(BundleRuntimeEffective(path=path, value=effective, source=source))
    return tuple(result)


def _bundle_code(project: TypefluxProjectSpec, resolved: ProjectResolvedWorkflow) -> Any:
    project_dir = project.project_dir
    sha = _git(project_dir, "rev-parse", "HEAD")
    if not sha:
        return None
    branch = _git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(project_dir, "status", "--porcelain")
    remote = _git(project_dir, "remote", "get-url", "origin")
    top = _git(project_dir, "rev-parse", "--show-toplevel")

    def _relative(path: Any) -> str | None:
        if not top:
            return None
        try:
            return Path(path).resolve().relative_to(Path(top).resolve()).as_posix()
        except ValueError:
            return None

    return BundleCode(
        sha=sha,
        branch=branch if branch and branch != "HEAD" else None,
        dirty=bool(status),
        repo_url=_normalize_remote(remote) if remote else None,
        manifest_path=_relative(project.manifest_path),
        workflow_path=_relative(resolved.workflow_path),
    )


def _bundle_links(spec: TypefluxYamlSpec) -> BundleLinks | None:
    values: dict[str, str] = {}
    for field, variable in _EXTERNAL_LINK_ENV_VARS.items():
        raw = os.environ.get(variable, "").strip()
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            _logger.warning("ignoring %s: not an http(s) URL; external links stay hidden", variable)
            continue
        # The bundle is secret-safe: a URL carrying userinfo (https://token@host) would
        # publish the credential through the control-plane API — reject it whole (fail
        # closed, like the scheme check; same behavior/message as the TS control plane).
        if parsed.username or parsed.password:
            _logger.warning(
                "ignoring %s: URL embeds credentials; external links stay hidden", variable
            )
            continue
        values[field] = raw
    # The YAML-dictated registry is the source of truth for the Langfuse
    # project URL: when the registry is Langfuse, derive {host}/project/{id}
    # through its own configured client; the env var stays as the fallback
    # for observer-only setups.
    if spec.runtime.registry.type == "langfuse":
        derived = _langfuse_project_url(getattr(spec.runtime.registry, "host", None))
        explicit = values.get("langfuse_project")
        if derived:
            if explicit and urlparse(explicit).netloc != urlparse(derived).netloc:
                _logger.warning(
                    "LANGFUSE_PROJECT_URL host %s does not match the configured "
                    "registry %s; using the registry-derived project URL",
                    urlparse(explicit).netloc,
                    urlparse(derived).netloc,
                )
            values["langfuse_project"] = derived
    # The Temporal Web base is per-environment: derive it from the resolved
    # temporal address so a cloud environment links to Temporal Cloud and a
    # localhost one links to the local UI. An explicit TEMPORAL_UI_URL wins
    # (self-hosted UIs); the execution path is identical for both.
    if "temporal_ui" not in values:
        derived_ui = _temporal_ui_url(spec.runtime.temporal.address)
        if derived_ui:
            values["temporal_ui"] = derived_ui
    if not values:
        return None
    return BundleLinks(**values)


def _temporal_ui_url(address: str | None) -> str | None:
    if not address:
        return None
    host = address.split(":", 1)[0].strip().lower()
    if "tmprl.cloud" in host:
        return "https://cloud.temporal.io"
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return "http://localhost:8233"
    return None


#: Project URLs are stable per host+credentials; cache the lookup.
_LANGFUSE_PROJECT_URL_CACHE: dict[str, str | None] = {}


def _langfuse_project_url(configured_host: str | None) -> str | None:
    host = (
        configured_host
        or os.getenv("LANGFUSE_HOST")
        or os.getenv("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    ).rstrip("/")
    if host in _LANGFUSE_PROJECT_URL_CACHE:
        return _LANGFUSE_PROJECT_URL_CACHE[host]
    url: str | None = None
    try:
        from langfuse import Langfuse

        client = Langfuse(host=host)
        projects = client.api.projects.get()
        data = getattr(projects, "data", None) or []
        project_id = getattr(data[0], "id", None) if data else None
        if project_id:
            url = f"{host}/project/{project_id}"
    except Exception as exc:  # noqa: BLE001 - links degrade to absent.
        # Failures are not cached: a transient registry error at first
        # resolve must not suppress derived links until restart.
        _logger.warning("could not derive the Langfuse project URL: %s", exc)
        return None
    _LANGFUSE_PROJECT_URL_CACHE[host] = url
    return url


def _bundle_topology(workflow_cls: type, spec: TypefluxYamlSpec) -> BundleTopology:
    """Project the workflow into its nodes+edges DAG (#55; TS ``buildBundleTopology``).

    Emission order is NORMATIVE for cross-edition fixture parity (byte-identical
    topology for the same spec): depth-first — each step's node, then (for a
    parallel node, per branch in declared order) the branch edge, the branch's
    nodes and internal edges, and its collect edge; consecutive-step edges
    interleave in walk order; review edges last. A V1 linear spec projects
    byte-identically to the pre-composition code.
    """
    calls = getattr(workflow_cls, "__typeflux_activity_calls__")
    nodes: list[BundleTopologyNode] = []
    edges: list[BundleTopologyEdge] = []

    def project_sequence(sequence: Sequence[Any]) -> tuple[str | None, str | None]:
        previous: str | None = None
        first: str | None = None
        for call in sequence:
            if previous is not None:
                # A sequential edge INTO a when-gated step is `conditional`,
                # carrying the rendered predicate (#55 §7). A gated FIRST step of a
                # sequence has no incoming edge to carry its condition — the gate
                # still shows in lifecycle provenance (step_skipped).
                if call.when is not None:
                    edges.append(
                        BundleTopologyEdge(
                            source=previous,
                            target=call.step_id,
                            kind="conditional",
                            condition=render_when_gate(call.when),
                        )
                    )
                else:
                    edges.append(
                        BundleTopologyEdge(source=previous, target=call.step_id, kind="sequential")
                    )
            if isinstance(call, ParallelCallSpec):
                nodes.append(BundleTopologyNode(id=call.step_id, kind="parallel"))
                for branch in call.branches:
                    # Branch edge first, then the branch's own nodes/edges, then its
                    # collect edge (the normative emission order — see the docstring).
                    first_id = branch.calls[0].step_id if branch.calls else None
                    if first_id is not None:
                        edges.append(
                            BundleTopologyEdge(
                                source=call.step_id,
                                target=first_id,
                                kind="branch",
                                condition=(
                                    render_when_gate(branch.when)
                                    if branch.when is not None
                                    else None
                                ),
                            )
                        )
                    _first, last = project_sequence(branch.calls)
                    if last is not None:
                        # The collected value materializes AT the parallel node
                        # (`context[block.id]`).
                        edges.append(
                            BundleTopologyEdge(source=last, target=call.step_id, kind="collect")
                        )
            elif isinstance(call, (SubworkflowCallSpec, MapSubworkflowCallSpec)):
                # A sub-workflow node (plain `workflow:` step OR `map.workflow` fan-out)
                # projects to `{id, kind:"workflow", workflow:<child MANIFEST id>}` —
                # byte-identical to the TS edition (#55 §7); it calls no PARENT activity,
                # so `activity` stays absent.
                nodes.append(
                    BundleTopologyNode(
                        id=call.step_id,
                        kind="workflow",
                        workflow=call.child_workflow_id,
                    )
                )
            else:
                nodes.append(
                    BundleTopologyNode(
                        id=call.step_id,
                        kind="map" if isinstance(call, MapCallSpec) else "activity",
                        activity=call.activity_name,
                    )
                )
            if first is None:
                first = call.step_id
            previous = call.step_id
        return first, previous

    project_sequence(calls)
    lifecycle = spec.workflow.lifecycle
    # Review/route edges per GATE (#55 slice 4): the single `review` and the named `gates`
    # normalize through resolved_gates(), so a multi-gate workflow projects every gate's
    # decision + timeout routes (single-review output is unchanged — same edge fields).
    gates = lifecycle.resolved_gates() if lifecycle is not None and lifecycle.enabled else []
    for gate in gates:
        edges.extend(
            BundleTopologyEdge(
                source=gate.after_step,
                target=route.route,
                kind="review",
                condition=decision,
            )
            for decision, route in sorted(gate.user_decisions.items())
        )
        # The timeout's route action is a real edge too, so a step reachable only
        # on timeout isn't shown as orphaned in the topology (#297 review).
        timeout = gate.timeout
        if timeout is not None and timeout.on_timeout == "route" and timeout.route is not None:
            edges.append(
                BundleTopologyEdge(
                    source=gate.after_step,
                    target=timeout.route,
                    kind="review",
                    condition="timeout",
                )
            )
    return BundleTopology(nodes=nodes, edges=tuple(edges))


def _bundle_retry_policy(retry_policy: Any) -> BundleRetryPolicy | None:
    if retry_policy is None:
        return None
    return BundleRetryPolicy(
        maximum_attempts=retry_policy.maximum_attempts,
        initial_interval_seconds=_interval_seconds(retry_policy.initial_interval),
        maximum_interval_seconds=_interval_seconds(retry_policy.maximum_interval),
        backoff_coefficient=retry_policy.backoff_coefficient,
    )


def _interval_seconds(value: timedelta | None) -> float | None:
    return value.total_seconds() if isinstance(value, timedelta) else None


def _bundle_lifecycle(spec: TypefluxYamlSpec) -> BundleLifecycle | None:
    lifecycle = spec.workflow.lifecycle
    if lifecycle is None or not lifecycle.enabled:
        return None
    review = lifecycle.review

    def _timeout(gate: Any) -> BundleLifecycleReviewTimeout | None:
        if gate.timeout is None:
            return None
        return BundleLifecycleReviewTimeout(
            seconds=gate.timeout.seconds,
            on_timeout=gate.timeout.on_timeout,
            route=gate.timeout.route,
        )

    return BundleLifecycle(
        enabled=lifecycle.enabled,
        progress=lifecycle.progress,
        cancellation=lifecycle.cancellation,
        status_event_limit=lifecycle.history.status_event_limit,
        review=(
            BundleLifecycleReview(
                after_step=review.after_step,
                invalid_user_decision=review.invalid_user_decision,
                user_decisions={
                    decision: route.route
                    for decision, route in sorted(review.user_decisions.items())
                },
                timeout=_timeout(review),
            )
            if review is not None
            else None
        ),
        # Additive: the named gates (#55 slice 4). None for single-review specs, so their
        # bundle output stays byte-identical under exclude_none.
        gates=(
            tuple(
                BundleLifecycleGate(
                    id=gate.id,
                    after_step=gate.after_step,
                    invalid_user_decision=gate.invalid_user_decision,
                    user_decisions={
                        decision: route.route
                        for decision, route in sorted(gate.user_decisions.items())
                    },
                    timeout=_timeout(gate),
                )
                for gate in lifecycle.gates
            )
            if lifecycle.gates is not None
            else None
        ),
    )


def _deployment_preview_notices(spec: TypefluxYamlSpec) -> list[str]:
    # Sub-workflow visibility notice (#55 §6 mitigation b) — the same condition and
    # EXACT text as the validation layer's `subworkflow_visibility` check: a frozen
    # workflow.version on a sub-workflow parent without a configured search attribute
    # degrades the frozen-version scan at deploy time.
    from typeflux.project.validation import SUBWORKFLOW_VISIBILITY_NOTICE
    from typeflux.yaml.workflow import collect_subworkflow_references

    if (
        collect_subworkflow_references(spec)
        and spec.workflow.version is not None
        and spec.runtime.temporal.workflow_search_attribute is None
    ):
        return [SUBWORKFLOW_VISIBILITY_NOTICE]
    return []


def _deployment_preview(
    project: TypefluxProjectSpec,
    *,
    spec: TypefluxYamlSpec,
    workflow_id: str,
    environment_id: str,
    policy_ids: Sequence[str],
    deployment_image: str | None,
) -> dict[str, Any] | None:
    if deployment_image is None:
        return None
    from typeflux.project.deployment import (
        ProjectDeploymentError,
        build_project_deployment_plan,
    )

    notices = _deployment_preview_notices(spec)
    try:
        plan = build_project_deployment_plan(
            project,
            environment_id=environment_id,
            workflow_ids=(workflow_id,),
            policy_ids=tuple(policy_ids),
            image=deployment_image,
            allow_mutable_image=True,
        )
    except ProjectDeploymentError as exc:
        # The preview is advisory: a failing plan must not make the bundle
        # unresolvable. Admission failures are already in the validation
        # section; the preview records why generation failed. Advisory notices
        # ride along either way — they describe the SPEC, not the plan.
        return {"error": str(exc), **({"notices": notices} if notices else {})}
    workers = []
    for worker in plan.workers:
        workers.append(
            {
                "name": worker.name,
                "workflow_id": worker.workflow_id,
                "workflow_name": worker.workflow_name,
                "task_queue": worker.task_queue,
                "config_map_name": worker.config_map_name,
                # Config values stay out of the bundle; the deploy command is
                # the authoritative artifact generator.
                "config_map_keys": sorted(worker.config_map),
                "secret_name": worker.secret_name,
                "secret_env": [ref.model_dump(mode="json") for ref in worker.secret_env],
                "secret_files": [ref.model_dump(mode="json") for ref in worker.secret_files],
                "policy": worker.policy.model_dump(mode="json"),
            }
        )
    return {
        "target": plan.target,
        "environment_id": plan.environment_id,
        "image": plan.image,
        "image_digest_pinned": plan.image_digest_pinned,
        "workers": workers,
        **({"notices": notices} if notices else {}),
    }


__all__ = [
    "BUNDLE_VERSION",
    "ResolvedWorkflowBundle",
    "resolve_workflow_bundle",
]
