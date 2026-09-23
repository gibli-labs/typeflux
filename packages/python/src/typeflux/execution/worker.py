from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactPolicy
from typeflux.core.contracts import (
    AIActivity,
    CachedSessionHandle,
    MapActivityContext,
    YamlWorkflowActivity,
)
from typeflux.execution.cache import CacheStore
from typeflux.execution.controls import (
    ProviderCallLimiter,
    ProviderCallLimits,
    ProviderRateLimitController,
    ProviderRateLimitPolicy,
    ProviderRetryPolicy,
)
from typeflux.execution.executor import (
    AIActivityOutputValidationError,
    ModerationBlockedError,
    PolicyGuard,
    execute_ai_activity,
    execute_prepared_ai_activity,
    execute_prepared_ai_activity_async,
    prepare_ai_activity_execution,
    prepare_session_cache,
)
from typeflux.execution.lifecycle import (
    NO_OP_LIFECYCLE,
    ActivityCancelled,
    ActivityLifecycle,
    heartbeat_interval_for,
)
from typeflux.execution.observer import AIActivityObserver
from typeflux.execution.preflight import PreflightReport, preflight_ai_activities
from typeflux.execution.provider_identity import provider_identifier
from typeflux.execution.session_cache import (
    UnstableCachePrefixError,
    cache_prep_activity_name,
    cache_release_activity_name,
)
from typeflux.execution.starter import execute_workflow as execute_typeflux_workflow
from typeflux.manifests import (
    AIInvocationContext,
    build_activity_rollup_entry,
    build_unresolved_activity_rollup_entry,
)
from typeflux.observability.backend import ObservabilityBackend, TraceWriter
from typeflux.observability.langfuse import (
    LangfuseObservabilityBackend,
    LangfuseTraceWriter,
)
from typeflux.prompts import PromptRegistry
from typeflux.prompts.errors import PromptResolutionError
from typeflux.providers import AsyncModelProvider, ModelProvider, provider_default_params
from typeflux.providers.errors import (
    ProviderCacheUnavailableError,
    ProviderError,
    ProviderPolicyError,
)


def _activity_info() -> Any:
    try:
        from temporalio import activity as temporal_activity
    except ModuleNotFoundError as exc:
        raise RuntimeError("temporalio is required to run Temporal activities") from exc
    return temporal_activity.info()


class _TemporalActivityLifecycle:
    """Backs the executor's :class:`ActivityLifecycle` with the Temporal context.

    Kept trivial so the cooperative-cancellation logic stays in the (testable,
    temporalio-free) executor and ``lifecycle`` module.
    """

    def __init__(self, temporal_activity: Any, heartbeat_interval_seconds: float | None) -> None:
        self._activity = temporal_activity
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def heartbeat(self) -> None:
        self._activity.heartbeat()

    def raise_if_cancelled(self) -> None:
        try:
            cancelled = self._activity.is_cancelled()
        except RuntimeError:
            # Outside a Temporal activity context (e.g. direct unit test): there
            # is no cancellation to observe.
            return
        if cancelled:
            raise ActivityCancelled("Temporal activity cancelled")


def _activity_info_or_none() -> Any:
    try:
        return _activity_info()
    except RuntimeError:
        # Outside a Temporal activity context (e.g. direct unit-test invocation).
        return None


def _activity_lifecycle() -> ActivityLifecycle:
    try:
        from temporalio import activity as temporal_activity
    except ModuleNotFoundError:
        return NO_OP_LIFECYCLE
    info = _activity_info_or_none()
    timeout = getattr(info, "heartbeat_timeout", None)
    interval = heartbeat_interval_for(timeout.total_seconds() if timeout is not None else None)
    return _TemporalActivityLifecycle(temporal_activity, interval)


def _build_invocation_context(
    activity: AIActivity,
    *,
    task_queue: str | None = None,
    map_context: MapActivityContext | None = None,
) -> AIInvocationContext:
    info = _activity_info()
    workflow_type = getattr(info, "workflow_type", None)
    activity_type = getattr(info, "activity_type", None)
    return AIInvocationContext(
        temporal_namespace=getattr(info, "workflow_namespace", None),
        temporal_workflow_type=workflow_type,
        temporal_workflow_id=getattr(info, "workflow_id", None),
        temporal_run_id=getattr(info, "workflow_run_id", None),
        temporal_activity_type=activity_type,
        temporal_activity_id=getattr(info, "activity_id", None),
        temporal_activity_attempt=getattr(info, "attempt", None),
        typeflux_activity_name=activity.name,
        typeflux_manifest_hash="pending",
        temporal_task_queue=task_queue,
        map_step_id=map_context.map_step_id if map_context is not None else None,
        map_index=map_context.map_index if map_context is not None else None,
        map_size=map_context.map_size if map_context is not None else None,
        map_concurrency=map_context.map_concurrency if map_context is not None else None,
        # #715 slice 1: the workflow threads the run's subject ids through the
        # per-call context envelope; from here they reach the cross-run cache
        # record (`build_cache_record`).
        subject_ids=tuple(map_context.subject_ids) if map_context is not None else (),
    )


def build_temporal_activity(
    activity: AIActivity,
    *,
    registry: PromptRegistry,
    provider: ModelProvider,
    task_queue: str | None = None,
    observer: AIActivityObserver | None = None,
    provider_call_limiter: ProviderCallLimiter | None = None,
    provider_rate_limit_controller: ProviderRateLimitController | None = None,
    provider_retry_policy: ProviderRetryPolicy | None = None,
    artifact_policy: ArtifactPolicy | None = None,
    provider_model_policy_guard: PolicyGuard | None = None,
    deps: Any = None,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
    cache_store: CacheStore | None = None,
):
    async def temporal_activity_fn(
        input_value: BaseModel,
        map_context: MapActivityContext | None = None,
    ) -> BaseModel:
        try:
            input_value = _coerce_activity_input(activity, input_value)
            map_context = _coerce_map_context(map_context)
            cached_session = map_context.cached_session if map_context is not None else None
            invocation_context = _build_invocation_context(
                activity,
                task_queue=task_queue,
                map_context=map_context,
            )
            lifecycle = _activity_lifecycle()
            async_provider = _async_provider(provider)
            if provider_rate_limit_controller is not None:
                prepared = await asyncio.to_thread(
                    prepare_ai_activity_execution,
                    activity=activity,
                    input_value=input_value,
                    registry=registry,
                    provider=provider,
                    invocation_context=invocation_context,
                    observer=observer,
                    provider_retry_policy=provider_retry_policy,
                    artifact_policy=artifact_policy,
                    provider_model_policy_guard=provider_model_policy_guard,
                    cached_session=cached_session,
                )
                selection = provider_rate_limit_controller.select(
                    provider_name=provider_identifier(provider),
                    provider_model=prepared.provider_model,
                )
                policy_metadata = selection.to_metadata()
                selected_limiter = provider_rate_limit_controller.limiter_for(selection)
                if selected_limiter is None:
                    return await _execute_prepared_from_worker(
                        prepared,
                        provider=provider,
                        async_provider=async_provider,
                        provider_call_metadata={
                            **policy_metadata,
                            "queued": False,
                            "throttled": False,
                            "queued_seconds": 0.0,
                            "throttled_seconds": 0.0,
                        },
                        moderation_policy_guard=provider_model_policy_guard,
                        lifecycle=lifecycle,
                        cached_session=cached_session,
                        deps=deps,
                        tenant_resolver=tenant_resolver,
                        cache_store=cache_store,
                    )
                async with selected_limiter.limit() as provider_wait:
                    return await _execute_prepared_from_worker(
                        prepared,
                        provider=provider,
                        async_provider=async_provider,
                        provider_call_metadata={
                            **policy_metadata,
                            **provider_wait.to_metadata(),
                        },
                        moderation_policy_guard=provider_model_policy_guard,
                        lifecycle=lifecycle,
                        cached_session=cached_session,
                        deps=deps,
                        tenant_resolver=tenant_resolver,
                        cache_store=cache_store,
                    )
            if provider_call_limiter is None or not provider_call_limiter.limits.enabled:
                if async_provider is not None:
                    prepared = await asyncio.to_thread(
                        prepare_ai_activity_execution,
                        activity=activity,
                        input_value=input_value,
                        registry=registry,
                        provider=provider,
                        invocation_context=invocation_context,
                        observer=observer,
                        provider_retry_policy=provider_retry_policy,
                        artifact_policy=artifact_policy,
                        provider_model_policy_guard=provider_model_policy_guard,
                        cached_session=cached_session,
                    )
                    return await execute_prepared_ai_activity_async(
                        prepared,
                        provider=async_provider,
                        moderation_policy_guard=provider_model_policy_guard,
                        lifecycle=lifecycle,
                        cached_session=cached_session,
                        deps=deps,
                        tenant_resolver=tenant_resolver,
                        cache_store=cache_store,
                    )
                return await asyncio.to_thread(
                    execute_ai_activity,
                    activity=activity,
                    input_value=input_value,
                    registry=registry,
                    provider=provider,
                    invocation_context=invocation_context,
                    observer=observer,
                    provider_retry_policy=provider_retry_policy,
                    artifact_policy=artifact_policy,
                    provider_model_policy_guard=provider_model_policy_guard,
                    moderation_policy_guard=provider_model_policy_guard,
                    provider_call_metadata={"execution_mode": "sync_thread"},
                    lifecycle=lifecycle,
                    cached_session=cached_session,
                    deps=deps,
                    tenant_resolver=tenant_resolver,
                    cache_store=cache_store,
                )

            async with provider_call_limiter.limit() as provider_wait:
                if async_provider is not None:
                    prepared = await asyncio.to_thread(
                        prepare_ai_activity_execution,
                        activity=activity,
                        input_value=input_value,
                        registry=registry,
                        provider=provider,
                        invocation_context=invocation_context,
                        observer=observer,
                        provider_retry_policy=provider_retry_policy,
                        artifact_policy=artifact_policy,
                        provider_model_policy_guard=provider_model_policy_guard,
                        cached_session=cached_session,
                    )
                    return await execute_prepared_ai_activity_async(
                        prepared,
                        provider=async_provider,
                        provider_call_metadata=provider_wait.to_metadata(),
                        moderation_policy_guard=provider_model_policy_guard,
                        lifecycle=lifecycle,
                        cached_session=cached_session,
                        deps=deps,
                        tenant_resolver=tenant_resolver,
                        cache_store=cache_store,
                    )
                return await asyncio.to_thread(
                    execute_ai_activity,
                    activity=activity,
                    input_value=input_value,
                    registry=registry,
                    provider=provider,
                    invocation_context=invocation_context,
                    observer=observer,
                    provider_retry_policy=provider_retry_policy,
                    artifact_policy=artifact_policy,
                    provider_model_policy_guard=provider_model_policy_guard,
                    moderation_policy_guard=provider_model_policy_guard,
                    provider_call_metadata=_provider_call_metadata_with_execution_mode(
                        provider_wait.to_metadata(),
                        execution_mode="sync_thread",
                    ),
                    lifecycle=lifecycle,
                    cached_session=cached_session,
                    deps=deps,
                    tenant_resolver=tenant_resolver,
                    cache_store=cache_store,
                )
        except ProviderCacheUnavailableError as exc:
            # The referenced session cache vanished mid-fan-out (expired/deleted).
            # The per-item messages were built WITHOUT the cached reference
            # artifacts (they lived in the cache), so recover by re-running this
            # item once uncached — strip the handle so prepare re-includes the
            # full context. The retry re-enters this function and so reuses the
            # same error handling. Guard prevents a loop: an uncached run carries
            # no cache and cannot raise this again with a recoverable handle (#368).
            if (
                cached_session is not None
                and cached_session.supported
                and cached_session.style == "reference"
            ):
                uncached_context = (
                    map_context.model_copy(update={"cached_session": None})
                    if map_context is not None
                    else None
                )
                return await temporal_activity_fn(input_value, uncached_context)
            _raise_temporal_non_retryable_if_needed(exc)
            raise
        except ModerationBlockedError as exc:
            # A blocked moderation verdict is terminal: the same output reproduces
            # it, so Temporal retries only multiply provider spend (#158).
            _raise_temporal_non_retryable_if_needed(exc)
            raise
        except (AIActivityOutputValidationError, PromptResolutionError, ProviderError) as exc:
            # Exhausted validation repair is terminal: validation_retries
            # already re-prompted with repair instructions, so blind Temporal
            # retries of the same prompt only multiply provider spend.
            _raise_temporal_non_retryable_if_needed(exc)
            raise

    temporal_activity_fn.__name__ = activity.name
    temporal_activity_fn.__qualname__ = activity.name
    temporal_activity_fn.__annotations__ = {
        "input_value": activity.input_type,
        "map_context": MapActivityContext | None,
        "return": activity.output_type,
    }

    try:
        from temporalio import activity as temporal_activity
    except ModuleNotFoundError:
        return temporal_activity_fn
    return temporal_activity.defn(name=activity.name)(temporal_activity_fn)


def build_cache_prep_activity(
    activity: AIActivity,
    *,
    registry: PromptRegistry,
    provider: ModelProvider,
    artifact_policy: ArtifactPolicy | None = None,
    provider_model_policy_guard: PolicyGuard | None = None,
):
    """Build the Temporal activity that prepares a cached session once before a
    map fan-out (#60). Captures the provider/registry in a closure (no name
    lookup), mirroring build_temporal_activity, and stamps ``created_at`` here so
    the recorded handle is replay-stable. Receives a **representative item** so it
    can resolve the activity's ``cache: reference`` artifacts (which must be
    identical across items) for the cached prefix (#363)."""

    async def prepare_cache_fn(input_value: BaseModel) -> CachedSessionHandle:
        input_value = _coerce_activity_input(activity, input_value)
        created_at = datetime.now(UTC).isoformat()
        try:
            return await asyncio.to_thread(
                prepare_session_cache,
                activity=activity,
                registry=registry,
                provider=provider,
                created_at=created_at,
                input_value=input_value,
                artifact_policy=artifact_policy,
                provider_model_policy_guard=provider_model_policy_guard,
            )
        except (UnstableCachePrefixError, ProviderPolicyError) as exc:
            # Permanent misconfigurations — a templated system prefix, or a model
            # the policy guard rejects — fail fast and loud rather than letting
            # Temporal retry them on the map's bounded policy (pointless backoff
            # before the identical failure). #60
            _raise_non_retryable(exc)
            raise

    name = cache_prep_activity_name(activity.name)
    prepare_cache_fn.__name__ = name
    prepare_cache_fn.__qualname__ = name
    prepare_cache_fn.__annotations__ = {
        "input_value": activity.input_type,
        "return": CachedSessionHandle,
    }

    try:
        from temporalio import activity as temporal_activity
    except ModuleNotFoundError:
        return prepare_cache_fn
    return temporal_activity.defn(name=name)(prepare_cache_fn)


def build_cache_release_activity(activity: AIActivity, *, provider: ModelProvider):
    """Build the Temporal activity that releases a reference-style cache after a
    map fan-out (#368). Best-effort: ``release_cached_session`` swallows provider
    errors (the cache TTL-expires regardless), so this never fails the workflow."""

    async def release_cache_fn(handle: CachedSessionHandle) -> None:
        # release_cached_session is an optional provider capability (not on the
        # ModelProvider protocol); only reference-style providers define it, and
        # the workflow only schedules this for a handle with a cache_id, so it is
        # present in practice. Probe defensively to stay type-safe and a no-op if
        # a provider ever lacks it.
        release = getattr(provider, "release_cached_session", None)
        if release is not None:
            await asyncio.to_thread(release, handle)

    name = cache_release_activity_name(activity.name)
    release_cache_fn.__name__ = name
    release_cache_fn.__qualname__ = name
    release_cache_fn.__annotations__ = {"handle": CachedSessionHandle, "return": type(None)}

    try:
        from temporalio import activity as temporal_activity
    except ModuleNotFoundError:
        return release_cache_fn
    return temporal_activity.defn(name=name)(release_cache_fn)


async def _execute_prepared_from_worker(
    prepared,
    *,
    provider: ModelProvider,
    async_provider: AsyncModelProvider | None,
    provider_call_metadata: dict[str, Any] | None,
    moderation_policy_guard: PolicyGuard | None = None,
    lifecycle: ActivityLifecycle = NO_OP_LIFECYCLE,
    cached_session: CachedSessionHandle | None = None,
    deps: Any = None,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
    cache_store: CacheStore | None = None,
) -> BaseModel:
    if async_provider is not None:
        return await execute_prepared_ai_activity_async(
            prepared,
            provider=async_provider,
            provider_call_metadata=provider_call_metadata,
            moderation_policy_guard=moderation_policy_guard,
            lifecycle=lifecycle,
            cached_session=cached_session,
            deps=deps,
            tenant_resolver=tenant_resolver,
            cache_store=cache_store,
        )
    return await asyncio.to_thread(
        execute_prepared_ai_activity,
        prepared,
        provider=provider,
        moderation_policy_guard=moderation_policy_guard,
        provider_call_metadata=_provider_call_metadata_with_execution_mode(
            provider_call_metadata,
            execution_mode="sync_thread",
        ),
        lifecycle=lifecycle,
        cached_session=cached_session,
        deps=deps,
        tenant_resolver=tenant_resolver,
        cache_store=cache_store,
    )


def _coerce_activity_input(activity: AIActivity, input_value: Any) -> BaseModel:
    if isinstance(input_value, activity.input_type):
        return input_value
    if isinstance(input_value, Mapping):
        return activity.input_type.model_validate(input_value)
    return input_value


def _coerce_map_context(map_context: Any) -> MapActivityContext | None:
    if map_context is None or isinstance(map_context, MapActivityContext):
        return map_context
    if isinstance(map_context, Mapping):
        return MapActivityContext.model_validate(map_context)
    raise TypeError("map_context must be a MapActivityContext or mapping")


class TypefluxWorker:
    def __init__(
        self,
        *,
        client: Any,
        task_queue: str,
        activities: Sequence[YamlWorkflowActivity],
        registry: PromptRegistry,
        provider: ModelProvider,
        observer: AIActivityObserver | None = None,
        langfuse_client: Any | None = None,
        observability: ObservabilityBackend | None = None,
        workflows: Sequence[type] = (),
        provider_call_limits: ProviderCallLimits | None = None,
        provider_rate_limit_policy: ProviderRateLimitPolicy | None = None,
        provider_retry_policy: ProviderRetryPolicy | None = None,
        artifact_policy: ArtifactPolicy | None = None,
        provider_model_policy_guard: PolicyGuard | None = None,
        deps: Any = None,
        tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
        cache_store: CacheStore | None = None,
        **worker_kwargs: Any,
    ) -> None:
        self.client = client
        self.task_queue = task_queue
        self.activities = tuple(activities)
        self.registry = registry
        self.provider = provider
        self.observability = observability
        if self.observability is None and langfuse_client is not None:
            self.observability = LangfuseObservabilityBackend.from_client(langfuse_client)
        self.observer = observer or (
            self.observability.writer.create_activity_observer()
            if self.observability is not None
            else None
        )
        self.langfuse_client = langfuse_client
        self.workflows = tuple(workflows)
        self.provider_call_limiter = ProviderCallLimiter(provider_call_limits)
        self.provider_rate_limit_controller = (
            ProviderRateLimitController(
                policy=provider_rate_limit_policy,
                legacy_limits=provider_call_limits,
            )
            if provider_rate_limit_policy is not None
            else None
        )
        self.provider_retry_policy = provider_retry_policy
        self.artifact_policy = artifact_policy
        self.provider_model_policy_guard = provider_model_policy_guard
        self.deps = deps
        self.tenant_resolver = tenant_resolver
        self.cache_store = cache_store
        self.worker_kwargs = worker_kwargs

    def preflight(self) -> PreflightReport:
        return preflight_ai_activities(
            activities=self.activities,
            registry=self.registry,
            provider=self.provider,
            provider_default_params=provider_default_params(self.provider),
        )

    def build_worker(self) -> Any:
        try:
            from temporalio.worker import Worker
        except ModuleNotFoundError as exc:
            raise RuntimeError("temporalio is required to create a TypefluxWorker") from exc

        temporal_activities = [
            build_temporal_activity(
                activity,
                registry=self.registry,
                provider=self.provider,
                task_queue=self.task_queue,
                observer=self.observer,
                provider_call_limiter=self.provider_call_limiter,
                provider_rate_limit_controller=self.provider_rate_limit_controller,
                provider_retry_policy=self.provider_retry_policy,
                artifact_policy=self.artifact_policy,
                provider_model_policy_guard=self.provider_model_policy_guard,
                deps=self.deps,
                tenant_resolver=self.tenant_resolver,
                cache_store=self.cache_store,
            )
            if isinstance(activity, AIActivity)
            # Plain Temporal activities are registered as-is: the decorated
            # callable carries its own definition, and the AI wrapper
            # (registry/provider/preflight/repair/observability) must not
            # apply to non-AI code.
            else activity.activity
            for activity in self.activities
        ]
        temporal_activities.extend(self._cache_prep_activities())
        return Worker(
            self.client,
            task_queue=self.task_queue,
            workflows=list(self.workflows),
            activities=temporal_activities,
            **self.worker_kwargs,
        )

    def _cache_prep_activities(self) -> list[Any]:
        """Register the cache-prep/release activities per cache-enabled AIActivity
        (#60/#368).

        The generated workflow calls ``{activity}.__prepare_cache__`` before a
        cached map fan-out and ``{activity}.__release_cache__`` after it; both
        must be registered on the worker. We only emit them when caching is
        requested, and fail closed on a name collision with a real activity so a
        generated activity can never shadow one.
        """
        # Both AIActivity and TemporalActivityDescriptor expose ``.name``.
        existing = {activity.name for activity in self.activities}
        generated: list[Any] = []
        for activity in self.activities:
            if not isinstance(activity, AIActivity):
                continue
            if activity.session_cache is None or not activity.session_cache.enabled:
                continue
            for generated_name in (
                cache_prep_activity_name(activity.name),
                cache_release_activity_name(activity.name),
            ):
                if generated_name in existing:
                    raise ValueError(
                        f"cache activity name {generated_name!r} collides with a registered "
                        f"activity; rename the activity to enable session caching"
                    )
            generated.append(
                build_cache_prep_activity(
                    activity,
                    registry=self.registry,
                    provider=self.provider,
                    artifact_policy=self.artifact_policy,
                    provider_model_policy_guard=self.provider_model_policy_guard,
                )
            )
            generated.append(build_cache_release_activity(activity, provider=self.provider))
        return generated

    async def run(self) -> None:
        worker = self.build_worker()
        await worker.run()

    async def execute_workflow(
        self,
        workflow: Any,
        input_value: Any,
        *,
        id: str,
        task_queue: str | None = None,
        result_type: type | None = None,
        langfuse_client: Any | None = None,
        trace_writer: TraceWriter | None = None,
        workflow_name: str | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **execute_kwargs: Any,
    ) -> Any:
        resolved_trace_writer = trace_writer or (
            self.observability.writer if self.observability is not None else None
        )
        resolved_langfuse_client = (
            langfuse_client or self.langfuse_client or _observer_langfuse_client(self.observer)
        )
        if resolved_trace_writer is None and resolved_langfuse_client is not None:
            resolved_trace_writer = LangfuseTraceWriter(client=resolved_langfuse_client)
        return await execute_typeflux_workflow(
            client=self.client,
            workflow=workflow,
            input_value=input_value,
            id=id,
            task_queue=task_queue or self.task_queue,
            result_type=result_type,
            trace_writer=resolved_trace_writer,
            workflow_name=workflow_name,
            tags=tags,
            metadata=metadata,
            activities=self.activities,
            activity_rollup=(
                build_activity_rollup(self.activities, self.registry, self.provider)
                if resolved_trace_writer is not None
                and getattr(resolved_trace_writer, "enabled", False)
                else None
            ),
            **execute_kwargs,
        )


def _observer_langfuse_client(observer: AIActivityObserver | None) -> Any | None:
    return getattr(observer, "client", None)


def _async_provider(provider: ModelProvider) -> AsyncModelProvider | None:
    supports_async = getattr(provider, "supports_async_structured_call", True)
    if supports_async is False:
        return None
    method = getattr(provider, "async_structured_call", None)
    if callable(method):
        return cast(AsyncModelProvider, provider)
    return None


def _provider_call_metadata_with_execution_mode(
    provider_call_metadata: dict[str, Any] | None,
    *,
    execution_mode: str,
) -> dict[str, Any]:
    metadata = dict(provider_call_metadata or {})
    metadata.setdefault("execution_mode", execution_mode)
    return metadata


def build_activity_rollup(
    activities: Sequence[YamlWorkflowActivity],
    registry: PromptRegistry,
    provider: ModelProvider,
) -> tuple[str | dict[str, Any], ...]:
    rollup: list[str | dict[str, Any]] = []
    default_params = provider_default_params(provider)
    for activity in activities:
        if not isinstance(activity, AIActivity):
            # Plain Temporal activities appear as planned activity names; the
            # rollup's prompt/provider metadata is AI-only.
            rollup.append(activity.name)
            continue
        try:
            resolved_prompt = registry.resolve(activity.prompt_ref)
        except Exception as exc:
            rollup.append(build_unresolved_activity_rollup_entry(activity=activity, error=exc))
            continue
        rollup.append(
            build_activity_rollup_entry(
                activity=activity,
                resolved_prompt=resolved_prompt,
                provider_params=default_params.merge(
                    resolved_prompt.provider_params,
                    activity.provider_params,
                ),
            )
        )
    return tuple(rollup)


def _raise_non_retryable(error: Exception) -> None:
    try:
        from temporalio.exceptions import ApplicationError
    except ModuleNotFoundError:
        return
    raise ApplicationError(
        str(error),
        type=type(error).__name__,
        non_retryable=True,
    ) from error


def _raise_temporal_non_retryable_if_needed(
    error: AIActivityOutputValidationError
    | ModerationBlockedError
    | PromptResolutionError
    | ProviderError,
) -> None:
    if getattr(error, "retryable", False):
        return
    try:
        from temporalio.exceptions import ApplicationError
    except ModuleNotFoundError:
        return
    raise ApplicationError(
        str(error),
        type=type(error).__name__,
        non_retryable=True,
    ) from error


__all__ = ["TypefluxWorker", "build_activity_rollup", "build_temporal_activity"]
