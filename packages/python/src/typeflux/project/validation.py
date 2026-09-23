from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from typeflux.execution.starter import _build_workflow_manifest
from typeflux.project.environment import (
    ProjectResolvedWorkflow,
    create_workflow_with_subworkflows,
    project_environment_context,
    resolve_project_workflow,
)
from typeflux.project.loader import validate_project
from typeflux.project.policy import compose_project_policies
from typeflux.project.policy_enforcement import (
    max_declared_closure_risk_tier,
    risk_tier_enforcement_gap,
    select_project_policy_ids_for_workflow,
    validate_project_policy,
    validate_subworkflow_closure_policy,
    workflow_bound_in_other_environments,
)
from typeflux.project.spec import (
    ProjectResolvedWorkflowValidation,
    ProjectValidationCheck,
    ProjectValidationIssue,
    ProjectValidationReport,
    TypefluxProjectSpec,
)
from typeflux.yaml.imports import collect_activities, validate_extension_imports
from typeflux.yaml.runtime import _yaml_metadata_contributors
from typeflux.yaml.secrets import SecretValueSpec, resolve_optional_secret_text


def validate_project_bundle(
    project: TypefluxProjectSpec,
    *,
    environment_id: str | None = None,
    workflow_ids: Sequence[str] = (),
    policy_ids: Sequence[str] = (),
) -> ProjectValidationReport:
    """Validate project references and optionally resolved environment/workflow bundles."""

    reference_report = validate_project(project)
    if environment_id is None and not workflow_ids and not policy_ids:
        return reference_report

    issues = list(reference_report.issues)
    resolved_workflows: list[ProjectResolvedWorkflowValidation] = []

    if environment_id is None:
        required_flags = []
        if workflow_ids:
            required_flags.append("--workflow")
        if policy_ids:
            required_flags.append("--policy")
        prefix = " and ".join(required_flags) or "resolved project validation"
        issues.append(
            ProjectValidationIssue(
                code="validation_environment_required",
                message=f"{prefix} requires --environment for resolved project validation",
            )
        )
        return _report_with_resolved_checks(
            reference_report,
            issues=issues,
            resolved_workflows=resolved_workflows,
        )

    if environment_id not in project.environments:
        issues.append(
            ProjectValidationIssue(
                code="unknown_validation_environment",
                message=f"unknown project environment: {environment_id}",
                reference=environment_id,
            )
        )

    selected_workflow_ids = tuple(workflow_ids) or tuple(
        workflow.id for workflow in project.workflows
    )
    known_workflow_ids = {workflow.id for workflow in project.workflows}
    known_policy_ids = set(project.policies)
    for workflow_id in selected_workflow_ids:
        if workflow_id not in known_workflow_ids:
            issues.append(
                ProjectValidationIssue(
                    code="unknown_validation_workflow",
                    message=f"unknown project workflow: {workflow_id}",
                    reference=workflow_id,
                )
            )
    for policy_id in policy_ids:
        if policy_id not in known_policy_ids:
            issues.append(
                ProjectValidationIssue(
                    code="unknown_validation_policy",
                    message=f"unknown project policy: {policy_id}",
                    reference=policy_id,
                )
            )

    if reference_report.issues or any(
        issue.code
        in {
            "unknown_validation_environment",
            "unknown_validation_workflow",
            "unknown_validation_policy",
        }
        for issue in issues
    ):
        return _report_with_resolved_checks(
            reference_report,
            issues=issues,
            resolved_workflows=resolved_workflows,
        )

    for workflow_id in selected_workflow_ids:
        validation = _validate_resolved_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=select_project_policy_ids_for_workflow(
                project,
                environment_id=environment_id,
                workflow_id=workflow_id,
                explicit_policy_ids=tuple(policy_ids),
            ),
        )
        resolved_workflows.append(validation)
        issues.extend(
            _issue_from_failed_check(
                check,
                workflow_id=workflow_id,
                environment_id=environment_id,
                path=validation.workflow_path,
            )
            for check in validation.checks
            if check.status == "failed"
        )

    return _report_with_resolved_checks(
        reference_report,
        issues=issues,
        resolved_workflows=resolved_workflows,
    )


def _validate_resolved_workflow(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    policy_ids: Sequence[str],
) -> ProjectResolvedWorkflowValidation:
    checks: list[ProjectValidationCheck] = []
    try:
        resolved = resolve_project_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
        )
    except Exception as exc:  # noqa: BLE001 - report project validation context.
        checks.append(
            _failed_check(
                "environment_workflow_resolution",
                f"failed to resolve project environment/workflow bundle: {exc}",
            )
        )
        return ProjectResolvedWorkflowValidation(
            workflow_id=workflow_id,
            environment_id=environment_id,
            ok=False,
            checks=tuple(checks),
        )

    checks.append(
        _passed_check(
            "environment_workflow_resolution",
            details={
                "environment_profile_path": str(resolved.environment.profile_path),
                "workflow_path": str(resolved.workflow_path),
            },
        )
    )
    checks.append(
        _passed_check(
            "observability_config",
            details={
                "type": resolved.spec.runtime.observability.type,
                "execution_manifest": resolved.spec.runtime.observability.execution_manifest,
            },
        )
    )

    activities = None
    workflow_class = None
    with project_environment_context(resolved.application):
        tls_check = _temporal_tls_invariant_check(resolved.spec)
        if tls_check is not None:
            checks.append(tls_check)
        codec_check = _payload_codec_presence_check(resolved.spec)
        if codec_check is not None:
            checks.append(codec_check)
        policy_failed = False
        composed_policy = None
        composition_failed = False
        if policy_ids:
            try:
                policy = compose_project_policies(project, tuple(policy_ids))
            except Exception as exc:  # noqa: BLE001 - report validation context.
                policy_failed = True
                composition_failed = True
                checks.append(_failed_check("policy_composition", str(exc)))
            else:
                composed_policy = policy
                policy_checks = validate_project_policy(
                    project=project,
                    resolved=resolved,
                    policy=policy,
                )
                checks.extend(policy_checks)
                policy_failed = any(check.status == "failed" for check in policy_checks)
                # Transitive-closure admission (#55 §9): validate the parent's referenced
                # sub-workflows against the parent's composed policy. Emitted only for a
                # workflow with >= 1 sub-workflow reference (V1 validate output unchanged);
                # a child violation fails the parent's admission.
                closure_check = validate_subworkflow_closure_policy(
                    project=project,
                    resolved=resolved,
                    policy=policy,
                    environment_id=environment_id,
                )
                if closure_check is not None:
                    checks.append(closure_check)
                    policy_failed = policy_failed or closure_check.status == "failed"
        else:
            checks.append(
                _skipped_check(
                    "policy_enforcement",
                    "no project policies selected for this workflow/environment",
                )
            )

        risk_tier_check = _risk_tier_binding_check(
            project,
            resolved=resolved,
            environment_id=environment_id,
            policy_ids=tuple(policy_ids),
            policy=composed_policy,
            composition_failed=composition_failed,
        )
        checks.append(risk_tier_check)
        policy_failed = policy_failed or risk_tier_check.status == "failed"

        try:
            validate_extension_imports(resolved.spec)
        except Exception as exc:  # noqa: BLE001 - report validation context.
            checks.append(_failed_check("provider_import_policy", str(exc)))
        else:
            checks.append(_passed_check("provider_import_policy"))

        try:
            activities = collect_activities(resolved.spec)
        except Exception as exc:  # noqa: BLE001 - report validation context.
            checks.append(_failed_check("activity_imports", str(exc)))
        else:
            checks.append(
                _passed_check(
                    "activity_imports",
                    details={"activity_count": len(activities)},
                )
            )

        if activities is None:
            checks.append(
                _skipped_check(
                    "workflow_graph",
                    "skipped because activity imports failed",
                )
            )
        elif policy_failed:
            checks.append(
                _skipped_check(
                    "workflow_graph",
                    "skipped because policy enforcement failed",
                )
            )
        else:
            try:
                workflow_class, _subworkflows, _activities = create_workflow_with_subworkflows(
                    project, resolved, activities=activities
                )
            except Exception as exc:  # noqa: BLE001 - report validation context.
                checks.append(_failed_check("workflow_graph", str(exc)))
            else:
                checks.append(
                    _passed_check(
                        "workflow_graph",
                        details={"step_count": len(resolved.spec.workflow.steps)},
                    )
                )

        # Sub-workflow visibility notice (#55 §6 mitigation b): emitted for workflows
        # with >= 1 direct sub-workflow reference ONLY (V1 validate output stays
        # byte-identical), immediately after workflow_graph (cross-edition golden
        # order — the TS edition emits the byte-identical check).
        visibility = _subworkflow_visibility_check(resolved.spec)
        if visibility is not None:
            checks.append(visibility)

        if policy_failed:
            checks.append(
                _skipped_check(
                    "execution_manifest",
                    "skipped because policy enforcement failed",
                )
            )
        elif activities is None or workflow_class is None:
            checks.append(
                _skipped_check(
                    "execution_manifest",
                    "skipped because workflow graph validation failed",
                )
            )
        elif not resolved.spec.runtime.observability.execution_manifest:
            checks.append(
                _skipped_check(
                    "execution_manifest",
                    "runtime.observability.execution_manifest is disabled",
                )
            )
        else:
            checks.extend(_validate_execution_manifest(resolved, activities))

    return ProjectResolvedWorkflowValidation(
        workflow_id=workflow_id,
        environment_id=environment_id,
        ok=not any(check.status == "failed" for check in checks),
        workflow_path=str(resolved.workflow_path),
        environment_profile_path=str(resolved.environment.profile_path),
        yaml_project=resolved.spec.project,
        yaml_name=resolved.spec.name,
        workflow_name=resolved.spec.workflow.name,
        task_queue=resolved.spec.task_queue,
        checks=tuple(checks),
    )


#: The #55 §6 (mitigation b) visibility notice — cross-edition EXACT text (the TS
#: edition emits the byte-identical check; it lands in the conformance validate golden).
SUBWORKFLOW_VISIBILITY_NOTICE = (
    "workflow.version is declared but runtime.temporal.workflow_search_attribute is "
    "not configured; wide sub-workflow fan-outs degrade the frozen-version scan — "
    "configure the search attribute (#55)"
)


def _temporal_tls_invariant_check(spec: Any) -> ProjectValidationCheck | None:
    """#796: the api-key-requires-TLS invariant at ONE stage regardless of authoring shape.

    A literal key with ``tls: false`` fails at YAML load; a ``value_from`` reference used to
    be checked only at client connect. Emitted ONLY for that reference+tls-disabled shape
    (existing validate outputs are unchanged): the reference is resolved here when its
    source is visible to validate — a resolving credential FAILS exactly like the literal
    would have — and an unresolvable source is an explicit deferred notice, never silence.
    Cross-edition EXACT text (the TS edition emits the byte-identical check)."""
    temporal = spec.runtime.temporal
    if not isinstance(temporal.api_key, SecretValueSpec) or temporal.tls is not False:
        return None
    try:
        resolved_key = resolve_optional_secret_text(
            temporal.api_key, runtime_path="runtime.temporal.api_key"
        )
    except ValueError:
        resolved_key = None  # Required source absent here — may still resolve at runtime.
    if resolved_key:
        return _failed_check(
            "temporal_tls_invariant",
            "runtime.temporal.api_key resolves to a credential but runtime.temporal.tls is "
            "disabled — the api-key-requires-TLS invariant fails (the literal form fails at "
            "YAML load; the reference form is enforced here and at client connect)",
        )
    return _skipped_check(
        "temporal_tls_invariant",
        "TLS invariant deferred: runtime.temporal.api_key is a value_from reference whose "
        "source is not resolvable at validate time; enforced at client connect",
    )


def _payload_codec_presence_check(spec: Any) -> ProjectValidationCheck | None:
    """#797: wherever a deferred codec presence check is in play, say what ``required:
    false`` does NOT defer. Emitted ONLY when the codec declares such a key (existing
    validate outputs unchanged); always ``passed`` — this is a semantics notice, not a
    finding. Cross-edition EXACT details (the TS edition emits the byte-identical check)."""
    codec = spec.runtime.temporal.payload_codec
    if codec is None:
        return None
    deferred = [key.id for key in codec.keys if key.value_from.required is False]
    if not deferred:
        return None
    return _passed_check(
        "payload_codec_presence",
        details={
            "deferred_keys": deferred,
            "runtime_behavior": (
                "required: false defers only the offline presence check — the codec always "
                "fail-closes at runtime on an unset or invalid key"
            ),
        },
    )


def _subworkflow_visibility_check(spec: Any) -> ProjectValidationCheck | None:
    """The ``subworkflow_visibility`` check (#55 §6 mitigation b): always present —
    and always ``passed`` (advisory, never a rejection) — for a workflow with >= 1
    direct sub-workflow reference; ``None`` (nothing emitted) otherwise. The
    ``notice`` detail appears ONLY when a frozen ``workflow.version`` label is
    declared without a configured search attribute: the frozen-version scan then
    pages through every child fan-out row."""
    from typeflux.yaml.workflow import collect_subworkflow_references

    if not collect_subworkflow_references(spec):
        return None
    search_attribute = spec.runtime.temporal.workflow_search_attribute
    version = spec.workflow.version
    details: dict[str, Any] = {
        "search_attribute_configured": search_attribute is not None,
        "workflow_version": version,
    }
    if version is not None and search_attribute is None:
        details["notice"] = SUBWORKFLOW_VISIBILITY_NOTICE
    return _passed_check("subworkflow_visibility", details=details)


def _validate_execution_manifest(
    resolved: ProjectResolvedWorkflow,
    activities: dict[str, Any],
) -> tuple[ProjectValidationCheck, ...]:
    try:
        manifest = _build_workflow_manifest(
            workflow_name=resolved.spec.workflow.name,
            workflow_id=f"typeflux-validate-{resolved.workflow_id}",
            task_queue=resolved.spec.task_queue,
            activities=tuple(activities.values()),
            metadata_contributors=_yaml_metadata_contributors(resolved.spec),
        )
    except Exception as exc:  # noqa: BLE001 - report validation context.
        return (_failed_check("execution_manifest", str(exc)),)
    return (
        _passed_check(
            "execution_manifest",
            details={
                "activity_count": len(manifest.activities),
                "map_step_count": len(manifest.map_steps),
            },
        ),
    )


def _report_with_resolved_checks(
    reference_report: ProjectValidationReport,
    *,
    issues: list[ProjectValidationIssue],
    resolved_workflows: list[ProjectResolvedWorkflowValidation],
) -> ProjectValidationReport:
    return reference_report.model_copy(
        update={
            "ok": not issues,
            "issues": tuple(issues),
            "resolved_workflows": tuple(resolved_workflows),
        }
    )


def _issue_from_failed_check(
    check: ProjectValidationCheck,
    *,
    workflow_id: str,
    environment_id: str,
    path: str | None,
) -> ProjectValidationIssue:
    return ProjectValidationIssue(
        code=f"resolved_{check.code}_failed",
        message=check.message or f"resolved check failed: {check.code}",
        reference=f"{environment_id}:{workflow_id}",
        path=path,
    )


def _risk_tier_binding_check(
    project: TypefluxProjectSpec,
    *,
    resolved: ProjectResolvedWorkflow,
    environment_id: str,
    policy_ids: tuple[str, ...],
    policy: Any,
    composition_failed: bool,
) -> ProjectValidationCheck:
    """The validation face of the #788 fail-close (audit B1), sharing
    ``risk_tier_enforcement_gap`` with the runtime guard so the two can never disagree.

    One deliberate softening the guard does not have: a workflow whose elevated tier is
    unbound HERE but bound (with policies) in another environment's target is the normal
    multi-environment shape — validating every workflow under one environment must not
    fail prod-only workflows. That case reports ``skipped`` naming the environments;
    actually STARTING the workflow in this environment still fails closed at guard
    build."""
    if composition_failed:
        return _skipped_check("risk_tier_binding", "skipped because policy composition failed")
    gap = risk_tier_enforcement_gap(
        project,
        resolved=resolved,
        environment_id=environment_id,
        selected_policy_ids=policy_ids,
        policy=policy,
    )
    if gap is None:
        declared = max_declared_closure_risk_tier(
            project, resolved=resolved, environment_id=environment_id
        )
        return _passed_check(
            "risk_tier_binding",
            details={"declared": declared or "safe", "policies": list(policy_ids)},
        )
    if not policy_ids:
        elsewhere = workflow_bound_in_other_environments(
            project, workflow_id=resolved.workflow_id, environment_id=environment_id
        )
        if elsewhere:
            return _skipped_check(
                "risk_tier_binding",
                "elevated risk tier is bound only in other environments "
                f"({', '.join(elsewhere)}) — not validated here; starting this workflow in "
                f"{environment_id!r} fails closed at guard build.",
            )
    return _failed_check("risk_tier_binding", gap)


def _passed_check(code: str, *, details: dict[str, Any] | None = None) -> ProjectValidationCheck:
    return ProjectValidationCheck(
        code=code,
        status="passed",
        details=details or {},
    )


def _failed_check(code: str, message: str) -> ProjectValidationCheck:
    return ProjectValidationCheck(
        code=code,
        status="failed",
        message=message,
    )


def _skipped_check(code: str, message: str) -> ProjectValidationCheck:
    return ProjectValidationCheck(
        code=code,
        status="skipped",
        message=message,
    )


__all__ = ["validate_project_bundle"]
