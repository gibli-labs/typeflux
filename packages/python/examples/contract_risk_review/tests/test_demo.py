from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from examples.contract_risk_review.main import (
    DEFAULT_CONTRACT_PATH,
    bootstrap_langfuse,
    chat_prompt_payload,
    sample_input,
)
from examples.contract_risk_review.schemas import ContractAnalysisInput, ContractRiskReview
from typeflux.core.artifacts import resolve_artifact_inputs
from typeflux.project import load_project_spec, resolve_project_workflow
from typeflux.yaml import load_yaml_spec
from typeflux.yaml.imports import collect_activities
from typeflux.yaml.runtime import _build_artifact_policy


def test_contract_risk_review_yaml_loads() -> None:
    spec = load_yaml_spec(Path("examples/contract_risk_review/typeflux.yaml"))
    activities = collect_activities(spec)
    policy = _build_artifact_policy(spec)

    assert spec.workflow.name == "ContractRiskReviewWorkflow"
    assert spec.workflow.input == "schemas:ContractAnalysisInput"
    assert spec.workflow.output == "schemas:ContractRiskReview"
    assert "analyze_contract" in activities
    assert activities["analyze_contract"].input_type is ContractAnalysisInput
    assert activities["analyze_contract"].output_type is ContractRiskReview
    assert activities["analyze_contract"].prompt_ref.prompt_type == "chat"
    assert activities["analyze_contract"].artifact_inputs[0].name == "contracts"
    assert activities["analyze_contract"].artifact_inputs[0].media_types == ("application/pdf",)
    assert activities["analyze_contract"].artifact_inputs[0].attach is not None
    assert activities["analyze_contract"].artifact_inputs[0].attach.text == "Contract PDFs:"
    assert policy.allowed_media_types == ("application/pdf",)


def test_contract_risk_review_anthropic_yaml_sets_large_max_tokens() -> None:
    spec = load_yaml_spec(Path("examples/contract_risk_review/typeflux.anthropic.yaml"))
    activities = collect_activities(spec)

    assert spec.runtime.provider.type == "anthropic"
    assert spec.runtime.provider.provider_params().to_dict()["max_tokens"] == 16000
    assert activities["analyze_contract"].provider_params.to_dict()["max_tokens"] == 16000
    assert activities["analyze_contract"].provider_params.to_dict()["timeout"] == 300
    assert activities["analyze_contract"].start_to_close_timeout is not None
    assert activities["analyze_contract"].start_to_close_timeout.total_seconds() == 600


def test_contract_risk_review_gemini_yaml_uses_gemini_provider() -> None:
    spec = load_yaml_spec(Path("examples/contract_risk_review/typeflux.gemini.yaml"))
    activities = collect_activities(spec)

    assert spec.runtime.provider.type == "gemini"
    assert spec.workflow.name == "ContractRiskReviewGeminiWorkflow"
    # thinking_budget: 0 disables Gemini 2.5 thinking for this deterministic
    # extraction, so the output budget matches OpenAI/Anthropic (no
    # over-provisioning to absorb thinking tokens).
    assert activities["analyze_contract"].provider_params.to_dict()["max_tokens"] == 16000
    assert activities["analyze_contract"].provider_params.to_dict()["thinking_budget"] == 0
    assert activities["analyze_contract"].prompt_ref.prompt_type == "chat"


def test_contract_risk_review_langsmith_yaml_uses_remote_registry() -> None:
    spec = load_yaml_spec(Path("examples/contract_risk_review/typeflux.langsmith.yaml"))
    activities = collect_activities(spec)

    assert spec.workflow.name == "ContractRiskReviewLangSmithWorkflow"
    assert spec.runtime.registry.type == "langsmith"
    # label is the moveable selector on both backends (Langfuse label / LangSmith
    # commit tag); defaults to production, mirroring the Langfuse example.
    assert spec.runtime.registry.label == "production"
    assert not spec.runtime.registry.prompts
    assert activities["analyze_contract"].prompt_ref.name == "typeflux-contract-risk-review"
    assert activities["analyze_contract"].prompt_ref.prompt_type == "chat"
    # Fully LangSmith: prompts AND traces resolve/export through LangSmith.
    assert spec.runtime.observability.type == "langsmith"


def test_contract_prompt_payload_preserves_mustache_placeholders() -> None:
    payload = chat_prompt_payload()
    user_message = payload[1]["content"]

    assert isinstance(user_message, str)
    assert "{{ engagement_id }}" in user_message
    assert "{{ business_context }}" in user_message
    assert "{{ review_objective }}" in user_message
    assert user_message.count("{{ engagement_id }}") == 1
    assert "{{{ engagement_id }}}" not in user_message


def test_sample_input_defaults_to_checked_in_fixture() -> None:
    value = sample_input(DEFAULT_CONTRACT_PATH)

    assert value.engagement_id == "contract-risk-review-live"
    assert value.contract_files == [str(DEFAULT_CONTRACT_PATH)]


def test_contract_fixture_resolves_through_artifact_policy() -> None:
    spec = load_yaml_spec(Path("examples/contract_risk_review/typeflux.yaml"))
    activity = collect_activities(spec)["analyze_contract"]
    policy = _build_artifact_policy(spec)

    groups = resolve_artifact_inputs(
        sample_input(DEFAULT_CONTRACT_PATH),
        activity.artifact_inputs,
        policy=policy,
    )

    artifact = groups[0].artifacts[0]
    assert artifact.local_path == DEFAULT_CONTRACT_PATH.resolve()
    assert artifact.media_type == "application/pdf"
    assert artifact.sha256
    assert artifact.size_bytes is not None and artifact.size_bytes > 0


def test_project_profiles_resolve_contract_risk_review_task_queues(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "cloud.tmprl.cloud:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "cloud-namespace")
    monkeypatch.setenv("TEMPORAL_API_KEY", "test-temporal-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "test-langfuse-public-key")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "test-langfuse-secret-key")
    monkeypatch.delenv("TYPEFLUX_LOCAL_OBSERVABILITY", raising=False)

    project = load_project_spec(Path("examples/typeflux.project.yaml"))
    local = resolve_project_workflow(
        project,
        workflow_id="contract_risk_review",
        environment_id="local",
    )
    cloud = resolve_project_workflow(
        project,
        workflow_id="contract_risk_review",
        environment_id="temporal_cloud_dev",
    )

    assert local.spec.task_queue == "contract-risk-review-local-typeflux"
    assert local.spec.runtime.observability.type == "langfuse"
    assert cloud.spec.task_queue == "contract-risk-review-cloud-typeflux"
    assert cloud.spec.runtime.temporal.tls is True
    assert cloud.spec.runtime.registry.type == "langfuse"
    assert cloud.spec.runtime.observability.type == "langfuse"


def test_contract_prompt_bootstrap_creates_langfuse_chat_prompt(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeLangfuse:
        def create_prompt(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(version=42)

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PROMPT_LABEL", "production")
    monkeypatch.setattr("examples.contract_risk_review.main._langfuse_client", FakeLangfuse)

    assert bootstrap_langfuse() == 0

    assert calls["name"] == "analyze_contract"
    assert calls["type"] == "chat"
    assert calls["labels"] == ["production"]
    assert calls["prompt"] == chat_prompt_payload()
    prompt = calls["prompt"]
    assert isinstance(prompt, list)
    assert prompt[0]["role"] == "system"
    assert prompt[1]["role"] == "user"
    assert isinstance(prompt[1]["content"], str)
    assert "Review objective: {{ review_objective }}" in prompt[1]["content"]
    config = calls["config"]
    assert isinstance(config, dict)
    assert "model" not in config
    assert config["provider_params"] == {"temperature": 0}
    assert config["typeflux"]["prompt"]["type"] == "chat"
    assert config["typeflux"]["provider_hint"] == {"name": "configured-by-typeflux-yaml"}
    assert config["typeflux"]["artifact_inputs"]["contracts"]["attach"] == {
        "role": "user",
        "text": "Contract PDFs:",
    }
