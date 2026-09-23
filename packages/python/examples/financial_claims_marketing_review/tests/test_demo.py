from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from examples.financial_claims_marketing_review import main as example_main
from examples.financial_claims_marketing_review.fakes import FakeProvider
from examples.financial_claims_marketing_review.pipeline import (
    inline_registry,
    run_offline_pipeline,
)
from examples.financial_claims_marketing_review.schemas import MarketingReviewPacket
from typeflux.observability import NoOpObservabilityBackend
from typeflux.yaml import build_runtime, load_yaml_spec
from typeflux.yaml.imports import collect_activities
from typeflux.yaml.spec import WorkflowMapStepSpec

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
YAML_PATH = EXAMPLE_DIR / "typeflux.yaml"


def test_chat_prompt_files_include_roles_and_placeholders() -> None:
    review_prompt = example_main.CHAT_PROMPTS["review_claim"]
    assert [message.role for message in review_prompt.messages] == ["system", "user"]
    review_payload = example_main.chat_prompt_payload(review_prompt)
    assert review_payload[0]["role"] == "system"
    assert "financial marketing compliance reviewer" in review_payload[0]["content"]
    assert "{{submission_id}}" in review_payload[1]["content"]
    assert "{{claim_text}}" in review_payload[1]["content"]
    assert "{{evidence}}" in review_payload[1]["content"]

    consolidate_prompt = example_main.CHAT_PROMPTS["consolidate_review"]
    assert [message.role for message in consolidate_prompt.messages] == ["system", "user"]
    consolidate_payload = example_main.chat_prompt_payload(consolidate_prompt)
    assert "{{reviews}}" in consolidate_payload[1]["content"]


def test_sample_submission_exercises_fan_out_and_redaction_inputs() -> None:
    submission = example_main.sample_submission()

    assert submission.submission_id == "MKT-2026-RET-0042"
    assert len(submission.claims) == 4
    combined = "\n".join(
        [submission.reviewer_email, *(claim.contact for claim in submission.claims)]
    )
    assert "compliance.reviewer@example.com" in combined
    assert "maria.chen@example.com" in combined
    assert "555-867-5309" in combined
    assert any("guaranteed 12% annual return" in claim.claim_text for claim in submission.claims)
    assert any("Approved by regulators" in claim.claim_text for claim in submission.claims)


def test_offline_pipeline_end_to_end_returns_marketing_review_packet() -> None:
    result = run_offline_pipeline(
        example_main.sample_submission(),
        registry=inline_registry(),
        provider=FakeProvider(),
    )

    assert isinstance(result, MarketingReviewPacket)
    assert result.submission_id == "MKT-2026-RET-0042"
    assert result.final_decision == "legal_review_required"
    assert result.legal_review_claim_ids == ["CLM-001", "CLM-004"]


def test_yaml_spec_parses_chat_prompt_map_step() -> None:
    spec = load_yaml_spec(YAML_PATH)
    activities = collect_activities(spec)

    assert spec.workflow.name == "FinancialClaimsMarketingReviewWorkflow"
    assert activities["review_marketing_claim"].prompt_ref.prompt_type == "chat"
    assert activities["consolidate_marketing_review"].prompt_ref.prompt_type == "chat"
    first_step = spec.workflow.steps[0]
    assert isinstance(first_step, WorkflowMapStepSpec)
    assert first_step.id == "review_claims"
    assert first_step.map.activity == "review_marketing_claim"
    assert first_step.map.over == "input.claims"
    assert first_step.map.concurrency == 3
    assert first_step.map.collect.output == "schemas:ClaimComplianceReviewBatch"
    assert first_step.map.collect.field == "reviews"


def test_langfuse_bootstrap_payloads_are_chat_prompts() -> None:
    for definition in example_main.CHAT_PROMPTS.values():
        payload = example_main.chat_prompt_payload(definition)
        assert payload
        assert {message["role"] for message in payload} <= {"system", "user", "assistant"}
        assert all(isinstance(message["content"], str) for message in payload)
        assert payload[0]["role"] == "system"
        assert definition.activity.prompt_ref.prompt_type == "chat"
        config = example_main._prompt_config(definition.activity, model="gpt-4o-mini")
        assert config["typeflux"]["prompt"]["type"] == "chat"


@pytest.mark.asyncio
async def test_build_runtime_wires_provider_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeRegistry:
        def __init__(self, **kwargs: Any) -> None:
            calls["registry_kwargs"] = kwargs

    class FakeOpenAIProvider:
        provider_name = "openai"

        def __init__(self, **kwargs: Any) -> None:
            calls["provider_kwargs"] = kwargs
            self.default_model = kwargs.get("default_model")

    async def fake_connect(spec, *, plugin):
        calls["plugin"] = plugin
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime.LangfusePromptRegistry", FakeRegistry)
    monkeypatch.setattr("typeflux.yaml.runtime.OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(
        "typeflux.yaml.runtime._build_observability",
        lambda spec, **kwargs: NoOpObservabilityBackend(),
    )
    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    runtime = await build_runtime(load_yaml_spec(YAML_PATH))

    controller = runtime.worker.provider_rate_limit_controller
    assert controller is not None
    model_selection = controller.select(provider_name="openai", provider_model="gpt-4o-mini")
    assert model_selection.policy_source == "model"
    assert model_selection.limits is not None
    assert model_selection.limits.max_concurrent == 1
    assert model_selection.limits.min_interval_seconds == 0.25
    assert calls["provider_kwargs"]["default_model"] == "gpt-4o-mini"
