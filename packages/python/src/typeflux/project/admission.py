"""Spec ADMISSION seam (#298 Phase B, design D298-3).

``admit_spec`` is the library entry point for deciding whether a spec — one authored
by an agent or submitted from outside the trusted filesystem — may run under a
project's governance. It is deliberately NOT a parallel enforcement engine: it parses
the spec with the same bounded loader every workflow uses, resolves the SAME effective
runtime the manifest flow would produce (profile composition + environment overlay +
per-slot overrides for the target slot, mirroring ``resolve_project_workflow``),
resolves the composed policy the target (environment, workflow slot) would apply, and
runs the EXISTING deterministic, live-provider-free check pipeline
(``validate_project_policy`` + composition ceilings + closure admission). The result is
a typed ``AdmissionReport`` of the same per-check shape the validate report already
produces, carrying the admitted spec (so a caller can build the workflow FROM the
exact artifact that was admitted, with its provenance attached).

Origin is one bit (design D298-2). ``origin="external"`` is the hostile-input posture,
enforced WITHOUT importing anything (module import is arbitrary code execution, the
single most dangerous capability):

- a spec that declares ``activities.modules`` or any other module-import-gated
  capability (a custom provider/registry/observability ``class:``, or a moderator
  callable) is a STRUCTURAL admission failure
  (``admission_external_modules_forbidden``);
- every SCHEMA type reference (``workflow.input``/``output``, each activity
  definition's ``input``/``output``, map/parallel ``collect.output``) resolves via
  ``import_type_ref`` at graph build — importing whatever module the spec names (the
  ``project:`` field is submitter-controlled, so refs reach arbitrary modules). Each
  ref's resolved module is therefore validated against the composed policy's
  ``imports.allowed_module_roots`` structurally (``admission_schema_ref_roots``),
  before anything would import.

The recommended external-origin policy baseline (tight import roots, tight composition
ceilings, provider allow-lists, ``require_secret_references``) is documented, not
hard-coded, so operators express "what externally submitted specs may do" in the same
policy YAML as everything else.

A control-plane upload endpoint is explicitly deferred (Phase C) until a consumer
exists; this seam is designed so that endpoint is a thin adapter over ``admit_spec``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from typeflux.project.environment import (
    ProjectResolvedWorkflow,
    _merged_overrides,
    _resolved_profile_overrides,
    build_project_environment_application,
    load_project_environment,
    project_environment_context,
)
from typeflux.project.policy import ComposedProjectPolicy, compose_project_policies
from typeflux.project.policy_enforcement import (
    _list_at_or_none,
    _module_in_any_root,
    risk_tier_enforcement_gap,
    select_project_policy_ids_for_workflow,
    validate_project_policy,
    validate_subworkflow_closure_policy,
)
from typeflux.project.spec import ProjectValidationCheck, TypefluxProjectSpec
from typeflux.yaml.loader import (
    MAX_YAML_BYTES,
    _deep_merge,
    _interpolate_env,
    strict_safe_load,
)
from typeflux.yaml.overrides import validate_yaml_overrides
from typeflux.yaml.spec import (
    TypefluxYamlSpec,
    WorkflowMapStepSpec,
    WorkflowParallelStepSpec,
)

SpecOrigin = Literal["operator", "external"]

#: Synthetic source path for env-interpolation error context — admission has no file.
_ADMISSION_SOURCE = Path("<admission>")


class AdmissionReport(BaseModel):
    """The typed outcome of ``admit_spec`` (#298). ``checks`` reuses the existing
    ``ProjectValidationCheck`` shape (code/status/message/details) so an admission
    report composes with every other check-report surface; ``admitted`` is true only
    when no check failed. ``spec`` is the parsed, environment-resolved spec that was
    evaluated — build the workflow FROM it and the manifest carries the admission
    provenance (D298-4); it is excluded from serialization (``to_dict`` stays a pure
    report)."""

    model_config = ConfigDict(extra="forbid")

    admitted: bool
    spec_origin: SpecOrigin
    environment_id: str
    #: The resolved target slot (the explicit ``workflow_id`` argument, else the
    #: submitted spec's own ``name``) whose bound policies governed the submission.
    workflow_id: str | None = None
    policy_hash: str | None = None
    checks: tuple[ProjectValidationCheck, ...] = ()
    #: The admitted spec (None when parse/schema failed), carrying
    #: ``_admission_provenance`` so downstream runtime builds stamp the
    #: ``AdmissionContributor`` metadata. Excluded from ``to_dict``.
    spec: TypefluxYamlSpec | None = Field(default=None, exclude=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def admit_spec(
    source: str | bytes | Mapping[str, Any],
    project: TypefluxProjectSpec,
    environment_id: str,
    *,
    origin: SpecOrigin,
    workflow_id: str | None = None,
    policy_ids: Sequence[str] = (),
) -> AdmissionReport:
    """Decide whether ``source`` may be admitted under ``project`` in ``environment_id``.

    ``source`` is YAML text/bytes or a pre-parsed mapping; text/bytes go through the
    bounded loader (``MAX_YAML_BYTES``). ``workflow_id`` names the target validation
    slot whose bound policies govern the submission (design D298-2); when omitted, the
    slot is the submitted spec's own ``name`` — and the SAME resolved slot drives the
    environment's per-slot overrides and the manifest slot's profile composition, so
    admission evaluates exactly the effective runtime ``resolve_project_workflow``
    would produce. ``policy_ids`` overrides the policy selection explicitly.
    ``origin="external"`` additionally forbids module-import-gated capabilities and
    out-of-root schema refs structurally, WITHOUT importing.

    Never raises for a governance violation — every outcome is a check in the returned
    report. Only a caller error (an unknown environment, a broken manifest profile)
    propagates.
    """
    checks: list[ProjectValidationCheck] = []

    # ── parse (bounded) ──────────────────────────────────────────────────────
    try:
        raw = _parse_source(source)
    except Exception as exc:  # noqa: BLE001 - a parse failure is an admission decision.
        checks.append(
            ProjectValidationCheck(
                code="admission_parse",
                status="failed",
                message=f"spec could not be parsed for admission: {exc}",
            )
        )
        return AdmissionReport(
            admitted=False,
            spec_origin=origin,
            environment_id=environment_id,
            workflow_id=workflow_id,
            checks=tuple(checks),
        )
    checks.append(_passed("admission_parse"))

    # The target slot: the explicit argument wins; else the submitted spec's own name
    # (available on the raw mapping before schema validation). Everything downstream —
    # policy selection, per-slot environment overrides, profile composition, and the
    # closure-walk root — keys off this ONE resolved id, so an inferred slot can never
    # select a slot's policy while skipping that same slot's overrides.
    raw_name = raw.get("name")
    slot_id = workflow_id or (raw_name if isinstance(raw_name, str) and raw_name else None)

    # ── resolve the SAME effective runtime the manifest flow would produce ────
    # (mirrors resolve_project_workflow: workflow YAML < profiles < environment
    # overrides, with project runtime defaults beneath). Manifest/profile config
    # defects propagate — they are operator errors, not admission decisions.
    environment = load_project_environment(project, environment_id)
    if slot_id is not None:
        manifest_workflow = next((w for w in project.workflows if w.id == slot_id), None)
        if workflow_id is not None and manifest_workflow is None:
            # An EXPLICIT slot must exist — resolve_project_workflow would raise
            # "unknown project workflow" here, and proceeding without the manifest
            # slot would silently skip its profile selection. Only an INFERRED slot
            # (spec.name) may be absent: that is the new-workflow submission case.
            checks.append(
                ProjectValidationCheck(
                    code="admission_unknown_workflow",
                    status="failed",
                    message=(
                        f"workflow {workflow_id!r} is not declared in the project manifest; "
                        f"declared: {', '.join(sorted(w.id for w in project.workflows))}"
                    ),
                )
            )
            return AdmissionReport(
                admitted=False,
                spec_origin=origin,
                environment_id=environment_id,
                workflow_id=slot_id,
                checks=tuple(checks),
            )
        profile_overrides, _components = _resolved_profile_overrides(
            project,
            workflow=manifest_workflow,
            environment=environment,
            workflow_id=slot_id,
        )
        environment_overrides = _merged_overrides(environment, slot_id)
    else:
        profile_overrides = {}
        environment_overrides = _deep_merge({}, environment.overrides)
    overrides = _deep_merge(profile_overrides, environment_overrides)
    runtime_defaults = project.defaults.runtime or None
    layered = dict(raw)
    if runtime_defaults:
        # Same allowlist discipline as load_yaml_spec: every layer that merges into
        # the submitted spec is validated, so admission cannot accept layered config
        # the manifest load path would reject. Failures raise — operator config
        # errors, not admission decisions.
        defaults_layer = {"runtime": dict(runtime_defaults)}
        validate_yaml_overrides(defaults_layer)
        layered = _deep_merge(defaults_layer, layered)
    if overrides:
        validate_yaml_overrides(overrides)
        layered = _deep_merge(layered, dict(overrides))
    application = build_project_environment_application(environment, environment_id=environment_id)
    spec: TypefluxYamlSpec | None = None
    with project_environment_context(application):
        try:
            spec = TypefluxYamlSpec.model_validate(
                _interpolate_env(layered, source_path=_ADMISSION_SOURCE)
            )
        except Exception as exc:  # noqa: BLE001 - a schema failure is an admission decision.
            checks.append(
                ProjectValidationCheck(
                    code="admission_spec_shape",
                    status="failed",
                    message=f"spec failed schema validation for admission: {exc}",
                )
            )
            return AdmissionReport(
                admitted=False,
                spec_origin=origin,
                environment_id=environment_id,
                workflow_id=slot_id,
                checks=tuple(checks),
            )
        checks.append(_passed("admission_spec_shape"))

        # ── external-origin structural gates (evaluated WITHOUT importing) ────
        if origin == "external":
            checks.append(_external_modules_check(spec))

        # ── composed policy binding (design D298-2) ──────────────────────────
        selected = select_project_policy_ids_for_workflow(
            project,
            environment_id=environment_id,
            workflow_id=slot_id or spec.name,
            explicit_policy_ids=policy_ids,
        )
        policy: ComposedProjectPolicy | None = None
        if not selected:
            # Fail-closed for external origin: an ungoverned external spec must not be
            # admitted. An operator-origin submission with no bound policy is the
            # trusted path, so it is a skip (the filesystem posture is unchanged).
            if origin == "external":
                checks.append(
                    ProjectValidationCheck(
                        code="admission_policy_selection",
                        status="failed",
                        message=(
                            "external-origin admission requires a governing policy; bind one "
                            "via a validation.targets entry for the workflow slot, or pass an "
                            "explicit policy id"
                        ),
                    )
                )
            else:
                checks.append(
                    ProjectValidationCheck(
                        code="admission_policy_selection",
                        status="skipped",
                        message="no project policies selected for this workflow/environment",
                    )
                )
        else:
            try:
                policy = compose_project_policies(project, selected)
            except Exception as exc:  # noqa: BLE001 - a composition conflict is a decision.
                checks.append(_failed("policy_composition", str(exc)))
            else:
                checks.append(
                    ProjectValidationCheck(
                        code="admission_policy_selection",
                        status="passed",
                        details={
                            "selected_policy_ids": list(policy.selected_policy_ids),
                            "applied_policy_ids": list(policy.applied_policy_ids),
                            "policy_hash": policy.policy_hash,
                        },
                    )
                )
                # Schema refs are the OTHER import surface (codex P1): every type ref
                # resolves via import_type_ref at graph build, importing whatever
                # module the (submitter-controlled) `project:` prefix names. Gate them
                # against the composed policy's import roots BEFORE anything imports.
                if origin == "external":
                    checks.append(_external_schema_ref_check(spec, policy))
                resolved = ProjectResolvedWorkflow(
                    project=project,
                    workflow_id=slot_id or spec.name,
                    workflow_path=_ADMISSION_SOURCE,
                    environment_id=environment_id,
                    environment=environment,
                    application=application,
                    spec=spec,
                )
                checks.extend(
                    validate_project_policy(project=project, resolved=resolved, policy=policy)
                )
                # #788: an elevated declared tier (closure-aware) must actually be
                # enforced by the composed policy — admission shares the same gap
                # predicate as validation and the runtime guard.
                binding_gap = risk_tier_enforcement_gap(
                    project,
                    resolved=resolved,
                    environment_id=environment_id,
                    selected_policy_ids=selected,
                    policy=policy,
                )
                if binding_gap is not None:
                    checks.append(
                        ProjectValidationCheck(
                            code="risk_tier_binding", status="failed", message=binding_gap
                        )
                    )
                closure = validate_subworkflow_closure_policy(
                    project=project,
                    resolved=resolved,
                    policy=policy,
                    environment_id=environment_id,
                )
                if closure is not None:
                    checks.append(closure)

    admitted = not any(check.status == "failed" for check in checks)
    report = AdmissionReport(
        admitted=admitted,
        spec_origin=origin,
        environment_id=environment_id,
        workflow_id=slot_id,
        policy_hash=policy.policy_hash if policy is not None else None,
        checks=tuple(checks),
        spec=spec,
    )
    # Attach the provenance so a caller that builds the workflow FROM the returned
    # spec stamps admission metadata (#298 D298-4); the operator filesystem flow never
    # reaches here, so its manifest is byte-unchanged.
    if spec is not None:
        spec._admission_provenance = report
    return report


def _parse_source(source: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        # A pre-parsed mapping has no text to bound-check; the caller is trusted for
        # the size of an in-memory object. Copy so admission never mutates the input.
        return dict(source)
    text = source.decode("utf-8") if isinstance(source, bytes) else source
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise ValueError(f"spec exceeds the {MAX_YAML_BYTES} byte admission limit")
    raw = strict_safe_load(text)
    if raw is None:
        raise ValueError("empty spec")
    if not isinstance(raw, dict):
        raise TypeError("Typeflux spec must be a mapping")
    return raw


def _external_modules_check(spec: TypefluxYamlSpec) -> ProjectValidationCheck:
    """Structural external-origin gate (#298): reject any module-import-gated capability
    on spec shape alone, without importing. Each of these declares code the worker would
    ``import`` — the arbitrary-code-execution surface an external submission must never
    reach."""
    offenders: list[str] = []
    if spec.activities.modules:
        modules = ", ".join(repr(module.module) for module in spec.activities.modules)
        offenders.append(f"activities.modules ({modules})")
    if spec.runtime.provider.provider_class is not None:
        offenders.append(f"runtime.provider.class ({spec.runtime.provider.provider_class!r})")
    if spec.runtime.registry.registry_class is not None:
        offenders.append(f"runtime.registry.class ({spec.runtime.registry.registry_class!r})")
    if spec.runtime.observability.backend_class is not None:
        offenders.append(
            f"runtime.observability.class ({spec.runtime.observability.backend_class!r})"
        )
    for definition in spec.activities.definitions:
        moderation = definition.moderation
        if moderation is not None and moderation.moderator is not None:
            offenders.append(
                f"activity {definition.name!r} moderation.moderator ({moderation.moderator!r})"
            )
    if offenders:
        return _failed(
            "admission_external_modules_forbidden",
            "external-origin spec declares module-import-gated capabilities that are "
            "forbidden for untrusted submissions (arbitrary code execution): "
            + "; ".join(offenders),
        )
    return _passed("admission_external_modules_forbidden")


def _iter_type_refs(spec: TypefluxYamlSpec) -> Iterator[tuple[str, str]]:
    """Every ``module:Type`` schema reference a spec resolves at graph build, as
    ``(location, ref)`` — the exact set ``import_type_ref`` is called on:
    ``workflow.input``/``output``, each activity definition's ``input``/``output``,
    and every map/parallel ``collect.output`` (recursively through branches)."""
    yield "workflow.input", spec.workflow.input
    yield "workflow.output", spec.workflow.output
    for definition in spec.activities.definitions:
        yield f"activities.definitions[{definition.name!r}].input", definition.input
        yield f"activities.definitions[{definition.name!r}].output", definition.output

    def walk(steps: Sequence[Any]) -> Iterator[tuple[str, str]]:
        for step in steps:
            if isinstance(step, WorkflowMapStepSpec):
                yield f"workflow step {step.id!r} map.collect.output", step.map.collect.output
            elif isinstance(step, WorkflowParallelStepSpec):
                yield (
                    f"workflow step {step.id!r} parallel.collect.output",
                    step.parallel.collect.output,
                )
                for branch in step.parallel.branches:
                    yield from walk(branch.steps)

    yield from walk(spec.workflow.steps)


def _resolved_ref_module(project_name: str, ref: str) -> str | None:
    """The module ``import_type_ref`` WOULD import for ``ref`` (mirrors
    ``yaml.imports._type_module_name``), computed without importing. ``None`` for a
    malformed ref (no ``module:Type`` shape) — the graph build would reject it, but
    admission still treats it as an offender rather than silently skipping."""
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        return None
    if module_name == project_name or module_name.startswith(f"{project_name}."):
        return module_name
    return f"{project_name}.{module_name}"


def _external_schema_ref_check(
    spec: TypefluxYamlSpec,
    policy: ComposedProjectPolicy,
) -> ProjectValidationCheck:
    """Structural external-origin gate over SCHEMA refs (#298, codex P1): every type
    ref resolves through ``import_type_ref`` at graph build — an import of whatever
    module the ref (prefixed by the submitter-controlled ``project:`` field) names. So
    each ref's resolved module must fall inside the composed policy's
    ``imports.allowed_module_roots``, verified on spec shape alone, WITHOUT importing.
    Skipped when the policy does not constrain module roots (like ``policy_imports``,
    a policy governs only what it declares — the documented external baseline sets
    roots)."""
    allowed_roots = _list_at_or_none(policy.payload, "imports", "allowed_module_roots")
    if allowed_roots is None:
        return ProjectValidationCheck(
            code="admission_schema_ref_roots",
            status="skipped",
            message=(
                "policy does not constrain imports.allowed_module_roots — schema type "
                "refs are unbounded; the external-origin baseline should set roots"
            ),
        )
    offenders: list[dict[str, str]] = []
    for location, ref in _iter_type_refs(spec):
        module = _resolved_ref_module(spec.project, ref)
        if module is None:
            offenders.append({"location": location, "ref": ref, "module": "<malformed>"})
        elif not _module_in_any_root(module, allowed_roots):
            offenders.append({"location": location, "ref": ref, "module": module})
    if offenders:
        rendered = "; ".join(
            f"{item['location']}: {item['ref']!r} resolves to module {item['module']!r}"
            for item in offenders
        )
        return ProjectValidationCheck(
            code="admission_schema_ref_roots",
            status="failed",
            message=(
                "external-origin spec declares schema type refs that resolve outside the "
                f"policy's imports.allowed_module_roots (refs import modules): {rendered}"
            ),
            details={"offending_refs": offenders, "allowed_module_roots": list(allowed_roots)},
        )
    return ProjectValidationCheck(
        code="admission_schema_ref_roots",
        status="passed",
        details={"allowed_module_roots": list(allowed_roots)},
    )


def _passed(code: str) -> ProjectValidationCheck:
    return ProjectValidationCheck(code=code, status="passed")


def _failed(code: str, message: str) -> ProjectValidationCheck:
    return ProjectValidationCheck(code=code, status="failed", message=message)


__all__ = [
    "AdmissionReport",
    "SpecOrigin",
    "admit_spec",
]
