"""Local SDK acceptance using the real activity pipeline."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from typeflux import TypefluxError
from typeflux.core.contracts import (
    ActivityContext,
    AIActivity,
    CachedSessionHandle,
    ModerationResult,
    OutputCheckViolation,
    PromptRef,
    ProviderParams,
)
from typeflux.execution.lifecycle import ActivityCancelled
from typeflux.observability.backend import NoOpTraceWriter, NoOpWorkflowObservation
from typeflux.providers.errors import ProviderAuthError, ProviderTransientError
from typeflux.yaml import (
    LocalWorkflowError,
    RuntimeCapabilityError,
    TypefluxYamlSpec,
    build_local_runtime,
)
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
async def test_local_provider_error_identifies_workflow_step_and_cause(fixture):
    provider, writer = fixture
    failure = RuntimeError("provider execution refused")
    provider.failure = failure
    runtime = build_local_runtime(
        spec(steps=[{"id": "first", "activity": "echo"}, {"id": "later", "activity": "echo"}])
    )
    with pytest.raises(LocalWorkflowError) as raised:
        await runtime.execute_workflow({"n": 1})
    assert raised.value.workflow == "Root"
    assert raised.value.step == "first"
    assert raised.value.__cause__ is failure
    assert [n for n, *_ in provider.calls] == [1]
    assert writer.errors == [raised.value]


@pytest.mark.asyncio
async def test_local_domain_failures_still_match_the_public_typeflux_error_base(fixture):
    provider, _ = fixture
    failure = ProviderAuthError("provider refused credentials", provider="fake")
    provider.failure = failure
    with pytest.raises(TypefluxError) as raised:
        await build_local_runtime(spec()).execute_workflow({"n": 1})
    assert isinstance(raised.value, LocalWorkflowError)
    assert isinstance(raised.value, RuntimeError)
    assert (raised.value.workflow, raised.value.step) == ("Root", "first")
    assert raised.value.__cause__ is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sync_provider", "hook", "prompt_resolution"])
async def test_local_extension_and_preparation_errors_keep_original_cause(
    fixture, monkeypatch, source
):
    provider, writer = fixture
    failure = RuntimeError("extension refused")
    raw = spec().model_dump()

    def refuse(*args, **kwargs):
        raise failure

    if source == "sync_provider":
        provider.supports_async_structured_call = False
        provider.structured_call = refuse
    elif source == "hook":

        def refuse_hook(value: Item, output: Item) -> Item:
            raise failure

        module = ModuleType("local_fixture.activities")
        module.ALL_ACTIVITIES = [
            AIActivity("echo", Item, Item, PromptRef("echo"), hook=refuse_hook)
        ]
        monkeypatch.setitem(sys.modules, module.__name__, module)
        raw["activities"] = {"modules": [{"module": "activities"}]}
    runtime = build_local_runtime(TypefluxYamlSpec.model_validate(raw))
    if source == "prompt_resolution":
        monkeypatch.setattr(runtime._registry, "resolve", refuse)
    with pytest.raises(LocalWorkflowError) as raised:
        await runtime.execute_workflow({"n": 1})
    assert (raised.value.workflow, raised.value.step) == ("Root", "first")
    assert raised.value.__cause__ is failure
    assert writer.errors == [raised.value]


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", [False, True])
async def test_local_nested_failures_preserve_deepest_child_context(fixture, mapping):
    provider, writer = fixture
    failure = RuntimeError("child execution refused")
    provider.failure = failure
    child = spec(name="Child", steps=[{"id": "child_activity", "activity": "echo"}])
    parent = (
        mapped(name="Parent", target={"workflow": "child"}, concurrency=1)
        if mapping
        else spec(name="Parent", steps=[{"id": "call_child", "workflow": "child"}])
    )
    runtime = build_local_runtime(parent, resolve_workflow=lambda ref: child)
    with pytest.raises(LocalWorkflowError) as raised:
        await runtime.execute_workflow({"items": [{"n": 1}, {"n": 2}]} if mapping else {"n": 1})
    assert (raised.value.workflow, raised.value.step) == ("Child", "child_activity")
    assert raised.value.__cause__ is failure
    assert not isinstance(raised.value.__cause__, LocalWorkflowError)
    assert [n for n, *_ in provider.calls] == [1]
    assert writer.errors == [raised.value]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["map", "parallel"])
@pytest.mark.parametrize("failure", ["size", "schema"])
async def test_local_collection_errors_identify_the_collecting_step(
    fixture, monkeypatch, shape, failure
):
    from pydantic import model_validator

    provider, _ = fixture
    if failure == "schema":
        if shape == "map":

            class Checked(Batch):
                @model_validator(mode="after")
                def refuse_results(self):
                    if any(item.n > 1 for item in self.items):
                        raise ValueError("collection rejected")
                    return self

        else:

            class Checked(Branches):
                @model_validator(mode="after")
                def refuse_results(self):
                    raise ValueError("collection rejected")

        monkeypatch.setattr(
            sys.modules["local_fixture.models"], "Batch" if shape == "map" else "Branches", Checked
        )
    limit = 1 if failure == "size" else 1_500_000
    source = (
        mapped(limit=limit)
        if shape == "map"
        else spec(
            output="Branches",
            steps=[
                {
                    "id": "joined",
                    "parallel": {
                        "branches": [
                            {"id": "left", "steps": [{"id": "left_activity", "activity": "echo"}]},
                            {
                                "id": "right",
                                "when": {"path": "input.n", "lt": 0},
                                "steps": [{"id": "right_activity", "activity": "echo"}],
                            },
                        ],
                        "collect": {"output": "models:Branches", "max_bytes": limit},
                    },
                }
            ],
        )
    )
    runtime = build_local_runtime(source)
    with pytest.raises(LocalWorkflowError) as raised:
        await runtime.execute_workflow({"items": [{"n": 1}]} if shape == "map" else {"n": 1})
    assert (raised.value.workflow, raised.value.step) == (
        "Root",
        "mapped" if shape == "map" else "joined",
    )
    assert isinstance(
        raised.value.__cause__, ValidationError if failure == "schema" else ValueError
    )
    assert [n for n, *_ in provider.calls] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["root_output", "child_input", "child_output"])
async def test_local_workflow_boundary_errors_do_not_invent_a_step(fixture, boundary):
    provider, _ = fixture

    async def invalid_output(**kwargs):
        return Item.model_construct(n="invalid extension output")

    provider.async_structured_call = invalid_output
    child = spec(name="Child")
    steps = []
    if boundary == "child_input":
        steps.append({"id": "before_child", "activity": "echo"})
    if boundary != "root_output":
        steps.append({"id": "child", "workflow": "child"})
    runtime = build_local_runtime(spec(steps=steps), resolve_workflow=lambda ref: child)
    with (
        pytest.warns(UserWarning, match="Pydantic serializer warnings"),
        pytest.raises(LocalWorkflowError) as raised,
    ):
        await runtime.execute_workflow({"n": 1})
    assert raised.value.workflow == ("Root" if boundary == "root_output" else "Child")
    assert raised.value.step is None
    assert isinstance(raised.value.__cause__, ValidationError)


@pytest.mark.asyncio
async def test_local_attributed_map_failure_drains_before_cache_release_and_observation_flush(
    fixture,
):
    provider, writer = fixture
    provider.supports_session_cache = True
    provider.session_cache_style = "reference"
    second_started = asyncio.Event()
    events, started = [], []
    failure = RuntimeError("map item failed")

    async def call(*, messages, **kwargs):
        n = int(messages[-1].content)
        started.append(n)
        if n == 1:
            await second_started.wait()
            raise failure
        second_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("drained")

    provider.async_structured_call = call
    provider.release_cached_session = lambda handle: events.append("release")
    raw = mapped().model_dump()
    raw["activities"]["definitions"][0]["cache"] = {"enabled": True}
    raw["runtime"]["registry"]["prompts"]["echo"] = {
        "messages": [
            {"role": "system", "content": "Stable prefix"},
            {"role": "user", "content": "{{n}}"},
        ]
    }
    runtime = build_local_runtime(TypefluxYamlSpec.model_validate(raw))
    writer.flush = lambda: events.append("flush")
    with pytest.raises(LocalWorkflowError) as raised:
        await asyncio.wait_for(
            runtime.execute_workflow({"items": [{"n": n} for n in (1, 2, 3)]}), 2
        )
    assert (raised.value.workflow, raised.value.step) == ("Root", "mapped")
    assert raised.value.__cause__ is failure
    # Threaded activity preparation may reorder these concurrent provider starts.
    assert sorted(started) == [1, 2]
    assert events == ["drained", "release", "flush"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_type", [asyncio.CancelledError, ActivityCancelled])
async def test_local_cancellation_is_never_an_execution_error(fixture, cancel_type):
    provider, _ = fixture
    cancellation = cancel_type("stop")
    provider.failure = cancellation
    with pytest.raises(cancel_type) as raised:
        await build_local_runtime(spec()).execute_workflow({"n": 1})
    assert raised.value is cancellation


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
    with pytest.raises(LocalWorkflowError, match="collect.max_bytes=1") as raised:
        await build_local_runtime(mapped(limit=1)).execute_workflow({"items": [{"n": 1}]})
    assert isinstance(raised.value.__cause__, ValueError)


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


@pytest.mark.asyncio
async def test_shared_local_execution_fixtures(fixture):
    provider, _ = fixture
    path = (
        Path(__file__).resolve().parents[3] / "contracts/local-runtime/golden/execution-cases.json"
    )
    fixtures = json.loads(path.read_text())
    for case in fixtures["cases"]:
        raw = copy.deepcopy(fixtures["base"])
        raw["workflow"].update(case["workflow"])
        children = {}
        for child_id, workflow in case["children"].items():
            child = copy.deepcopy(fixtures["base"])
            child["name"] = child_id
            child["workflow"] = {"name": child_id, **workflow}
            children[child_id] = TypefluxYamlSpec.model_validate(child)
        provider.calls.clear()
        runtime = build_local_runtime(
            TypefluxYamlSpec.model_validate(raw), resolve_workflow=children.get, workflow_id="root"
        )
        result = await runtime.execute_workflow(case["input"])
        assert result.model_dump() == case["expected"], case["name"]
        calls = [n for n, *_ in provider.calls]
        if case.get("call_order") == "unordered":
            assert sorted(calls) == sorted(case["calls"]), case["name"]
        else:
            assert calls == case["calls"], case["name"]
