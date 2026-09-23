from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactGroupPart, TextPart
from typeflux.core.contracts import AIActivity, ChatMessage, PromptRef, ProviderParams
from typeflux.execution.executor import (
    execute_ai_activity,
    prepare_ai_activity_execution,
)
from typeflux.project.policy import ComposedProjectPolicy
from typeflux.project.policy_enforcement import RuntimePolicyGuard
from typeflux.prompts import InlinePromptRegistry, LangfusePromptRegistry
from typeflux.prompts.context import LANGFUSE_PROMPT_CONTEXT_KEY
from typeflux.prompts.errors import (
    PromptNotFoundError,
    PromptRegistryAuthError,
    PromptRegistryConfigError,
    PromptRegistryUnavailableError,
    PromptResolutionError,
)
from typeflux.providers.errors import ProviderPolicyError
from typeflux.testing import FakeProvider


class _FakeLangfuse:
    def __init__(self) -> None:
        self.created = []
        self.get_calls = []
        self.prompt = SimpleNamespace(
            prompt="Hello {{name}}",
            version=7,
            config={"model": "gpt-4o-mini", "temperature": 0, "template_format": "mustache"},
        )

    def get_prompt(self, name, *, version=None, label=None, type):
        self.get_calls.append({"name": name, "version": version, "label": label, "type": type})
        return self.prompt

    def create_prompt(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(version=8)


def test_langfuse_prompt_registry_resolves_text_prompt() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("demo", label="production"))

    assert resolved.messages[0].content == "Hello {{name}}"
    assert resolved.resolved_version == "7"
    assert resolved.model is None
    assert resolved.temperature == 0
    assert resolved.metadata["langfuse.prompt_type"] == "text"
    assert resolved.metadata["langfuse.prompt_config"]["model"] == "gpt-4o-mini"
    assert resolved.observation_context[LANGFUSE_PROMPT_CONTEXT_KEY] is client.prompt
    assert LANGFUSE_PROMPT_CONTEXT_KEY not in resolved.metadata
    assert client.get_calls == [
        {"name": "demo", "version": None, "label": "production", "type": "text"}
    ]


def test_langfuse_prompt_registry_pins_immutable_registry_version() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("demo", version=7))

    assert resolved.resolved_version == "7"
    assert client.get_calls == [{"name": "demo", "version": 7, "label": None, "type": "text"}]


def test_langfuse_prompt_registry_selects_label() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client)

    registry.resolve(PromptRef("demo", label="canary"))

    assert client.get_calls == [
        {"name": "demo", "version": None, "label": "canary", "type": "text"}
    ]


def test_langfuse_prompt_registry_defaults_to_production_label() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client)

    registry.resolve(PromptRef("demo"))

    assert client.get_calls == [
        {"name": "demo", "version": None, "label": "production", "type": "text"}
    ]


def test_langfuse_prompt_registry_keeps_config_model_out_of_execution_by_default() -> None:
    client = _FakeLangfuse()
    client.prompt.config = {
        "model": "claude-test",
        "provider_model": "claude-test",
        "temperature": 0,
        "provider_params": {
            "model": "claude-test",
            "max_tokens": 12000,
        },
    }
    registry = LangfusePromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("demo", label="production"))

    assert resolved.model is None
    assert resolved.provider_params.model is None
    assert resolved.provider_params.to_dict() == {
        "temperature": 0,
        "max_tokens": 12000,
    }
    assert resolved.metadata["langfuse.prompt_config"]["model"] == "claude-test"
    assert resolved.metadata["langfuse.prompt_config"]["provider_model"] == "claude-test"
    assert resolved.metadata["langfuse.prompt_config"]["provider_params"]["model"] == "claude-test"


def test_langfuse_prompt_registry_resolves_provider_params_with_model_override_opt_in() -> None:
    client = _FakeLangfuse()
    client.prompt.config = {
        "model": "claude-test",
        "temperature": 0,
        "provider_params": {
            "max_tokens": 12000,
            "top_p": 0.8,
            "stop": ["DONE"],
        },
    }
    registry = LangfusePromptRegistry(client=client, allow_prompt_model_override=True)

    resolved = registry.resolve(PromptRef("demo", label="production"))

    assert resolved.model == "claude-test"
    assert resolved.provider_params.to_dict() == {
        "model": "claude-test",
        "temperature": 0,
        "max_tokens": 12000,
        "top_p": 0.8,
        "stop": ["DONE"],
    }


class _GreetInput(BaseModel):
    name: str


class _GreetOutput(BaseModel):
    text: str


def _greet_activity(provider_params: ProviderParams | None = None) -> AIActivity:
    return AIActivity(
        name="greet",
        input_type=_GreetInput,
        output_type=_GreetOutput,
        prompt_ref=PromptRef("demo", label="production"),
        provider_params=provider_params or ProviderParams(),
    )


def _model_policy_guard(allowed_model: str) -> RuntimePolicyGuard:
    policy = ComposedProjectPolicy(
        selected_policy_ids=("p",),
        applied_policy_ids=("p",),
        policy_names=("p",),
        policy_hash="hash",
        payload={"providers": {"allowed": {"fake": {"models": [allowed_model]}}}},
    )
    return RuntimePolicyGuard(policy=policy, provider_name="fake")


def test_yaml_provider_model_is_effective_over_prompt_config_by_default() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client)
    provider = FakeProvider([_GreetOutput(text="hi")])
    provider.default_provider_params = ProviderParams(model="provider-config-model")

    execute_ai_activity(
        activity=_greet_activity(),
        input_value=_GreetInput(name="Ada"),
        registry=registry,
        provider=provider,
    )

    assert provider.calls[0]["model"] == "provider-config-model"


def test_prompt_config_model_is_effective_with_override_opt_in() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client, allow_prompt_model_override=True)
    provider = FakeProvider([_GreetOutput(text="hi")])
    provider.default_provider_params = ProviderParams(model="provider-config-model")

    execute_ai_activity(
        activity=_greet_activity(),
        input_value=_GreetInput(name="Ada"),
        registry=registry,
        provider=provider,
    )

    assert provider.calls[0]["model"] == "gpt-4o-mini"


def test_policy_guard_checks_yaml_model_when_override_disabled() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client)
    provider = FakeProvider([_GreetOutput(text="hi")])
    provider.default_provider_params = ProviderParams(model="provider-config-model")

    # Prompt config names gpt-4o-mini, which this policy forbids; with the
    # override disabled the effective model is the allowed provider model.
    execute_ai_activity(
        activity=_greet_activity(),
        input_value=_GreetInput(name="Ada"),
        registry=registry,
        provider=provider,
        provider_model_policy_guard=_model_policy_guard("provider-config-model"),
    )

    assert provider.calls[0]["model"] == "provider-config-model"


def test_policy_guard_checks_prompt_selected_model_with_override_opt_in() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client, allow_prompt_model_override=True)
    provider = FakeProvider([_GreetOutput(text="hi")])
    provider.default_provider_params = ProviderParams(model="provider-config-model")

    with pytest.raises(ProviderPolicyError, match="gpt-4o-mini"):
        execute_ai_activity(
            activity=_greet_activity(),
            input_value=_GreetInput(name="Ada"),
            registry=registry,
            provider=provider,
            provider_model_policy_guard=_model_policy_guard("provider-config-model"),
        )


def test_provider_hint_mismatch_is_surfaced_as_sanitized_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FakeLangfuse()
    client.prompt.config = {
        "typeflux": {"provider_hint": {"name": "openai", "model": "gpt-4o-mini"}},
    }
    registry = LangfusePromptRegistry(client=client)
    provider = FakeProvider([_GreetOutput(text="hi")])

    with caplog.at_level(logging.WARNING, logger="typeflux.execution.executor"):
        prepared = prepare_ai_activity_execution(
            activity=_greet_activity(),
            input_value=_GreetInput(name="Ada"),
            registry=registry,
            provider=provider,
        )

    assert prepared.resolved_prompt.metadata["langfuse.provider_hint_mismatch"] == {
        "hint": "openai",
        "provider": "fake",
    }
    warning = next(record for record in caplog.records if "provider hint" in record.getMessage())
    assert "'openai'" in warning.getMessage()
    assert "'fake'" in warning.getMessage()
    assert "Hello" not in warning.getMessage()


def test_matching_provider_hint_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    client = _FakeLangfuse()
    client.prompt.config = {"typeflux": {"provider_hint": {"name": "fake"}}}
    registry = LangfusePromptRegistry(client=client)
    provider = FakeProvider([_GreetOutput(text="hi")])

    with caplog.at_level(logging.WARNING, logger="typeflux.execution.executor"):
        prepared = prepare_ai_activity_execution(
            activity=_greet_activity(),
            input_value=_GreetInput(name="Ada"),
            registry=registry,
            provider=provider,
        )

    assert "langfuse.provider_hint_mismatch" not in prepared.resolved_prompt.metadata
    assert not [record for record in caplog.records if "provider hint" in record.getMessage()]


def test_langfuse_prompt_registry_rejects_invalid_provider_params_stop() -> None:
    client = _FakeLangfuse()
    client.prompt.config = {"provider_params": {"stop": [3]}}
    registry = LangfusePromptRegistry(client=client)

    with pytest.raises(PromptRegistryConfigError, match="provider_params invalid"):
        registry.resolve(PromptRef("demo", label="production"))


def test_langfuse_prompt_registry_resolves_explicit_chat_prompt() -> None:
    client = _PromptByTypeLangfuse(
        chat_prompt=[
            {"role": "system", "content": "Use the {{policy}} policy."},
            {"role": "user", "content": "Review {{claim_id}}", "name": "adjuster"},
        ]
    )
    registry = LangfusePromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("demo", label="production", prompt_type="chat"))

    assert resolved.messages == (
        ChatMessage(role="system", content="Use the {{policy}} policy."),
        ChatMessage(role="user", content="Review {{claim_id}}", name="adjuster"),
    )
    assert resolved.metadata["langfuse.prompt_type"] == "chat"
    assert (
        resolved.observation_context[LANGFUSE_PROMPT_CONTEXT_KEY] is client.returned_prompts["chat"]
    )
    assert client.get_calls == [
        {"name": "demo", "version": None, "label": "production", "type": "chat"}
    ]


def test_langfuse_prompt_registry_resolves_typeflux_content_parts() -> None:
    client = _PromptByTypeLangfuse(
        chat_prompt=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Review {{claim_id}}"},
                    {"type": "artifact_group", "group": "claim_documents"},
                ],
            },
        ]
    )
    registry = LangfusePromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("demo", label="production", prompt_type="chat"))

    assert resolved.messages == (
        ChatMessage(
            role="user",
            content=(
                TextPart("Review {{claim_id}}"),
                ArtifactGroupPart(group="claim_documents"),
            ),
        ),
    )


def test_langfuse_prompt_registry_auto_falls_back_to_chat_prompt() -> None:
    client = _PromptByTypeLangfuse(
        text_error=_SdkError("not found", status_code=404),
        chat_prompt=[
            SimpleNamespace(role="system", content="Be concise."),
            SimpleNamespace(role="assistant", content="Acknowledged."),
            SimpleNamespace(role="user", content="Classify {{subject}}"),
        ],
    )
    registry = LangfusePromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("demo", label="production"))

    assert resolved.messages == (
        ChatMessage(role="system", content="Be concise."),
        ChatMessage(role="assistant", content="Acknowledged."),
        ChatMessage(role="user", content="Classify {{subject}}"),
    )
    assert resolved.metadata["langfuse.prompt_type"] == "chat"
    assert (
        resolved.observation_context[LANGFUSE_PROMPT_CONTEXT_KEY] is client.returned_prompts["chat"]
    )
    assert client.get_calls == [
        {"name": "demo", "version": None, "label": "production", "type": "text"},
        {"name": "demo", "version": None, "label": "production", "type": "chat"},
    ]


def test_langfuse_prompt_registry_rejects_invalid_chat_message_role() -> None:
    client = _PromptByTypeLangfuse(chat_prompt=[{"role": "tool", "content": "nope"}])
    registry = LangfusePromptRegistry(client=client)

    with pytest.raises(PromptRegistryConfigError, match="role must be system"):
        registry.resolve(PromptRef("demo", prompt_type="chat"))


def test_langfuse_prompt_registry_rejects_unknown_chat_content_parts() -> None:
    client = _PromptByTypeLangfuse(chat_prompt=[{"role": "user", "content": [{"text": "hi"}]}])
    registry = LangfusePromptRegistry(client=client)

    with pytest.raises(PromptRegistryConfigError, match="content must be text or Typeflux"):
        registry.resolve(PromptRef("demo", prompt_type="chat"))


def test_langfuse_prompt_registry_creates_text_prompt() -> None:
    client = _FakeLangfuse()
    registry = LangfusePromptRegistry(client=client)

    version = registry.create_prompt(
        name="demo",
        prompt="Hello",
        label="production",
        tags=["typeflux"],
        config={"template_format": "mustache"},
        commit_message="bootstrap",
    )

    assert version == "8"
    assert client.created[0]["name"] == "demo"
    assert client.created[0]["labels"] == ["production"]
    assert client.created[0]["type"] == "text"


def test_inline_prompt_registry_missing_prompt_raises_not_found() -> None:
    registry = InlinePromptRegistry({})

    with pytest.raises(PromptNotFoundError) as exc_info:
        registry.resolve(PromptRef("missing"))

    assert exc_info.value.retryable is False


def test_langfuse_prompt_registry_classifies_not_found() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(_SdkError("not found", status_code=404))
    )

    with pytest.raises(PromptNotFoundError) as exc_info:
        registry.resolve(PromptRef("missing", label="production"))

    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == 404


def test_langfuse_prompt_registry_classifies_not_found_by_sdk_error_type() -> None:
    registry = LangfusePromptRegistry(client=_FailingLangfuse(_named_error("NotFoundError")))

    with pytest.raises(PromptNotFoundError) as exc_info:
        registry.resolve(PromptRef("missing", label="production"))

    assert exc_info.value.retryable is False


def test_langfuse_prompt_registry_classifies_auth_error() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(_SdkError("forbidden", status_code=403))
    )

    with pytest.raises(PromptRegistryAuthError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is False


@pytest.mark.parametrize("error_name", ["UnauthorizedError", "AccessDeniedError"])
def test_langfuse_prompt_registry_classifies_auth_error_by_sdk_error_type(error_name: str) -> None:
    registry = LangfusePromptRegistry(client=_FailingLangfuse(_named_error(error_name)))

    with pytest.raises(PromptRegistryAuthError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is False


def test_langfuse_prompt_registry_classifies_method_not_allowed_as_config() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(_named_error("MethodNotAllowedError"))
    )

    with pytest.raises(PromptRegistryConfigError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is False


def test_langfuse_prompt_registry_classifies_method_not_allowed_status_as_config() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(_SdkError("method not allowed", status_code=405))
    )

    with pytest.raises(PromptRegistryConfigError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is False


def test_langfuse_prompt_registry_classifies_unavailable_error() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(_SdkError("service unavailable", status_code=503))
    )

    with pytest.raises(PromptRegistryUnavailableError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is True


def test_langfuse_prompt_registry_classifies_unavailable_by_sdk_error_type() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(_named_error("ServiceUnavailableError"))
    )

    with pytest.raises(PromptRegistryUnavailableError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is True


@pytest.mark.parametrize("status_code", [408, 429])
def test_langfuse_prompt_registry_classifies_retryable_status_codes(status_code: int) -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(_SdkError("retry later", status_code=status_code))
    )

    with pytest.raises(PromptRegistryUnavailableError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is True


def test_langfuse_prompt_registry_classifies_dns_error_as_unavailable() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(OSError("[Errno 8] nodename nor servname provided, or not known"))
    )

    with pytest.raises(PromptRegistryUnavailableError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert exc_info.value.retryable is True


def test_langfuse_prompt_registry_does_not_classify_unknown_error_from_message_text() -> None:
    registry = LangfusePromptRegistry(
        client=_FailingLangfuse(RuntimeError("invalid forbidden prompt hit rate limit"))
    )

    with pytest.raises(PromptResolutionError) as exc_info:
        registry.resolve(PromptRef("demo"))

    assert type(exc_info.value) is PromptResolutionError
    assert exc_info.value.retryable is False


def test_langfuse_prompt_registry_validates_prompt_shape() -> None:
    client = SimpleNamespace(
        get_prompt=lambda name, *, version=None, label=None, type: SimpleNamespace(
            prompt=None, version=1, config={}
        )
    )
    registry = LangfusePromptRegistry(client=client)

    with pytest.raises(PromptRegistryConfigError):
        registry.resolve(PromptRef("demo"))


def _client_with_config(config: object) -> SimpleNamespace:
    return SimpleNamespace(
        get_prompt=lambda name, *, version=None, label=None, type: SimpleNamespace(
            prompt="Hello {{name}}", version=1, config=config
        )
    )


def test_langfuse_prompt_registry_rejects_non_mapping_config() -> None:
    registry = LangfusePromptRegistry(client=_client_with_config(["not", "a", "mapping"]))

    with pytest.raises(PromptRegistryConfigError, match="config must be a mapping"):
        registry.resolve(PromptRef("demo"))


def test_langfuse_prompt_registry_rejects_non_string_model() -> None:
    registry = LangfusePromptRegistry(client=_client_with_config({"model": 123}))

    with pytest.raises(PromptRegistryConfigError, match="model must be a string"):
        registry.resolve(PromptRef("demo"))


def test_langfuse_prompt_registry_rejects_non_numeric_temperature() -> None:
    registry = LangfusePromptRegistry(client=_client_with_config({"temperature": "hot"}))

    with pytest.raises(PromptRegistryConfigError, match="temperature must be numeric"):
        registry.resolve(PromptRef("demo"))


def test_langfuse_prompt_registry_falls_back_to_chat_on_config_status() -> None:
    client = _PromptByTypeLangfuse(
        text_error=_SdkError("unprocessable", status_code=422),
        chat_prompt=[{"role": "user", "content": "Chat {{value}}"}],
    )
    registry = LangfusePromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("demo", label="production"))

    assert resolved.metadata["langfuse.prompt_type"] == "chat"
    assert [call["type"] for call in client.get_calls] == ["text", "chat"]


class _SdkError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _named_error(name: str) -> Exception:
    return type(name, (Exception,), {})(name)


class _FailingLangfuse:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_prompt(self, name, *, version=None, label=None, type):
        raise self.error


class _PromptByTypeLangfuse:
    def __init__(
        self,
        *,
        text_prompt: str = "Text {{value}}",
        chat_prompt: list[object] | None = None,
        text_error: Exception | None = None,
    ) -> None:
        self.text_prompt = text_prompt
        self.chat_prompt = chat_prompt or [{"role": "user", "content": "Chat {{value}}"}]
        self.text_error = text_error
        self.get_calls = []
        self.returned_prompts = {}

    def get_prompt(self, name, *, version=None, label=None, type):
        self.get_calls.append({"name": name, "version": version, "label": label, "type": type})
        if type == "text":
            if self.text_error is not None:
                raise self.text_error
            prompt = SimpleNamespace(prompt=self.text_prompt, version=11, config={})
            self.returned_prompts["text"] = prompt
            return prompt
        if type == "chat":
            prompt = SimpleNamespace(
                prompt=self.chat_prompt,
                version=12,
                config={"provider_model": "gpt-4o-mini", "temperature": 0.1},
            )
            self.returned_prompts["chat"] = prompt
            return prompt
        raise AssertionError(f"unexpected prompt type: {type}")
