from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel, Field, ValidationError

from typeflux import ChatMessage, ProviderParams, TypeSafeProvider
from typeflux.core import AIActivity, CacheConfig, PromptRef, ResolvedPrompt
from typeflux.execution.cache import InMemoryCacheStore
from typeflux.execution.controls import ProviderRetryPolicy
from typeflux.execution.executor import execute_ai_activity
from typeflux.prompts import InlinePromptRegistry
from typeflux.providers import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderTransientError,
)
from typeflux.yaml.runtime import _build_provider
from typeflux.yaml.spec import ProviderSpec

QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Route this ticket",
        "criteria": {"billing": "Payments", "technical": "Bugs"},
    },
    "urgency": {"type": "score", "instructions": "Urgency", "criteria": ["Routine", "Urgent"]},
    "refund": {"type": "noul", "instructions": "Requests a refund"},
}
MESSAGES = [
    ChatMessage(role="system", content=json.dumps(QUESTIONS)),
    ChatMessage(role="user", content="Refund my duplicate charge"),
]
RESPONSE = {
    "model": "jev-1.14.0",
    "answers": {
        "route": {
            "type": "choice",
            "choice": "billing",
            "confidence": 0.9,
            "probabilities": {"billing": 0.95, "technical": 0.05},
        },
        "urgency": {
            "type": "score",
            "score": 0.7,
            "confidence": 0.8,
            "probabilities": {"0": 0.3, "1": 0.7},
            "legend": {"0": "Routine", "1": "Urgent"},
        },
        "refund": {"type": "noul", "noul": 0.99},
    },
    "usage": {"input_tokens": 100, "output_tokens": 20},
}


class Input(BaseModel):
    text: str


class Noul(BaseModel):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


class Output(BaseModel):
    route: dict
    urgency: dict
    refund: Noul


def fixture(call=None, model="jev-latest"):
    call = call if call is not None else Mock(return_value=copy.deepcopy(RESPONSE))
    return TypeSafeProvider(
        default_model=model, typesafe_client=SimpleNamespace(system_one=call)
    ), call


def test_typesafe_native_decisions_usage_and_overrides():
    provider, call = fixture()
    usage = []
    result = provider.structured_call(
        messages=MESSAGES,
        output_schema=Output,
        usage_sink=usage.append,
        model="jev-1.13.0",
        provider_params=ProviderParams(timeout=2),
        metadata={"secret": "private"},
    )
    assert result.model_dump() == RESPONSE["answers"]
    assert call.call_args.kwargs == {
        "model": "jev-1.13.0",
        "state": MESSAGES[1].content,
        "questions": QUESTIONS,
        "timeout": 2,
    }
    assert usage[0].model == "jev-1.13.0"
    assert usage[0].reported_model == "jev-1.14.0"
    assert usage[0].usage_details() == {"input": 100, "output": 20, "total": 120}
    assert not provider.supports_session_cache


def test_typesafe_activity_policy_cache_and_retry():
    error = RuntimeError("fixture-secret")
    error.status_code = 529
    provider, call = fixture(Mock(side_effect=[error, RESPONSE, RESPONSE]))
    registry = InlinePromptRegistry(
        {"p": ResolvedPrompt(ref=PromptRef("p"), messages=tuple(MESSAGES))}
    )
    activity = AIActivity(
        name="decide",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("p"),
        cache=CacheConfig(),
    )
    guard = Mock()
    args = dict(
        activity=activity,
        input_value=Input(text="ticket"),
        registry=registry,
        cache_store=InMemoryCacheStore(),
        provider_model_policy_guard=guard,
        provider_retry_policy=ProviderRetryPolicy(max_attempts=2),
    )
    assert execute_ai_activity(**args, provider=provider).refund.noul == 0.99
    execute_ai_activity(**args, provider=provider)
    assert call.call_count == 2
    other, _ = fixture(call, "jev-1.13.0")
    execute_ai_activity(**args, provider=other)
    assert call.call_count == 3
    assert guard.enforce_provider_model.call_args.kwargs["provider_name"] == "typesafe"
    assert guard.enforce_provider_model.call_args.kwargs["provider_model"] == "jev-1.13.0"


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimitError),
        (529, ProviderTransientError),
        (503, ProviderTransientError),
        (422, ProviderConfigError),
    ],
)
def test_typesafe_errors_are_classified_and_redacted(status, error_type):
    error = RuntimeError("fixture-secret")
    error.status_code = status
    error.response = SimpleNamespace(headers={"retry-after": "2"})
    provider, call = fixture(Mock(side_effect=error))
    with pytest.raises(error_type) as raised:
        provider.structured_call(messages=MESSAGES, output_schema=Output)
    assert call.call_count == 1
    assert raised.value.original is None
    assert raised.value.__cause__ is None
    assert "fixture-secret" not in str(raised.value)
    if status in (429, 529, 503):
        assert raised.value.retry_after_seconds == 2


@pytest.mark.parametrize(
    "question",
    [
        {},
        {"q": {"type": "box", "instructions": "Find it"}},
        {"q": {"type": "score", "instructions": "Rate", "criteria": ["Only one"]}},
        {"q": {"type": "choice", "instructions": "Choose", "criteria": {}}},
    ],
)
def test_typesafe_rejects_invalid_questions_before_transport(question):
    provider, call = fixture()
    with pytest.raises(ProviderConfigError):
        provider.structured_call(
            messages=[ChatMessage(role="system", content=json.dumps(question)), MESSAGES[1]],
            output_schema=Output,
        )
    assert not call.called


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["answers"].pop("refund"),
        lambda r: r["answers"]["refund"].update(noul=1.1),
        lambda r: r["answers"]["route"].update(choice="unknown"),
        lambda r: r["answers"]["urgency"].update(score=float("nan")),
    ],
)
def test_typesafe_rejects_malformed_answers(mutation):
    response = copy.deepcopy(RESPONSE)
    mutation(response)
    provider, _ = fixture(Mock(return_value=response))
    with pytest.raises(ProviderConfigError):
        provider.structured_call(messages=MESSAGES, output_schema=Output)


def test_typesafe_rejects_unsupported_params_and_validates_output():
    provider, call = fixture()
    with pytest.raises(ProviderConfigError, match="temperature"):
        provider.structured_call(messages=MESSAGES, output_schema=Output, temperature=0)
    assert not call.called
    with pytest.raises(ValidationError):
        provider.structured_call(messages=MESSAGES, output_schema=Input)


@pytest.mark.asyncio
async def test_typesafe_async_and_cancellation():
    call = AsyncMock(return_value=RESPONSE)
    provider = TypeSafeProvider(async_typesafe_client=SimpleNamespace(system_one=call))
    assert (
        await provider.async_structured_call(messages=MESSAGES, output_schema=Output)
    ).refund.noul == 0.99
    entered = asyncio.Event()

    async def pending(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    call.side_effect = pending
    task = asyncio.create_task(
        provider.async_structured_call(messages=MESSAGES, output_schema=Output)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_typesafe_yaml_and_auth(monkeypatch):
    monkeypatch.setattr("typeflux.providers.typesafe.load_env", lambda: None)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated")
    spec = ProviderSpec(type="typesafe")
    assert spec.model == "jev-1.14.0"
    with pytest.raises(ProviderAuthError, match="TYPESAFE_API_KEY"):
        _build_provider(
            SimpleNamespace(runtime=SimpleNamespace(provider=spec)), enable_langfuse=False
        )
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")
    provider = _build_provider(
        SimpleNamespace(runtime=SimpleNamespace(provider=spec)), enable_langfuse=False
    )
    assert provider.provider_name == "typesafe"
    with pytest.raises(ValidationError, match="base_url"):
        ProviderSpec(type="typesafe", base_url="https://wrong.invalid")


def test_typesafe_official_sdk_wire():
    sdk = pytest.importorskip("typesafe_sdk")
    httpx = pytest.importorskip("httpx2")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=RESPONSE)

    with sdk.TypeSafeClient(
        api_key="fixture-key",
        transport=httpx.MockTransport(respond),
        retry=sdk.RetryPolicy(max_retries=0),
        compression=False,
    ) as client:
        provider = TypeSafeProvider(typesafe_client=client)
        result = provider.structured_call(messages=MESSAGES, output_schema=Output)
    assert result.refund.noul == 0.99
    assert str(requests[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert json.loads(requests[0].content) == {
        "model": "jev-1.14.0",
        "questions": QUESTIONS,
        "state": MESSAGES[1].content,
    }


@pytest.mark.asyncio
async def test_typesafe_yaml_example_executes(monkeypatch):
    from typeflux.yaml.loader import load_yaml_spec
    from typeflux.yaml.runtime import build_runtime

    path = Path(__file__).parents[1] / "examples/typesafe_decisions/typeflux.yaml"
    provider, call = fixture(
        Mock(
            return_value={
                **RESPONSE,
                "answers": {
                    "route": {
                        **RESPONSE["answers"]["route"],
                        "probabilities": {"billing": 0.95, "technical": 0.05, "human_review": 0.0},
                    }
                },
            }
        )
    )
    spec = load_yaml_spec(path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")

    async def connect(*args, **kwargs):
        return object()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", connect)
    runtime = await build_runtime(spec)
    result = execute_ai_activity(
        activity=runtime.activities["decide"],
        input_value=runtime.activities["decide"].input_type(text="A duplicate charge"),
        registry=runtime.registry,
        provider=provider,
    )
    assert result.selected_path() == "billing"
    assert call.call_args.kwargs["state"] == "A duplicate charge"
