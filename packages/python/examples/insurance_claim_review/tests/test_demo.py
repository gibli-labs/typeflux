from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from examples.insurance_claim_review import main as example_main
from examples.insurance_claim_review.fakes import FakeProvider
from examples.insurance_claim_review.pipeline import inline_registry, run_offline_pipeline
from examples.insurance_claim_review.schemas import ClaimReviewPacket
from typeflux.observability import NoOpObservabilityBackend
from typeflux.yaml import build_runtime, load_yaml_spec
from typeflux.yaml.spec import WorkflowMapStepSpec

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
YAML_PATH = EXAMPLE_DIR / "typeflux.yaml"


def test_prompt_files_exist_and_include_expected_placeholders() -> None:
    expected = {
        "review_evidence.txt": [
            "{{claim_id}}",
            "{{evidence_id}}",
            "{{kind}}",
            "{{source}}",
            "{{received_at}}",
            "{{content}}",
        ],
        "consolidate_claim.txt": ["{{reviews}}"],
    }
    for filename, placeholders in expected.items():
        body = (example_main.PROMPT_DIR / filename).read_text(encoding="utf-8")
        for placeholder in placeholders:
            assert placeholder in body


def test_sample_claim_exercises_moderate_fan_out_and_redaction_inputs() -> None:
    claim = example_main.sample_claim()

    assert claim.claim_id == "CLM-2026-0042"
    assert len(claim.evidence) == 4
    combined = "\n".join(item.content for item in claim.evidence)
    assert "jordan.lee@example.com" in combined
    assert "555-123-4567" in combined
    assert "4242 4242 4242 4242" in combined


def test_offline_pipeline_end_to_end_returns_claim_review_packet() -> None:
    result = run_offline_pipeline(
        example_main.sample_claim(),
        registry=inline_registry(),
        provider=FakeProvider(),
    )

    assert isinstance(result, ClaimReviewPacket)
    assert result.claim_id == "CLM-2026-0042"
    assert result.evidence_count == 4
    assert result.approval_required is True


def test_yaml_spec_parses_map_step() -> None:
    spec = load_yaml_spec(YAML_PATH)

    assert spec.workflow.name == "InsuranceClaimReviewWorkflow"
    first_step = spec.workflow.steps[0]
    assert isinstance(first_step, WorkflowMapStepSpec)
    assert first_step.id == "review_evidence"
    assert first_step.map.activity == "review_evidence_item"
    assert first_step.map.over == "input.evidence"
    assert first_step.map.concurrency == 3
    assert first_step.map.collect.output == "schemas:EvidenceReviewBatch"
    assert first_step.map.collect.field == "reviews"


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
    default_selection = controller.select(provider_name="anthropic", provider_model="claude")
    assert default_selection.policy_source == "default"
    assert default_selection.limits is not None
    assert default_selection.limits.max_concurrent == 4
    assert calls["provider_kwargs"]["default_model"] == "gpt-4o-mini"
