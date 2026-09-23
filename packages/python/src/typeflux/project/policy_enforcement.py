from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from typeflux.core.errors import TypefluxError
from typeflux.project.environment import (
    ProjectResolvedWorkflow,
    resolve_project_workflow,
)
from typeflux.project.policy import (
    ComposedProjectPolicy,
    compose_project_policies,
)
from typeflux.project.spec import (
    ProjectValidationCheck,
    TypefluxProjectSpec,
)
from typeflux.yaml.secrets import secret_reference_records, secret_value_configured
from typeflux.yaml.spec import (
    RISK_TIER_ORDER,
    TypefluxYamlSpec,
    WorkflowMapStepSpec,
    WorkflowParallelStepSpec,
)
from typeflux.yaml.workflow import collect_subworkflow_references


def validate_project_policy(
    *,
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    policy: ComposedProjectPolicy,
) -> tuple[ProjectValidationCheck, ...]:
    """Validate a resolved workflow/environment bundle against a composed policy."""

    payload = policy.payload
    checks = [
        _passed_check(
            "policy_selection",
            details=_policy_details(policy),
        )
    ]
    checks.append(_passed_check("policy_allowlists"))
    checks.append(_validate_provider(resolved, payload))
    checks.append(_validate_secrets(resolved, payload))
    checks.append(_validate_registry(resolved, payload))
    checks.append(_validate_imports(resolved, payload))
    checks.append(_validate_observability(resolved, payload))
    checks.append(_validate_temporal(resolved, payload))
    checks.append(_validate_artifacts(resolved, payload))
    checks.append(_validate_review(resolved, payload))
    checks.append(_validate_semantics(resolved, payload))
    checks.append(_validate_provider_retry(resolved, payload))
    checks.append(_validate_provider_limits(resolved, payload))
    checks.append(_validate_composition(resolved, payload))
    checks.append(_validate_risk_tier(resolved, payload))
    return tuple(checks)


class ProjectPolicyEnforcementError(TypefluxError, ValueError):
    """Raised when a resolved project workflow violates selected runtime policy."""

    def __init__(
        self,
        message: str,
        *,
        checks: Sequence[ProjectValidationCheck] = (),
    ) -> None:
        super().__init__(message)
        self.checks = tuple(checks)


@dataclass(frozen=True)
class RuntimePolicyGuard:
    """Runtime policy guard for checks that become concrete during execution."""

    policy: ComposedProjectPolicy
    provider_name: str | None = None
    enforcement_mode: str = "runtime"

    def enforce_provider_model(
        self,
        *,
        provider_name: str,
        provider_model: str | None,
        activity_name: str | None = None,
        prompt_name: str | None = None,
    ) -> None:
        # Enforce against the spec provider identity the policy/admission used
        # (provider.type), not a caller-derived name. Otherwise a custom provider
        # whose object name differs from its spec type could pass admission yet be
        # blocked at activity execution under a different key.
        policy_provider_name = self.provider_name or provider_name
        failure = _provider_model_policy_failure(
            self.policy.payload,
            provider_name=policy_provider_name,
            provider_model=provider_model,
        )
        if failure is None:
            return
        context = {
            key: value
            for key, value in {
                "activity": activity_name,
                "prompt": prompt_name,
                "provider": policy_provider_name,
                "model": provider_model,
            }.items()
            if value is not None
        }
        details = ", ".join(f"{key}={value!r}" for key, value in context.items())
        suffix = f" ({details})" if details else ""
        raise ProjectPolicyEnforcementError(f"{failure}{suffix}")

    def enforce_moderation_config(
        self,
        *,
        activity_name: str,
        moderation_configured: bool,
        on_violation: str | None,
    ) -> None:
        """Config-level moderation policy: ``required`` and ``require_block`` (#158).

        Raised before the moderator runs (or for an activity with no moderator),
        so a regulated activity that omits moderation — or uses ``flag`` where the
        policy mandates fail-closed — is rejected rather than running unguarded.
        """
        semantics = _mapping_at(self.policy.payload, "semantics")
        if not semantics:
            return
        # require_block implies moderation must be present: a fail-closed mandate
        # is meaningless if an activity can opt out by declaring no moderator, so
        # either flag rejects an unmoderated regulated activity.
        requires_moderation = (
            semantics.get("required") is True or semantics.get("require_block") is True
        )
        if requires_moderation and not moderation_configured:
            raise ProjectPolicyEnforcementError(
                f"activity {activity_name!r} must declare moderation "
                "(required by selected project policy)"
            )
        if (
            semantics.get("require_block") is True
            and moderation_configured
            and on_violation != "block"
        ):
            raise ProjectPolicyEnforcementError(
                f"activity {activity_name!r} moderation must use on_violation='block' "
                f"(required by selected project policy), not {on_violation!r}"
            )

    def moderation_policy_block(
        self,
        *,
        activity_name: str,
        categories: Sequence[str],
        max_score: float | None,
    ) -> str | None:
        """Verdict escalation: return a reason when the policy forces a block (#158).

        Policy can only tighten, never loosen. It applies the org's bar to the
        moderator's *reported* ``categories``/``max_score`` independent of the
        moderator's own ``flagged`` decision — so a stricter policy threshold
        blocks output a lenient moderator cleared (``flagged=False``), and a
        disallowed category blocks even when the activity only chose ``flag``.
        Returns ``None`` when the policy doesn't escalate.
        """
        semantics = _mapping_at(self.policy.payload, "semantics")
        if not semantics:
            return None
        disallowed = _list_at_or_none(semantics, "categories")
        if disallowed:
            hit = sorted(set(categories) & set(disallowed))
            if hit:
                label = "category" if len(hit) == 1 else "categories"
                return (
                    f"moderation policy blocked activity {activity_name!r}: "
                    f"disallowed {label} {', '.join(hit)}"
                )
        threshold = semantics.get("score_threshold")
        if (
            isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
            and max_score is not None
            and max_score >= threshold
        ):
            return (
                f"moderation policy blocked activity {activity_name!r}: "
                f"score {max_score} >= threshold {threshold}"
            )
        return None


def _iter_subworkflow_closure(
    project: TypefluxProjectSpec,
    *,
    resolved: ProjectResolvedWorkflow,
    environment_id: str,
) -> Iterator[tuple[str, ProjectResolvedWorkflow | None, Exception | None]]:
    """BFS over the transitive sub-workflow closure, yielding one
    ``(ref, child, error)`` per unique member (the parent itself is not yielded).

    ``child`` is the resolved sibling (with ``error`` ``None``); an unresolvable
    reference yields ``(ref, None, exc)`` and is NOT expanded further. The ONE walk —
    visit order, dedup, and error semantics — shared by the closure admission check
    and the bundle risk-tier projection, so the two can never drift (#300 slice 2
    review). Cycles cannot loop it: the ``visited`` set is the belt-and-braces guard.
    """
    visited: set[str] = {resolved.workflow_id}
    frontier: list[str] = list(collect_subworkflow_references(resolved.spec))
    while frontier:
        ref = frontier.pop(0)
        if ref in visited:
            continue
        visited.add(ref)
        try:
            # Children resolve under the SAME interpolation base the parent was resolved
            # with (#760, retained on the artifact): closure-policy admission of a
            # hermetically-resolved parent must never read the operator's shell.
            child = resolve_project_workflow(
                project,
                workflow_id=ref,
                environment_id=environment_id,
                base_env=resolved.base_env,
            )
        except Exception as exc:  # noqa: BLE001 - the consumer decides (fail vs skip).
            yield ref, None, exc
            continue
        frontier.extend(collect_subworkflow_references(child.spec))
        yield ref, child, None


def validate_subworkflow_closure_policy(
    *,
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    policy: ComposedProjectPolicy,
    environment_id: str,
) -> ProjectValidationCheck | None:
    """Transitive-closure admission (#55 §9, governance closure).

    A parent workflow that references sub-workflows (``workflow:`` steps or
    ``map.workflow`` fan-outs) pins a SPECIFIC child plan + digest — the composed
    program is one versioned artifact. Admitting the parent must therefore guarantee
    the WHOLE tree is compliant under the parent's composed policy, not rely on each
    child being separately admitted (a child may otherwise be governed by a laxer
    policy, or by none). This walks the transitive closure of sub-workflow references
    and validates every referenced child's resolved spec against the parent's composed
    ``policy``, aggregating any child violations into one ``policy_subworkflow_closure``
    check.

    Returns ``None`` when the parent references no sub-workflows (the V1 path — the
    check is not emitted, so non-composed workflows keep byte-identical validate
    output). The graph layer already rejects reference cycles at build time; the
    ``visited`` set here is a belt-and-braces guard so a cycle never loops this walk.

    The ``composition.max_subworkflow_depth`` ceiling (#298) rides this same walk: the
    reference tree's depth is measured (the parent is depth 0), and a tree deeper than
    the ceiling fails naming the deepest chain. Depth is the LONGEST root-to-leaf path
    (a DAG diamond is bounded by its deepest arm), independent of the BFS visit order.
    """
    direct = collect_subworkflow_references(resolved.spec)
    if not direct:
        return None

    order: list[str] = []
    failures: list[str] = []
    unresolved: list[str] = []
    # Direct references of each resolved (or unresolvable) workflow id, for the
    # longest-path depth measurement below. The root seeds it with its own direct refs.
    references: dict[str, tuple[str, ...]] = {resolved.workflow_id: tuple(direct)}
    # Per-member flattened step counts for the TREE-WIDE max_total_steps ceiling
    # (#298): unique members only (a diamond's shared child counts once — the composed
    # program contains one copy of its plan).
    member_step_counts: dict[str, int] = {
        resolved.workflow_id: _collect_composition_metrics(
            resolved.spec.workflow.steps
        ).flattened_step_count
    }
    # Per-member EFFECTIVE risk tier for the closure cascade (#300 D300-3): each
    # member's declared tier lifted by the same policy floor. The parent's effective
    # tier maxes over every member's — a parent embedding a higher-tier child inherits
    # at least that tier, because the parent's run executes the child's effects.
    member_risk_tiers: dict[str, str] = {}
    _parent_risk = evaluate_risk_tier(resolved.spec, policy.payload)
    if _parent_risk is not None:
        member_risk_tiers[resolved.workflow_id] = _parent_risk.effective
    # ONE closure walk (shared with the bundle risk-tier projection): a dangling ref
    # is already a workflow_graph failure; here it fails the closure (below).
    for ref, child, error in _iter_subworkflow_closure(
        project, resolved=resolved, environment_id=environment_id
    ):
        if child is None:
            unresolved.append(f"{ref} ({error})")
            references[ref] = ()
            continue
        order.append(ref)
        child_checks = validate_project_policy(project=project, resolved=child, policy=policy)
        for check in child_checks:
            if check.status == "failed":
                message = check.message or f"policy check failed: {check.code}"
                failures.append(f"sub-workflow {ref!r}: {check.code}: {message}")
        references[ref] = tuple(collect_subworkflow_references(child.spec))
        member_step_counts[ref] = _collect_composition_metrics(
            child.spec.workflow.steps
        ).flattened_step_count
        child_risk = evaluate_risk_tier(child.spec, policy.payload)
        if child_risk is not None:
            member_risk_tiers[ref] = child_risk.effective

    details: dict[str, Any] = {
        "referenced_workflows": order,
        "policy_hash": policy.policy_hash,
    }
    if unresolved:
        details["unresolved_references"] = unresolved
    # A child that cannot be resolved is a FAIL, not a silent pass: this check is the
    # runtime-guard admission's only view of the sub-workflow tree (no workflow_graph
    # check runs there), so a dangling/malformed reference must fail closed here rather
    # than admit a parent whose child could not be evaluated. (The validate report's
    # workflow_graph check reports the precise dangling error alongside.)
    closure_failures = list(failures)
    for ref in unresolved:
        closure_failures.append(f"sub-workflow {ref} could not be resolved for policy closure")

    composition = _mapping_at(policy.payload, "composition")

    # composition.max_total_steps ceiling (#298): TREE-WIDE flattened sum over the
    # whole closure (parent + every transitively referenced child, unique members
    # once), so a program split into many small sub-workflows cannot evade the
    # per-member max_steps bound by decomposition.
    max_total_steps = composition.get("max_total_steps")
    if isinstance(max_total_steps, int):
        total_steps = sum(member_step_counts.values())
        details["closure_total_steps"] = total_steps
        if total_steps > max_total_steps:
            breakdown = ", ".join(
                f"{member}: {count}" for member, count in sorted(member_step_counts.items())
            )
            closure_failures.append(
                f"sub-workflow closure total step count {total_steps} exceeds composition "
                f"ceiling max_total_steps {max_total_steps} ({breakdown})"
            )

    # composition.max_subworkflow_depth ceiling (#298): the parent is depth 0, so the
    # tree depth is the longest chain length minus one. Measure it over the reference
    # map (the LONGEST path, so a diamond is bounded by its deepest arm) and fail naming
    # the offending chain when it overflows.
    max_subworkflow_depth = composition.get("max_subworkflow_depth")
    if isinstance(max_subworkflow_depth, int):
        deepest_chain = _deepest_reference_chain(resolved.workflow_id, references)
        observed_depth = len(deepest_chain) - 1
        details["subworkflow_depth"] = observed_depth
        if observed_depth > max_subworkflow_depth:
            closure_failures.append(
                f"sub-workflow reference depth {observed_depth} exceeds composition ceiling "
                f"{max_subworkflow_depth} (deepest chain: {' -> '.join(deepest_chain)})"
            )

    # Risk-tier cascade (#300 D300-3): the parent's effective tier maxes over every
    # closure member's effective tier. When a member LIFTS the parent above its own
    # declared+floor effective, the parent re-evaluates its macro requirements at the
    # lifted tier — a safe parent embedding a human_gated child must itself satisfy the
    # human_gated controls (or deny). Only the LIFT is reported here; the parent's own
    # tier is already covered by its ``policy_risk_tier`` check.
    if _parent_risk is not None:
        cascade = _risk_tier_cascade(
            parent_workflow_id=resolved.workflow_id,
            parent_eval=_parent_risk,
            member_risk_tiers=member_risk_tiers,
            spec=resolved.spec,
            payload=policy.payload,
        )
        if cascade is not None:
            details["risk_tier_cascade"] = {
                "parent_effective": cascade.parent_effective,
                "cascade_effective": cascade.evaluation.effective,
                "lifted_by": cascade.lifted_by,
                "requirements": cascade.evaluation.requirement_details(),
            }
            for message in _risk_tier_failures(cascade.evaluation, include_require_declared=False):
                closure_failures.append(
                    f"risk tier cascade from sub-workflow {cascade.lifted_by!r}: {message}"
                )

    if closure_failures:
        return ProjectValidationCheck(
            code="policy_subworkflow_closure",
            status="failed",
            message="; ".join(closure_failures),
            details=details,
        )
    return _passed_check("policy_subworkflow_closure", details=details)


def _deepest_reference_chain(
    root: str,
    references: Mapping[str, tuple[str, ...]],
) -> list[str]:
    """The longest root-to-leaf chain of workflow ids over the reference DAG (#298).

    Memoized per node (longest chain below a node is path-independent in a DAG); the
    ``visiting`` set is a belt-and-braces cycle guard so a reference cycle that slipped
    past the graph builder cannot loop this walk.
    """
    cache: dict[str, list[str]] = {}

    def deepest(node: str, visiting: frozenset[str]) -> list[str]:
        if node in cache:
            return cache[node]
        best: list[str] = []
        for child in references.get(node, ()):
            if child in visiting:
                continue
            sub = deepest(child, visiting | {node})
            if len(sub) > len(best):
                best = sub
        result = [node, *best]
        cache[node] = result
        return result

    return deepest(root, frozenset())


def select_project_policy_ids_for_workflow(
    project: TypefluxProjectSpec,
    *,
    environment_id: str,
    workflow_id: str,
    explicit_policy_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    if explicit_policy_ids:
        return tuple(_dedupe(explicit_policy_ids))
    selected: list[str] = []
    for target in project.validation.targets.values():
        if target.environment is not None and target.environment != environment_id:
            continue
        if workflow_id not in target.workflows:
            continue
        selected.extend(target.policies)
    return tuple(_dedupe(selected))


#: Tiers whose declaration is a governance INTENTION that must be enforced (#788).
#: `safe`/undeclared is a declaration that requires nothing.
ELEVATED_RISK_TIERS = ("policy_gated", "human_gated", "prohibited")


def max_declared_closure_risk_tier(
    project: TypefluxProjectSpec,
    *,
    resolved: ProjectResolvedWorkflow,
    environment_id: str,
) -> str | None:
    """The highest risk tier declared by the workflow OR its sub-workflow closure (#788).

    A safe parent embedding an elevated child still executes the child's effects, so
    the binding requirement follows the closure exactly like the #300 cascade does.
    An unresolvable child contributes nothing here — that is safe because every run
    path resolves children BEFORE execution (``resolve_subworkflows_for`` on the CLI
    and CP binding paths), so an unresolvable child blocks the run on its own.
    """
    best: str | None = resolved.spec.workflow.risk_tier
    if collect_subworkflow_references(resolved.spec):
        for _ref, child, _error in _iter_subworkflow_closure(
            project, resolved=resolved, environment_id=environment_id
        ):
            if child is None:
                continue
            declared = child.spec.workflow.risk_tier
            if declared is not None:
                best = declared if best is None else _max_tier(best, declared)
    return best


def workflow_bound_in_other_environments(
    project: TypefluxProjectSpec, *, workflow_id: str, environment_id: str
) -> tuple[str, ...]:
    """Environments (other than ``environment_id``) whose targets bind this workflow (#788).

    Distinguishes "bound elsewhere, just not here" (a normal multi-environment shape —
    validation reports it without failing) from "bound nowhere" (fails closed)."""
    environments: list[str] = []
    for target in project.validation.targets.values():
        if workflow_id not in target.workflows or not target.policies:
            continue
        if target.environment is not None and target.environment != environment_id:
            environments.append(target.environment)
    return tuple(dict.fromkeys(environments))


def risk_tier_enforcement_gap(
    project: TypefluxProjectSpec,
    *,
    resolved: ProjectResolvedWorkflow,
    environment_id: str,
    selected_policy_ids: Sequence[str],
    policy: ComposedProjectPolicy | None,
) -> str | None:
    """The human-readable gap when a declared elevated tier would run unenforced (#788).

    Returns ``None`` when nothing is declared elevated (closure-aware max over the
    parent and every resolvable sub-workflow; an unresolvable child is blocked earlier
    by child resolution on every run path) or when the tier IS enforced — meaning
    ``evaluate_risk_tier`` at the closure-lifted tier yields a denial or at least one
    requirement. A merely PRESENT ``risk_tiers`` key whose effective-tier block demands
    nothing is still a gap (the hollow-dimension bypass)."""
    declared = max_declared_closure_risk_tier(
        project, resolved=resolved, environment_id=environment_id
    )
    if declared not in ELEVATED_RISK_TIERS:
        return None
    subject = (
        f"workflow {resolved.workflow_id!r} declares risk_tier {declared!r} "
        "(directly or via its sub-workflow closure)"
    )
    if not selected_policy_ids:
        return (
            f"{subject} but no project policy is selected for this workflow/environment — "
            "the declared tier would run UNENFORCED. Bind the workflow in project "
            "validation.targets (or pass --policy) to a policy whose risk_tiers dimension "
            "defines the tier's requirements."
        )
    assert policy is not None
    evaluation = evaluate_risk_tier(resolved.spec, policy.payload, cascade_floor=declared)
    if evaluation is None:
        return (
            f"{subject} but the composed policy ({', '.join(selected_policy_ids)}) declares "
            "no risk_tiers dimension — the declared tier would run UNENFORCED "
            "(evaluate_risk_tier has nothing in play). Add a risk_tiers block to the policy."
        )
    if not (evaluation.denied or evaluation.requirements):
        return (
            f"{subject} but the composed policy ({', '.join(selected_policy_ids)}) defines no "
            f"requirements for the effective tier {evaluation.effective!r} — the declared tier "
            "would run UNENFORCED (a hollow risk_tiers dimension). Add requirements (or a "
            "denial) for the tier."
        )
    return None


def build_project_policy_runtime_guard(
    *,
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    policy_ids: Sequence[str] = (),
    enforcement_mode: str = "runtime",
) -> RuntimePolicyGuard | None:
    selected_policy_ids = select_project_policy_ids_for_workflow(
        project,
        environment_id=resolved.environment_id,
        workflow_id=resolved.workflow_id,
        explicit_policy_ids=policy_ids,
    )
    if not selected_policy_ids:
        gap = risk_tier_enforcement_gap(
            project,
            resolved=resolved,
            environment_id=resolved.environment_id,
            selected_policy_ids=(),
            policy=None,
        )
        if gap is not None:
            # Fail closed (#788, audit B1): the spec declares a governance intention and
            # nothing would enforce it — refusing beats silently running unenforced.
            raise ProjectPolicyEnforcementError(gap)
        return None
    policy = compose_project_policies(project, selected_policy_ids)
    gap = risk_tier_enforcement_gap(
        project,
        resolved=resolved,
        environment_id=resolved.environment_id,
        selected_policy_ids=selected_policy_ids,
        policy=policy,
    )
    if gap is not None:
        raise ProjectPolicyEnforcementError(gap)
    checks = list(validate_project_policy(project=project, resolved=resolved, policy=policy))
    # Transitive-closure admission (#55 §9): a parent's referenced sub-workflows are
    # admitted under the parent's composed policy too, so a non-compliant child fails
    # the parent's admission fail-closed (never emitted for a non-composed workflow).
    closure = validate_subworkflow_closure_policy(
        project=project,
        resolved=resolved,
        policy=policy,
        environment_id=resolved.environment_id,
    )
    if closure is not None:
        checks.append(closure)
    failures = tuple(check for check in checks if check.status == "failed")
    if failures:
        raise ProjectPolicyEnforcementError(
            _policy_failure_message(failures),
            checks=checks,
        )
    return RuntimePolicyGuard(
        policy=policy,
        provider_name=resolved.spec.runtime.provider.type,
        enforcement_mode=enforcement_mode,
    )


def _policy_details(policy: ComposedProjectPolicy) -> dict[str, Any]:
    return {
        "selected_policy_ids": list(policy.selected_policy_ids),
        "applied_policy_ids": list(policy.applied_policy_ids),
        "policy_hash": policy.policy_hash,
    }


def _validate_provider(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    allowed = _mapping_at_or_none(payload, "providers", "allowed")
    if allowed is None:
        return _skipped_check("policy_provider", "policy does not constrain provider/model")
    provider = resolved.spec.runtime.provider
    failure = _provider_model_policy_failure(
        payload,
        provider_name=provider.type,
        provider_model=provider.model,
    )
    if failure is None:
        failure = _provider_base_url_policy_failure(
            payload,
            provider_name=provider.type,
            base_url=provider.base_url,
        )
    if failure is not None:
        return _failed_check(
            "policy_provider",
            failure,
        )
    return _passed_check(
        "policy_provider",
        details={"provider": provider.type, "model": provider.model},
    )


def _provider_base_url_policy_failure(
    payload: Mapping[str, Any],
    *,
    provider_name: str,
    base_url: str | None,
) -> str | None:
    allowed = _mapping_at_or_none(payload, "providers", "allowed")
    if allowed is None:
        return None
    provider_policy = allowed.get(provider_name)
    if provider_policy is None:
        return None
    base_urls = _list_at_or_none(provider_policy, "base_urls")
    if base_urls is None:
        return None
    # An unset base_url targets the provider's official default endpoint and
    # is always allowed; explicit endpoints must be on the allow-list.
    if base_url is None or base_url in base_urls:
        return None
    return f"provider base_url {base_url!r} is not allowed for provider {provider_name!r}"


def _validate_secrets(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    secrets_policy = _mapping_at(payload, "secrets")
    if not secrets_policy or secrets_policy.get("require_secret_references") is not True:
        return _skipped_check("policy_secrets", "policy does not require secret references")
    failures: list[str] = []
    # Env interpolation resolves before spec validation, so interpolated
    # values are indistinguishable from hardcoded literals here — both fail;
    # the enforced contract is typed value_from references only. The check is
    # driven by the shared secret-slot inventory (SECRET_SLOT_PATHS), so every
    # slot the bundle reports — api keys AND custom-extension config entries
    # (#792) — is enforced by the same walk; a slot cannot be inventoried yet
    # escape this policy. TLS cert and payload-codec fields are typed-only by
    # schema and never produce a literal record.
    for record in secret_reference_records(resolved.spec):
        if record.source_kind == "literal":
            failures.append(
                f"{record.runtime_path} must use a value_from secret reference, "
                "not a literal credential value"
            )
    if failures:
        return _failed_check("policy_secrets", "; ".join(failures))
    return _passed_check("policy_secrets", details={"require_secret_references": True})


def _validate_registry(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    registry_policy = _mapping_at(payload, "runtime", "registry")
    if not registry_policy:
        return _skipped_check("policy_registry", "policy does not constrain prompt registry")
    allowed_hosts = _list_at_or_none(registry_policy, "allowed_hosts")
    if allowed_hosts is None:
        return _skipped_check("policy_registry", "policy does not constrain prompt registry")
    registry = resolved.spec.runtime.registry
    # Inline registries have no host; custom registries own their own connection.
    if registry.type in ("inline", "custom"):
        return _passed_check("policy_registry", details={"registry": registry.type})
    if registry.type == "langsmith":
        host_env = ("LANGSMITH_HOST", "LANGCHAIN_ENDPOINT")
        host_hint = "LANGSMITH_HOST"
    else:
        host_env = ("LANGFUSE_HOST", "LANGFUSE_BASE_URL")
        host_hint = "LANGFUSE_HOST"
    effective_host = registry.host or next(
        (value for name in host_env if (value := os.environ.get(name))), None
    )
    if effective_host is None:
        return _failed_check(
            "policy_registry",
            "policy constrains registry hosts, but no registry host is configured "
            f"(set runtime.registry.host or {host_hint})",
        )
    if effective_host not in allowed_hosts:
        return _failed_check(
            "policy_registry",
            f"registry host {effective_host!r} is not allowed by selected project policy",
        )
    return _passed_check(
        "policy_registry",
        details={"registry": registry.type, "host": effective_host},
    )


def _validate_imports(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    imports_policy = _mapping_at(payload, "imports")
    if not imports_policy:
        return _skipped_check("policy_imports", "policy does not constrain imports")

    failures: list[str] = []
    runtime_imports = resolved.spec.runtime.imports
    allowed_roots = _list_at_or_none(imports_policy, "allowed_module_roots")
    allow_absolute_modules = imports_policy.get("allow_absolute_activity_modules")

    # One source of truth for the three `type: custom` extension kinds, so a new
    # one can't be partially wired (the dangerous miss is forgetting a kind here
    # and silently not bounding it against project policy).
    extension_kinds = (
        (
            "provider",
            "allow_provider_class",
            resolved.spec.runtime.provider.provider_class,
        ),
        (
            "registry",
            "allow_registry_class",
            resolved.spec.runtime.registry.registry_class,
        ),
        (
            "observability",
            "allow_observability_class",
            resolved.spec.runtime.observability.backend_class,
        ),
    )
    for kind, flag, class_value in extension_kinds:
        if imports_policy.get(flag) is False:
            if getattr(runtime_imports, flag):
                failures.append(f"runtime.imports.{flag} is broader than project policy")
            if class_value is not None:
                failures.append(f"runtime.{kind}.class is not allowed by project policy")
    # Moderator callables are per-activity, not a single runtime class (#158), but
    # the workflow flag is governed like the extension-class flags so a locked
    # project policy can forbid arbitrary moderator imports.
    if imports_policy.get("allow_moderator_callable") is False:
        if runtime_imports.allow_moderator_callable:
            failures.append(
                "runtime.imports.allow_moderator_callable is broader than project policy"
            )
        for definition in resolved.spec.activities.definitions:
            if definition.moderation is not None and definition.moderation.moderator is not None:
                failures.append(
                    f"activity {definition.name!r} moderation.moderator is not allowed by "
                    "project policy"
                )
    if allow_absolute_modules is False:
        if runtime_imports.allow_absolute_activity_modules:
            failures.append(
                "runtime.imports.allow_absolute_activity_modules is broader than project policy"
            )
        for module in resolved.spec.activities.modules:
            if module.absolute:
                failures.append(
                    f"absolute activity module {module.module!r} is not allowed by project policy"
                )

    if allowed_roots is not None:
        for runtime_root in runtime_imports.allowed_module_roots:
            if not _module_in_any_root(runtime_root, allowed_roots):
                failures.append(
                    f"runtime import root {runtime_root!r} is outside project policy roots"
                )
        for kind, _flag, class_value in extension_kinds:
            if class_value is None:
                continue
            module_name, _, _ = class_value.partition(":")
            if not _module_in_root(
                module_name,
                resolved.spec.project,
            ) and not _module_in_any_root(module_name, allowed_roots):
                failures.append(
                    f"runtime.{kind}.class imports {module_name!r} outside policy roots"
                )
        for module in resolved.spec.activities.modules:
            if module.absolute and not _module_in_any_root(module.module, allowed_roots):
                failures.append(
                    f"absolute activity module {module.module!r} is outside policy roots"
                )
        for definition in resolved.spec.activities.definitions:
            moderation = definition.moderation
            if moderation is None or moderation.moderator is None:
                continue
            module_name, _, _ = moderation.moderator.partition(":")
            if not _module_in_root(module_name, resolved.spec.project) and not _module_in_any_root(
                module_name, allowed_roots
            ):
                failures.append(
                    f"activity {definition.name!r} moderation.moderator imports "
                    f"{module_name!r} outside policy roots"
                )

    if failures:
        return _failed_check("policy_imports", "; ".join(failures))
    return _passed_check("policy_imports")


def _validate_observability(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    observability_policy = _mapping_at(payload, "observability")
    if not observability_policy:
        return _skipped_check(
            "policy_observability",
            "policy does not constrain observability",
        )
    failures: list[str] = []
    observability = resolved.spec.runtime.observability
    backend = observability.type or "none"
    if observability_policy.get("required") is True and backend == "none":
        failures.append("observability backend is required by project policy")
    allowed_backends = _list_at_or_none(observability_policy, "allowed_backends")
    if allowed_backends is not None and backend not in allowed_backends:
        failures.append(f"observability backend {backend!r} is not allowed")
    redaction_policy = _mapping_at(observability_policy, "redaction")
    if redaction_policy.get("required") is True and not _redaction_enabled(resolved.spec):
        failures.append("observability redaction is required by project policy")
    if (
        redaction_policy.get("preserve_typeflux_metadata") is True
        and not observability.redaction.preserve_typeflux_metadata
    ):
        failures.append("redaction must preserve typeflux metadata")
    required_custom_rules = _list_at_or_none(redaction_policy, "require_custom_rules")
    if required_custom_rules:
        # Requiring named custom rules means requiring they actually RUN. `_build_redactor`
        # no-ops the ENTIRE redactor when redaction.enabled is false, so a policy that names
        # required rules while leaving redaction disabled would report the control satisfied
        # while PII flows plaintext — fail-closed on both knobs. `_redaction_enabled` mirrors
        # `_build_redactor`'s own enabled predicate (default-on unless explicit enabled:false).
        if not _redaction_enabled(resolved.spec):
            failures.append(
                "observability policy requires custom redaction rules ["
                + ", ".join(sorted(required_custom_rules))
                + "] but observability.redaction.enabled is false — required rules must "
                "actually run"
            )
        missing = _missing_custom_redaction_rules(resolved.spec, required_custom_rules)
        if missing:
            failures.append(
                "observability redaction is missing required custom rules: "
                + ", ".join(sorted(missing))
            )
    if failures:
        return _failed_check("policy_observability", "; ".join(failures))
    return _passed_check("policy_observability", details={"backend": backend})


def _validate_temporal(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    temporal_policy = _mapping_at(payload, "runtime", "temporal")
    if not temporal_policy:
        return _skipped_check("policy_temporal", "policy does not constrain Temporal")
    failures: list[str] = []
    temporal = resolved.spec.runtime.temporal
    region = os.environ.get("TYPEFLUX_TEMPORAL_REGION")
    address = temporal.address
    namespace = temporal.namespace
    allowed_addresses = _list_at_or_none(temporal_policy, "allowed_addresses")
    if allowed_addresses is not None and address not in allowed_addresses:
        failures.append(f"Temporal address {address!r} is not allowed")
    allowed_namespaces = _list_at_or_none(temporal_policy, "allowed_namespaces")
    if allowed_namespaces is not None and namespace not in allowed_namespaces:
        failures.append(f"Temporal namespace {namespace!r} is not allowed")
    allowed_regions = _list_at_or_none(temporal_policy, "allowed_regions")
    address_regions = _mapping_at_or_none(temporal_policy, "address_regions")
    if address_regions is not None:
        # The address-to-region mapping is policy-authored truth; a
        # self-attested TYPEFLUX_TEMPORAL_REGION can only corroborate it.
        mapped_region = address_regions.get(address)
        if not isinstance(mapped_region, str):
            failures.append(f"Temporal address {address!r} has no region mapping in project policy")
        else:
            if allowed_regions is not None and mapped_region not in allowed_regions:
                failures.append(
                    f"Temporal address {address!r} maps to region {mapped_region!r}, "
                    "which is not allowed"
                )
            if region is not None and region != mapped_region:
                failures.append(
                    f"self-attested Temporal region {region!r} conflicts with the policy "
                    f"mapping {mapped_region!r} for address {address!r}"
                )
            region = mapped_region
    elif allowed_regions is not None and region not in allowed_regions:
        failures.append(f"Temporal region {region!r} is not allowed")
    tls_enabled = temporal.tls is not False
    if temporal_policy.get("require_tls") is True and not tls_enabled:
        failures.append("Temporal TLS is required by project policy")
    api_key_configured = secret_value_configured(temporal.api_key)
    if temporal_policy.get("require_api_key") is True and not api_key_configured:
        failures.append("Temporal API key is required by project policy")
    payload_codec_configured = _payload_codec_configured(resolved.spec)
    if temporal_policy.get("require_payload_codec") is True and not payload_codec_configured:
        failures.append(
            "Temporal payload codec is required by project policy but "
            "runtime.temporal.payload_codec is not declared"
        )
    if failures:
        return _failed_check("policy_temporal", "; ".join(failures))
    return _passed_check(
        "policy_temporal",
        details={
            "address": address,
            "namespace": namespace,
            "region": region,
            "tls_enabled": tls_enabled,
            "api_key_configured": api_key_configured,
            "payload_codec_configured": payload_codec_configured,
        },
    )


def _validate_artifacts(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    artifact_policy = _mapping_at(payload, "artifacts")
    if not artifact_policy:
        return _skipped_check("policy_artifacts", "policy does not constrain artifacts")
    failures: list[str] = []
    runtime_artifacts = resolved.spec.runtime.artifacts
    allowed_sources = _list_at_or_none(artifact_policy, "allowed_sources")
    if allowed_sources is not None:
        for source in runtime_artifacts.allowed_sources:
            if source not in allowed_sources:
                failures.append(f"artifact source {source!r} is not allowed")
    allowed_media_types = _list_at_or_none(artifact_policy, "allowed_media_types")
    if allowed_media_types is not None:
        if not runtime_artifacts.allowed_media_types:
            failures.append("runtime.artifacts.allowed_media_types must constrain media types")
        for media_type in runtime_artifacts.allowed_media_types:
            if not _media_constraint_allowed(media_type, allowed_media_types):
                failures.append(f"artifact media type {media_type!r} is not allowed")
    max_bytes = artifact_policy.get("max_bytes")
    if isinstance(max_bytes, int):
        if runtime_artifacts.max_bytes is None:
            failures.append("runtime.artifacts.max_bytes must be configured")
        elif runtime_artifacts.max_bytes > max_bytes:
            failures.append(
                f"runtime.artifacts.max_bytes {runtime_artifacts.max_bytes} exceeds {max_bytes}"
            )
    for definition in resolved.spec.activities.definitions:
        for artifact in definition.artifacts:
            if allowed_media_types is not None:
                for media_type in artifact.media_types:
                    if not _media_constraint_allowed(media_type, allowed_media_types):
                        failures.append(
                            f"activity {definition.name!r} artifact {artifact.name!r} "
                            f"media type {media_type!r} is not allowed"
                        )
            if isinstance(max_bytes, int) and artifact.max_bytes is not None:
                if artifact.max_bytes > max_bytes:
                    failures.append(
                        f"activity {definition.name!r} artifact {artifact.name!r} "
                        f"max_bytes {artifact.max_bytes} exceeds {max_bytes}"
                    )
    if failures:
        return _failed_check("policy_artifacts", "; ".join(failures))
    return _passed_check("policy_artifacts")


def _validate_review(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    review_policy = _mapping_at(payload, "review")
    if not review_policy:
        return _skipped_check("policy_review", "policy does not constrain review routes")
    failures: list[str] = []
    lifecycle = resolved.spec.workflow.lifecycle
    # The check quantifies over ALL gates (#55 §9): the single `review` and the named
    # `gates` list normalize through resolved_gates(), so a multi-gate workflow satisfies
    # require_review_routes and every gate must honor the invalid_user_decision constraint.
    gates = _resolved_review_gates(resolved.spec)
    single_review = lifecycle is not None and lifecycle.review is not None
    if review_policy.get("require_review_routes") is True and not gates:
        failures.append("workflow lifecycle review routes are required by project policy")
    invalid_user_decision = review_policy.get("invalid_user_decision")
    if invalid_user_decision is not None:
        if not gates:
            failures.append("workflow lifecycle review is required for invalid_user_decision")
        for gate in gates:
            if gate.invalid_user_decision != invalid_user_decision:
                label = (
                    "workflow lifecycle review"
                    if single_review
                    else f"workflow lifecycle gate {gate.id!r}"
                )
                failures.append(
                    f"{label} invalid_user_decision "
                    f"{gate.invalid_user_decision!r} does not match policy {invalid_user_decision!r}"
                )
    if failures:
        return _failed_check("policy_review", "; ".join(failures))
    return _passed_check("policy_review")


def _validate_semantics(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    semantics_policy = _mapping_at(payload, "semantics")
    if not semantics_policy:
        return _skipped_check("policy_semantics", "policy does not constrain moderation")
    requires_moderation = (
        semantics_policy.get("required") is True or semantics_policy.get("require_block") is True
    )
    require_block = semantics_policy.get("require_block") is True
    # YAML activity definitions now carry moderation (#158), so admission verifies
    # required/require_block before deploy. Validates ALL definitions (like
    # _validate_artifacts), so admission is intentionally stricter than the
    # per-executed-activity runtime guard. Activities defined only in Python are
    # invisible here and remain enforced at the runtime checkpoint.
    definitions = resolved.spec.activities.definitions
    if not requires_moderation or not definitions:
        return _skipped_check(
            "policy_semantics",
            "moderation policy is also enforced at the runtime output checkpoint",
        )
    failures: list[str] = []
    missing = set(_activities_missing_moderation(resolved.spec))
    for definition in definitions:
        moderation = definition.moderation
        if definition.name in missing:
            failures.append(
                f"activity {definition.name!r} must declare moderation "
                "(required by selected project policy)"
            )
        elif require_block and moderation is not None and moderation.on_violation != "block":
            failures.append(
                f"activity {definition.name!r} moderation must use on_violation='block' "
                f"(required by selected project policy), not {moderation.on_violation!r}"
            )
    if failures:
        return _failed_check("policy_semantics", "; ".join(failures))
    return _passed_check("policy_semantics", details={"activities": len(definitions)})


def _validate_provider_retry(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    retry_policy = _mapping_at(payload, "runtime", "provider_retry")
    if not retry_policy:
        return _skipped_check(
            "policy_provider_retry",
            "policy does not constrain provider retry",
        )
    failures: list[str] = []
    retry = resolved.spec.runtime.provider_retry
    max_attempts = retry.max_attempts if retry is not None else 1
    policy_max_attempts = retry_policy.get("max_attempts")
    if isinstance(policy_max_attempts, int) and max_attempts > policy_max_attempts:
        failures.append(f"provider retry max_attempts {max_attempts} exceeds {policy_max_attempts}")
    retry_rate_limits = retry.retry_rate_limits if retry is not None else True
    if retry_policy.get("retry_rate_limits") is False and retry_rate_limits:
        failures.append("provider retry_rate_limits is broader than project policy")
    retry_transient_errors = retry.retry_transient_errors if retry is not None else True
    if retry_policy.get("retry_transient_errors") is False and retry_transient_errors:
        failures.append("provider retry_transient_errors is broader than project policy")
    if failures:
        return _failed_check("policy_provider_retry", "; ".join(failures))
    return _passed_check("policy_provider_retry")


def _validate_provider_limits(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    limits_policy = _mapping_at(payload, "runtime", "provider_limits")
    if not limits_policy:
        return _skipped_check(
            "policy_provider_limits",
            "policy does not constrain provider limits",
        )
    provider = resolved.spec.runtime.provider
    policy_limits = _select_provider_limit(
        limits_policy,
        provider_name=provider.type,
        provider_model=provider.model,
    )
    if policy_limits is None:
        return _skipped_check(
            "policy_provider_limits",
            "policy does not constrain this provider/model limit",
        )
    runtime_limits = _select_runtime_provider_limit(
        resolved,
        provider_name=provider.type,
        provider_model=provider.model,
    )
    if runtime_limits is None:
        return _failed_check(
            "policy_provider_limits",
            "runtime.provider_limits must be configured for constrained provider/model",
        )
    failures = _provider_limit_failures(runtime_limits, policy_limits)
    if failures:
        return _failed_check("policy_provider_limits", "; ".join(failures))
    return _passed_check("policy_provider_limits")


def _select_provider_limit(
    limits_policy: Mapping[str, Any],
    *,
    provider_name: str,
    provider_model: str | None,
) -> Mapping[str, Any] | None:
    providers = _mapping_at(limits_policy, "providers")
    provider_limits = _mapping_at(providers, provider_name)
    if provider_limits:
        models = _mapping_at(provider_limits, "models")
        if provider_model is not None:
            model_limits = _mapping_at(models, provider_model)
            if model_limits:
                return model_limits
        limits = _mapping_at(provider_limits, "limits")
        if limits:
            return limits
    default_limits = _mapping_at(limits_policy, "default")
    return default_limits or None


def _select_runtime_provider_limit(
    resolved: ProjectResolvedWorkflow,
    *,
    provider_name: str,
    provider_model: str | None,
) -> Any:
    provider_limits = resolved.spec.runtime.provider_limits
    if provider_limits is None:
        return None
    provider_spec = provider_limits.providers.get(provider_name)
    if provider_spec is not None:
        if provider_model is not None and provider_model in provider_spec.models:
            return provider_spec.models[provider_model]
        if (
            provider_spec.max_concurrent is not None
            or provider_spec.min_interval_seconds is not None
        ):
            return provider_spec
    return provider_limits.default


def _provider_limit_failures(runtime_limits: Any, policy_limits: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    policy_max_concurrent = policy_limits.get("max_concurrent")
    if isinstance(policy_max_concurrent, int):
        runtime_max_concurrent = runtime_limits.max_concurrent
        if runtime_max_concurrent is None or runtime_max_concurrent > policy_max_concurrent:
            failures.append(
                f"provider max_concurrent must be configured and <= {policy_max_concurrent}"
            )
    policy_min_interval = policy_limits.get("min_interval_seconds")
    if isinstance(policy_min_interval, (int, float)):
        runtime_min_interval = runtime_limits.min_interval_seconds
        if runtime_min_interval is None or runtime_min_interval < policy_min_interval:
            failures.append(
                f"provider min_interval_seconds must be configured and >= {policy_min_interval}"
            )
    return failures


@dataclass(frozen=True)
class _CompositionMetrics:
    """Tree-wide graph-shape measurements for the composition ceilings (#298)."""

    flattened_step_count: int
    max_parallel_width: int
    max_parallel_nesting: int
    widest_parallel_step: str | None
    deepest_parallel_step: str | None
    map_over_workflow_step_ids: tuple[str, ...]


def _collect_composition_metrics(steps: Sequence[Any]) -> _CompositionMetrics:
    """Walk a workflow's step tree once, gathering every composition measurement.

    ``flattened_step_count`` counts every id-bearing node (parallel containers AND
    their branch steps, recursively); ``max_parallel_width`` is the largest branch
    count of any single ``parallel`` block; ``max_parallel_nesting`` the deepest
    parallel-in-parallel level; ``map_over_workflow_step_ids`` every ``map.workflow``
    fan-out step. Pure and deterministic — no I/O, no imports.
    """
    total = 0
    max_width = 0
    widest_step: str | None = None
    max_nesting = 0
    deepest_step: str | None = None
    map_wf_ids: list[str] = []

    def walk(nodes: Sequence[Any], depth: int) -> None:
        nonlocal total, max_width, widest_step, max_nesting, deepest_step
        for step in nodes:
            total += 1
            if isinstance(step, WorkflowMapStepSpec) and step.map.workflow is not None:
                map_wf_ids.append(step.id)
            elif isinstance(step, WorkflowParallelStepSpec):
                width = len(step.parallel.branches)
                if width > max_width:
                    max_width = width
                    widest_step = step.id
                nesting = depth + 1
                if nesting > max_nesting:
                    max_nesting = nesting
                    deepest_step = step.id
                for branch in step.parallel.branches:
                    walk(branch.steps, nesting)

    walk(steps, 0)
    return _CompositionMetrics(
        flattened_step_count=total,
        max_parallel_width=max_width,
        max_parallel_nesting=max_nesting,
        widest_parallel_step=widest_step,
        deepest_parallel_step=deepest_step,
        map_over_workflow_step_ids=tuple(map_wf_ids),
    )


def _validate_composition(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    """Composition ceilings on a workflow's OWN graph shape (#298, code
    ``policy_composition_ceilings``). Pure tree-walks — flattened step count, parallel
    width across this workflow's tree, parallel nesting depth, and ``map.workflow``
    presence — PER-WORKFLOW knobs that land at every admission point through the same
    pipeline as every other dimension. The TREE-WIDE ceilings — ``max_subworkflow_depth``
    and the closure-summed ``max_total_steps`` — are cross-workflow and enforced by the
    closure walk (``validate_subworkflow_closure_policy``); this validator applies
    ``max_total_steps`` only as the single-workflow lower bound.

    Each closure member is validated against the PARENT's composed policy (the closure
    walk re-runs ``validate_project_policy`` per child), so a child that overflows any
    ceiling fails the parent's admission.
    """
    composition = _mapping_at(payload, "composition")
    if not composition:
        return _skipped_check(
            "policy_composition_ceilings",
            "policy does not constrain composition",
        )
    metrics = _collect_composition_metrics(resolved.spec.workflow.steps)
    failures: list[str] = []
    max_steps = composition.get("max_steps")
    if isinstance(max_steps, int) and metrics.flattened_step_count > max_steps:
        failures.append(
            f"workflow flattened step count {metrics.flattened_step_count} exceeds "
            f"composition ceiling {max_steps}"
        )
    # max_total_steps is TREE-WIDE (the closure walk enforces the true sum); a single
    # workflow's own count is a lower bound of any tree containing it, so enforcing it
    # here covers the non-composed case (where no closure check is emitted) and rejects
    # early when even one member already overflows the whole-tree budget.
    max_total_steps = composition.get("max_total_steps")
    if isinstance(max_total_steps, int) and metrics.flattened_step_count > max_total_steps:
        failures.append(
            f"workflow flattened step count {metrics.flattened_step_count} exceeds "
            f"tree-wide composition ceiling max_total_steps {max_total_steps}"
        )
    max_width = composition.get("max_parallel_width")
    if isinstance(max_width, int) and metrics.max_parallel_width > max_width:
        failures.append(
            f"parallel block {metrics.widest_parallel_step!r} has width "
            f"{metrics.max_parallel_width} exceeding composition ceiling {max_width}"
        )
    max_nesting = composition.get("max_parallel_nesting")
    if isinstance(max_nesting, int) and metrics.max_parallel_nesting > max_nesting:
        failures.append(
            f"parallel nesting depth {metrics.max_parallel_nesting} at step "
            f"{metrics.deepest_parallel_step!r} exceeds composition ceiling {max_nesting}"
        )
    if composition.get("allow_map_over_workflow") is False and metrics.map_over_workflow_step_ids:
        ids = ", ".join(repr(step_id) for step_id in metrics.map_over_workflow_step_ids)
        failures.append(
            f"map-over-workflow fan-out is not allowed by composition policy (steps: {ids})"
        )
    if failures:
        return _failed_check("policy_composition_ceilings", "; ".join(failures))
    return _passed_check(
        "policy_composition_ceilings",
        details={
            "flattened_step_count": metrics.flattened_step_count,
            "max_parallel_width": metrics.max_parallel_width,
            "max_parallel_nesting": metrics.max_parallel_nesting,
        },
    )


# ── risk tiers (#300) ─────────────────────────────────────────────────────────
#
# A risk tier is a NAMED MACRO over the controls that already exist: the effective
# tier's policy block expands into the same predicates the other checks use (review
# gate, moderation, redaction, provider allow-list), reported under the ONE
# ``policy_risk_tier`` check code with each expanded requirement named in details.
# No parallel enforcement engine (D300-4).


def _tier_rank(tier: str) -> int:
    # Unknown/malformed tiers rank as the strictest so a typo fails closed, never open.
    try:
        return RISK_TIER_ORDER.index(tier)
    except ValueError:
        return len(RISK_TIER_ORDER)


def _max_tier(*tiers: str) -> str:
    return max(tiers, key=_tier_rank)


@dataclass(frozen=True)
class _RiskTierRequirement:
    name: str
    satisfied: bool


@dataclass(frozen=True)
class RiskTierEvaluation:
    """The evaluated risk posture of one workflow under a composed policy (#300).

    ``declared`` is the workflow's declared-or-``safe`` tier; ``floor`` the policy
    ``min_tier`` (or ``safe``); ``effective`` = ``max(declared, floor, cascade_floor)``.
    ``floor_source`` records what set the effective tier (``declared`` / ``policy_floor``
    / ``closure``). ``requirements`` is the effective tier's macro expansion. The
    contributor stamps a redaction-safe subset (tier names + satisfied control names).
    """

    declared: str
    floor: str
    effective: str
    floor_source: str
    undeclared: bool
    require_declared: bool
    denied: bool
    requirements: tuple[_RiskTierRequirement, ...]

    def satisfied_controls(self) -> list[str]:
        return [req.name for req in self.requirements if req.satisfied]

    def unsatisfied_controls(self) -> list[str]:
        return [req.name for req in self.requirements if not req.satisfied]

    def requirement_details(self) -> list[dict[str, Any]]:
        return [{"name": req.name, "satisfied": req.satisfied} for req in self.requirements]


@dataclass(frozen=True)
class RiskTierCascade:
    """The sub-workflow closure LIFT of a parent's effective risk tier (#300 D300-3).

    Present only when a closure member's effective tier exceeds the parent's own
    declared+floor effective — ``lifted_by`` is the workflow id that raises it,
    ``parent_effective`` the parent's pre-lift tier, and ``evaluation`` the parent
    RE-EVALUATED at the lifted tier (its macro requirements re-expanded). The closure
    admission check and the bundle projection derive from this ONE result.
    """

    lifted_by: str
    parent_effective: str
    evaluation: RiskTierEvaluation


@dataclass(frozen=True)
class RiskTierPosture:
    """A workflow's full risk-tier posture for surfacing (#300 slice 2): its own
    ``base`` evaluation plus the ``cascade`` lift when a higher-tier child raises it
    (``None`` when nothing lifts). Built by ``evaluate_workflow_risk_tier`` from the
    SAME ``evaluate_risk_tier`` / ``_risk_tier_cascade`` the admission checks use."""

    base: RiskTierEvaluation
    cascade: RiskTierCascade | None


def evaluate_risk_tier(
    spec: TypefluxYamlSpec,
    payload: Mapping[str, Any],
    *,
    cascade_floor: str | None = None,
) -> RiskTierEvaluation | None:
    """Evaluate the workflow's effective risk tier and expand its macro requirements.

    Operates on the resolved ``spec`` (so both admission and the metadata contributor
    share one evaluation). Returns ``None`` when the policy declares no ``risk_tiers``
    dimension (nothing in play). ``cascade_floor`` lifts the effective tier for the
    sub-workflow closure cascade (D300-3): a parent embedding a higher-tier child is
    re-evaluated at the lifted tier. The effective tier's block is expanded into the
    SAME predicates the standalone checks use — never a second copy of their logic.
    """
    risk_policy = _mapping_at(payload, "risk_tiers")
    if not risk_policy:
        return None

    raw_declared = spec.workflow.risk_tier
    undeclared = raw_declared is None
    declared = raw_declared or "safe"
    floor = _string_or(risk_policy.get("min_tier"), "safe")
    effective = _max_tier(declared, floor)
    if cascade_floor is not None:
        effective = _max_tier(effective, cascade_floor)

    if _tier_rank(effective) == _tier_rank(declared) and effective == declared:
        floor_source = "declared"
    elif cascade_floor is not None and effective == cascade_floor and effective != declared:
        floor_source = "closure"
    else:
        floor_source = "policy_floor"

    tier_block = _mapping_at(risk_policy, effective)
    requirements: list[_RiskTierRequirement] = []
    if tier_block.get("require_review") is True:
        requirements.append(
            _RiskTierRequirement("require_review", bool(_resolved_review_gates(spec)))
        )
    if tier_block.get("require_moderation") is True:
        requirements.append(
            _RiskTierRequirement("require_moderation", not _activities_missing_moderation(spec))
        )
    if tier_block.get("require_redaction") is True:
        requirements.append(_RiskTierRequirement("require_redaction", _redaction_enabled(spec)))
    if tier_block.get("require_payload_codec") is True:
        requirements.append(
            _RiskTierRequirement("require_payload_codec", _payload_codec_configured(spec))
        )
    if tier_block.get("require_compensation") is True:
        requirements.append(
            _RiskTierRequirement(
                "require_compensation", not _side_effecting_steps_missing_compensation(spec)
            )
        )
    constrain_providers = _mapping_at_or_none(tier_block, "constrain_providers")
    if constrain_providers is not None:
        # D300-6: the SAME mapping shape + the EXACT predicate as `providers.allowed`
        # (provider AND model), so tier-level and policy-level provider constraints can
        # never drift. `provider.model` is materialized at load (ProviderSpec), so this
        # evaluates the concrete model the runtime will use.
        provider = spec.runtime.provider
        failure = _provider_model_policy_failure(
            {"providers": {"allowed": constrain_providers}},
            provider_name=provider.type,
            provider_model=provider.model,
        )
        requirements.append(_RiskTierRequirement("constrain_providers", failure is None))

    return RiskTierEvaluation(
        declared=declared,
        floor=floor,
        effective=effective,
        floor_source=floor_source,
        undeclared=undeclared,
        require_declared=risk_policy.get("require_declared") is True,
        denied=effective == "prohibited",
        requirements=tuple(requirements),
    )


def _risk_tier_cascade(
    *,
    parent_workflow_id: str,
    parent_eval: RiskTierEvaluation,
    member_risk_tiers: Mapping[str, str],
    spec: TypefluxYamlSpec,
    payload: Mapping[str, Any],
) -> RiskTierCascade | None:
    """Compute the sub-workflow closure LIFT of a parent's effective tier (#300 D300-3).

    The parent's effective tier maxes over every closure member's; when the strictest
    member exceeds the parent's own declared+floor effective, the parent is re-evaluated
    at that lifted tier (its macro requirements re-expanded via ``evaluate_risk_tier``).
    Returns ``None`` when no member lifts the parent. ONE source of truth for both the
    closure admission check and the bundle risk-tier projection.
    """
    lifting_member = max(
        (m for m in member_risk_tiers if m != parent_workflow_id),
        key=lambda m: _tier_rank(member_risk_tiers[m]),
        default=None,
    )
    if lifting_member is None:
        return None
    cascade_floor = member_risk_tiers[lifting_member]
    if _tier_rank(cascade_floor) <= _tier_rank(parent_eval.effective):
        return None
    lifted = evaluate_risk_tier(spec, payload, cascade_floor=cascade_floor)
    assert lifted is not None  # risk_tiers present ⇒ non-None
    return RiskTierCascade(
        lifted_by=lifting_member,
        parent_effective=parent_eval.effective,
        evaluation=lifted,
    )


def _closure_member_effective_tiers(
    project: TypefluxProjectSpec,
    *,
    resolved: ProjectResolvedWorkflow,
    policy: ComposedProjectPolicy,
    environment_id: str,
) -> dict[str, str]:
    """The effective risk tier of every sub-workflow closure member (#300 D300-3), keyed by
    workflow id. Consumes the SAME ``_iter_subworkflow_closure`` walk as the closure
    admission check, so the bundle projection and admission agree on the cascade; a
    dangling reference is skipped here (it fails the closure check on its own path, not
    this read projection)."""
    member_risk_tiers: dict[str, str] = {}
    parent = evaluate_risk_tier(resolved.spec, policy.payload)
    if parent is not None:
        member_risk_tiers[resolved.workflow_id] = parent.effective
    for ref, child, _error in _iter_subworkflow_closure(
        project, resolved=resolved, environment_id=environment_id
    ):
        if child is None:
            continue
        child_risk = evaluate_risk_tier(child.spec, policy.payload)
        if child_risk is not None:
            member_risk_tiers[ref] = child_risk.effective
    return member_risk_tiers


def evaluate_workflow_risk_tier(
    project: TypefluxProjectSpec,
    *,
    resolved: ProjectResolvedWorkflow,
    policy: ComposedProjectPolicy,
    environment_id: str,
) -> RiskTierPosture | None:
    """The workflow's full risk-tier posture for surfacing (#300 slice 2).

    Its own ``base`` evaluation (``evaluate_risk_tier``) plus the sub-workflow closure
    ``cascade`` lift (``_risk_tier_cascade``) when a higher-tier child raises it. Returns
    ``None`` when the composed policy declares no ``risk_tiers`` dimension. Shares the
    evaluation and cascade helpers with admission, so the bundle surfaces the SAME result
    the ``policy_risk_tier`` / ``policy_subworkflow_closure`` checks compute — never a
    second evaluation path.
    """
    base = evaluate_risk_tier(resolved.spec, policy.payload)
    if base is None:
        return None
    member_risk_tiers = _closure_member_effective_tiers(
        project, resolved=resolved, policy=policy, environment_id=environment_id
    )
    cascade = _risk_tier_cascade(
        parent_workflow_id=resolved.workflow_id,
        parent_eval=base,
        member_risk_tiers=member_risk_tiers,
        spec=resolved.spec,
        payload=policy.payload,
    )
    return RiskTierPosture(base=base, cascade=cascade)


def _validate_risk_tier(
    resolved: ProjectResolvedWorkflow,
    payload: Mapping[str, Any],
) -> ProjectValidationCheck:
    """Risk-tier admission (#300, code ``policy_risk_tier``), fail-closed.

    Effective tier = ``max(declared-or-safe, min_tier floor)``; ``require_declared``
    rejects an undeclared workflow; ``effective == prohibited`` denies outright;
    otherwise the effective tier's block expands into the existing predicates and any
    unsatisfied requirement fails. The closure cascade (a parent lifted by a higher-tier
    child) is enforced separately by ``validate_subworkflow_closure_policy``.
    """
    evaluation = evaluate_risk_tier(resolved.spec, payload)
    if evaluation is None:
        return _skipped_check("policy_risk_tier", "policy does not constrain risk tiers")
    details: dict[str, Any] = {
        "declared": evaluation.declared,
        "floor": evaluation.floor,
        "effective": evaluation.effective,
        "floor_source": evaluation.floor_source,
        "requirements": evaluation.requirement_details(),
    }
    failures = _risk_tier_failures(evaluation)
    if failures:
        return ProjectValidationCheck(
            code="policy_risk_tier",
            status="failed",
            message="; ".join(failures),
            details=details,
        )
    return _passed_check("policy_risk_tier", details=details)


def _risk_tier_failures(
    evaluation: RiskTierEvaluation,
    *,
    include_require_declared: bool = True,
) -> list[str]:
    failures: list[str] = []
    # The cascade path suppresses this: the parent's undeclared state is already
    # reported by its own ``policy_risk_tier`` check, so the cascade re-report is noise.
    if include_require_declared and evaluation.require_declared and evaluation.undeclared:
        failures.append(
            "workflow must declare workflow.risk_tier (required by selected project policy)"
        )
    if evaluation.denied:
        failures.append(
            f"risk tier {evaluation.effective!r} denies admission "
            f"(effective tier is prohibited; floor_source={evaluation.floor_source})"
        )
    for name in evaluation.unsatisfied_controls():
        failures.append(
            f"risk tier {evaluation.effective!r} requires {name} "
            f"(unsatisfied; floor_source={evaluation.floor_source})"
        )
    return failures


# ── shared control predicates (reused by the standalone checks AND the risk-tier
#    macro, so a tier expands into the SAME logic — one source of truth per control) ──


def _resolved_review_gates(spec: TypefluxYamlSpec) -> list[Any]:
    # Only an ENABLED lifecycle attaches review gates to the generated workflow
    # (identity.py `_lifecycle_payload` keys on `lifecycle.enabled` for the same
    # reason) — a review block under a disabled/omitted lifecycle is inert and must
    # NOT satisfy require_review / require_review_routes (it would fail open: the
    # policy reads "reviewed" while no review ever executes). TS `enabledReviewGates`
    # applies the identical guard.
    lifecycle = spec.workflow.lifecycle
    if lifecycle is None or not lifecycle.enabled:
        return []
    return lifecycle.resolved_gates()


def _activities_missing_moderation(spec: TypefluxYamlSpec) -> list[str]:
    return [
        definition.name
        for definition in spec.activities.definitions
        if definition.moderation is None
    ]


def _redaction_enabled(spec: TypefluxYamlSpec) -> bool:
    return spec.runtime.observability.redaction.enabled


def _payload_codec_configured(spec: TypefluxYamlSpec) -> bool:
    return spec.runtime.temporal.payload_codec is not None


def _missing_custom_redaction_rules(
    spec: TypefluxYamlSpec,
    required: Sequence[str],
) -> list[str]:
    present = {rule.name for rule in spec.runtime.observability.redaction.custom_rules}
    return [name for name in required if name not in present]


def _side_effecting_steps_missing_compensation(spec: TypefluxYamlSpec) -> list[str]:
    """Step ids that invoke a ``side_effecting`` activity yet declare no ``compensate:``
    (#299 D299-5, the ``require_compensation`` predicate).

    ``side_effecting`` is an author declaration on an inline activity DEFINITION; a STEP
    is side-effecting when it invokes such an activity — a plain ``activity`` step or a
    ``map`` fanning that activity (both invoke the activity and both accept ``compensate:``;
    a map over a side-effecting activity is if anything MORE side-effecting, once per item).
    Sub-workflow steps invoke a child workflow, not an activity, so they are out of scope
    here — a child's own side effects are governed inside the child, and the closure cascade
    lifts a ``require_compensation`` tier through sub-workflows exactly like the other
    requirements. Walks the full step tree (recursing into parallel branches). Empty list ⇒
    satisfied; module-loaded activities cannot be declared side-effecting in YAML (like
    ``moderation``, the flag lives only on inline definitions), so they never trip this.
    """
    side_effecting = {
        definition.name
        for definition in spec.activities.definitions
        if getattr(definition, "side_effecting", False)
    }
    if not side_effecting:
        return []
    return _iter_side_effecting_missing(spec.workflow.steps, side_effecting)


def _iter_side_effecting_missing(steps: Iterable[Any], side_effecting: set[str]) -> list[str]:
    from typeflux.yaml.spec import (
        WorkflowMapStepSpec,
        WorkflowParallelStepSpec,
        WorkflowStepSpec,
    )

    missing: list[str] = []
    for step in steps:
        if isinstance(step, WorkflowStepSpec):
            if step.activity in side_effecting and step.compensate is None:
                missing.append(step.id)
        elif isinstance(step, WorkflowMapStepSpec):
            if (
                step.map.activity is not None
                and step.map.activity in side_effecting
                and step.compensate is None
            ):
                missing.append(step.id)
        elif isinstance(step, WorkflowParallelStepSpec):
            for branch in step.parallel.branches:
                missing.extend(_iter_side_effecting_missing(branch.steps, side_effecting))
    return missing


def _string_or(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _mapping_at(value: Mapping[str, Any] | object, *path: str) -> dict[str, Any]:
    current: object = value
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return dict(current) if isinstance(current, Mapping) else {}


def _mapping_at_or_none(value: Mapping[str, Any] | object, *path: str) -> dict[str, Any] | None:
    current: object = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return dict(current) if isinstance(current, Mapping) else None


def _list_at(value: Mapping[str, Any] | object, *path: str) -> list[Any]:
    current: object = value
    for key in path:
        if not isinstance(current, Mapping):
            return []
        current = current.get(key)
    return list(current) if isinstance(current, list) else []


def _list_at_or_none(value: Mapping[str, Any] | object, *path: str) -> list[Any] | None:
    current: object = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return list(current) if isinstance(current, list) else None


def _provider_model_policy_failure(
    payload: Mapping[str, Any],
    *,
    provider_name: str,
    provider_model: str | None,
) -> str | None:
    allowed = _mapping_at_or_none(payload, "providers", "allowed")
    if allowed is None:
        return None
    provider_policy = allowed.get(provider_name)
    if provider_policy is None:
        return f"provider {provider_name!r} is not allowed by selected project policy"
    models = _list_at_or_none(provider_policy, "models")
    if models is not None and provider_model not in models:
        return f"provider model {provider_model!r} is not allowed for provider {provider_name!r}"
    return None


def _policy_failure_message(failures: Sequence[ProjectValidationCheck]) -> str:
    parts = []
    for check in failures:
        message = check.message or f"policy check failed: {check.code}"
        parts.append(f"{check.code}: {message}")
    return "project policy enforcement failed: " + "; ".join(parts)


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _module_in_any_root(module_name: str, roots: Sequence[str]) -> bool:
    return any(_module_in_root(module_name, root) for root in roots)


def _module_in_root(module_name: str, root: str) -> bool:
    return module_name == root or module_name.startswith(f"{root}.")


def _media_constraint_allowed(candidate: str, allowed: Sequence[Any]) -> bool:
    return any(
        isinstance(pattern, str) and _media_type_matches(candidate, pattern) for pattern in allowed
    )


def _media_type_matches(candidate: str, pattern: str) -> bool:
    if pattern == "*/*":
        return True
    if candidate == pattern:
        return True
    if pattern.endswith("/*") and not candidate.endswith("/*"):
        return candidate.startswith(pattern[:-1])
    if candidate.endswith("/*") and pattern.endswith("/*"):
        return candidate == pattern
    return False


def _passed_check(code: str, *, details: dict[str, Any] | None = None) -> ProjectValidationCheck:
    return ProjectValidationCheck(code=code, status="passed", details=details or {})


def _failed_check(code: str, message: str) -> ProjectValidationCheck:
    return ProjectValidationCheck(code=code, status="failed", message=message)


def _skipped_check(code: str, message: str) -> ProjectValidationCheck:
    return ProjectValidationCheck(code=code, status="skipped", message=message)


__all__ = [
    "ProjectPolicyEnforcementError",
    "RiskTierCascade",
    "RiskTierEvaluation",
    "RiskTierPosture",
    "RuntimePolicyGuard",
    "build_project_policy_runtime_guard",
    "evaluate_risk_tier",
    "evaluate_workflow_risk_tier",
    "select_project_policy_ids_for_workflow",
    "validate_project_policy",
    "validate_subworkflow_closure_policy",
]
