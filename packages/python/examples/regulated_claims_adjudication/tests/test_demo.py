from __future__ import annotations

from pathlib import Path

from examples.regulated_claims_adjudication.schemas import (
    ClaimAssessment,
    ClaimInput,
    CompliancePolicyResult,
    FinalAdjudication,
)
from typeflux.core.contracts import AIActivity, TemporalActivityDescriptor
from typeflux.yaml import load_yaml_spec
from typeflux.yaml.imports import collect_activities
from typeflux.yaml.workflow import create_workflow

YAML_PATH = Path("examples/regulated_claims_adjudication/typeflux.anthropic.yaml")


def test_regulated_claims_yaml_mixes_ai_and_plain_temporal_activities() -> None:
    spec = load_yaml_spec(YAML_PATH, load_dotenv=False)
    activities = collect_activities(spec)

    assert spec.workflow.name == "RegulatedClaimsAdjudicationWorkflow"
    assert isinstance(activities["assess_claim"], AIActivity)
    assert activities["assess_claim"].input_type is ClaimInput
    assert activities["assess_claim"].output_type is ClaimAssessment

    for name in ("apply_compliance_policy", "escalate_case", "finalize_adjudication"):
        descriptor = activities[name]
        assert isinstance(descriptor, TemporalActivityDescriptor)
        assert descriptor.definition_source.kind == "python"

    assert activities["apply_compliance_policy"].input_type is ClaimAssessment
    assert activities["apply_compliance_policy"].output_type is CompliancePolicyResult
    assert activities["finalize_adjudication"].output_type is FinalAdjudication


def test_regulated_claims_graph_and_lifecycle_validate() -> None:
    spec = load_yaml_spec(YAML_PATH, load_dotenv=False)
    activities = collect_activities(spec)

    # Graph validation accepts the AI -> plain -> plain chain.
    workflow_cls = create_workflow(spec, activities)
    assert workflow_cls.__typeflux_workflow_name__ == "RegulatedClaimsAdjudicationWorkflow"

    review = spec.workflow.lifecycle.review
    assert review.after_step == "apply_compliance_policy"
    assert review.invalid_user_decision == "fail"
    assert {key: route.route for key, route in review.user_decisions.items()} == {
        "approve_payment": "finalize_adjudication",
        "escalate_fraud": "escalate_case",
    }


def test_regulated_claims_provider_uses_anthropic_secret_reference() -> None:
    spec = load_yaml_spec(YAML_PATH, load_dotenv=False)

    provider = spec.runtime.provider
    assert provider.type == "anthropic"
    assert provider.model == "claude-sonnet-4-6"
    assert provider.api_key is not None
    assert provider.api_key.value_from.env == "ANTHROPIC_API_KEY"
    assert provider.provider_params().to_dict()["max_tokens"] == 4096


def test_regulated_claims_prompt_keeps_special_characters_and_placeholder_literal() -> None:
    spec = load_yaml_spec(YAML_PATH, load_dotenv=False)

    system_message = spec.runtime.registry.prompts["assess_claim"].messages[0].content
    # Env interpolation must not touch prompt text; special characters are
    # preserved for the model verbatim.
    assert "${AUDIT_REGION}" in system_message
    assert "Smith & Sons <Underwriting>" in system_message
