"""Single-process YAML execution using the ordinary Typeflux activity pipeline.

There is no worker, server, persistence, resume, scheduler or activity retry here.
Provider retries remain part of the activity pipeline. Cancel the executing asyncio
Task to request cancellation; blocking provider calls must return before cleanup.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel

from typeflux.composition import fan_out
from typeflux.core.contracts import AIActivity, CachedSessionHandle
from typeflux.core.subjects import resolve_subject_ids
from typeflux.env import load_env
from typeflux.execution.cache import CacheStore, SubjectErasableCacheStore
from typeflux.execution.controls import ProviderRateLimitController
from typeflux.execution.executor import (
    execute_prepared_ai_activity,
    execute_prepared_ai_activity_async,
    prepare_ai_activity_execution,
    prepare_session_cache,
)
from typeflux.execution.lifecycle import ActivityCancelled
from typeflux.execution.preflight import preflight_ai_activities
from typeflux.execution.provider_identity import provider_identifier
from typeflux.execution.starter import (
    _build_workflow_manifest,
    workflow_invocation_metadata,
    workflow_search_tags,
)
from typeflux.execution.worker import build_activity_rollup
from typeflux.manifests.activity import AIInvocationContext
from typeflux.metadata import LocalRuntimeContributor
from typeflux.providers import AsyncModelProvider, ModelProvider, provider_default_params
from typeflux.yaml import runtime as services
from typeflux.yaml.engine import RuntimeCapabilityError, validate_local_capabilities
from typeflux.yaml.imports import collect_activities, import_type_ref
from typeflux.yaml.planning import (
    WorkflowPlan,
    _resolve_context_path,
    collect_subworkflow_references,
    evaluate_when_gate,
    plan_workflow,
    validate_unique_yaml_workflow_names,
    when_gate_from_spec,
)
from typeflux.yaml.spec import (
    TypefluxYamlSpec,
    WorkflowAnyStepSpec,
    WorkflowMapStepSpec,
    WorkflowParallelBranchSpec,
    WorkflowParallelStepSpec,
    WorkflowSubworkflowStepSpec,
)


def _validated_value(model: type[BaseModel], value: Any) -> BaseModel:
    # Pydantic normally trusts existing model instances, including model_construct
    # and later mutations. The public execution boundary validates their data too.
    return model.model_validate(
        value.model_dump() if isinstance(value, BaseModel) else value
    ).model_copy(deep=True)


class _LocalLifecycle:
    heartbeat_interval_seconds: float | None = None

    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def heartbeat(self) -> None:
        pass

    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise ActivityCancelled


async def _drain(task: asyncio.Task[Any]) -> Any:
    """Keep resource cleanup attached even if a caller repeats cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _in_thread(fn: Callable[..., Any], cancellation: _LocalLifecycle, **kwargs: Any) -> Any:
    """Drain a blocking call before releasing its limiter or cached resources."""
    task = asyncio.create_task(asyncio.to_thread(fn, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancellation.cancelled.set()
        with suppress(BaseException):
            await _drain(task)
        raise


@dataclass(frozen=True)
class _Workflow:
    spec: TypefluxYamlSpec
    plan: WorkflowPlan
    activities: Mapping[str, AIActivity]
    collect_types: Mapping[str, type[BaseModel]]


class LocalYamlRuntime:
    """A prepared, non-durable SDK runtime. Use on one asyncio event loop.

    ``execute_workflow`` accepts a model or JSON-compatible mapping and returns the
    workflow output model. Concurrent invocations share provider limits and caches.
    Runtime inputs/specs are copied; input models inside a parallel branch are also
    isolated, so one branch's hook cannot mutate a sibling's value.
    """

    def __init__(
        self,
        *,
        root: _Workflow,
        workflows: Mapping[str, _Workflow],
        activities: Mapping[str, AIActivity],
        provider: ModelProvider,
        registry: Any,
        observability: Any,
        policy_guard: Any,
        cache_store: CacheStore | None,
        deps: Any,
        tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None,
    ) -> None:
        self._root = root
        self._workflows = workflows
        self._activities = activities
        self._provider = provider
        self._registry = registry
        self._observability = observability
        self._policy_guard = policy_guard
        self._cache_store = cache_store
        self._deps = deps
        self._tenant_resolver = tenant_resolver
        self._observer = observability.writer.create_activity_observer(
            metadata_contributors=services._yaml_metadata_contributors(
                root.spec, policy_guard=policy_guard
            )
        )
        limits = services._build_provider_rate_limit_policy(root.spec)
        self._limits = ProviderRateLimitController(policy=limits) if limits else None
        self._retry = services._build_provider_retry_policy(root.spec)
        self._artifact_policy = services._build_artifact_policy(root.spec)

    async def execute_workflow(self, input_value: BaseModel | Mapping[str, Any]) -> BaseModel:
        """Run to completion; cancellation/failure never rolls back external effects."""
        value = _validated_value(self._root.plan.input_type, input_value)
        subjects = resolve_subject_ids(
            value, tuple(item.to_subject_input() for item in self._root.spec.workflow.subjects)
        )
        contributors = (
            *services._yaml_metadata_contributors(self._root.spec, policy_guard=self._policy_guard),
            LocalRuntimeContributor(self._manifest_steps(self._root)),
        )
        activities = tuple(self._activities.values())
        rollup = build_activity_rollup(activities, self._registry, self._provider)
        run_id = str(uuid4())
        manifest = _build_workflow_manifest(
            workflow_name=self._root.spec.workflow.name,
            workflow_id=run_id,
            task_queue=None,
            activities=activities,
            activity_rollup=rollup,
            metadata_contributors=contributors,
        )
        metadata = workflow_invocation_metadata(
            workflow_name=self._root.spec.workflow.name,
            workflow_id=run_id,
            task_queue=None,
            activities=activities,
            activity_rollup=rollup,
            metadata_contributors=contributors,
            execution_manifest=manifest,
        )
        tags = workflow_search_tags(
            workflow_name=self._root.spec.workflow.name,
            activities=activities,
            activity_rollup=rollup,
            metadata=metadata,
            metadata_contributors=contributors,
        )
        writer = self._observability.writer
        try:
            with writer.observe_workflow_invocation(
                workflow_name=self._root.spec.workflow.name,
                input_value=value,
                metadata=metadata,
                tags=tags,
                subject_ids=subjects,
            ) as observation:
                try:
                    result = await self._run_workflow(self._root, value, subjects)
                except BaseException as exc:
                    observation.update_error(exc)
                    raise
                observation.update_output(result)
                return result
        finally:
            writer.flush()

    def _manifest_steps(self, workflow: _Workflow) -> list[dict[str, Any]]:
        def steps(sequence: Sequence[WorkflowAnyStepSpec]) -> list[dict[str, Any]]:
            result = []
            for step in sequence:
                payload = step.model_dump(mode="json", exclude_none=True)
                if isinstance(step, WorkflowSubworkflowStepSpec):
                    payload["steps"] = self._manifest_steps(self._workflows[step.workflow])
                elif isinstance(step, WorkflowMapStepSpec) and step.map.workflow is not None:
                    payload["map"]["steps"] = self._manifest_steps(
                        self._workflows[step.map.workflow]
                    )
                elif isinstance(step, WorkflowParallelStepSpec):
                    for branch, branch_payload in zip(
                        step.parallel.branches, payload["parallel"]["branches"], strict=True
                    ):
                        branch_payload["steps"] = steps(branch.steps)
                result.append(payload)
            return result

        return steps(workflow.plan.steps)

    async def _run_workflow(
        self, workflow: _Workflow, value: Any, subjects: tuple[str, ...]
    ) -> BaseModel:
        value = _validated_value(workflow.plan.input_type, value)
        result = await self._sequence(
            workflow, workflow.plan.steps, value, {"input": value}, subjects
        )
        return _validated_value(workflow.plan.output_type, result)

    async def _sequence(
        self,
        workflow: _Workflow,
        steps: Sequence[WorkflowAnyStepSpec],
        value: Any,
        context: dict[str, Any],
        subjects: tuple[str, ...],
    ) -> Any:
        for step in steps:
            if step.when is not None and not evaluate_when_gate(
                when_gate_from_spec(step.when), context
            ):
                break
            if isinstance(step, WorkflowMapStepSpec):
                value = await self._map(workflow, step, context, subjects)
            elif isinstance(step, WorkflowParallelStepSpec):
                # Gates see only the parent scope; concurrent siblings never see
                # each other's intermediate writes. Merge completed scopes afterwards.
                async def branch_run(
                    branch: WorkflowParallelBranchSpec,
                ) -> tuple[Any, dict[str, Any]]:
                    if branch.when is not None and not evaluate_when_gate(
                        when_gate_from_spec(branch.when), context
                    ):
                        return None, {}
                    branch_context = {
                        key: item.model_copy(deep=True) if isinstance(item, BaseModel) else item
                        for key, item in context.items()
                    }
                    result = await self._sequence(
                        workflow,
                        branch.steps,
                        value.model_copy(deep=True),
                        branch_context,
                        subjects,
                    )
                    return result, {
                        key: item for key, item in branch_context.items() if key not in context
                    }

                branches = await fan_out(
                    step.parallel.branches, branch_run, concurrency=len(step.parallel.branches)
                )
                payload = {}
                for branch, (result, additions) in zip(
                    step.parallel.branches, branches, strict=True
                ):
                    payload[branch.id] = result
                    context.update(additions)
                value = self._collect(workflow, step.id, payload, step.parallel.collect.max_bytes)
            elif isinstance(step, WorkflowSubworkflowStepSpec):
                value = await self._run_workflow(self._workflows[step.workflow], value, subjects)
            else:
                value = await self._activity(workflow.activities[step.activity], value, subjects)
            context[step.id] = value
        return value

    def _collect(
        self, workflow: _Workflow, step_id: str, payload: dict[str, Any], limit: int
    ) -> BaseModel:
        result = workflow.collect_types[step_id].model_validate(payload)
        if limit and len(result.model_dump_json().encode("utf-8")) > limit:
            raise ValueError(
                f"step {step_id!r} collected payload exceeds collect.max_bytes={limit}; shrink outputs or raise collect.max_bytes"
            )
        return result

    async def _map(
        self,
        workflow: _Workflow,
        step: WorkflowMapStepSpec,
        context: Mapping[str, Any],
        subjects: tuple[str, ...],
    ) -> BaseModel:
        mapping = step.map
        items = _resolve_context_path(context, mapping.over)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            raise TypeError(f"step {step.id!r} map.over did not resolve to a sequence")
        cached: CachedSessionHandle | None = None
        lifecycle = _LocalLifecycle()
        activity = workflow.activities[mapping.activity] if mapping.activity else None
        try:
            if (
                items
                and activity is not None
                and activity.session_cache
                and activity.session_cache.enabled
            ):
                # Shield/drain preparation so cancellation cannot orphan a remote cache.
                prep = asyncio.create_task(
                    asyncio.to_thread(
                        prepare_session_cache,
                        activity=activity,
                        registry=self._registry,
                        provider=self._provider,
                        created_at=datetime.now(UTC).isoformat(),
                        input_value=activity.input_type.model_validate(items[0]),
                        artifact_policy=self._artifact_policy,
                        provider_model_policy_guard=self._policy_guard,
                    )
                )
                try:
                    cached = await asyncio.shield(prep)
                except asyncio.CancelledError:
                    with suppress(BaseException):
                        cached = await _drain(prep)
                    raise

            async def run_item(index: int) -> Any:
                if mapping.workflow is not None:
                    return await self._run_workflow(
                        self._workflows[mapping.workflow], items[index], subjects
                    )
                assert activity is not None
                return await self._activity(
                    activity,
                    items[index],
                    subjects,
                    cached=cached,
                    map_context=(step.id, index, len(items), mapping.concurrency),
                )

            results = await fan_out(range(len(items)), run_item, concurrency=mapping.concurrency)
            return self._collect(
                workflow, step.id, {mapping.collect.field: results}, mapping.collect.max_bytes
            )
        finally:
            release = getattr(self._provider, "release_cached_session", None)
            if cached is not None and cached.cache_id is not None and release is not None:
                with suppress(Exception):
                    await _in_thread(release, lifecycle, handle=cached)

    async def _activity(
        self,
        activity: AIActivity,
        value: Any,
        subjects: tuple[str, ...],
        *,
        cached: CachedSessionHandle | None = None,
        map_context: tuple[str, int, int, int] | None = None,
    ) -> BaseModel:
        lifecycle = _LocalLifecycle()
        invocation = AIInvocationContext(
            temporal_namespace=None,
            temporal_workflow_type=None,
            temporal_workflow_id=None,
            temporal_run_id=None,
            temporal_activity_type=None,
            temporal_activity_id=None,
            temporal_activity_attempt=None,
            typeflux_activity_name=activity.name,
            typeflux_manifest_hash="",
            map_step_id=map_context[0] if map_context else None,
            map_index=map_context[1] if map_context else None,
            map_size=map_context[2] if map_context else None,
            map_concurrency=map_context[3] if map_context else None,
            subject_ids=subjects,
        )
        prepared = await _in_thread(
            prepare_ai_activity_execution,
            lifecycle,
            activity=activity,
            input_value=_validated_value(activity.input_type, value),
            registry=self._registry,
            provider=self._provider,
            invocation_context=invocation,
            observer=self._observer,
            provider_retry_policy=self._retry,
            artifact_policy=self._artifact_policy,
            provider_model_policy_guard=self._policy_guard,
            cached_session=cached,
        )

        async def execute(metadata: dict[str, Any] | None = None) -> BaseModel:
            kwargs = dict(
                provider_call_metadata=metadata,
                moderation_policy_guard=self._policy_guard,
                lifecycle=lifecycle,
                cached_session=cached,
                deps=self._deps,
                cache_store=self._cache_store,
                tenant_resolver=self._tenant_resolver,
            )
            if getattr(self._provider, "supports_async_structured_call", True) and callable(
                getattr(self._provider, "async_structured_call", None)
            ):
                return await execute_prepared_ai_activity_async(
                    prepared, provider=cast(AsyncModelProvider, self._provider), **kwargs
                )
            return await _in_thread(
                execute_prepared_ai_activity,
                lifecycle,
                prepared=prepared,
                provider=self._provider,
                **kwargs,
            )

        try:
            if self._limits is None:
                return await execute()
            selection = self._limits.select(
                provider_name=provider_identifier(self._provider),
                provider_model=prepared.provider_model,
            )
            limiter = self._limits.limiter_for(selection)
            if limiter is None:
                return await execute(selection.to_metadata())
            async with limiter.limit() as wait:
                return await execute({**selection.to_metadata(), **wait.to_metadata()})
        except asyncio.CancelledError:
            lifecycle.cancelled.set()
            raise


def build_local_runtime(
    spec: TypefluxYamlSpec,
    *,
    resolve_workflow: Callable[[str], TypefluxYamlSpec | None] | None = None,
    workflow_id: str | None = None,
    policy_guard: Any | None = None,
    cache_store: CacheStore | None = None,
    deps: Any = None,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
) -> LocalYamlRuntime:
    """Validate the complete closure, then construct runtime services.

    ``resolve_workflow`` resolves IDs in the same project to already-resolved specs.
    Project callers supply the existing project policy guard after admission. Imported
    modules are trusted code, exactly as on the Temporal YAML path. Python portable
    activities are AIActivity descriptors; Temporal-decorated functions are rejected.
    """
    spec = spec.model_copy(deep=True)
    children: dict[str, TypefluxYamlSpec] = {}

    def resolve(ref: str) -> TypefluxYamlSpec | None:
        if ref not in children:
            child = resolve_workflow(ref) if resolve_workflow is not None else None
            if child is None:
                return None
            children[ref] = child.model_copy(deep=True)
        return children[ref]

    validate_local_capabilities(
        spec, resolve_workflow=resolve if resolve_workflow else None, workflow_id=workflow_id
    )
    specs = [spec, *children.values()]
    validate_unique_yaml_workflow_names(specs)
    if (
        any(current.runtime.cache_erasure == "targeted" for current in specs)
        and cache_store is not None
        and not isinstance(cache_store, SubjectErasableCacheStore)
    ):
        raise ValueError("runtime.cache_erasure: targeted requires a SubjectErasableCacheStore")
    activities_by_spec = {}
    activities: dict[str, AIActivity] = {}
    owners: dict[str, tuple[str, Any]] = {}
    # Same collision rule as the project Temporal builder; no worker is created.
    from typeflux.project.environment import _register_activity_owners

    for current in specs:
        discovered = collect_activities(current, local=True)
        for name, activity in discovered.items():
            assert isinstance(activity, AIActivity)
            for field in (
                "task_queue",
                "start_to_close_timeout",
                "heartbeat_timeout",
                "retry_policy",
            ):
                if getattr(activity, field) is not None:
                    raise RuntimeCapabilityError(
                        current.workflow.name,
                        f"activities.{name}.{field}",
                        "Temporal activity policy",
                        "Remove the setting or use runtime.engine: temporal.",
                    )
        _register_activity_owners(current.name, current, discovered, owners)
        portable = cast(dict[str, AIActivity], discovered)
        activities_by_spec[id(current)] = portable
        activities.update(portable)

    workflows: dict[str, _Workflow] = {}

    def prepare(current: TypefluxYamlSpec) -> _Workflow:
        records = {}
        for ref in collect_subworkflow_references(current):
            if ref not in workflows:
                workflows[ref] = prepare(children[ref])
            records[ref] = workflows[ref].plan
        own = activities_by_spec[id(current)]
        plan = plan_workflow(current, own, subworkflows=records)
        collects = {}

        def collect_types(steps: Sequence[WorkflowAnyStepSpec]) -> None:
            for step in steps:
                if isinstance(step, WorkflowMapStepSpec):
                    collects[step.id] = import_type_ref(current.project, step.map.collect.output)
                elif isinstance(step, WorkflowParallelStepSpec):
                    collects[step.id] = import_type_ref(
                        current.project, step.parallel.collect.output
                    )
                    for branch in step.parallel.branches:
                        collect_types(branch.steps)

        collect_types(plan.steps)
        return _Workflow(current, plan, own, collects)

    root = prepare(spec)
    child_specs = tuple(children.items())
    # All graph, capability and descriptor checks precede constructors/remote reads.
    load_env()
    expected_hash = os.getenv("TYPEFLUX_EXPECTED_POLICY_HASH")
    if expected_hash and expected_hash != getattr(
        getattr(policy_guard, "policy", None), "policy_hash", None
    ):
        raise ValueError(
            "TYPEFLUX_EXPECTED_POLICY_HASH requires a matching admitted project policy guard"
        )
    for current in specs:
        services._enforce_runtime_policy_guard(current, policy_guard)
    for ref, child in child_specs:
        services._assert_consistent_custom_provider_config(spec, ref, child)
    observability = services._build_observability(
        spec, policy_guard=policy_guard, child_specs=child_specs
    )
    registry = services._build_registry(spec, child_specs)
    provider = services._build_provider(spec, enable_langfuse=services._uses_langfuse(spec))
    services._validate_activity_provider_params(provider, tuple(activities.values()))
    preflight_ai_activities(
        activities=tuple(activities.values()),
        registry=registry,
        provider=provider,
        provider_default_params=provider_default_params(provider),
    ).raise_for_failures()
    return LocalYamlRuntime(
        root=root,
        workflows=workflows,
        activities=activities,
        provider=provider,
        registry=registry,
        observability=observability,
        policy_guard=policy_guard,
        cache_store=cache_store,
        deps=deps,
        tenant_resolver=tenant_resolver,
    )
