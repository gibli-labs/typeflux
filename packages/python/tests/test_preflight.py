from __future__ import annotations

import pytest
from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactGroupPart, ArtifactInput, TextPart
from typeflux.core.contracts import (
    AIActivity,
    ChatMessage,
    PromptRef,
    ProviderParams,
    ResolvedPrompt,
)
from typeflux.execution.preflight import PreflightError, preflight_ai_activities
from typeflux.manifests import schema_hash
from typeflux.prompts import InlinePromptRegistry


class InputModel(BaseModel):
    value: str


class OutputModel(BaseModel):
    label: str


def test_preflight_resolves_prompts_and_validates_contract_hashes() -> None:
    activity = _activity()
    registry = InlinePromptRegistry(
        {
            "demo": ResolvedPrompt(
                ref=PromptRef("demo"),
                messages=(ChatMessage(role="user", content="Classify {{ value }}"),),
                resolved_version="3",
                model="test-model",
                metadata={
                    "langfuse.prompt_config": {
                        "typeflux": {
                            "contracts": {
                                "input_schema_hash": schema_hash(InputModel),
                                "output_schema_hash": schema_hash(OutputModel),
                            }
                        }
                    }
                },
            )
        }
    )

    report = preflight_ai_activities(activities=[activity], registry=registry)

    assert report.ok is True
    assert report.resolved[0].activity_name == "demo_activity"
    assert report.resolved[0].resolved_prompt_version == "3"
    assert report.resolved[0].provider_model == "test-model"


def test_preflight_reports_missing_prompt() -> None:
    report = preflight_ai_activities(activities=[_activity()], registry=InlinePromptRegistry({}))

    assert report.ok is False
    assert report.failures[0].activity_name == "demo_activity"
    assert report.failures[0].error_type == "PromptNotFoundError"
    assert report.failures[0].retryable is False
    with pytest.raises(PreflightError):
        report.raise_for_failures()


def test_preflight_reports_prompt_schema_mismatch() -> None:
    registry = InlinePromptRegistry(
        {
            "demo": ResolvedPrompt(
                ref=PromptRef("demo"),
                messages=(ChatMessage(role="user", content="Classify {{ value }}"),),
                metadata={
                    "langfuse.prompt_config": {
                        "typeflux": {
                            "contracts": {
                                "input_schema_hash": "wrong",
                            }
                        }
                    }
                },
            )
        }
    )

    report = preflight_ai_activities(activities=[_activity()], registry=registry)

    assert report.ok is False
    assert report.failures[0].error_type == "PromptRegistryConfigError"
    assert "input_schema_hash mismatch" in report.failures[0].reason


def test_preflight_accepts_portable_content_parts() -> None:
    registry = InlinePromptRegistry(
        {
            "demo": ResolvedPrompt(
                ref=PromptRef("demo"),
                messages=(
                    ChatMessage(
                        role="user",
                        content=(
                            TextPart("Classify {{ value }}"),
                            ArtifactGroupPart(group="documents", text="Documents:"),
                        ),
                    ),
                ),
                resolved_version="content-parts-v1",
            )
        }
    )

    report = preflight_ai_activities(activities=[_activity()], registry=registry)

    assert report.ok is True
    assert report.resolved[0].resolved_prompt_version == "content-parts-v1"


def test_preflight_rejects_provider_specific_params_for_wrong_provider() -> None:
    registry = InlinePromptRegistry(
        {
            "demo": ResolvedPrompt(
                ref=PromptRef("demo"),
                messages=(ChatMessage(role="user", content="Classify {{ value }}"),),
                provider_params=ProviderParams(top_k=5),
            )
        }
    )

    report = preflight_ai_activities(
        activities=[_activity()],
        registry=registry,
        provider_name="openai",
        provider_default_params=ProviderParams(model="gpt-4o-mini"),
    )

    assert report.ok is False
    assert report.failures[0].error_type == "ProviderConfigError"
    assert "top_k" in report.failures[0].reason


def test_preflight_uses_provider_declared_supported_params() -> None:
    class CustomProvider:
        provider_name = "custom"
        supported_provider_params = {"model", "max_tokens"}

    registry = InlinePromptRegistry(
        {
            "demo": ResolvedPrompt(
                ref=PromptRef("demo"),
                messages=(ChatMessage(role="user", content="Classify {{ value }}"),),
                provider_params=ProviderParams(top_p=0.8),
            )
        }
    )

    report = preflight_ai_activities(
        activities=[_activity()],
        registry=registry,
        provider=CustomProvider(),
        provider_default_params=ProviderParams(model="custom-model"),
    )

    assert report.ok is False
    assert report.failures[0].error_type == "ProviderConfigError"
    assert "top_p" in report.failures[0].reason


def _artifact_activity(kind: str | None) -> AIActivity:
    return AIActivity(
        name="demo_activity",
        input_type=InputModel,
        output_type=OutputModel,
        prompt_ref=PromptRef("demo"),
        artifact_inputs=(
            ArtifactInput(name="attachment", from_path="input.attachment", kind=kind),
        ),
    )


def _text_registry() -> InlinePromptRegistry:
    return InlinePromptRegistry(
        {
            "demo": ResolvedPrompt(
                ref=PromptRef("demo"),
                messages=(ChatMessage(role="user", content="Classify {{ value }}"),),
            )
        }
    )


def test_preflight_rejects_unsupported_artifact_kind_for_provider() -> None:
    report = preflight_ai_activities(
        activities=[_artifact_activity("audio")],
        registry=_text_registry(),
        provider_name="openai",
        provider_default_params=ProviderParams(model="gpt-4o-mini"),
    )

    assert report.ok is False
    assert report.failures[0].error_type == "ProviderConfigError"
    assert "audio" in report.failures[0].reason
    assert "attachment" in report.failures[0].reason


def test_preflight_allows_supported_artifact_kind() -> None:
    report = preflight_ai_activities(
        activities=[_artifact_activity("image")],
        registry=_text_registry(),
        provider_name="anthropic",
        provider_default_params=ProviderParams(model="claude-sonnet-4-6"),
    )

    assert report.ok is True


def test_preflight_skips_undeclared_artifact_kind() -> None:
    # An undeclared kind is inferred at runtime from media type/source; preflight
    # must not reject it (the runtime content mapping still fails closed).
    report = preflight_ai_activities(
        activities=[_artifact_activity(None)],
        registry=_text_registry(),
        provider_name="openai",
        provider_default_params=ProviderParams(model="gpt-4o-mini"),
    )

    assert report.ok is True


def test_preflight_uses_provider_declared_supported_artifact_kinds() -> None:
    class CustomProvider:
        provider_name = "custom"
        supported_artifact_kinds = {"image"}

    report = preflight_ai_activities(
        activities=[_artifact_activity("document")],
        registry=_text_registry(),
        provider=CustomProvider(),
        provider_default_params=ProviderParams(model="custom-model"),
    )

    assert report.ok is False
    assert report.failures[0].error_type == "ProviderConfigError"
    assert "document" in report.failures[0].reason


def test_preflight_skips_artifact_check_when_provider_unknown() -> None:
    # No provider supplied → no capability declared → no artifact-kind rejection.
    report = preflight_ai_activities(
        activities=[_artifact_activity("audio")],
        registry=_text_registry(),
    )

    assert report.ok is True


def test_preflight_ignores_plain_temporal_activities() -> None:
    from temporalio import activity as temporal_activity

    from typeflux.core.contracts import TemporalActivityDescriptor

    @temporal_activity.defn(name="normalize")
    async def normalize(value: InputModel) -> OutputModel:
        return OutputModel(value=value.value)

    descriptor = TemporalActivityDescriptor(
        name="normalize",
        input_type=InputModel,
        output_type=OutputModel,
        activity=normalize,
    )
    registry = InlinePromptRegistry({"demo": "Classify {{ value }}"})

    report = preflight_ai_activities(activities=[_activity(), descriptor], registry=registry)

    assert report.ok is True
    assert [item.activity_name for item in report.resolved] == ["demo_activity"]


def _activity() -> AIActivity:
    return AIActivity(
        name="demo_activity",
        input_type=InputModel,
        output_type=OutputModel,
        prompt_ref=PromptRef("demo"),
    )
