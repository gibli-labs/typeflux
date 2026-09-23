from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from typeflux.core.errors import TypefluxError
from typeflux.manifests._common import canonical_json
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.loader import strict_safe_load
from typeflux.yaml.spec import MAX_PARALLEL_NESTING_DEPTH, RISK_TIER_ORDER, RiskTier

ArtifactSourcePolicy = Literal["local_path", "url", "object_uri", "provider_file"]
ObservabilityBackendPolicy = Literal["none", "langfuse"]
ReviewInvalidDecisionPolicy = Literal["warn", "fail"]

_ALLOWLIST_FIELD_NAMES = {
    "allowed_addresses",
    "allowed_backends",
    "allowed_hosts",
    "allowed_media_types",
    "allowed_module_roots",
    "allowed_namespaces",
    "allowed_regions",
    "allowed_sources",
    "base_urls",
    "models",
}


class ProjectPolicyError(TypefluxError, ValueError):
    """Raised when project policy loading or composition fails."""


class PolicyProviderAllowanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] | None = None
    base_urls: list[str] | None = None

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="providers.allowed.<provider>.models")

    @field_validator("base_urls")
    @classmethod
    def _validate_base_urls(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="providers.allowed.<provider>.base_urls")


class PolicyProvidersSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: dict[str, PolicyProviderAllowanceSpec] | None = None

    @field_validator("allowed")
    @classmethod
    def _validate_allowed(
        cls,
        value: dict[str, PolicyProviderAllowanceSpec] | None,
    ) -> dict[str, PolicyProviderAllowanceSpec] | None:
        if value is None:
            return None
        for provider_name in value:
            _validate_non_empty_string(provider_name, field="provider name")
        return value


class PolicyRedactionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool | None = None
    preserve_typeflux_metadata: bool | None = None
    #: Named custom rules (#188 D188-4) a workflow's redaction config MUST declare.
    #: A plain (non-allowlist) list, so composed policies UNION the required names
    #: (`_merge_policy_value` dedupes the concatenation); missing names fail admission.
    require_custom_rules: list[str] | None = None

    @field_validator("require_custom_rules")
    @classmethod
    def _validate_require_custom_rules(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="observability.redaction.require_custom_rules")


class PolicyObservabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool | None = None
    allowed_backends: list[ObservabilityBackendPolicy] | None = None
    redaction: PolicyRedactionSpec = Field(default_factory=PolicyRedactionSpec)


class PolicyTemporalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_addresses: list[str] | None = None
    allowed_namespaces: list[str] | None = None
    allowed_regions: list[str] | None = None
    address_regions: dict[str, str] | None = None
    require_tls: bool | None = None
    require_api_key: bool | None = None
    #: Reject a workflow whose ``runtime.temporal.payload_codec`` is absent (#188 D188-2).
    #: A PII codec must never silently degrade to plaintext, so this is the fail-closed
    #: sibling of ``require_tls``/``require_api_key``; OR-merges across composed policies.
    require_payload_codec: bool | None = None

    @field_validator("allowed_addresses", "allowed_namespaces", "allowed_regions")
    @classmethod
    def _validate_allowed_strings(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="runtime.temporal allow-list")

    @field_validator("address_regions")
    @classmethod
    def _validate_address_regions(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        for address, region in value.items():
            _validate_non_empty_string(address, field="runtime.temporal.address_regions address")
            _validate_non_empty_string(region, field="runtime.temporal.address_regions region")
        return value


class PolicyProviderRetrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int | None = None
    initial_backoff_seconds: float | None = None
    max_backoff_seconds: float | None = None
    backoff_multiplier: float | None = None
    retry_rate_limits: bool | None = None
    retry_transient_errors: bool | None = None

    @field_validator("max_attempts")
    @classmethod
    def _validate_max_attempts(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("runtime.provider_retry.max_attempts must be >= 1")
        return value

    @field_validator("initial_backoff_seconds", "max_backoff_seconds")
    @classmethod
    def _validate_backoff(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("runtime.provider_retry backoff values must be >= 0")
        return value

    @field_validator("backoff_multiplier")
    @classmethod
    def _validate_backoff_multiplier(cls, value: float | None) -> float | None:
        if value is not None and value < 1:
            raise ValueError("runtime.provider_retry.backoff_multiplier must be >= 1")
        return value


class PolicyProviderCallLimitsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int | None = None
    min_interval_seconds: float | None = None

    @field_validator("max_concurrent")
    @classmethod
    def _validate_max_concurrent(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("runtime.provider_limits max_concurrent must be >= 1")
        return value

    @field_validator("min_interval_seconds")
    @classmethod
    def _validate_min_interval(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("runtime.provider_limits min_interval_seconds must be >= 0")
        return value


class PolicyProviderRateLimitProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limits: PolicyProviderCallLimitsSpec | None = None
    models: dict[str, PolicyProviderCallLimitsSpec] = Field(default_factory=dict)

    @field_validator("models")
    @classmethod
    def _validate_models(
        cls,
        value: dict[str, PolicyProviderCallLimitsSpec],
    ) -> dict[str, PolicyProviderCallLimitsSpec]:
        for model_name in value:
            _validate_non_empty_string(model_name, field="provider limit model name")
        return value


class PolicyProviderLimitsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: PolicyProviderCallLimitsSpec | None = None
    providers: dict[str, PolicyProviderRateLimitProviderSpec] = Field(default_factory=dict)

    @field_validator("providers")
    @classmethod
    def _validate_providers(
        cls,
        value: dict[str, PolicyProviderRateLimitProviderSpec],
    ) -> dict[str, PolicyProviderRateLimitProviderSpec]:
        for provider_name in value:
            _validate_non_empty_string(provider_name, field="provider limit provider name")
        return value


class PolicyRegistrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] | None = None

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_allowed_hosts(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="runtime.registry.allowed_hosts")


class PolicyRuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temporal: PolicyTemporalSpec = Field(default_factory=PolicyTemporalSpec)
    registry: PolicyRegistrySpec = Field(default_factory=PolicyRegistrySpec)
    provider_retry: PolicyProviderRetrySpec | None = None
    provider_limits: PolicyProviderLimitsSpec | None = None


class PolicyArtifactsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_sources: list[ArtifactSourcePolicy] | None = None
    allowed_media_types: list[str] | None = None
    max_bytes: int | None = None

    @field_validator("allowed_media_types")
    @classmethod
    def _validate_allowed_media_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="artifacts.allowed_media_types")

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("artifacts.max_bytes must be >= 0")
        return value


class PolicyReviewSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_review_routes: bool | None = None
    invalid_user_decision: ReviewInvalidDecisionPolicy | None = None


class PolicySemanticsSpec(BaseModel):
    """Policy-governed semantic moderation of activity output (#158 PR2).

    ``required`` (and ``require_block``, which implies it) mandates that governed
    activities declare a moderator; ``require_block`` further forbids ``flag``-only
    moderation (regulated workflows must fail-closed). ``categories``/
    ``score_threshold`` apply the org's bar to the moderator's *reported*
    categories/score at runtime, independent of the moderator's own ``flagged``
    decision — so a stricter threshold blocks output a lenient moderator cleared.
    Policy can tighten, never loosen, an activity's own ``on_violation``.

    ``score_threshold`` is omitted to disable; a value of ``0.0`` blocks any
    scored verdict (``max_score >= 0.0``).
    """

    model_config = ConfigDict(extra="forbid")

    required: bool | None = None
    require_block: bool | None = None
    categories: list[str] | None = None
    score_threshold: float | None = None

    @field_validator("categories")
    @classmethod
    def _validate_categories(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="semantics.categories")

    @field_validator("score_threshold")
    @classmethod
    def _validate_score_threshold(cls, value: float | None) -> float | None:
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError("semantics.score_threshold must be between 0.0 and 1.0")
        return value


class PolicySecretsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_secret_references: bool | None = None


class PolicyImportsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_absolute_activity_modules: bool | None = None
    allow_provider_class: bool | None = None
    allow_registry_class: bool | None = None
    allow_observability_class: bool | None = None
    allow_moderator_callable: bool | None = None
    allowed_module_roots: list[str] | None = None

    @field_validator("allowed_module_roots")
    @classmethod
    def _validate_allowed_module_roots(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(value, field="imports.allowed_module_roots")


#: The >= 1 contract on every ceiling: 0 is never a silent no-op, and "forbid" has
#: dedicated spellings (see the message).
_CEILING_MIN_MESSAGE = (
    "composition ceilings must be >= 1 (a zero ceiling is not expressible: use "
    "allow_map_over_workflow: false to forbid map-over-workflow fan-out, or omit the "
    "workflow from validation targets to forbid it entirely)"
)


class PolicyCompositionSpec(BaseModel):
    """Composition ceilings on a workflow's graph shape (#298 Phase A, the #55 §9
    handoff). All checks are pure tree-walks over the parsed spec — deterministic,
    no I/O, no module imports — so they land at every admission point through the
    same pipeline as every other policy dimension.

    Scope of each knob: ``max_steps`` (flattened step count — every id-bearing node,
    parallel branch steps included), ``max_parallel_width`` (branch count of any
    single ``parallel`` block), and ``max_parallel_nesting`` are PER WORKFLOW —
    evaluated against each sub-workflow closure member individually under the
    parent's composed policy. ``max_total_steps`` (the flattened sum over the WHOLE
    closure — parent plus every transitively referenced child, so a program split
    into many small children cannot evade the bound) and ``max_subworkflow_depth``
    (reference-tree depth; the parent is depth 0) are TREE-WIDE, enforced by the
    closure walk. ``max_parallel_nesting`` may only TIGHTEN the in-spec hard ceiling
    of ``MAX_PARALLEL_NESTING_DEPTH`` — a value above it is a policy-load error,
    since a policy can never weaken a guardrail. ``allow_map_over_workflow`` forbids
    ``map.workflow`` fan-out when false.

    Every ceiling is ``>= 1`` by contract: 0 is never a silent no-op — to forbid
    ``map.workflow`` fan-out use ``allow_map_over_workflow: false``, and to forbid a
    workflow entirely omit it from the validation targets.
    """

    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = None
    max_total_steps: int | None = None
    max_parallel_width: int | None = None
    max_parallel_nesting: int | None = None
    max_subworkflow_depth: int | None = None
    allow_map_over_workflow: bool | None = None

    @field_validator("max_steps", "max_total_steps", "max_parallel_width", "max_subworkflow_depth")
    @classmethod
    def _validate_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError(_CEILING_MIN_MESSAGE)
        return value

    @field_validator("max_parallel_nesting")
    @classmethod
    def _validate_max_parallel_nesting(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError(_CEILING_MIN_MESSAGE)
        if value > MAX_PARALLEL_NESTING_DEPTH:
            # A policy may only TIGHTEN the in-spec hard ceiling, never widen it:
            # the spec loader rejects nesting deeper than MAX_PARALLEL_NESTING_DEPTH
            # regardless, so a policy value above it would be silently ineffective.
            raise ValueError(
                "composition.max_parallel_nesting may only tighten the in-spec ceiling "
                f"of {MAX_PARALLEL_NESTING_DEPTH}; got {value}"
            )
        return value


class PolicyRiskTierProviderAllowanceSpec(BaseModel):
    """A per-tier provider allowance (#300 D300-6): ``{models?: [...]}``.

    The same shape as ``providers.allowed``'s value restricted to ``models`` — the
    tier macro evaluates it through the EXACT ``_provider_model_policy_failure``
    predicate, so tier-level and policy-level provider constraints can never drift.
    (``base_urls`` is deliberately absent: the macro checks provider+model; accepting
    a knob it never evaluates would be a silent fail-open.)
    """

    model_config = ConfigDict(extra="forbid")

    models: list[str] | None = None

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_string_list(
            value, field="risk_tiers.<tier>.constrain_providers.<provider>.models"
        )


class PolicyRiskTierRequirementsSpec(BaseModel):
    """The requirements one risk tier expands to (#300 D300-4), the MACRO body.

    Each knob maps to an EXISTING policy control so the tier is a named macro over
    the checks already in place (one source of truth per control): ``require_review``
    → a lifecycle review gate (``_validate_review``), ``require_moderation`` → every
    activity declares moderation (``_validate_semantics``), ``require_redaction`` →
    observability redaction on (``_validate_observability``), ``require_payload_codec``
    → a Temporal payload codec is declared (``_validate_temporal``; #188 D188-2, so a
    ``human_gated``/``prohibited`` tier can imply encryption-at-rest), ``constrain_providers``
    → the workflow's provider AND model must pass this per-tier allow-list (D300-6:
    the same mapping shape + predicate as ``providers.allowed``, so an operator gets
    identical semantics at both levels), ``require_compensation`` → every
    ``side_effecting`` activity STEP declares ``compensate:`` (#299 D299-5, the saga
    guard). All optional so a tier declares only what it tightens; ``require_*`` booleans
    OR-merge on composition, ``constrain_providers``
    INTERSECTS like ``providers.allowed`` (shared provider keys survive, their
    ``models`` lists intersect).
    """

    model_config = ConfigDict(extra="forbid")

    require_review: bool | None = None
    require_moderation: bool | None = None
    require_redaction: bool | None = None
    require_payload_codec: bool | None = None
    require_compensation: bool | None = None
    constrain_providers: dict[str, PolicyRiskTierProviderAllowanceSpec] | None = None

    @field_validator("constrain_providers")
    @classmethod
    def _validate_constrain_providers(
        cls,
        value: dict[str, PolicyRiskTierProviderAllowanceSpec] | None,
    ) -> dict[str, PolicyRiskTierProviderAllowanceSpec] | None:
        if value is None:
            return None
        for provider_name in value:
            _validate_non_empty_string(provider_name, field="provider name")
        return value


class PolicyRiskTiersSpec(BaseModel):
    """The ``risk_tiers`` policy dimension (#300): the DEFINITION half of risk tiers.

    A workflow DECLARES its tier (``workflow.risk_tier``); this policy DEFINES what
    each tier requires and the floor. ``min_tier`` is a floor — every workflow is
    evaluated at ``max(declared, min_tier)`` (a floor lifts, never errors), and merges
    to the HIGHEST tier across composed policies (a sibling ordered-enum rule beside
    the numeric most-restrictive table). ``require_declared`` additionally rejects a
    workflow that never declared a tier (regulated projects opt in). Reaching
    ``prohibited`` as the effective tier denies admission outright.
    """

    model_config = ConfigDict(extra="forbid")

    min_tier: RiskTier | None = None
    require_declared: bool | None = None
    safe: PolicyRiskTierRequirementsSpec = Field(default_factory=PolicyRiskTierRequirementsSpec)
    policy_gated: PolicyRiskTierRequirementsSpec = Field(
        default_factory=PolicyRiskTierRequirementsSpec
    )
    human_gated: PolicyRiskTierRequirementsSpec = Field(
        default_factory=PolicyRiskTierRequirementsSpec
    )
    prohibited: PolicyRiskTierRequirementsSpec = Field(
        default_factory=PolicyRiskTierRequirementsSpec
    )


class TypefluxProjectPolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    version: Literal["1"] = "1"
    name: str
    description: str | None = None
    extends: list[str] = Field(default_factory=list)
    providers: PolicyProvidersSpec = Field(default_factory=PolicyProvidersSpec)
    observability: PolicyObservabilitySpec = Field(default_factory=PolicyObservabilitySpec)
    runtime: PolicyRuntimeSpec = Field(default_factory=PolicyRuntimeSpec)
    artifacts: PolicyArtifactsSpec = Field(default_factory=PolicyArtifactsSpec)
    review: PolicyReviewSpec = Field(default_factory=PolicyReviewSpec)
    semantics: PolicySemanticsSpec = Field(default_factory=PolicySemanticsSpec)
    imports: PolicyImportsSpec = Field(default_factory=PolicyImportsSpec)
    secrets: PolicySecretsSpec = Field(default_factory=PolicySecretsSpec)
    composition: PolicyCompositionSpec = Field(default_factory=PolicyCompositionSpec)
    risk_tiers: PolicyRiskTiersSpec = Field(default_factory=PolicyRiskTiersSpec)
    policy_id: str | None = Field(default=None, exclude=True)
    policy_path: Path | None = Field(default=None, exclude=True)

    @field_validator("name", "description")
    @classmethod
    def _validate_optional_strings(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_non_empty_string(value, field="policy string")
        return value

    @field_validator("extends")
    @classmethod
    def _validate_extends(cls, value: list[str]) -> list[str]:
        for policy_id in value:
            _validate_policy_id(policy_id)
        return value

    def to_payload(self, *, include_extends: bool = True) -> dict[str, Any]:
        exclude = {"policy_id", "policy_path"}
        if not include_extends:
            exclude.add("extends")
        return _drop_empty(
            self.model_dump(
                mode="json",
                exclude=exclude,
                exclude_none=True,
                exclude_defaults=True,
            )
        )


class ComposedProjectPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_policy_ids: tuple[str, ...]
    applied_policy_ids: tuple[str, ...]
    policy_names: tuple[str, ...]
    policy_hash: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_project_policy(
    project: TypefluxProjectSpec,
    policy_id: str,
) -> TypefluxProjectPolicySpec:
    _validate_policy_id(policy_id)
    try:
        raw_path = project.policies[policy_id]
    except KeyError as exc:
        raise ProjectPolicyError(f"unknown project policy: {policy_id}") from exc
    policy_path = _resolve_project_path(project, raw_path)
    raw = _load_yaml_mapping(policy_path)
    spec = TypefluxProjectPolicySpec.model_validate(raw)
    if spec.name != policy_id:
        raise ProjectPolicyError(
            f"policy {policy_id!r} name must match the project policy id: {spec.name!r}"
        )
    return spec.model_copy(update={"policy_id": policy_id, "policy_path": policy_path})


def load_project_policies(
    project: TypefluxProjectSpec,
    policy_ids: Sequence[str] | None = None,
) -> tuple[TypefluxProjectPolicySpec, ...]:
    selected = tuple(policy_ids) if policy_ids is not None else tuple(project.policies)
    return tuple(load_project_policy(project, policy_id) for policy_id in selected)


def compose_project_policies(
    project: TypefluxProjectSpec,
    policy_ids: Sequence[str],
) -> ComposedProjectPolicy:
    selected_policy_ids = tuple(policy_ids)
    if not selected_policy_ids:
        raise ProjectPolicyError("at least one project policy id is required for composition")
    applied = _resolve_policy_closure(project, selected_policy_ids)
    merged: dict[str, Any] = {}
    for policy in applied:
        constraints = policy.to_payload(include_extends=False)
        for metadata_key in ("version", "name", "description"):
            constraints.pop(metadata_key, None)
        merged = _merge_policy_payloads(
            merged,
            constraints,
            path=policy.policy_id or policy.name,
        )
    payload = _drop_empty(
        {
            "version": "1",
            "selected_policy_ids": list(selected_policy_ids),
            "applied_policy_ids": [policy.policy_id or policy.name for policy in applied],
            "policy_names": [policy.name for policy in applied],
            **merged,
        }
    )
    policy_hash = sha256(
        canonical_json(_canonical_hash_payload(payload)).encode("utf-8")
    ).hexdigest()
    return ComposedProjectPolicy(
        selected_policy_ids=selected_policy_ids,
        applied_policy_ids=tuple(policy.policy_id or policy.name for policy in applied),
        policy_names=tuple(policy.name for policy in applied),
        policy_hash=policy_hash,
        payload=payload,
    )


def policy_content_hash(policy: TypefluxProjectPolicySpec) -> str:
    return sha256(canonical_json(policy.to_payload()).encode("utf-8")).hexdigest()


def _canonical_hash_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    canonical = dict(payload)
    for key in ("selected_policy_ids", "applied_policy_ids", "policy_names"):
        value = canonical.get(key)
        if isinstance(value, list):
            canonical[key] = sorted(value)
    return canonical


def _resolve_policy_closure(
    project: TypefluxProjectSpec,
    policy_ids: Sequence[str],
) -> tuple[TypefluxProjectPolicySpec, ...]:
    applied: list[TypefluxProjectPolicySpec] = []
    applied_ids: set[str] = set()
    visiting: list[str] = []

    def visit(policy_id: str) -> None:
        _validate_policy_id(policy_id)
        if policy_id in applied_ids:
            return
        if policy_id in visiting:
            cycle = " -> ".join((*visiting, policy_id))
            raise ProjectPolicyError(f"project policy extends cycle detected: {cycle}")
        visiting.append(policy_id)
        policy = load_project_policy(project, policy_id)
        for parent_id in policy.extends:
            visit(parent_id)
        visiting.pop()
        if policy_id not in applied_ids:
            applied.append(policy)
            applied_ids.add(policy_id)

    for policy_id in policy_ids:
        visit(policy_id)
    return tuple(applied)


def _merge_policy_payloads(
    left: dict[str, Any],
    right: Mapping[str, Any],
    *,
    path: str,
) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        item_path = f"{path}.{key}"
        if key not in merged:
            merged[key] = value
            continue
        merged[key] = _merge_policy_value(merged[key], value, path=item_path)
    return merged


def _merge_policy_value(left: Any, right: Any, *, path: str) -> Any:
    if isinstance(left, dict) and isinstance(right, Mapping):
        if _is_provider_allowlist_path(path):
            return _intersect_mapping_allowlist(left, right, path=path)
        return _merge_policy_payloads(left, right, path=path)
    if isinstance(left, list) and isinstance(right, list):
        if _is_allowlist_path(path):
            return _intersect_allowlist(left, right, path=path)
        return _dedupe_sequence((*left, *right))
    if isinstance(left, bool) and isinstance(right, bool):
        merged_bool = _merge_bool_policy_value(left, right, path=path)
        if merged_bool is not None:
            return merged_bool
    merged_numeric = _merge_most_restrictive_numeric(left, right, path=path)
    if merged_numeric is not None:
        return merged_numeric
    merged_enum = _merge_most_restrictive_ordered_enum(left, right, path=path)
    if merged_enum is not None:
        return merged_enum
    if left == right:
        return left
    raise ProjectPolicyError(f"conflicting project policy values at {path}: {left!r} != {right!r}")


def _dedupe_sequence(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _is_allowlist_path(path: str) -> bool:
    return path.rsplit(".", maxsplit=1)[-1] in _ALLOWLIST_FIELD_NAMES


def _is_provider_allowlist_path(path: str) -> bool:
    # `risk_tiers.<tier>.constrain_providers` (#300 D300-6) shares `providers.allowed`'s
    # mapping shape, so it composes by the same mapping-allowlist INTERSECTION (shared
    # provider keys survive; their nested `models` lists intersect via the recursion).
    return path.endswith("providers.allowed") or path.endswith(".constrain_providers")


def _intersect_allowlist(left: list[Any], right: list[Any], *, path: str) -> list[Any]:
    if not left or not right:
        return []
    effective = [value for value in right if value in left]
    if not effective:
        # Explicit empty means intentional deny-all. Two non-empty allow-lists
        # that intersect to empty are treated as contradictory policy inputs.
        raise ProjectPolicyError(
            f"conflicting project policy allow-list at {path}: no overlapping values"
        )
    return _sorted_allowlist(effective)


def _intersect_mapping_allowlist(
    left: dict[str, Any],
    right: Mapping[str, Any],
    *,
    path: str,
) -> dict[str, Any]:
    if not left or not right:
        return {}
    shared = [key for key in right if key in left]
    if not shared:
        # Explicit empty means intentional deny-all. Two non-empty allow-lists
        # that intersect to empty are treated as contradictory policy inputs.
        raise ProjectPolicyError(
            f"conflicting project policy allow-list at {path}: no overlapping keys"
        )
    return {key: _merge_policy_value(left[key], right[key], path=f"{path}.{key}") for key in shared}


def _sorted_allowlist(values: Sequence[Any]) -> list[Any]:
    return sorted(_dedupe_sequence(values), key=_allowlist_sort_key)


def _allowlist_sort_key(value: Any) -> tuple[int, str]:
    return (0, value) if isinstance(value, str) else (1, canonical_json(value))


def _drop_empty(value: Any, *, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        payload = {key: _drop_empty(item, path=(*path, key)) for key, item in value.items()}
        return {
            key: item
            for key, item in payload.items()
            if item not in ({}, [], None) or _preserve_empty_value((*path, key), item)
        }
    if isinstance(value, list):
        return [_drop_empty(item, path=path) for item in value if item is not None]
    return value


def _preserve_empty_value(path: tuple[str, ...], value: Any) -> bool:
    if value == {}:
        return _preserve_empty_mapping(path)
    if value == []:
        return _preserve_empty_allowlist(path)
    return False


def _preserve_empty_mapping(path: tuple[str, ...]) -> bool:
    # An explicit empty `constrain_providers` (or an empty per-provider allowance
    # under it) is an intentional deny-all, exactly like `providers.allowed` (#300
    # D300-6) — it must survive drop-empty or the constraint silently vanishes.
    return (
        path == ("providers", "allowed")
        or (len(path) == 3 and path[:2] == ("providers", "allowed"))
        or (bool(path) and path[-1] == "constrain_providers")
        or (len(path) >= 2 and path[-2] == "constrain_providers")
    )


def _preserve_empty_allowlist(path: tuple[str, ...]) -> bool:
    return bool(path) and path[-1] in _ALLOWLIST_FIELD_NAMES


#: Numeric policy scalars with a well-defined monotonic "stricter" direction.
#: Conflicting values for these fields merge to the most-restrictive value
#: (which can never weaken a guardrail) instead of hard-failing. Ambiguous
#: retry/backoff fields are deliberately absent and keep hard-failing.
_MOST_RESTRICTIVE_NUMERIC_DIRECTIONS: dict[str, str] = {
    "max_bytes": "min",
    "max_concurrent": "min",
    "min_interval_seconds": "max",
    # A lower moderation threshold blocks more output, so it is the stricter one.
    "score_threshold": "min",
    # Composition ceilings (#298): a lower ceiling is always the stricter one, so
    # composing policies takes the min. ``allow_map_over_workflow`` is a boolean and
    # merges by ``allow_`` AND polarity via ``_merge_bool_policy_value``.
    "max_steps": "min",
    "max_total_steps": "min",
    "max_parallel_width": "min",
    "max_parallel_nesting": "min",
    "max_subworkflow_depth": "min",
}


def _merge_most_restrictive_numeric(left: Any, right: Any, *, path: str) -> int | float | None:
    field_name = path.rsplit(".", maxsplit=1)[-1]
    direction = _MOST_RESTRICTIVE_NUMERIC_DIRECTIONS.get(field_name)
    if direction is None or not _is_real_number(left) or not _is_real_number(right):
        return None
    return min(left, right) if direction == "min" else max(left, right)


#: Ordered-enum policy scalars whose conflicting values merge to the STRICTEST
#: (highest-ranked) member instead of hard-failing — the sibling of the numeric
#: most-restrictive table for closed, totally-ordered vocabularies (#300). For
#: ``min_tier`` a higher risk tier is the stricter floor, so composing policies takes
#: the max by ``RISK_TIER_ORDER`` rank (org floor ``policy_gated`` + tenant floor
#: ``human_gated`` ⇒ ``human_gated``).
_MOST_RESTRICTIVE_ORDERED_ENUMS: dict[str, tuple[str, ...]] = {
    "min_tier": RISK_TIER_ORDER,
}


def _merge_most_restrictive_ordered_enum(left: Any, right: Any, *, path: str) -> str | None:
    field_name = path.rsplit(".", maxsplit=1)[-1]
    order = _MOST_RESTRICTIVE_ORDERED_ENUMS.get(field_name)
    if order is None or not isinstance(left, str) or not isinstance(right, str):
        return None
    if left not in order or right not in order:
        return None
    return left if order.index(left) >= order.index(right) else right


def _is_real_number(value: Any) -> bool:
    # bool is an int subclass; policy booleans merge by polarity, not magnitude.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _merge_bool_policy_value(left: bool, right: bool, *, path: str) -> bool | None:
    field_name = path.rsplit(".", maxsplit=1)[-1]
    if (
        field_name == "required"
        or field_name.startswith("require_")
        or field_name.endswith("_required")
    ):
        return left or right
    if field_name.startswith("allow_") or field_name in {
        "retry_rate_limits",
        "retry_transient_errors",
    }:
        return left and right
    return None


def _resolve_project_path(project: TypefluxProjectSpec, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project.project_dir / path).resolve()


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    raw = strict_safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"empty YAML mapping: {path}")
    if not isinstance(raw, dict):
        raise TypeError(f"project policy must be a YAML mapping: {path}")
    return raw


def _validate_string_list(value: list[str], *, field: str) -> list[str]:
    for item in value:
        _validate_non_empty_string(item, field=field)
    return value


def _validate_non_empty_string(value: str, *, field: str) -> None:
    if not value or value.strip() != value:
        raise ValueError(f"{field} must be non-empty and trimmed")


def _validate_policy_id(value: str) -> None:
    _validate_non_empty_string(value, field="policy id")
    if any(separator in value for separator in ("/", "\\", ":")):
        raise ValueError("policy ids must be local project references, not paths or URLs")


__all__ = [
    "ArtifactSourcePolicy",
    "ComposedProjectPolicy",
    "ObservabilityBackendPolicy",
    "PolicyArtifactsSpec",
    "PolicyCompositionSpec",
    "PolicyImportsSpec",
    "PolicyObservabilitySpec",
    "PolicyProviderAllowanceSpec",
    "PolicyProviderCallLimitsSpec",
    "PolicyProviderLimitsSpec",
    "PolicyProviderRateLimitProviderSpec",
    "PolicyProviderRetrySpec",
    "PolicyProvidersSpec",
    "PolicyRedactionSpec",
    "PolicyReviewSpec",
    "PolicyRiskTierProviderAllowanceSpec",
    "PolicyRiskTierRequirementsSpec",
    "PolicyRiskTiersSpec",
    "PolicyRuntimeSpec",
    "PolicyTemporalSpec",
    "ProjectPolicyError",
    "ReviewInvalidDecisionPolicy",
    "TypefluxProjectPolicySpec",
    "compose_project_policies",
    "load_project_policies",
    "load_project_policy",
    "policy_content_hash",
]
