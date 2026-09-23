"""Spec admission seam (#298 Phase B) — Python edition.

Exercises ``admit_spec``: the bounded parse, the external-origin structural
module-import gate (evaluated WITHOUT importing), the composed-policy binding
(fail-closed for an ungoverned external submission), and the ``AdmissionContributor``
provenance stamp.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from typeflux.metadata import AdmissionContributor, WorkflowMetadataContext
from typeflux.project import load_project_spec
from typeflux.project.admission import admit_spec

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
MANIFEST = EXAMPLES / "typeflux.project.yaml"
PURE_CHILD = EXAMPLES / "claims_review_composition" / "claims_review_pure.yaml"
MODULES_PARENT = EXAMPLES / "claims_review_composition" / "claims_review.yaml"


@pytest.fixture
def project():
    return load_project_spec(MANIFEST)


def _codes(report) -> dict[str, str]:
    return {check.code: check.status for check in report.checks}


def test_external_pure_yaml_child_is_admitted(monkeypatch: pytest.MonkeyPatch, project) -> None:
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    report = admit_spec(
        PURE_CHILD.read_text(encoding="utf-8"),
        project,
        "local",
        origin="external",
        workflow_id="claims_review_pure",
    )
    assert report.admitted is True, [c.model_dump() for c in report.checks if c.status == "failed"]
    codes = _codes(report)
    assert codes["admission_external_modules_forbidden"] == "passed"
    # Project-package schema refs resolve inside the policy's allowed roots (base
    # allows [examples]), so the schema-ref gate passes rather than skips.
    assert codes["admission_schema_ref_roots"] == "passed"
    assert codes["policy_composition_ceilings"] == "passed"
    assert codes["policy_subworkflow_closure"] == "passed"
    assert report.policy_hash is not None


def test_external_modules_spec_is_rejected_without_importing(
    monkeypatch: pytest.MonkeyPatch, project
) -> None:
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    report = admit_spec(
        MODULES_PARENT.read_text(encoding="utf-8"),
        project,
        "local",
        origin="external",
        workflow_id="claims_review",
    )
    assert report.admitted is False
    assert _codes(report)["admission_external_modules_forbidden"] == "failed"


def test_external_without_governing_policy_fails_closed(
    monkeypatch: pytest.MonkeyPatch, project
) -> None:
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    # An INFERRED slot no validation target binds (a new-workflow submission) →
    # no governing policy → external is refused. (An explicit undeclared slot is
    # now a separate admission_unknown_workflow failure.)
    report = admit_spec(
        PURE_CHILD.read_text(encoding="utf-8").replace(
            "name: claims_review_pure", "name: unbound_probe", 1
        ),
        project,
        "local",
        origin="external",
    )
    assert report.admitted is False
    assert _codes(report)["admission_policy_selection"] == "failed"


def test_operator_without_policy_skips_and_admits(monkeypatch: pytest.MonkeyPatch, project) -> None:
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    # Operator origin is the trusted path: no bound policy is a skip, not a rejection.
    report = admit_spec(
        PURE_CHILD.read_text(encoding="utf-8").replace(
            "name: claims_review_pure", "name: unbound_probe", 1
        ),
        project,
        "local",
        origin="operator",
    )
    assert _codes(report)["admission_policy_selection"] == "skipped"
    assert report.admitted is True


def test_parse_failure_is_a_check_not_a_raise(project) -> None:
    report = admit_spec("this: [is not: valid: typeflux", project, "local", origin="external")
    assert report.admitted is False
    assert report.checks[0].code in {"admission_parse", "admission_spec_shape"}
    assert report.checks[0].status == "failed"


def test_admit_accepts_bytes(monkeypatch: pytest.MonkeyPatch, project) -> None:
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    report = admit_spec(
        PURE_CHILD.read_bytes(),
        project,
        "local",
        origin="external",
        workflow_id="claims_review_pure",
    )
    assert report.admitted is True


def test_admission_contributor_stamps_only_when_admission_ran() -> None:
    ctx = WorkflowMetadataContext(workflow_name="w", workflow_id="w", task_queue="q")
    # Inert with no report (operator filesystem flow).
    assert AdmissionContributor.from_report(None).workflow(ctx).workflow_metadata == {}
    # Stamps safe identity when admission ran.
    report = _FakeReport(spec_origin="external", admitted=True, policy_hash="deadbeef")
    contribution = AdmissionContributor.from_report(report).workflow(ctx)
    assert contribution.workflow_metadata == {
        "typeflux": {
            "admission": {
                "spec_origin": "external",
                "status": "admitted",
                "policy_hash": "deadbeef",
            }
        }
    }
    assert "typeflux.admission.policy_hash" in contribution.redaction_exclusions


class _FakeReport:
    def __init__(self, *, spec_origin: str, admitted: bool, policy_hash: str) -> None:
        self.spec_origin = spec_origin
        self.admitted = admitted
        self.policy_hash = policy_hash


# ── schema-ref import surface (#298 review, codex P1) ──────────────────────────


class _ImportSpy:
    """A ``sys.meta_path`` finder that records every module name Python tries to
    resolve — proof admission never triggers an import of a spec-named module."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001, ANN201
        self.requested.append(fullname)
        return None


_HOSTILE_SPEC = """
project: evil_admission_probe
name: hostile
task_queue: q
runtime:
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions:
    - { name: a, input: "evil_admission_probe:In", output: "evil_admission_probe:Out", prompt: p/x }
workflow:
  name: Hostile
  input: "evil_admission_probe:In"
  output: "evil_admission_probe:Out"
  steps:
    - { id: s, activity: a }
"""


def test_external_schema_refs_outside_roots_reject_without_importing(
    monkeypatch: pytest.MonkeyPatch, project
) -> None:
    # A hostile spec controls `project:` — its type refs would import_module()
    # arbitrary modules at graph build. Admission must reject them structurally
    # against imports.allowed_module_roots (base allows only [examples]) and must
    # NEVER ask the import system for the hostile module.
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    spy = _ImportSpy()
    sys.meta_path.insert(0, spy)
    try:
        report = admit_spec(
            _HOSTILE_SPEC,
            project,
            "local",
            origin="external",
            workflow_id="claims_review_pure",  # a slot binding base+composition policies
        )
    finally:
        sys.meta_path.remove(spy)
    assert report.admitted is False
    codes = _codes(report)
    assert codes["admission_schema_ref_roots"] == "failed"
    ref_check = next(c for c in report.checks if c.code == "admission_schema_ref_roots")
    assert "evil_admission_probe" in (ref_check.message or "")
    assert ref_check.details["allowed_module_roots"] == ["examples"]
    offending = {item["location"] for item in ref_check.details["offending_refs"]}
    assert "workflow.input" in offending
    assert any(item.startswith("activities.definitions") for item in offending)
    # The proof: admission never asked the import machinery for the hostile module.
    assert not any(name.startswith("evil_admission_probe") for name in spy.requested)


def test_external_schema_ref_gate_skips_when_policy_sets_no_roots(tmp_path: Path) -> None:
    # A policy with no imports.allowed_module_roots does not constrain refs — the gate
    # reports SKIPPED (a policy governs only what it declares), never a silent pass.
    manifest = _write_admission_project(
        tmp_path,
        policy_yaml='version: "1"\nname: guard\n',
        environment_yaml="name: local\n",
    )
    project = load_project_spec(manifest)
    report = admit_spec(
        _spec_yaml(name="slotwf", model="gpt-4o-mini"),
        project,
        "local",
        origin="external",
    )
    assert _codes(report)["admission_schema_ref_roots"] == "skipped"


# ── admission resolves the SAME effective runtime as the manifest flow (#298 review) ──


def _spec_yaml(*, name: str, model: str) -> str:
    return (
        "project: demo_admission_pkg\n"
        f"name: {name}\n"
        "task_queue: q\n"
        "runtime:\n"
        "  registry: { type: inline, prompts: { p/x: hi } }\n"
        f"  provider: {{ type: openai, model: {model} }}\n"
        "activities:\n"
        "  definitions:\n"
        '    - { name: a, input: "demo_admission_pkg:In", output: "demo_admission_pkg:Out", prompt: p/x }\n'
        "workflow:\n"
        "  name: SlotWorkflow\n"
        '  input: "demo_admission_pkg:In"\n'
        '  output: "demo_admission_pkg:Out"\n'
        "  steps:\n"
        "    - { id: s, activity: a }\n"
    )


def _write_admission_project(
    tmp_path: Path,
    *,
    policy_yaml: str,
    environment_yaml: str,
    workflow_profiles: str = "",
    profiles_block: str = "",
) -> Path:
    (tmp_path / "environments").mkdir(exist_ok=True)
    (tmp_path / "environments" / "local.yaml").write_text(environment_yaml, encoding="utf-8")
    (tmp_path / "policies").mkdir(exist_ok=True)
    (tmp_path / "policies" / "guard.yaml").write_text(policy_yaml, encoding="utf-8")
    (tmp_path / "slotwf.yaml").write_text(
        _spec_yaml(name="slotwf", model="gpt-4o-mini"), encoding="utf-8"
    )
    (tmp_path / "typeflux.project.yaml").write_text(
        'version: "1"\n'
        "name: demo\n"
        "workflows:\n"
        "  - id: slotwf\n"
        "    path: slotwf.yaml\n"
        f"{workflow_profiles}"
        "environments:\n"
        "  local: environments/local.yaml\n"
        "policies:\n"
        "  guard: policies/guard.yaml\n"
        f"{profiles_block}"
        "validation:\n"
        "  targets:\n"
        "    local:\n"
        "      environment: local\n"
        "      workflows: [slotwf]\n"
        "      policies: [guard]\n",
        encoding="utf-8",
    )
    return tmp_path / "typeflux.project.yaml"


_GUARD_POLICY = (
    'version: "1"\nname: guard\nproviders:\n  allowed:\n    openai:\n      models: [gpt-4o-mini]\n'
)


def test_inferred_slot_applies_per_slot_environment_overrides(tmp_path: Path) -> None:
    # The exact review scenario: the slot binds a policy AND the environment carries a
    # per-slot override that makes the effective runtime violate it. With NO
    # workflow_id passed, the slot is inferred from the spec's own name — and the SAME
    # resolved slot must drive the overrides, so admission rejects (it previously
    # selected the slot's policy while skipping the slot's overrides, admitting wrongly).
    manifest = _write_admission_project(
        tmp_path,
        policy_yaml=_GUARD_POLICY,
        environment_yaml=(
            "name: local\n"
            "workflows:\n"
            "  slotwf:\n"
            "    overrides:\n"
            "      runtime:\n"
            "        provider:\n"
            "          model: gpt-4o\n"
        ),
    )
    project = load_project_spec(manifest)
    report = admit_spec(
        _spec_yaml(name="slotwf", model="gpt-4o-mini"),
        project,
        "local",
        origin="external",
        # NO workflow_id: the slot is inferred from the spec's name.
    )
    assert report.workflow_id == "slotwf"
    assert report.admitted is False
    provider_check = next(c for c in report.checks if c.code == "policy_provider")
    assert provider_check.status == "failed"
    assert "gpt-4o" in (provider_check.message or "")


def test_explicit_unknown_workflow_id_fails_closed(tmp_path: Path) -> None:
    # An EXPLICIT slot that the manifest does not declare must fail like
    # resolve_project_workflow would — proceeding with manifest_workflow=None
    # silently skips that slot's profile selection (Bugbot).
    manifest = _write_admission_project(
        tmp_path, policy_yaml=_GUARD_POLICY, environment_yaml="name: local\n"
    )
    project = load_project_spec(manifest)
    report = admit_spec(
        _spec_yaml(name="slotwf", model="gpt-4o-mini"),
        project,
        "local",
        origin="external",
        workflow_id="nope",
    )
    assert report.admitted is False
    unknown = next(c for c in report.checks if c.code == "admission_unknown_workflow")
    assert unknown.status == "failed"
    assert "'nope'" in (unknown.message or "")
    assert "slotwf" in (unknown.message or "")  # names the declared slots


def test_admission_validates_override_allowlist_like_the_loader(tmp_path: Path) -> None:
    # Environment overrides merged into the submitted spec pass through the SAME
    # validate_yaml_overrides allowlist as load_yaml_spec — layered config the
    # manifest load path would reject must not be silently accepted (Bugbot).
    manifest = _write_admission_project(
        tmp_path,
        policy_yaml=_GUARD_POLICY,
        environment_yaml=(
            "name: local\n"
            "workflows:\n"
            "  slotwf:\n"
            "    overrides:\n"
            "      workflow:\n"
            "        name: Hijacked\n"
        ),
    )
    project = load_project_spec(manifest)
    with pytest.raises(ValueError, match="not an allowed environment override"):
        admit_spec(
            _spec_yaml(name="slotwf", model="gpt-4o-mini"),
            project,
            "local",
            origin="external",
            workflow_id="slotwf",
        )


def test_admission_applies_manifest_profile_composition(tmp_path: Path) -> None:
    # A provider profile selected by the manifest slot changes a policy-relevant
    # setting (the model). Admission must evaluate the SAME effective runtime the
    # manifest flow would produce — profile included — so the submission rejects.
    (tmp_path / "profiles" / "provider").mkdir(parents=True)
    (tmp_path / "profiles" / "provider" / "big-model.yaml").write_text(
        'version: "1"\n'
        "name: big-model\n"
        "kind: provider\n"
        "runtime:\n"
        "  provider:\n"
        "    type: openai\n"
        "    model: gpt-4o\n",
        encoding="utf-8",
    )
    manifest = _write_admission_project(
        tmp_path,
        policy_yaml=_GUARD_POLICY,
        environment_yaml="name: local\n",
        workflow_profiles="    profiles:\n      provider: big-model\n",
        profiles_block="profiles:\n  provider:\n    big-model: profiles/provider/big-model.yaml\n",
    )
    project = load_project_spec(manifest)
    report = admit_spec(
        _spec_yaml(name="slotwf", model="gpt-4o-mini"),
        project,
        "local",
        origin="external",
        workflow_id="slotwf",
    )
    assert report.admitted is False
    provider_check = next(c for c in report.checks if c.code == "policy_provider")
    assert provider_check.status == "failed"
    assert "gpt-4o" in (provider_check.message or "")


# ── the admitted spec is returned and carries provenance end to end (#298 review) ──


def test_report_carries_spec_and_runtime_stamps_admission_metadata(
    monkeypatch: pytest.MonkeyPatch, project
) -> None:
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    report = admit_spec(
        PURE_CHILD.read_text(encoding="utf-8"),
        project,
        "local",
        origin="external",
        workflow_id="claims_review_pure",
    )
    assert report.admitted is True
    # The report returns the exact evaluated spec, with the provenance attached.
    assert report.spec is not None
    assert report.spec._admission_provenance is report
    # ...but the serialized report stays a pure report (no spec payload).
    assert "spec" not in report.to_dict()

    # e2e: building the runtime's metadata contributors FROM the returned spec stamps
    # the admission provenance (safe identity only) with redaction exclusions honored.
    from typeflux.yaml.runtime import _yaml_metadata_contributors

    ctx = WorkflowMetadataContext(workflow_name="w", workflow_id="w", task_queue="q")
    contributions = [
        contributor.workflow(ctx) for contributor in _yaml_metadata_contributors(report.spec)
    ]
    stamped = [
        c
        for c in contributions
        if c.workflow_metadata.get("typeflux", {}).get("admission") is not None
    ]
    assert len(stamped) == 1
    admission_meta = stamped[0].workflow_metadata["typeflux"]["admission"]
    assert admission_meta == {
        "spec_origin": "external",
        "status": "admitted",
        "policy_hash": report.policy_hash,
    }
    assert stamped[0].workflow_manifest["contributions"]["admission"] == admission_meta
    assert "typeflux.admission.spec_origin" in stamped[0].redaction_exclusions
    assert "typeflux.admission.status" in stamped[0].redaction_exclusions
    assert "typeflux.admission.policy_hash" in stamped[0].redaction_exclusions


def test_operator_filesystem_flow_stamps_no_admission_metadata(project) -> None:
    # A spec resolved through the manifest flow (never via admit_spec) contributes no
    # admission metadata — the manifest stays byte-unchanged.
    from typeflux.project.environment import resolve_project_workflow
    from typeflux.yaml.runtime import _yaml_metadata_contributors

    resolved = resolve_project_workflow(
        project, workflow_id="claims_review_pure", environment_id="local"
    )
    ctx = WorkflowMetadataContext(workflow_name="w", workflow_id="w", task_queue="q")
    contributions = [
        contributor.workflow(ctx) for contributor in _yaml_metadata_contributors(resolved.spec)
    ]
    assert not any(
        c.workflow_metadata.get("typeflux", {}).get("admission") is not None for c in contributions
    )
