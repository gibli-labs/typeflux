from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, ValidationError

from typeflux.core import (
    AIActivity,
    CacheConfig,
    ChatMessage,
    PromptRef,
    ProviderParams,
    ResolvedPrompt,
)
from typeflux.core.artifacts import ProviderExtensionPart
from typeflux.execution.cache import InMemoryCacheStore
from typeflux.execution.controls import ProviderRetryPolicy
from typeflux.execution.executor import execute_ai_activity
from typeflux.prompts import InlinePromptRegistry
from typeflux.providers import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderTransientError,
    VercelAIGatewayProvider,
)
from typeflux.providers.vercel_ai_gateway import VERCEL_AI_GATEWAY_BASE_URL
from typeflux.yaml.runtime import _build_provider
from typeflux.yaml.spec import ProviderSpec


class Output(BaseModel):
    label: str


class Input(BaseModel):
    text: str


MODEL = "openai/gpt-4o-mini"
MESSAGES = [ChatMessage(role="user", content="Return label ok")]


def completion(parsed=None, finish="stop"):
    return SimpleNamespace(
        model="gpt-4o-mini-2024-07-18",
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(
                    parsed=Output(label="ok") if parsed is None else parsed,
                    refusal=None,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=4,
            total_tokens=9,
            prompt_tokens_details={"cached_tokens": 2},
        ),
        provider_metadata={"gateway": {"routing": {"finalProvider": "azure", "secret": "discard"}}},
    )


def provider_with(parse=None, model=MODEL):
    parse = parse if parse is not None else Mock(return_value=completion())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))
    return VercelAIGatewayProvider(default_model=model, openai_client=client), parse


def test_gateway_structured_output_and_usage():
    provider, parse = provider_with()
    usage = []
    result = provider.structured_call(
        messages=MESSAGES,
        output_schema=Output,
        usage_sink=usage.append,
        metadata={"secret": "must-not-forward"},
        provider_params=ProviderParams(
            temperature=0, max_tokens=64, top_p=0.9, stop=("END",), timeout=30
        ),
    )
    assert result == Output(label="ok")
    assert parse.call_count == 1
    request = parse.call_args.kwargs
    assert request == {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Return label ok"}],
        "response_format": Output,
        "temperature": 0,
        "max_tokens": 64,
        "top_p": 0.9,
        "stop": ["END"],
        "timeout": 30,
    }
    assert usage[0].model == MODEL
    assert usage[0].reported_model == "gpt-4o-mini-2024-07-18"
    assert usage[0].upstream_provider == "azure"
    assert usage[0].usage_details() == {"input": 5, "output": 4, "total": 9, "cache_read": 2}
    assert provider.provider_name == "vercel-ai-gateway"
    assert not provider.supports_session_cache


def test_gateway_activity_policy_cache_and_validation():
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("p"),
        cache=CacheConfig(),
        validation_retries=1,
    )
    registry = InlinePromptRegistry(
        {
            "p": ResolvedPrompt(
                ref=PromptRef("p"),
                messages=tuple(MESSAGES),
                resolved_version="v1",
            )
        }
    )
    store = InMemoryCacheStore()
    guard = Mock()
    provider, parse = provider_with(Mock(side_effect=[completion({"label": 12}), completion()]))
    args = dict(
        activity=activity,
        input_value=Input(text="hello"),
        registry=registry,
        provider_model_policy_guard=guard,
        cache_store=store,
    )
    assert execute_ai_activity(**args, provider=provider) == Output(label="ok")
    assert parse.call_count == 2  # Invalid output participates in existing repair semantics.
    assert execute_ai_activity(**args, provider=provider) == Output(label="ok")
    assert parse.call_count == 2
    other, other_parse = provider_with(model="anthropic/claude-haiku-4.5")
    execute_ai_activity(**args, provider=other)
    assert other_parse.call_count == 1
    assert guard.enforce_provider_model.call_args.kwargs["provider_name"] == "vercel-ai-gateway"
    assert (
        guard.enforce_provider_model.call_args.kwargs["provider_model"]
        == "anthropic/claude-haiku-4.5"
    )


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimitError),
        (408, ProviderTransientError),
        (409, ProviderTransientError),
        (503, ProviderTransientError),
        (400, ProviderConfigError),
        (422, ProviderConfigError),
    ],
)
def test_gateway_failures_are_classified_and_redacted(status, error_type):
    error = RuntimeError("fixture-secret-must-not-escape")
    error.status_code = status
    error.response = SimpleNamespace(headers={"retry-after": "2"})
    provider, parse = provider_with(Mock(side_effect=error))
    with pytest.raises(error_type) as raised:
        provider.structured_call(messages=MESSAGES, output_schema=Output)
    assert parse.call_count == 1
    assert raised.value.provider == "vercel-ai-gateway"
    assert raised.value.original is None
    assert raised.value.__cause__ is None
    assert "fixture-secret" not in str(raised.value) + repr(raised.value.to_metadata())
    if status in {429, 503}:
        assert raised.value.retry_after_seconds == 2


def test_gateway_transient_retry_uses_existing_policy():
    error = RuntimeError("unavailable")
    error.status_code = 503
    provider, parse = provider_with(Mock(side_effect=[error, completion()]))
    registry = InlinePromptRegistry(
        {"p": ResolvedPrompt(ref=PromptRef("p"), messages=tuple(MESSAGES))}
    )
    activity = AIActivity(
        name="classify", input_type=Input, output_type=Output, prompt_ref=PromptRef("p")
    )
    assert execute_ai_activity(
        activity=activity,
        input_value=Input(text="hello"),
        registry=registry,
        provider=provider,
        provider_retry_policy=ProviderRetryPolicy(max_attempts=2),
    ) == Output(label="ok")
    assert parse.call_count == 2


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_gateway_rejects_truncated_or_filtered_output(finish):
    provider, _ = provider_with(Mock(return_value=completion(finish=finish)))
    usage = []
    with pytest.raises(ProviderConfigError):
        provider.structured_call(messages=MESSAGES, output_schema=Output, usage_sink=usage.append)
    assert usage == []


def test_gateway_rejects_unsupported_options_before_transport():
    provider, parse = provider_with()
    with pytest.raises(ProviderConfigError, match="seed"):
        provider.structured_call(
            messages=MESSAGES, output_schema=Output, provider_params=ProviderParams(seed=1)
        )
    with pytest.raises(ProviderConfigError, match="text"):
        provider.structured_call(
            messages=[
                ChatMessage(
                    role="user",
                    content=(
                        ProviderExtensionPart(
                            provider="openai", payload={"type": "text", "text": "no"}
                        ),
                    ),
                )
            ],
            output_schema=Output,
        )
    assert parse.call_count == 0
    with pytest.raises(ProviderConfigError, match="author/model"):
        provider_with(model="gpt-4o-mini")


def test_gateway_yaml_configuration_and_authentication(monkeypatch):
    with pytest.raises(ValidationError, match="author/model"):
        ProviderSpec(type="vercel-ai-gateway")
    with pytest.raises(ValidationError, match="base_url"):
        ProviderSpec(type="vercel-ai-gateway", model=MODEL, base_url="https://example.com")
    spec = ProviderSpec(type="vercel-ai-gateway", params={"model": MODEL, "max_tokens": 64})
    assert spec.model == MODEL
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "unused-fixture-key")
    with pytest.raises(ProviderAuthError, match="VERCEL_API_KEY"):
        _build_provider(
            SimpleNamespace(runtime=SimpleNamespace(provider=spec)), enable_langfuse=False
        )
    monkeypatch.setenv("VERCEL_API_KEY", "gateway-fixture-key")
    provider = _build_provider(
        SimpleNamespace(runtime=SimpleNamespace(provider=spec)), enable_langfuse=False
    )
    assert isinstance(provider, VercelAIGatewayProvider)
    assert provider._api_key == "gateway-fixture-key"
    assert provider.default_provider_params.max_tokens == 64
    assert provider._client is None  # SDK loading/creation is lazy.


@pytest.mark.asyncio
async def test_gateway_async_timeout_and_cancellation():
    entered = asyncio.Event()

    async def parse(**kwargs):
        assert kwargs["timeout"] == 30
        entered.set()
        await asyncio.Event().wait()

    provider, _ = provider_with(parse)
    assert provider.supports_async_structured_call
    task = asyncio.create_task(
        provider.async_structured_call(
            messages=MESSAGES, output_schema=Output, provider_params=ProviderParams(timeout=30)
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "content,finish,expected",
    [
        ('{"label":"ok"}', "stop", None),
        ('{"label":12}', "stop", ValidationError),
        ('{"label":', "stop", ValidationError),
        ('{"label":"ok"}', "length", ProviderConfigError),
    ],
)
def test_gateway_real_sdk_wire_and_validation(content, finish, expected):
    sdk = pytest.importorskip("openai")
    http = pytest.importorskip("httpx2")
    requests = []

    def handle(request):
        requests.append(request)
        return http.Response(
            200,
            json={
                "id": "chatcmpl_fixture",
                "object": "chat.completion",
                "created": 1,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
            },
        )

    with http.Client(transport=http.MockTransport(handle)) as transport:
        client = sdk.OpenAI(
            api_key="fixture-key",
            base_url=VERCEL_AI_GATEWAY_BASE_URL,
            max_retries=0,
            http_client=transport,
        )
        provider = VercelAIGatewayProvider(default_model=MODEL, openai_client=client)
        if expected:
            with pytest.raises(expected):
                provider.structured_call(messages=MESSAGES, output_schema=Output)
        else:
            assert provider.structured_call(messages=MESSAGES, output_schema=Output) == Output(
                label="ok"
            )
    assert len(requests) == 1
    assert str(requests[0].url) == VERCEL_AI_GATEWAY_BASE_URL + "/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer fixture-key"
    assert json.loads(requests[0].content)["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_gateway_yaml_example_executes_validated_activity(monkeypatch):
    from pathlib import Path

    from examples.vercel_ai_gateway.schemas import Input as ExampleInput
    from typeflux.yaml import build_runtime, load_yaml_spec

    path = Path(__file__).parents[1] / "examples/vercel_ai_gateway/typeflux.yaml"
    spec = load_yaml_spec(path, env={})
    monkeypatch.setenv("VERCEL_API_KEY", "gateway-fixture-key")

    async def connect(*args, **kwargs):
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", connect)
    runtime = await build_runtime(spec)
    parse = Mock(return_value=completion({"label": "billing"}))
    runtime.provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))
    )
    result = execute_ai_activity(
        activity=runtime.activities["classify"],
        input_value=ExampleInput(text="invoice"),
        registry=runtime.registry,
        provider=runtime.provider,
    )
    assert result.label == "billing"
    assert parse.call_args.kwargs["model"] == MODEL
    assert parse.call_args.kwargs["max_tokens"] == 128


@pytest.mark.asyncio
async def test_gateway_lazy_clients_use_gateway_credentials_and_no_sdk_retries(monkeypatch):
    import sys

    configurations = []

    class Client:
        def __init__(self, **kwargs):
            configurations.append(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(parse=lambda **kwargs: completion())
            )

    class AsyncClient(Client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

            async def parse(**kwargs):
                return completion()

            self.chat.completions.parse = parse

    monkeypatch.setitem(
        sys.modules, "openai", SimpleNamespace(OpenAI=Client, AsyncOpenAI=AsyncClient)
    )
    monkeypatch.setenv("VERCEL_API_KEY", "environment-fixture")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.invalid")
    provider = VercelAIGatewayProvider(default_model=MODEL, api_key="explicit-fixture")
    assert configurations == []
    provider.structured_call(messages=MESSAGES, output_schema=Output)
    assert await provider.async_structured_call(messages=MESSAGES, output_schema=Output) == Output(
        label="ok"
    )
    assert configurations == [
        {"api_key": "explicit-fixture", "base_url": VERCEL_AI_GATEWAY_BASE_URL, "max_retries": 0},
        {"api_key": "explicit-fixture", "base_url": VERCEL_AI_GATEWAY_BASE_URL, "max_retries": 0},
    ]


def test_gateway_optional_sdk_and_safe_observation_identity(monkeypatch):
    import builtins

    from typeflux.observability.semantic import _LangfuseObservationHandle
    from typeflux.providers import ProviderUsage

    original_import = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name == "openai":
            raise ModuleNotFoundError("openai")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sdk)
    provider = VercelAIGatewayProvider(default_model=MODEL, api_key="fixture-key")
    with pytest.raises(RuntimeError, match=r"typeflux\[openai\]"):
        provider.structured_call(messages=MESSAGES, output_schema=Output)
    # Injected clients remain usable with the optional SDK absent.
    injected, _ = provider_with()
    assert injected.structured_call(messages=MESSAGES, output_schema=Output) == Output(label="ok")
    observation = Mock()
    redactor = Mock(side_effect=lambda value: {"redacted": value})
    handle = _LangfuseObservationHandle(observation, redact=redactor)
    handle.update_usage(
        ProviderUsage(
            model=MODEL, reported_model="reported", upstream_provider="azure", input_tokens=5
        )
    )
    assert observation.update.call_args.kwargs == {
        "model": MODEL,
        "usage_details": {"input": 5},
        "metadata": {
            "redacted": {
                "provider_response": {"reported_model": "reported", "upstream_provider": "azure"}
            }
        },
    }
    redactor.assert_called_once()
