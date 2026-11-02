"""Local SDK acceptance using the real activity pipeline."""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from datetime import timedelta
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from typeflux.core.contracts import (
    ActivityContext,
    AIActivity,
    CachedSessionHandle,
    ModerationResult,
    OutputCheckViolation,
    PromptRef,
    ProviderParams,
)
from typeflux.observability.backend import NoOpTraceWriter, NoOpWorkflowObservation
from typeflux.providers.errors import ProviderTransientError
from typeflux.yaml import RuntimeCapabilityError, TypefluxYamlSpec, build_local_runtime
from typeflux.yaml import runtime as services


class Item(BaseModel):
    n: int


class Batch(BaseModel):
    items: list[Item]


class Branches(BaseModel):
    left: Item
    right: Item | None


class Provider:
    provider_name = "fake"
    supported_provider_params = frozenset(ProviderParams.empty().to_dict(include_empty=True))
    default_provider_params = ProviderParams()

    def __init__(self):
        self.calls, self.cancelled, self.released = [], [], []
        self.active = self.peak = 0
        self.failure = None
        self.block = False
        self.started = asyncio.Event()

    def structured_call(self, **kwargs):
        raise AssertionError("expected async pipeline")

    async def async_structured_call(
        self, *, messages, output_schema, cached_session=None, **kwargs
    ):
        n = int(messages[-1].content)
        self.calls.append((n, cached_session, kwargs))
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            if self.failure is not None:
                exc, self.failure = self.failure, None
                raise exc
            if self.block:
                await asyncio.Event().wait()
            await asyncio.sleep((10 - min(n, 9)) * 0.001)
            return output_schema(n=n + 1)
        except asyncio.CancelledError:
            self.cancelled.append(n)
            raise
        finally:
            self.active -= 1

    def prepare_cached_session(self, *, identity_hash, **kwargs):
        return CachedSessionHandle(
            provider="fake",
            identity_hash=identity_hash,
            supported=True,
            style="reference",
            cache_id="cache-1",
        )

    def release_cached_session(self, handle):
        self.released.append(handle.cache_id)


class RecordingWriter(NoOpTraceWriter):
    enabled = True

    def __init__(self):
        self.invocations, self.errors = [], []

    @contextmanager
    def observe_workflow_invocation(self, **kwargs):
        self.invocations.append(kwargs)
        observation = NoOpWorkflowObservation()
        observation.update_error = self.errors.append
        yield observation

    def configure_temporal_plugin(self):
        raise AssertionError("local execution configured a Temporal plugin")


@pytest.fixture
def fixture(monkeypatch):
    module = ModuleType("local_fixture.models")
    module.Item, module.Batch, module.Branches = Item, Batch, Branches
    monkeypatch.setitem(sys.modules, module.__name__, module)
    provider, writer = Provider(), RecordingWriter()
    monkeypatch.setattr(services, "_build_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(
        services, "_build_observability", lambda *a, **kw: SimpleNamespace(writer=writer)
    )
    return provider, writer


def spec(*, name="Root", steps=None, input="Item", output="Item", definitions=None, runtime=None):
    return TypefluxYamlSpec.model_validate(
        {
            "project": "local_fixture",
            "name": name,
            "runtime": {
                "engine": "local",
                "registry": {"type": "inline", "prompts": {"echo": "{{n}}"}},
                "provider": {"type": "fake"},
                **(runtime or {}),
            },
            "activities": {
                "definitions": definitions
                if definitions is not None
                else [
                    {
                        "name": "echo",
                        "input": "models:Item",
                        "output": "models:Item",
                        "prompt": "echo",
                    }
                ]
            },
            "workflow": {
                "name": name,
                "input": f"models:{input}",
                "output": f"models:{output}",
                "steps": steps or [{"id": "first", "activity": "echo"}],
            },
        }
    )


def mapped(*, target=None, concurrency=2, limit=1_500_000, **kwargs):
    return spec(
        input="Batch",
        output="Batch",
        steps=[
            {
                "id": "mapped",
                "map": {
                    **(target or {"activity": "echo"}),
                    "over": "input.items",
                    "concurrency": concurrency,
                    "collect": {"output": "models:Batch", "field": "items", "max_bytes": limit},
                },
            }
        ],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_local_sequence_gate_validation_and_snapshot(fixture):
    provider, writer = fixture
    source = spec(
        steps=[
            {"id": "first", "activity": "echo", "when": {"path": "input.n", "gt": 0}},
            {"id": "second", "activity": "echo", "when": {"path": "first.n", "lt": 5}},
        ]
    )
    runtime = build_local_runtime(source)
    source.workflow.steps[0].activity = "mutated-after-build"
    assert await runtime.execute_workflow({"n": 1}) == Item(n=3)
    assert await runtime.execute_workflow(Item(n=0)) == Item(n=0)
    assert [n for n, *_ in provider.calls] == [1, 2]
    with pytest.raises(ValidationError):
        await runtime.execute_workflow({"n": "invalid"})
    typeflux = writer.invocations[0]["metadata"]["typeflux"]
    manifest = typeflux["execution_manifest"]
    assert typeflux["runtime"] == {"engine": "local"}
    assert "task_queue" not in manifest and "temporal_run_id" not in manifest
    assert "task_queue" not in typeflux["workflow"]
    assert manifest["contributions"]["local"]["steps"][1]["when"]["path"] == "first.n"
    assert "typeflux.runtime:local" in writer.invocations[0]["tags"]
    assert not any(manifest["workflow_id"] in tag for tag in writer.invocations[0]["tags"])


@pytest.mark.asyncio
async def test_local_parallel_collects_skipped_branch_as_null(fixture):
    provider, _ = fixture
    runtime = build_local_runtime(
        spec(
            output="Branches",
            steps=[
                {
                    "id": "joined",
                    "parallel": {
                        "branches": [
                            {"id": "left", "steps": [{"id": "left_step", "activity": "echo"}]},
                            {
                                "id": "right",
                                "when": {"path": "input.n", "eq": 100},
                                "steps": [{"id": "right_step", "activity": "echo"}],
                            },
                        ],
                        "collect": {"output": "models:Branches"},
                    },
                }
            ],
        )
    )
    assert await runtime.execute_workflow({"n": 1}) == Branches(left=Item(n=2), right=None)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_local_maps_preserve_order_and_share_provider_limits(fixture):
    provider, _ = fixture
    runtime = build_local_runtime(
        mapped(runtime={"provider_limits": {"default": {"max_concurrent": 2}}}, concurrency=4)
    )
    values = {"items": [{"n": n} for n in range(6)]}
    results = await asyncio.gather(
        runtime.execute_workflow(values), runtime.execute_workflow(values)
    )
    assert all(result == Batch(items=[Item(n=n + 1) for n in range(6)]) for result in results)
    assert provider.peak == 2
    assert await runtime.execute_workflow({"items": []}) == Batch(items=[])
    with pytest.raises(ValueError, match="collect.max_bytes=1"):
        await build_local_runtime(mapped(limit=1)).execute_workflow({"items": [{"n": 1}]})


@pytest.mark.asyncio
async def test_local_nested_workflow_and_workflow_map(fixture):
    provider, writer = fixture
    leaf = spec(name="Leaf")
    child = spec(name="Child", definitions=[], steps=[{"id": "nested", "workflow": "leaf"}])
    root = mapped(target={"workflow": "child"}, definitions=[])
    lookups = []

    def resolve(ref):
        lookups.append(ref)
        return {"leaf": leaf, "child": child}.get(ref)

    runtime = build_local_runtime(root, resolve_workflow=resolve, workflow_id="root")
    assert await runtime.execute_workflow({"items": [{"n": 1}, {"n": 2}]}) == Batch(
        items=[Item(n=2), Item(n=3)]
    )
    assert lookups == ["child", "leaf"]
    assert len(provider.calls) == 2
    steps = writer.invocations[0]["metadata"]["typeflux"]["execution_manifest"]["contributions"][
        "local"
    ]["steps"]
    assert steps[0]["map"]["steps"][0]["steps"][0]["activity"] == "echo"


@pytest.mark.parametrize("failure", ["capability", "type", "reference", "cycle", "collision"])
def test_local_complete_preflight_precedes_runtime_services(fixture, monkeypatch, failure):
    called = []
    monkeypatch.setattr(services, "_build_provider", lambda *a, **k: called.append("provider"))
    monkeypatch.setattr(services, "_build_observability", lambda *a, **k: called.append("observer"))
    root = spec(
        definitions=[],
        steps=[{"id": "child", "workflow": "child", "when": {"path": "input.n", "eq": -1}}],
    )
    child = spec(name="Child")
    if failure == "capability":
        child.workflow.risk_tier = "human_gated"
    if failure == "type":
        child.workflow.output = "models:Batch"
    if failure == "reference":
        child.workflow.steps[0].activity = "missing"
    if failure == "cycle":
        child = spec(name="Child", steps=[{"id": "self", "workflow": "child"}])
    if failure == "collision":
        root.activities = spec().activities
        child.activities.definitions[0].validation_retries = 5
    with pytest.raises((ValueError, TypeError, RuntimeCapabilityError)):
        build_local_runtime(root, resolve_workflow={"child": child}.get)
    assert called == []


@pytest.mark.parametrize("temporal", ["callable", "timeout"])
def test_local_rejects_imported_temporal_contracts(fixture, monkeypatch, temporal):
    from temporalio import activity as temporal_activity

    module = ModuleType("local_fixture.activities")
    if temporal == "callable":

        @temporal_activity.defn
        async def echo(value: Item) -> Item:
            return value

        module.ALL_ACTIVITIES = [echo]
    else:
        module.ALL_ACTIVITIES = [
            AIActivity(
                "echo", Item, Item, PromptRef("echo"), start_to_close_timeout=timedelta(seconds=5)
            )
        ]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    raw = spec().model_dump()
    raw["activities"]["definitions"] = []
    raw["activities"]["modules"] = [{"module": "activities"}]
    with pytest.raises(RuntimeCapabilityError, match="Temporal activity"):
        build_local_runtime(TypefluxYamlSpec.model_validate(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_local_session_cache_cleanup_on_every_exit(fixture, outcome):
    provider, _ = fixture
    provider.supports_session_cache = True
    provider.session_cache_style = "reference"
    raw = mapped(concurrency=1).model_dump()
    raw["activities"]["definitions"][0]["cache"] = {"enabled": True}
    raw["runtime"]["registry"]["prompts"]["echo"] = {
        "messages": [
            {"role": "system", "content": "Stable instructions"},
            {"role": "user", "content": "{{n}}"},
        ]
    }
    if outcome == "failure":
        provider.failure = ValueError("provider failed")
    if outcome == "cancel":
        provider.block = True
    runtime = build_local_runtime(TypefluxYamlSpec.model_validate(raw))
    task = asyncio.create_task(runtime.execute_workflow({"items": [{"n": 1}, {"n": 2}]}))
    if outcome == "cancel":
        await asyncio.wait_for(provider.started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "failure":
        with pytest.raises(Exception, match="provider failed"):
            await task
    else:
        assert await task == Batch(items=[Item(n=2), Item(n=3)])
    assert provider.released == ["cache-1"]
    assert all(handle and handle.cache_id == "cache-1" for _, handle, _ in provider.calls)
    if outcome != "success":
        assert [n for n, *_ in provider.calls] == [1]


@pytest.mark.asyncio
async def test_local_uses_provider_retry_without_activity_retries(fixture):
    provider, _ = fixture
    provider.failure = ProviderTransientError("retry me")
    runtime = build_local_runtime(
        spec(
            runtime={
                "provider_retry": {
                    "max_attempts": 2,
                    "initial_backoff_seconds": 0.001,
                    "jitter_ratio": 0,
                }
            }
        )
    )
    assert await runtime.execute_workflow({"n": 1}) == Item(n=2)
    assert [n for n, *_ in provider.calls] == [1, 1]
    provider.failure = ValueError("do not retry")
    with pytest.raises(Exception, match="do not retry"):
        await runtime.execute_workflow({"n": 1})
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_local_sync_pipeline_retains_repair_hooks_cache_and_moderation(fixture, monkeypatch):
    from typeflux.core.contracts import (
        CacheConfig,
        ModerationConfig,
        ModerationResult,
        OutputCheckViolation,
    )
    from typeflux.execution.cache import InMemoryCacheStore
    from typeflux.testing import FakeProvider

    hooks, moderated = [], []

    def hook(value: Item, output: Item, context: ActivityContext) -> Item:
        hooks.append((context.deps, context.tenant))
        return Item(n=output.n + 10)

    def check(value: Item, output: Item) -> list[OutputCheckViolation]:
        return (
            [] if output.n == value.n + 1 else [OutputCheckViolation(message="increment the input")]
        )

    def moderate(output: Item) -> ModerationResult:
        moderated.append(output.n)
        return ModerationResult(flagged=False)

    activity = AIActivity(
        "echo",
        Item,
        Item,
        PromptRef("echo"),
        hook=hook,
        output_check=check,
        cache=CacheConfig(),
        moderation=ModerationConfig(moderator=moderate),
    )
    module = ModuleType("local_fixture.activities")
    module.ALL_ACTIVITIES = [activity]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    provider = FakeProvider([Item(n=99), Item(n=2)])
    monkeypatch.setattr(services, "_build_provider", lambda *a, **kw: provider)
    raw = spec().model_dump()
    raw["activities"] = {"modules": [{"module": "activities"}]}
    runtime = build_local_runtime(
        TypefluxYamlSpec.model_validate(raw),
        cache_store=InMemoryCacheStore(),
        deps="injected",
        tenant_resolver=lambda value: {"tenant": "test"},
    )
    for _ in range(2):
        assert await runtime.execute_workflow({"n": 1}) == Item(n=12)
    assert len(provider.calls) == 2  # one repair, then a cache hit
    assert hooks == [("injected", {"tenant": "test"})] * 2
    assert moderated == [12, 12]


def test_local_expected_policy_and_cache_capabilities_fail_closed(fixture, monkeypatch):
    monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", "expected")
    with pytest.raises(ValueError, match="matching admitted project policy"):
        build_local_runtime(spec())
    monkeypatch.delenv("TYPEFLUX_EXPECTED_POLICY_HASH")
    with pytest.raises(ValueError, match="SubjectErasableCacheStore"):
        build_local_runtime(spec(runtime={"cache_erasure": "targeted"}), cache_store=object())


@pytest.mark.asyncio
async def test_local_cancellation_during_cache_preparation_releases_handle(fixture):
    import threading

    provider, _ = fixture
    provider.supports_session_cache = True
    provider.session_cache_style = "reference"
    started, finish = threading.Event(), threading.Event()
    prepare = provider.prepare_cached_session

    def blocked_prepare(**kwargs):
        started.set()
        assert finish.wait(3)
        return prepare(**kwargs)

    provider.prepare_cached_session = blocked_prepare
    raw = mapped().model_dump()
    raw["activities"]["definitions"][0]["cache"] = {"enabled": True}
    raw["runtime"]["registry"]["prompts"]["echo"] = {
        "messages": [
            {"role": "system", "content": "Stable prefix"},
            {"role": "user", "content": "{{n}}"},
        ]
    }
    runtime = build_local_runtime(TypefluxYamlSpec.model_validate(raw))
    task = asyncio.create_task(runtime.execute_workflow({"items": [{"n": 1}]}))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.released == ["cache-1"]
    assert provider.calls == []
