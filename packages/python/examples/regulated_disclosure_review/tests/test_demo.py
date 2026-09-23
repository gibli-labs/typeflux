from __future__ import annotations

from pathlib import Path

import pytest

from examples.regulated_disclosure_review.main import sample_input
from typeflux.project import load_project_spec, validate_project_bundle
from typeflux.yaml import load_yaml_spec

YAML = "examples/regulated_disclosure_review/typeflux.yaml"
MANIFEST = "examples/typeflux.project.yaml"


def test_regulated_disclosure_yaml_is_regulated_shaped() -> None:
    spec = load_yaml_spec(YAML, load_dotenv=False)

    assert spec.runtime.provider.type == "openai"
    assert spec.runtime.provider.model == "gpt-4.1"
    assert spec.runtime.observability.type == "langfuse"
    assert spec.runtime.observability.redaction.enabled is True
    assert spec.workflow.lifecycle is not None
    review = spec.workflow.lifecycle.review
    assert review is not None
    assert review.invalid_user_decision == "fail"
    assert set(review.user_decisions) == {"prepare_submission", "route_compliance", "finalize"}


def test_regulated_policy_admits_in_cloud_and_base_in_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The temporal_cloud_dev environment reads connection settings from the
    # environment; set them hermetically so the test does not depend on a
    # developer's shell or a local .env.
    monkeypatch.setenv("TEMPORAL_ADDRESS", "regulated.tmprl.cloud:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "regulated.acct")
    monkeypatch.setenv("TEMPORAL_API_KEY", "test-cloud-key")
    project = load_project_spec(MANIFEST)

    cloud = validate_project_bundle(
        project,
        environment_id="temporal_cloud_dev",
        workflow_ids=("regulated_disclosure_review",),
        policy_ids=("regulated",),
    )
    assert cloud.ok, [i.message for i in cloud.issues]

    local = validate_project_bundle(
        project,
        environment_id="local",
        workflow_ids=("regulated_disclosure_review",),
        policy_ids=("base",),
    )
    assert local.ok, [i.message for i in local.issues]


def test_sample_input_is_disclosure_shaped() -> None:
    case = sample_input()
    assert case.case_id == "DISC-2026-0042"
    assert case.risk_notes


def test_readme_documents_the_regulated_showcase() -> None:
    readme = Path("examples/regulated_disclosure_review/README.md").read_text(encoding="utf-8")
    assert "regulated" in readme
    assert "temporal_cloud_regulated" in readme
