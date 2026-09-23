from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import typeflux.execution.worker as worker_module
import typeflux.manifests.workflow as manifests_workflow_module
from typeflux.core.contracts import (
    AIActivity,
    ChatMessage,
    MapActivityContext,
    ModerationConfig,
    ModerationResult,
    PromptRef,
    ResolvedPrompt,
    TemporalActivityDescriptor,
)
from typeflux.execution.controls import ProviderCallLimiter, ProviderCallLimits
from typeflux.execution.starter import workflow_invocation_metadata, workflow_search_tags
from typeflux.execution.worker import TypefluxWorker
from typeflux.manifests import CodeProvenance
from typeflux.project.policy import ComposedProjectPolicy
from typeflux.project.policy_enforcement import (
    RuntimePolicyGuard,
)
from typeflux.prompts import InlinePromptRegistry
from typeflux.prompts.errors import PromptRegistryUnavailableError
from typeflux.providers.errors import ProviderConfigError, ProviderTransientError
from typeflux.testing import FakeProvider


class Input(BaseModel):
    text: str


class Output(BaseModel):
    text: str


class AsyncCapableProvider:
    provider_name = "fake"
    default_model = "fake-model"

    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
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
        raise AssertionError("worker should prefer async_structured_call when available")

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(
                {
                    "messages": list(messages),
                    "output_schema": output_schema,
                    "model": model,
                    "temperature": temperature,
                    "metadata": metadata,
                }
            )
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            return output_schema(text="world")
        finally:
            self.active -= 1


class AsyncDisabledProvider(AsyncCapableProvider):
    supports_async_structured_call = False

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        self.calls.append(
            {
                "messages": list(messages),
                "output_schema": output_schema,
                "model": model,
                "temperature": temperature,
                "metadata": metadata,
            }
        )
        return output_schema(text="sync-world")

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        raise AssertionError("worker should respect supports_async_structured_call=False")


def test_workflow_search_tags_are_conservative_and_preserve_user_tags() -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    metadata = workflow_invocation_metadata(
        workflow_name="CodeWorkflow",
        workflow_id="wf-high-cardinality",
        temporal_run_id="run-high-cardinality",
        task_queue="code-task-queue",
        activities=[activity],
        tags=["user-tag"],
    )
    metadata["typeflux"]["execution_manifest"]["code_provenance"]["git_sha"] = "abcdef123456"
    metadata["typeflux"]["execution_manifest"]["code_provenance"]["deployment_id"] = "deploy-123"
    metadata["typeflux"]["execution_manifest"]["code_provenance"]["environment"] = "prod"

    tags = workflow_search_tags(
        workflow_name="CodeWorkflow",
        activities=[activity],
        user_tags=["user-tag"],
        metadata=metadata,
    )

    assert set(tags) >= {
        "user-tag",
        "typeflux",
        "typeflux.workflow:CodeWorkflow",
        "typeflux.activity:wrapped",
        "typeflux.prompt:wrapped",
        "typeflux.env:prod",
    }
    assert "typeflux.git:abcdef123456" not in tags
    assert "typeflux.deployment:deploy-123" not in tags
    assert "typeflux.workflow_id:wf-high-cardinality" not in tags


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_calls_execute_ai_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    input_value = Input(text="hello")
    output_value = Output(text="world")
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])
    calls = []

    def fake_execute_ai_activity(**kwargs):
        calls.append(kwargs)
        return output_value

    monkeypatch.setattr(worker_module, "execute_ai_activity", fake_execute_ai_activity)
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
    )

    result = await fn(input_value)

    assert result == output_value
    assert calls[0]["activity"] is activity
    assert calls[0]["input_value"] == input_value
    assert calls[0]["registry"] is registry
    assert calls[0]["provider"] is provider


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_prefers_async_provider_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = AsyncCapableProvider()

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
    )

    result = await fn(Input(text="hello"))

    assert result == Output(text="world")
    assert len(provider.calls) == 1
    assert provider.calls[0]["messages"][0].content == "Echo hello"
    assert provider.calls[0]["output_schema"] is Output
    assert provider.calls[0]["metadata"]["typeflux"]["provider_controls"]["execution_mode"] == (
        "async"
    )


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_blocks_disallowed_prompt_model_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        from temporalio.exceptions import ApplicationError
    except ModuleNotFoundError:
        pytest.skip("temporalio is not installed")
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry(
        {
            "wrapped": ResolvedPrompt(
                ref=PromptRef("wrapped"),
                messages=(ChatMessage(role="user", content="Echo {{ text }}"),),
                resolved_version="inline",
                model="blocked-model",
            )
        }
    )
    provider = AsyncCapableProvider()
    guard = RuntimePolicyGuard(
        policy=ComposedProjectPolicy(
            selected_policy_ids=("regulated",),
            applied_policy_ids=("regulated",),
            policy_names=("regulated",),
            policy_hash="0" * 64,
            payload={
                "version": "1",
                "providers": {"allowed": {"fake": {"models": ["allowed-model"]}}},
            },
        )
    )
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
        provider_model_policy_guard=guard,
    )

    with pytest.raises(ApplicationError, match="blocked-model") as exc_info:
        await fn(Input(text="hello"))

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "ProviderPolicyError"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_respects_disabled_async_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = AsyncDisabledProvider()

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
    )

    result = await fn(Input(text="hello"))

    assert result == Output(text="sync-world")
    assert len(provider.calls) == 1
    assert provider.calls[0]["messages"][0].content == "Echo hello"
    assert provider.calls[0]["metadata"]["typeflux"]["provider_controls"]["execution_mode"] == (
        "sync_thread"
    )


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_coerces_mapping_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    output_value = Output(text="world")
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])
    calls = []

    def fake_execute_ai_activity(**kwargs):
        calls.append(kwargs)
        return output_value

    monkeypatch.setattr(worker_module, "execute_ai_activity", fake_execute_ai_activity)
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
    )

    result = await fn({"text": "hello"})

    assert result == output_value
    assert calls[0]["input_value"] == Input(text="hello")


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_coerces_mapping_map_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    output_value = Output(text="world")
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])
    contexts = []

    def fake_execute_ai_activity(**kwargs):
        return output_value

    def fake_build_invocation_context(
        activity_arg: AIActivity,
        *,
        task_queue: str | None = None,
        map_context: MapActivityContext | None = None,
    ) -> None:
        contexts.append((activity_arg, task_queue, map_context))
        return None

    monkeypatch.setattr(worker_module, "execute_ai_activity", fake_execute_ai_activity)
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        fake_build_invocation_context,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
    )

    result = await fn(
        Input(text="hello"),
        {
            "map_step_id": "review_evidence",
            "map_index": 2,
            "map_size": 4,
            "map_concurrency": 3,
        },
    )

    assert result == output_value
    assert contexts == [
        (
            activity,
            None,
            MapActivityContext(
                map_step_id="review_evidence",
                map_index=2,
                map_size=4,
                map_concurrency=3,
            ),
        )
    ]


def _cache_worker(activities: list[Any]) -> TypefluxWorker:
    return TypefluxWorker(
        client=None,
        task_queue="q",
        activities=activities,
        registry=InlinePromptRegistry({"p": "hi {{ text }}"}),
        provider=FakeProvider([]),
    )


def test_worker_registers_prep_and_release_for_cache_enabled_activities() -> None:
    # #60/#368: build_worker emits a {name}.__prepare_cache__ AND a
    # {name}.__release_cache__ activity per cache-enabled AIActivity, and neither
    # for activities that don't request caching.
    from typeflux.core.contracts import SessionCacheConfig

    cached = AIActivity(
        name="extract",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("p"),
        session_cache=SessionCacheConfig(ttl_seconds=300),
    )
    plain = AIActivity(
        name="plain", input_type=Input, output_type=Output, prompt_ref=PromptRef("p")
    )
    disabled = AIActivity(
        name="off",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("p"),
        session_cache=SessionCacheConfig(enabled=False),
    )

    generated = _cache_worker([cached, plain, disabled])._cache_prep_activities()

    assert sorted(getattr(fn, "__name__", None) for fn in generated) == [
        "extract.__prepare_cache__",
        "extract.__release_cache__",
    ]


@pytest.mark.asyncio
async def test_per_item_recovers_uncached_on_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #368b: if a reference-style cache vanished mid-fan-out, the provider raises
    # ProviderCacheUnavailableError; the per-item activity re-runs once uncached
    # (handle stripped) instead of failing the item.
    from typeflux.core.contracts import CachedSessionHandle, ProviderParams
    from typeflux.providers.errors import ProviderCacheUnavailableError

    class _StaleCacheProvider:
        provider_name = "ref"
        supported_provider_params = FakeProvider.supported_provider_params

        def __init__(self) -> None:
            self.default_provider_params = ProviderParams()
            self.seen_sessions: list[Any] = []

        def structured_call(self, *, messages, output_schema, cached_session=None, **kwargs):
            self.seen_sessions.append(cached_session)
            if cached_session is not None and cached_session.supported:
                raise ProviderCacheUnavailableError("cache gone", provider="ref")
            return output_schema(text="recovered")

    activity = AIActivity(
        name="wrapped", input_type=Input, output_type=Output, prompt_ref=PromptRef("wrapped")
    )
    provider = _StaleCacheProvider()
    monkeypatch.setattr(worker_module, "_build_invocation_context", lambda *a, **k: None)

    fn = worker_module.build_temporal_activity(
        activity, registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}), provider=provider
    )
    handle = CachedSessionHandle(
        provider="ref",
        identity_hash="h",
        supported=True,
        style="reference",
        cache_id="cc/1",
        reference_cached=True,
    )
    result = await fn(
        Input(text="hi"),
        MapActivityContext(
            map_step_id="s", map_index=0, map_size=2, map_concurrency=1, cached_session=handle
        ),
    )
    assert result == Output(text="recovered")
    # First call carried the (doomed) cache; the recovery retried with no cache.
    assert provider.seen_sessions[0] is not None and provider.seen_sessions[0].supported
    assert provider.seen_sessions[-1] is None


@pytest.mark.asyncio
async def test_cache_release_activity_calls_provider_release() -> None:
    # #368: the {name}.__release_cache__ activity forwards the handle to the
    # provider's best-effort release_cached_session.
    from typeflux.core.contracts import CachedSessionHandle

    released: list[CachedSessionHandle] = []

    class _ReleasingProvider:
        provider_name = "ref"
        default_model = "m"

        def release_cached_session(self, handle: CachedSessionHandle) -> None:
            released.append(handle)

    activity = AIActivity(
        name="extract", input_type=Input, output_type=Output, prompt_ref=PromptRef("p")
    )
    fn = worker_module.build_cache_release_activity(activity, provider=_ReleasingProvider())
    handle = CachedSessionHandle(
        provider="ref", identity_hash="h", supported=True, style="reference", cache_id="c/1"
    )
    await fn(handle)
    assert released == [handle]


def test_worker_rejects_cache_prep_name_collision() -> None:
    from typeflux.core.contracts import SessionCacheConfig

    cached = AIActivity(
        name="extract",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("p"),
        session_cache=SessionCacheConfig(),
    )
    # A real activity already occupies the generated prep name.
    collider = AIActivity(
        name="extract.__prepare_cache__",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("p"),
    )

    with pytest.raises(ValueError, match="collides"):
        _cache_worker([cached, collider])._cache_prep_activities()


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_threads_cached_session_from_map_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #60 Stage 1: a CachedSessionHandle carried on MapActivityContext must reach
    # the executor, so per-item map calls reuse the prepared session. A bare
    # context (no handle) threads None — today's behavior.
    from typeflux.core.contracts import CachedSessionHandle

    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    output_value = Output(text="world")
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])
    seen: list[Any] = []

    def fake_execute_ai_activity(**kwargs):
        seen.append(kwargs.get("cached_session"))
        return output_value

    monkeypatch.setattr(worker_module, "execute_ai_activity", fake_execute_ai_activity)
    monkeypatch.setattr(worker_module, "_build_invocation_context", lambda *a, **k: None)

    fn = worker_module.build_temporal_activity(activity, registry=registry, provider=provider)
    handle = CachedSessionHandle(provider="fake", identity_hash="h", supported=True, style="prefix")

    await fn(
        Input(text="hello"),
        MapActivityContext(
            map_step_id="s", map_index=0, map_size=2, map_concurrency=1, cached_session=handle
        ),
    )
    # No map context at all (non-map activity) ⇒ no handle.
    await fn(Input(text="hello"))

    assert seen == [handle, None]


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_passes_task_queue_to_invocation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])
    contexts: list[dict[str, Any]] = []

    def fake_execute_ai_activity(**kwargs):
        return Output(text="world")

    def fake_build_invocation_context(
        activity_arg: AIActivity,
        *,
        task_queue: str | None = None,
        map_context: MapActivityContext | None = None,
    ) -> None:
        contexts.append(
            {
                "activity": activity_arg,
                "task_queue": task_queue,
                "map_context": map_context,
            }
        )
        return None

    monkeypatch.setattr(worker_module, "execute_ai_activity", fake_execute_ai_activity)
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        fake_build_invocation_context,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
        task_queue="code-task-queue",
    )

    result = await fn(Input(text="hello"))

    assert result == Output(text="world")
    assert contexts == [
        {
            "activity": activity,
            "task_queue": "code-task-queue",
            "map_context": None,
        }
    ]


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])

    def blocking_execute_ai_activity(**kwargs):
        time.sleep(0.2)
        return Output(text="world")

    monkeypatch.setattr(worker_module, "execute_ai_activity", blocking_execute_ai_activity)
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
    )
    activity_task = asyncio.create_task(fn(Input(text="hello")))
    sleep_task = asyncio.create_task(asyncio.sleep(0.01))

    done, pending = await asyncio.wait(
        {activity_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED
    )

    assert sleep_task in done
    assert activity_task in pending
    assert await activity_task == Output(text="world")


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_limits_concurrent_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])
    lock = threading.Lock()
    active = 0
    max_active = 0
    call_metadata: list[dict[str, Any] | None] = []

    def blocking_execute_ai_activity(**kwargs):
        nonlocal active, max_active
        call_metadata.append(kwargs.get("provider_call_metadata"))
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return Output(text=kwargs["input_value"].text)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(worker_module, "execute_ai_activity", blocking_execute_ai_activity)
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
        provider_call_limiter=ProviderCallLimiter(ProviderCallLimits(max_concurrent=1)),
    )

    results = await asyncio.gather(fn(Input(text="one")), fn(Input(text="two")))

    assert results == [Output(text="one"), Output(text="two")]
    assert max_active == 1
    assert all(metadata is not None for metadata in call_metadata)
    assert call_metadata[0]["max_concurrent"] == 1


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_cancels_while_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = FakeProvider([])
    calls: list[str] = []

    def blocking_execute_ai_activity(**kwargs):
        calls.append(kwargs["input_value"].text)
        time.sleep(0.1)
        return Output(text=kwargs["input_value"].text)

    monkeypatch.setattr(worker_module, "execute_ai_activity", blocking_execute_ai_activity)
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
        provider_call_limiter=ProviderCallLimiter(ProviderCallLimits(max_concurrent=1)),
    )

    first = asyncio.create_task(fn(Input(text="first")))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(fn(Input(text="second")))
    await asyncio.sleep(0.01)
    second.cancel()

    with pytest.raises(asyncio.CancelledError):
        await second
    assert await first == Output(text="first")
    assert calls == ["first"]


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_cancels_queued_async_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    registry = InlinePromptRegistry({"wrapped": "Echo {{ text }}"})
    provider = AsyncCapableProvider(delay_seconds=0.1)

    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    fn = worker_module.build_temporal_activity(
        activity,
        registry=registry,
        provider=provider,
        provider_call_limiter=ProviderCallLimiter(ProviderCallLimits(max_concurrent=1)),
    )

    first = asyncio.create_task(fn(Input(text="first")))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(fn(Input(text="second")))
    await asyncio.sleep(0.01)
    second.cancel()

    with pytest.raises(asyncio.CancelledError):
        await second
    assert await first == Output(text="world")
    assert len(provider.calls) == 1
    assert provider.calls[0]["messages"][0].content == "Echo first"


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_converts_non_retryable_prompt_errors() -> None:
    try:
        from temporalio.exceptions import ApplicationError
    except ModuleNotFoundError:
        pytest.skip("temporalio is not installed")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=InlinePromptRegistry({}),
        provider=FakeProvider([]),
    )

    try:
        with pytest.raises(ApplicationError) as exc_info:
            await fn(Input(text="hello"))
    finally:
        monkeypatch.undo()

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "PromptNotFoundError"


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_converts_exhausted_validation_to_non_retryable() -> None:
    try:
        from temporalio.exceptions import ApplicationError
    except ModuleNotFoundError:
        pytest.skip("temporalio is not installed")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    try:
        Output.model_validate({})
    except ValidationError as exc:
        validation_error = exc
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
        validation_retries=1,
    )
    provider = FakeProvider([validation_error, validation_error])
    fn = worker_module.build_temporal_activity(
        activity,
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=provider,
    )

    try:
        with pytest.raises(ApplicationError) as exc_info:
            await fn(Input(text="hello"))
    finally:
        monkeypatch.undo()

    # Validation repair already retried locally; the terminal failure is
    # non-retryable so Temporal cannot multiply provider attempts.
    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "AIActivityOutputValidationError"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_converts_blocked_moderation_to_non_retryable() -> None:
    # #158: a blocked moderation verdict is terminal — the same output reproduces
    # it, so Temporal must not retry (and multiply provider spend).
    try:
        from temporalio.exceptions import ApplicationError
    except ModuleNotFoundError:
        pytest.skip("temporalio is not installed")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    def moderator(output: Output) -> ModerationResult:
        return ModerationResult(flagged=True, categories=("policy",))

    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
        moderation=ModerationConfig(moderator=moderator, on_violation="block"),
    )
    provider = FakeProvider([Output(text="hi")])
    fn = worker_module.build_temporal_activity(
        activity,
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=provider,
    )

    try:
        with pytest.raises(ApplicationError) as exc_info:
            await fn(Input(text="hello"))
    finally:
        monkeypatch.undo()

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "ModerationBlockedError"


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_leaves_retryable_prompt_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )

    class UnavailableRegistry:
        def resolve(self, ref: PromptRef):
            raise PromptRegistryUnavailableError("registry unavailable", ref=ref)

    fn = worker_module.build_temporal_activity(
        activity,
        registry=UnavailableRegistry(),
        provider=FakeProvider([]),
    )

    with pytest.raises(PromptRegistryUnavailableError):
        await fn(Input(text="hello"))


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_converts_non_retryable_provider_errors() -> None:
    try:
        from temporalio.exceptions import ApplicationError
    except ModuleNotFoundError:
        pytest.skip("temporalio is not installed")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )

    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=FakeProvider([ProviderConfigError("bad provider config", provider="fake")]),
    )

    try:
        with pytest.raises(ApplicationError) as exc_info:
            await fn(Input(text="hello"))
    finally:
        monkeypatch.undo()

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "ProviderConfigError"


@pytest.mark.asyncio
async def test_temporal_activity_wrapper_leaves_retryable_provider_errors() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        worker_module,
        "_build_invocation_context",
        lambda activity, **kwargs: None,
    )
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    fn = worker_module.build_temporal_activity(
        activity,
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=FakeProvider([ProviderTransientError("provider unavailable", provider="fake")]),
    )

    try:
        with pytest.raises(ProviderTransientError):
            await fn(Input(text="hello"))
    finally:
        monkeypatch.undo()


def test_typeflux_worker_registers_plain_temporal_activities_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import temporalio.worker
    from temporalio import activity as temporal_activity

    @temporal_activity.defn(name="normalize")
    async def normalize(value: Input) -> Output:
        return Output(text=value.text)

    ai = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    descriptor = TemporalActivityDescriptor(
        name="normalize",
        input_type=Input,
        output_type=Output,
        activity=normalize,
    )
    captured: dict[str, Any] = {}

    class _CapturingWorker:
        def __init__(self, client: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(temporalio.worker, "Worker", _CapturingWorker)
    worker = TypefluxWorker(
        client=_FakeTemporalClient(),
        task_queue="code-task-queue",
        activities=[ai, descriptor],
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=FakeProvider([]),
        workflows=[CodeWorkflow],
    )

    worker.build_worker()

    registered = captured["activities"]
    assert len(registered) == 2
    # The plain Temporal activity is registered as-is, never wrapped.
    assert registered[1] is normalize
    # The AI activity goes through the Typeflux wrapper, not the raw object.
    assert registered[0] is not ai
    assert getattr(registered[0], "__name__", None) == "wrapped"


@pytest.mark.asyncio
async def test_typeflux_worker_rollup_lists_plain_temporal_activities_as_names() -> None:
    from temporalio import activity as temporal_activity

    @temporal_activity.defn(name="normalize")
    async def normalize(value: Input) -> Output:
        return Output(text=value.text)

    ai = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    descriptor = TemporalActivityDescriptor(
        name="normalize",
        input_type=Input,
        output_type=Output,
        activity=normalize,
    )
    client = _FakeTemporalClient()
    langfuse_client = _FakeLangfuseClient()
    worker = TypefluxWorker(
        client=client,
        task_queue="code-task-queue",
        activities=[ai, descriptor],
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=FakeProvider([]),
        langfuse_client=langfuse_client,
        workflows=[CodeWorkflow],
    )

    await worker.execute_workflow(CodeWorkflow.run, Input(text="hello"), id="wf-mixed")

    manifest = langfuse_client.start_calls[0]["metadata"]["typeflux"]["execution_manifest"]
    entries = manifest["activities"]
    # The AI activity carries the prompt/provider rollup; the plain activity
    # appears as a planned activity name only, with no invented AI metadata.
    assert entries[0]["activity_name"] == "wrapped"
    assert "prompt_ref" in entries[0]
    assert entries[1] == "normalize"
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_typeflux_worker_execute_workflow_uses_task_queue_and_result_type() -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    client = _FakeTemporalClient()
    worker = TypefluxWorker(
        client=client,
        task_queue="code-task-queue",
        activities=[activity],
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=FakeProvider([]),
        workflows=[CodeWorkflow],
    )

    result = await worker.execute_workflow(CodeWorkflow.run, Input(text="hello"), id="wf-code")

    assert result == Output(text="done")
    assert client.calls[0]["workflow"] is CodeWorkflow.run
    assert client.calls[0]["input_value"] == Input(text="hello")
    assert client.calls[0]["kwargs"]["id"] == "wf-code"
    assert client.calls[0]["kwargs"]["task_queue"] == "code-task-queue"
    assert client.calls[0]["kwargs"]["result_type"] is Output


@pytest.mark.asyncio
async def test_typeflux_worker_execute_workflow_rolls_up_langfuse_root_for_code_workflows() -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    client = _FakeTemporalClient()
    langfuse_client = _FakeLangfuseClient()
    worker = TypefluxWorker(
        client=client,
        task_queue="code-task-queue",
        activities=[activity],
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=FakeProvider([]),
        langfuse_client=langfuse_client,
        workflows=[CodeWorkflow],
    )

    result = await worker.execute_workflow(
        CodeWorkflow.run,
        Input(text="hello"),
        id="wf-code-traced",
        tags=["code"],
        metadata={"source": "unit"},
    )

    assert result == Output(text="done")
    assert langfuse_client.start_calls[0]["name"] == "TypefluxWorkflow:CodeWorkflow"
    assert langfuse_client.start_calls[0]["input"] == {"text": "hello"}
    metadata = langfuse_client.start_calls[0]["metadata"]
    assert metadata["source"] == "unit"
    assert set(metadata["tags"]) >= {
        "code",
        "typeflux",
        "typeflux.workflow:CodeWorkflow",
        "typeflux.activity:wrapped",
        "typeflux.prompt:wrapped",
    }
    assert metadata["typeflux"]["workflow"] == {
        "workflow_name": "CodeWorkflow",
        "workflow_id": "wf-code-traced",
        "task_queue": "code-task-queue",
    }
    assert metadata["typeflux"]["activities"] == ["wrapped"]
    execution_manifest = metadata["typeflux"]["execution_manifest"]
    assert execution_manifest["activities"][0]["activity_name"] == "wrapped"
    assert execution_manifest["activities"][0]["prompt_messages_hash"]
    assert langfuse_client.trace_io_calls == [
        {"input": {"text": "hello"}},
        {"output": {"text": "done"}},
    ]
    assert langfuse_client.observations[0].updates[-1] == {"output": {"text": "done"}}


@pytest.mark.asyncio
async def test_typeflux_worker_rollup_keeps_unresolved_prompt_failures() -> None:
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    client = _FakeTemporalClient()
    langfuse_client = _FakeLangfuseClient()
    worker = TypefluxWorker(
        client=client,
        task_queue="code-task-queue",
        activities=[activity],
        registry=InlinePromptRegistry({}),
        provider=FakeProvider([]),
        langfuse_client=langfuse_client,
        workflows=[CodeWorkflow],
    )

    await worker.execute_workflow(CodeWorkflow.run, Input(text="hello"), id="wf-code-traced")

    activity_rollup = langfuse_client.start_calls[0]["metadata"]["typeflux"]["execution_manifest"][
        "activities"
    ][0]
    assert activity_rollup["activity_name"] == "wrapped"
    assert activity_rollup["prompt_resolution"]["status"] == "failed"
    assert activity_rollup["prompt_resolution"]["error_type"] == "PromptNotFoundError"
    assert "Echo" not in str(activity_rollup)


@pytest.mark.asyncio
async def test_typeflux_worker_traced_execute_uses_start_workflow_and_records_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance_calls = []

    def fake_collect_code_provenance():
        provenance_calls.append(None)
        return CodeProvenance(
            available=True,
            source="git",
            git_ref="main",
            git_sha="abc123",
            package_version="0.1.0",
        )

    monkeypatch.setattr(
        manifests_workflow_module,
        "collect_code_provenance",
        fake_collect_code_provenance,
    )
    activity = AIActivity(
        name="wrapped",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("wrapped"),
    )
    client = _FakeStartTemporalClient()
    langfuse_client = _FakeLangfuseClient()
    worker = TypefluxWorker(
        client=client,
        task_queue="code-task-queue",
        activities=[activity],
        registry=InlinePromptRegistry({"wrapped": "Echo {{ text }}"}),
        provider=FakeProvider([]),
        langfuse_client=langfuse_client,
        workflows=[CodeWorkflow],
    )

    result = await worker.execute_workflow(
        CodeWorkflow.run, Input(text="hello"), id="wf-code-traced"
    )

    assert result == Output(text="done")
    assert client.start_calls[0]["kwargs"]["id"] == "wf-code-traced"
    initial_manifest = langfuse_client.start_calls[0]["metadata"]["typeflux"]["execution_manifest"]
    metadata_update = langfuse_client.observations[0].updates[-2]["metadata"]
    updated_manifest = metadata_update["typeflux"]["execution_manifest"]
    assert updated_manifest["temporal_run_id"] == "run-123"
    assert updated_manifest["code_provenance"] == initial_manifest["code_provenance"]
    assert updated_manifest["manifest_hash"] != initial_manifest["manifest_hash"]
    assert len(provenance_calls) == 1


class CodeWorkflow:
    async def run(self, input_value: Input) -> Output:
        return Output(text=input_value.text)


class _FakeTemporalClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_workflow(self, workflow, input_value, **kwargs):
        self.calls.append(
            {
                "workflow": workflow,
                "input_value": input_value,
                "kwargs": kwargs,
            }
        )
        result_type = kwargs.get("result_type")
        return result_type(text="done")


class _FakeStartTemporalClient:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []

    async def start_workflow(self, workflow, input_value, **kwargs):
        self.start_calls.append(
            {
                "workflow": workflow,
                "input_value": input_value,
                "kwargs": kwargs,
            }
        )
        return _FakeWorkflowHandle(kwargs.get("result_type"))


class _FakeWorkflowHandle:
    run_id = "run-123"

    def __init__(self, result_type: type[BaseModel]) -> None:
        self._result_type = result_type

    async def result(self):
        return self._result_type(text="done")


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.trace_io_calls: list[dict[str, Any]] = []
        self.observations: list[_FakeLangfuseObservation] = []

    def start_as_current_observation(self, **kwargs):
        self.start_calls.append(kwargs)
        observation = _FakeLangfuseObservation()
        self.observations.append(observation)
        return _FakeLangfuseContext(observation)

    def set_current_trace_io(self, **kwargs):
        self.trace_io_calls.append(kwargs)


class _FakeLangfuseContext:
    def __init__(self, observation) -> None:
        self._observation = observation

    def __enter__(self):
        return self._observation

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakeLangfuseObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
