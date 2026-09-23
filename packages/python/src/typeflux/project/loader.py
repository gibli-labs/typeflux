from __future__ import annotations

from pathlib import Path
from typing import Any

from typeflux.project.spec import (
    ProjectValidationIssue,
    ProjectValidationReport,
    ProjectWorkflowSpec,
    ProjectWorkflowSummary,
    TypefluxProjectSpec,
)
from typeflux.yaml.loader import load_yaml_spec, strict_safe_load
from typeflux.yaml.spec import TypefluxYamlSpec
from typeflux.yaml.workflow import validate_unique_yaml_workflow_names


def load_project_spec(path: str | Path) -> TypefluxProjectSpec:
    project_path = Path(path).expanduser().resolve()
    raw = _load_yaml_mapping(project_path, kind="Typeflux project spec")
    return TypefluxProjectSpec.model_validate(raw).model_copy(
        update={"manifest_path": project_path}
    )


def discover_project_workflows(project: TypefluxProjectSpec) -> tuple[ProjectWorkflowSpec, ...]:
    return tuple(
        workflow.model_copy(update={"resolved_path": _workflow_path(project, workflow)})
        for workflow in project.workflows
    )


def load_project_workflow_specs(project: TypefluxProjectSpec) -> tuple[TypefluxYamlSpec, ...]:
    specs = tuple(
        load_yaml_spec(_required_workflow_path(workflow))
        for workflow in discover_project_workflows(project)
    )
    validate_unique_yaml_workflow_names(specs)
    return specs


def validate_project(project: TypefluxProjectSpec) -> ProjectValidationReport:
    issues: list[ProjectValidationIssue] = []
    workflow_summaries: list[ProjectWorkflowSummary] = []
    loaded_specs: list[TypefluxYamlSpec] = []

    for workflow in discover_project_workflows(project):
        workflow_path = _required_workflow_path(workflow)
        if not workflow_path.exists():
            issues.append(
                _issue(
                    "missing_workflow_file",
                    f"workflow {workflow.id!r} file does not exist: {workflow_path}",
                    reference=workflow.id,
                    path=workflow_path,
                )
            )
            continue
        try:
            spec = load_yaml_spec(workflow_path)
        except Exception as exc:  # noqa: BLE001 - report validation context.
            issues.append(
                _issue(
                    "invalid_workflow_yaml",
                    f"workflow {workflow.id!r} failed to load: {exc}",
                    reference=workflow.id,
                    path=workflow_path,
                )
            )
            continue
        loaded_specs.append(spec)
        workflow_summaries.append(
            ProjectWorkflowSummary(
                id=workflow.id,
                path=str(workflow_path),
                yaml_project=spec.project,
                yaml_name=spec.name,
                workflow_name=spec.workflow.name,
                task_queue=spec.task_queue,
            )
        )

    if loaded_specs:
        try:
            validate_unique_yaml_workflow_names(tuple(loaded_specs))
        except Exception as exc:  # noqa: BLE001 - report validation context.
            issues.append(
                ProjectValidationIssue(
                    code="duplicate_workflow_name",
                    message=str(exc),
                )
            )

    _validate_reference_files(
        project,
        "environment",
        project.environments,
        issues,
        validate_environment_schema=True,
    )
    _validate_reference_files(project, "policy", project.policies, issues)
    _validate_profiles(project, issues)
    _validate_targets(project, issues)
    _validate_policy_compositions(project, issues)
    _validate_annotations(project, issues)

    return ProjectValidationReport(
        project_name=project.name,
        manifest_path=str(project.manifest_path),
        ok=not issues,
        issues=tuple(issues),
        workflows=tuple(workflow_summaries),
    )


def _workflow_path(project: TypefluxProjectSpec, workflow: ProjectWorkflowSpec) -> Path:
    if workflow.path is not None:
        return _resolve_project_path(project, workflow.path)
    if workflow.directory is None:
        raise ValueError("workflow must configure path or directory")
    return _resolve_project_path(project, workflow.directory) / project.defaults.workflow_filename


def _required_workflow_path(workflow: ProjectWorkflowSpec) -> Path:
    if workflow.resolved_path is None:
        raise ValueError(f"workflow {workflow.id!r} has not been discovered")
    return workflow.resolved_path


def _validate_profiles(
    project: TypefluxProjectSpec,
    issues: list[ProjectValidationIssue],
) -> None:
    from typeflux.project.profiles import (
        PROFILE_KINDS,
        load_project_profile,
        validate_profile_selection,
    )

    declared: dict[str, dict[str, str]] = {}
    if project.profiles is not None:
        declared = {kind: getattr(project.profiles, kind) for kind in PROFILE_KINDS}
    for kind, refs in declared.items():
        for profile_id in refs:
            try:
                load_project_profile(project, kind=kind, profile_id=profile_id)
            except Exception as exc:  # noqa: BLE001 - report validation context.
                issues.append(
                    _issue(
                        "invalid_component_profile",
                        f"{kind} profile {profile_id!r} failed to load: {exc}",
                        reference=profile_id,
                        path=project.manifest_path,
                    )
                )

    def _check_selection(selection: dict[str, str], *, context: str, reference: str) -> None:
        try:
            validate_profile_selection(selection, context=context)
        except Exception as exc:  # noqa: BLE001 - report validation context.
            issues.append(
                _issue(
                    "invalid_profile_selection",
                    str(exc),
                    reference=reference,
                    path=project.manifest_path,
                )
            )
            return
        for kind, profile_id in selection.items():
            if profile_id not in declared.get(kind, {}):
                issues.append(
                    _issue(
                        "unknown_profile_reference",
                        f"{context} selects unknown {kind} profile: {profile_id}",
                        reference=reference,
                        path=project.manifest_path,
                    )
                )

    for workflow in project.workflows:
        _check_selection(
            dict(workflow.profiles),
            context=f"workflow {workflow.id!r} profile selection",
            reference=workflow.id,
        )
    from typeflux.project.environment import load_project_environment

    for environment_id in project.environments:
        try:
            environment = load_project_environment(project, environment_id)
        except Exception:  # noqa: BLE001 - environment schema issues report separately.
            continue
        for workflow_id, env_workflow in environment.workflows.items():
            if not env_workflow.profiles:
                continue
            _check_selection(
                dict(env_workflow.profiles),
                context=(
                    f"environment {environment_id!r} profile selection for workflow {workflow_id!r}"
                ),
                reference=environment_id,
            )


def _validate_reference_files(
    project: TypefluxProjectSpec,
    kind: str,
    refs: dict[str, str],
    issues: list[ProjectValidationIssue],
    *,
    validate_environment_schema: bool = False,
) -> None:
    for ref_id, raw_path in refs.items():
        ref_path = _resolve_project_path(project, raw_path)
        if not ref_path.exists():
            issues.append(
                _issue(
                    f"missing_{kind}_file",
                    f"{kind} {ref_id!r} file does not exist: {ref_path}",
                    reference=ref_id,
                    path=ref_path,
                )
            )
            continue
        try:
            if kind == "policy":
                from typeflux.project.policy import load_project_policy

                load_project_policy(project, ref_id)
            else:
                _load_yaml_mapping(ref_path, kind=f"{kind} reference")
            if validate_environment_schema:
                from typeflux.project.environment import (
                    build_project_environment_application,
                    load_project_environment,
                )

                environment = load_project_environment(project, ref_id)
                build_project_environment_application(environment, environment_id=ref_id)
        except Exception as exc:  # noqa: BLE001 - report validation context.
            issues.append(
                _issue(
                    f"invalid_{kind}_yaml",
                    f"{kind} {ref_id!r} failed to load as a YAML mapping: {exc}",
                    reference=ref_id,
                    path=ref_path,
                )
            )


def _validate_annotations(
    project: TypefluxProjectSpec,
    issues: list[ProjectValidationIssue],
) -> None:
    # The insight-acknowledgement annotations file (#733) is parsed fail-closed: a
    # malformed/unparseable file is an AUTHORING error surfaced here on the validation
    # surface (never an enforcement verdict — the enforcement code-set stays untouched,
    # #723) while the served projection degrades to empty. An absent file is the common
    # case and no issue. The parse is the same one the /annotations projection uses, so
    # the two can never disagree about whether the file is valid.
    from typeflux.project.annotations import read_project_annotations

    result = read_project_annotations(project)
    if result.error is not None:
        issues.append(
            _issue(
                "invalid_annotations_file",
                f"annotations {result.path.name!r} failed to load: {result.error}",
                reference=result.path.name,
                path=result.path,
            )
        )


def _validate_targets(
    project: TypefluxProjectSpec,
    issues: list[ProjectValidationIssue],
) -> None:
    workflow_ids = {workflow.id for workflow in project.workflows}
    environment_ids = set(project.environments)
    policy_ids = set(project.policies)
    for target_name, target in project.validation.targets.items():
        for workflow_id in target.workflows:
            if workflow_id not in workflow_ids:
                issues.append(
                    ProjectValidationIssue(
                        code="unknown_target_workflow",
                        message=(
                            f"validation target {target_name!r} references unknown "
                            f"workflow: {workflow_id}"
                        ),
                        reference=target_name,
                    )
                )
        if target.environment is not None and target.environment not in environment_ids:
            issues.append(
                ProjectValidationIssue(
                    code="unknown_target_environment",
                    message=(
                        f"validation target {target_name!r} references unknown "
                        f"environment: {target.environment}"
                    ),
                    reference=target_name,
                )
            )
        for policy_id in target.policies:
            if policy_id not in policy_ids:
                issues.append(
                    ProjectValidationIssue(
                        code="unknown_target_policy",
                        message=(
                            f"validation target {target_name!r} references unknown "
                            f"policy: {policy_id}"
                        ),
                        reference=target_name,
                    )
                )


def _validate_policy_compositions(
    project: TypefluxProjectSpec,
    issues: list[ProjectValidationIssue],
) -> None:
    from typeflux.project.policy import compose_project_policies

    invalid_policy_ids = {
        issue.reference
        for issue in issues
        if issue.code in {"missing_policy_file", "invalid_policy_yaml"}
        and issue.reference is not None
    }
    targets_with_unknown_policy = {
        issue.reference
        for issue in issues
        if issue.code == "unknown_target_policy" and issue.reference is not None
    }
    for policy_id in project.policies:
        if policy_id in invalid_policy_ids:
            continue
        try:
            compose_project_policies(project, (policy_id,))
        except Exception as exc:  # noqa: BLE001 - report validation context.
            invalid_policy_ids.add(policy_id)
            issues.append(
                ProjectValidationIssue(
                    code="invalid_policy_composition",
                    message=f"policy {policy_id!r} failed to compose: {exc}",
                    reference=policy_id,
                )
            )

    for target_name, target in project.validation.targets.items():
        if not target.policies:
            continue
        if target_name in targets_with_unknown_policy:
            continue
        if any(policy_id in invalid_policy_ids for policy_id in target.policies):
            continue
        try:
            compose_project_policies(project, tuple(target.policies))
        except Exception as exc:  # noqa: BLE001 - report validation context.
            issues.append(
                ProjectValidationIssue(
                    code="invalid_target_policy_composition",
                    message=f"validation target {target_name!r} policies failed to compose: {exc}",
                    reference=target_name,
                )
            )


def _resolve_project_path(project: TypefluxProjectSpec, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project.project_dir / path).resolve()


def _load_yaml_mapping(path: Path, *, kind: str) -> dict[str, Any]:
    raw = strict_safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"empty YAML mapping: {path}")
    if not isinstance(raw, dict):
        raise TypeError(f"{kind} must be a YAML mapping: {path}")
    return raw


def _issue(
    code: str,
    message: str,
    *,
    reference: str,
    path: Path,
) -> ProjectValidationIssue:
    return ProjectValidationIssue(
        code=code,
        message=message,
        reference=reference,
        path=str(path),
    )


__all__ = [
    "discover_project_workflows",
    "load_project_spec",
    "load_project_workflow_specs",
    "validate_project",
]
