from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

import typeflux.execution.worker as worker_module
from typeflux.core.contracts import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.execution.controls import (
    ProviderCallLimiter,
    ProviderCallLimits,
    ProviderRateLimitController,
    ProviderRateLimitPolicy,
    ProviderRateLimitProviderPolicy,
    ProviderRetryPolicy,
)
from typeflux.prompts import InlinePromptRegistry
from typeflux.providers.errors import ProviderRateLimitError


class Input(BaseModel):
    text: str


class Output(BaseModel):
    text: str


class _SignalingSemaphore(asyncio.Semaphore):
    def __init__(self, value: int, *, on_block: Callable[[], None]) -> None:
        super().__init__(value)
        self._on_block = on_block

    async def acquire(self) -> bool:
        # Test-only hook: signal when an acquire would block behind a held provider slot.
        if self.locked():
            self._on_block()
        return await super().acquire()


class _SignalingLimiter(ProviderCallLimiter):
    def __init__(self, limits: ProviderCallLimits) -> None:
        super().__init__(limits)
        if limits.max_concurrent is None:
            raise AssertionError("_SignalingLimiter requires a concurrency limit")
        self.second_waiting = asyncio.Event()
        self._semaphore = _SignalingSemaphore(
            limits.max_concurrent,
            on_block=self.second_waiting.set,
        )


class BlockingProvider:
    provider_name = "fake"
    default_model = "fallback-model"

    def __init__(self, responses: list[BaseModel | Exception] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del messages, temperature
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(
                {
                    "model": model,
                    "metadata": metadata,
                }
            )
            time.sleep(0.05)
            if self.responses:
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
            return output_schema(text="ok")
        finally:
            with self._lock:
                self.active -= 1


class ControlledBlockingProvider(BlockingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered_call = threading.Event()
        self.release_call = threading.Event()

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del messages, temperature
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(
                {
                    "model": model,
                    "metadata": metadata,
                }
            )
            self.entered_call.set()
            if not self.release_call.wait(timeout=2.0):
                raise AssertionError("timed out waiting for controlled provider release")
            return output_schema(text="ok")
        finally:
            with self._lock:
                self.active -= 1


class AsyncBlockingProvider:
    provider_name = "fake"
    default_model = "fallback-model"

    def __init__(self, responses: list[BaseModel | Exception] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        raise AssertionError("worker should prefer async provider calls")

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del messages, temperature
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(
                {
                    "model": model,
                    "metadata": metadata,
                }
            )
            await asyncio.sleep(0.05)
            if self.responses:
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
            return output_schema(text="ok")
        finally:
            self.active -= 1


def test_provider_rate_limit_policy_precedence() -> None:
    policy = ProviderRateLimitPolicy(
        default=ProviderCallLimits(max_concurrent=9),
        providers={
            "openai": ProviderRateLimitProviderPolicy(
                limits=ProviderCallLimits(max_concurrent=6),
                models={"gpt-4o-mini": ProviderCallLimits(max_concurrent=3)},
            )
        },
    )

    model = policy.select(provider_name="openai", provider_model="gpt-4o-mini")
    provider = policy.select(provider_name="openai", provider_model="gpt-4o")
    default = policy.select(provider_name="anthropic", provider_model="claude")

    assert model.policy_source == "model"
    assert model.limits == ProviderCallLimits(max_concurrent=3)
    assert provider.policy_source == "provider"
    assert provider.limits == ProviderCallLimits(max_concurrent=6)
    assert default.policy_source == "default"
    assert default.limits == ProviderCallLimits(max_concurrent=9)


def test_provider_rate_limit_policy_uses_legacy_default_after_policy_defaults() -> None:
    policy = ProviderRateLimitPolicy()

    selection = policy.select(
        provider_name="openai",
        provider_model="gpt-4o-mini",
        legacy_limits=ProviderCallLimits(max_concurrent=2),
    )

    assert selection.policy_source == "legacy_default"
    assert selection.policy_key == "legacy_default"
    assert selection.limits == ProviderCallLimits(max_concurrent=2)


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_uses_model_specific_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _activity()
    provider = BlockingProvider()
    controller = ProviderRateLimitController(
        policy=ProviderRateLimitPolicy(
            providers={
                "fake": ProviderRateLimitProviderPolicy(
                    limits=ProviderCallLimits(max_concurrent=5),
                    models={"slow-model": ProviderCallLimits(max_concurrent=1)},
                )
            }
        )
    )

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=_registry(model="slow-model"),
        provider=provider,
        provider_rate_limit_controller=controller,
    )

    results = await asyncio.gather(fn(Input(text="one")), fn(Input(text="two")))

    assert results == [Output(text="ok"), Output(text="ok")]
    assert provider.max_active == 1
    controls = provider.calls[0]["metadata"]["typeflux"]["provider_controls"]
    assert controls["provider_name"] == "fake"
    assert controls["provider_model"] == "slow-model"
    assert controls["policy_source"] == "model"
    assert controls["policy_key"] == "provider:fake/model:slow-model"
    assert controls["max_concurrent"] == 1
    assert controls["execution_mode"] == "sync_thread"


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_uses_model_specific_policy_for_async_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _activity()
    provider = AsyncBlockingProvider()
    controller = ProviderRateLimitController(
        policy=ProviderRateLimitPolicy(
            providers={
                "fake": ProviderRateLimitProviderPolicy(
                    limits=ProviderCallLimits(max_concurrent=5),
                    models={"slow-model": ProviderCallLimits(max_concurrent=1)},
                )
            }
        )
    )

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=_registry(model="slow-model"),
        provider=provider,
        provider_rate_limit_controller=controller,
    )

    results = await asyncio.gather(fn(Input(text="one")), fn(Input(text="two")))

    assert results == [Output(text="ok"), Output(text="ok")]
    assert provider.max_active == 1
    controls = provider.calls[0]["metadata"]["typeflux"]["provider_controls"]
    assert controls["provider_name"] == "fake"
    assert controls["provider_model"] == "slow-model"
    assert controls["policy_source"] == "model"
    assert controls["policy_key"] == "provider:fake/model:slow-model"
    assert controls["max_concurrent"] == 1
    assert controls["execution_mode"] == "async"


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_uses_provider_policy_without_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _activity()
    provider = BlockingProvider()
    controller = ProviderRateLimitController(
        policy=ProviderRateLimitPolicy(
            providers={
                "fake": ProviderRateLimitProviderPolicy(limits=ProviderCallLimits(max_concurrent=1))
            }
        )
    )

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=_registry(model=None),
        provider=provider,
        provider_rate_limit_controller=controller,
    )

    await asyncio.gather(fn(Input(text="one")), fn(Input(text="two")))

    assert provider.max_active == 1
    controls = provider.calls[0]["metadata"]["typeflux"]["provider_controls"]
    assert controls["provider_model"] == "fallback-model"
    assert controls["policy_source"] == "provider"
    assert controls["policy_key"] == "provider:fake"


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_policy_cancels_while_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _activity()
    provider = ControlledBlockingProvider()
    controller = ProviderRateLimitController(
        policy=ProviderRateLimitPolicy(default=ProviderCallLimits(max_concurrent=1))
    )
    limiter = _SignalingLimiter(ProviderCallLimits(max_concurrent=1))

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    monkeypatch.setattr(controller, "limiter_for", lambda selection: limiter)
    fn = worker_module.build_temporal_activity(
        activity,
        registry=_registry(model="slow-model"),
        provider=provider,
        provider_rate_limit_controller=controller,
    )

    first = asyncio.create_task(fn(Input(text="first")))
    assert await asyncio.to_thread(provider.entered_call.wait, 1.0)
    try:
        second = asyncio.create_task(fn(Input(text="second")))
        await asyncio.wait_for(limiter.second_waiting.wait(), timeout=1.0)
        second.cancel()

        with pytest.raises(asyncio.CancelledError):
            await second
    finally:
        provider.release_call.set()
    assert await asyncio.wait_for(first, timeout=1.0) == Output(text="ok")
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_policy_metadata_survives_rate_limit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _activity()
    provider = BlockingProvider(
        [
            ProviderRateLimitError("rate limited", provider="fake", status_code=429),
            Output(text="ok"),
        ]
    )
    controller = ProviderRateLimitController(
        policy=ProviderRateLimitPolicy(default=ProviderCallLimits(max_concurrent=2))
    )

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=_registry(model="retry-model"),
        provider=provider,
        provider_rate_limit_controller=controller,
        provider_retry_policy=ProviderRetryPolicy(max_attempts=2),
    )

    assert await fn(Input(text="first")) == Output(text="ok")

    retry_controls = provider.calls[1]["metadata"]["typeflux"]["provider_controls"]
    assert retry_controls["policy_source"] == "default"
    assert retry_controls["provider_model"] == "retry-model"
    assert retry_controls["retry_attempt"] == 1
    assert retry_controls["previous_error_type"] == "ProviderRateLimitError"
    assert retry_controls["previous_error_status_code"] == 429
    assert retry_controls["rate_limited"] is True


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_policy_metadata_without_active_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _activity()
    provider = BlockingProvider()
    controller = ProviderRateLimitController(policy=ProviderRateLimitPolicy())

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=_registry(model="unlimited-model"),
        provider=provider,
        provider_rate_limit_controller=controller,
    )

    assert await fn(Input(text="first")) == Output(text="ok")

    controls = provider.calls[0]["metadata"]["typeflux"]["provider_controls"]
    assert controls["provider_name"] == "fake"
    assert controls["provider_model"] == "unlimited-model"
    assert controls["policy_source"] == "none"
    assert controls["policy_key"] == "none:fake:unlimited-model"
    assert controls["queued"] is False
    assert controls["throttled"] is False


def _activity() -> AIActivity:
    return AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )


def _registry(*, model: str | None) -> InlinePromptRegistry:
    return InlinePromptRegistry(
        {
            "wrapped": ResolvedPrompt(
                ref=PromptRef("wrapped"),
                messages=(ChatMessage(role="user", content="Echo {{ text }}"),),
                resolved_version="prompt-v1",
                model=model,
            )
        }
    )


def test_provider_retry_backoff_grows_by_multiplier() -> None:
    policy = ProviderRetryPolicy(initial_backoff_seconds=0.5, backoff_multiplier=2.0)

    assert policy.backoff_seconds(0) == 0.5
    assert policy.backoff_seconds(1) == 1.0
    assert policy.backoff_seconds(2) == 2.0


def test_provider_retry_backoff_clamps_to_max() -> None:
    policy = ProviderRetryPolicy(
        initial_backoff_seconds=1.0,
        backoff_multiplier=3.0,
        max_backoff_seconds=4.0,
    )

    assert policy.backoff_seconds(0) == 1.0
    assert policy.backoff_seconds(1) == 3.0
    assert policy.backoff_seconds(2) == 4.0


def test_provider_retry_rejects_backoff_multiplier_below_one() -> None:
    with pytest.raises(ValueError, match="backoff_multiplier must be >= 1"):
        ProviderRetryPolicy(backoff_multiplier=0.5)
