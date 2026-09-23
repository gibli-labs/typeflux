from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from threading import RLock
from typing import Any
from weakref import ReferenceType, WeakKeyDictionary, WeakValueDictionary, ref

from pydantic import BaseModel, ValidationError

from typeflux.core.artifacts import content_part_payload
from typeflux.core.contracts import AIActivity, ChatMessage, ProviderParams
from typeflux.execution.observer import ActivityObservation, ObservationHandle
from typeflux.manifests import (
    ActivityExecutionManifest,
    AIActivityManifest,
    AIInvocationContext,
    compact_activity_manifest,
    merge_activity_rollup,
)
from typeflux.metadata import (
    ActivityContextContributor,
    ActivityMetadataContext,
    MetadataContributor,
    activity_contribution,
)
from typeflux.observability.redaction import (
    Redactor,
    RegexPIIRedactor,
    _is_redacted_metadata,
    _mark_redacted_metadata,
)
from typeflux.prompts.context import langfuse_prompt_from_context
from typeflux.prompts.errors import PromptResolutionError
from typeflux.providers.base import ProviderUsage
from typeflux.providers.errors import ProviderError

_CURRENT_WORKFLOW_OBSERVATION: ContextVar[_LangfuseWorkflowInvocation | None] = ContextVar(
    "typeflux_current_workflow_observation",
    default=None,
)
_LANGFUSE_CLIENT_REDACTORS: WeakKeyDictionary[Any, Redactor] = WeakKeyDictionary()
_LANGFUSE_CLIENT_REDACTORS_BY_ID: dict[int, tuple[ReferenceType[Any], Redactor]] = {}
_LANGFUSE_CLIENT_REDACTORS_LOCK = RLock()


def _identity_payload(value: Any) -> Any:
    return value


def _payload_redaction(client: Any, redactor: Redactor | None = None) -> Callable[[Any], Any]:
    # Typeflux-built clients mask at the SDK boundary; user-supplied clients
    # get equivalent redaction applied to every observation payload here.
    if _registered_langfuse_redactor(client) is not None:
        return _identity_payload
    resolved = redactor or RegexPIIRedactor.default()

    def redact(value: Any) -> Any:
        if value is None:
            return None
        if _is_redacted_metadata(value):
            return dict(value)
        return resolved.redact(value)

    return redact


def _safe_status_message(error: BaseException) -> str:
    # Status messages bypass the metadata redaction boundary, so only
    # messages that are sanitized by construction pass through verbatim.
    if isinstance(error, (PromptResolutionError, ProviderError)):
        return str(error)
    if isinstance(error, ValidationError):
        return f"{error.error_count()} validation error(s) for {error.title}"
    return type(error).__name__


@contextmanager
def observe_workflow_invocation(
    *,
    client: Any,
    workflow_name: str,
    input_value: Any,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    subject_ids: Sequence[str] | None = None,
    rollup_sink: _ActivityTraceRollupSink | None = None,
    redactor: Redactor | None = None,
) -> Iterator[_LangfuseWorkflowInvocation]:
    """Observe a Langfuse workflow root span.

    Direct callers get same-context activity rollup through the workflow
    ContextVar. Cross-context rollup is intentionally owned by
    LangfuseTraceWriter, which passes a shared internal rollup sink to both the
    workflow observation and writer-created activity observer.

    ``subject_ids`` (#715 slice 1) sets the native Langfuse ``userId`` to the
    PRIMARY (first) subject — first-class ``trace.list(user_id=)`` — while the
    ``typeflux.subject:{id}`` tags (already folded into ``tags`` upstream) carry
    the full list. Native field and portable tags always agree.
    """
    redact = _payload_redaction(client, redactor)
    input_payload = redact(_payload(input_value))
    observation_name = workflow_trace_name(workflow_name)
    user_id = subject_ids[0] if subject_ids else None
    with ExitStack() as stack:
        if tags or user_id is not None:
            try:
                from langfuse import propagate_attributes
            except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                raise RuntimeError("langfuse is required for Langfuse tracing") from exc

            stack.enter_context(
                propagate_attributes(
                    trace_name=observation_name,
                    tags=tags or None,
                    user_id=user_id,
                )
            )
        cm = client.start_as_current_observation(
            name=observation_name,
            as_type="span",
            input=input_payload,
            metadata=redact(metadata),
        )
        observation = cm.__enter__()
        _set_trace_io(client, input=input_payload)
        handle = _LangfuseWorkflowInvocation(
            client=client,
            observation=observation,
            rollup_sink=rollup_sink,
            redact=redact,
        )
        if metadata is not None:
            handle.update_metadata(metadata)
        token = _CURRENT_WORKFLOW_OBSERVATION.set(handle)
        try:
            yield handle
        except BaseException as exc:
            handle.update_error(exc)
            raise
        finally:
            _CURRENT_WORKFLOW_OBSERVATION.reset(token)
            handle.unregister_rollup()
            cm.__exit__(None, None, None)


@contextmanager
def observe_lifecycle_operation(
    *,
    client: Any,
    operation_type: str,
    operation_name: str,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    redactor: Redactor | None = None,
) -> Iterator[_LangfuseObservationHandle]:
    redact = _payload_redaction(client, redactor)
    observation_name = _lifecycle_operation_name(operation_type, operation_name)
    with ExitStack() as stack:
        if tags:
            try:
                from langfuse import propagate_attributes
            except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                raise RuntimeError("langfuse is required for Langfuse tracing") from exc

            stack.enter_context(propagate_attributes(trace_name=observation_name, tags=tags))
        cm = client.start_as_current_observation(
            name=observation_name,
            as_type="span",
            metadata=redact(metadata),
        )
        observation = cm.__enter__()
        handle = _LangfuseObservationHandle(observation, redact=redact)
        try:
            yield handle
        except BaseException as exc:
            handle.update_error(exc)
            raise
        finally:
            cm.__exit__(None, None, None)


def invocation_metadata(
    *,
    manifest: AIActivityManifest,
    activity_execution_manifest: ActivityExecutionManifest | Mapping[str, Any] | None = None,
    invocation_context: AIInvocationContext | None,
    validation_attempt: int,
    extra: dict[str, Any] | None = None,
    metadata_contributors: Sequence[MetadataContributor] = (),
) -> dict[str, Any]:
    metadata: dict[str, Any] = dict(extra or {})
    activity_name = manifest.activity_name
    execution_payload = (
        _activity_execution_manifest_payload(activity_execution_manifest)
        if activity_execution_manifest is not None
        else None
    )
    typeflux = _base_typeflux_metadata(
        level="provider",
        activity_name=activity_name,
        activity_manifest_hash=manifest.manifest_hash,
        activity_execution_manifest_hash=(
            None if execution_payload is None else execution_payload.get("manifest_hash")
        ),
        invocation_context=invocation_context,
        activity_execution_manifest=activity_execution_manifest,
        metadata_contributors=metadata_contributors,
    )
    typeflux["provider"] = _drop_none(
        {
            "validation_attempt": validation_attempt,
            "hook_name": manifest.hook_name,
            "resolved_prompt_version": manifest.resolved_prompt_version,
        }
    )
    # Flat "typeflux.activity_name" / "typeflux.manifest_hash" join keys are a
    # legacy read-only contract; the structured typeflux.activity_name and
    # typeflux.join fields above are canonical.
    metadata["typeflux"] = _drop_none_deep(typeflux)
    return metadata


def _lifecycle_operation_name(operation_type: str, operation_name: str) -> str:
    label = "Query" if operation_type == "query" else "Signal"
    return f"TypefluxLifecycle{label}:{operation_name}"


def workflow_trace_name(workflow_name: str) -> str:
    """The canonical Langfuse trace name for a workflow's root observation."""
    return f"TypefluxWorkflow:{workflow_name}"


class LangfuseAIActivityObserver:
    def __init__(
        self,
        *,
        client: Any | None = None,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        timeout: int | None = 60,
        cost_semantic_generations: bool = False,
        redactor: Redactor | None = None,
        rollup_sink: _ActivityTraceRollupSink | None = None,
        metadata_contributors: Sequence[MetadataContributor] = (),
    ) -> None:
        self.redactor = (
            redactor or _registered_langfuse_redactor(client) or RegexPIIRedactor.default()
        )
        self.client = client or _build_langfuse_client(
            host=host,
            public_key=public_key,
            secret_key=secret_key,
            timeout=timeout,
            redactor=self.redactor,
        )
        self.cost_semantic_generations = cost_semantic_generations
        self._rollup_sink = rollup_sink
        self._metadata_contributors = tuple(metadata_contributors)
        self._payload_redact = _payload_redaction(self.client, self.redactor)

    @contextmanager
    def observe_activity(
        self,
        *,
        activity: AIActivity,
        input_value: BaseModel,
        manifest: AIActivityManifest,
        execution_manifest: ActivityExecutionManifest,
        invocation_context: AIInvocationContext | None,
    ) -> Iterator[ActivityObservation]:
        metadata = semantic_metadata(
            manifest=manifest,
            activity_execution_manifest=execution_manifest,
            invocation_context=invocation_context,
            level="activity",
            metadata_contributors=self._metadata_contributors,
        )
        workflow_observation = _CURRENT_WORKFLOW_OBSERVATION.get()
        if (
            workflow_observation is None
            and invocation_context is not None
            and self._rollup_sink is not None
        ):
            workflow_observation = self._rollup_sink.lookup(
                invocation_context.temporal_workflow_id,
                invocation_context.temporal_run_id,
            )
        if workflow_observation is not None:
            workflow_observation.merge_activity_manifest(execution_manifest)
        cm = self.client.start_as_current_observation(
            name=activity.name,
            as_type="span",
            input=self._payload_redact(_model_payload(input_value)),
            metadata=self._payload_redact(metadata),
        )
        observation = cm.__enter__()
        try:
            yield _LangfuseActivityObservation(
                observation=observation,
                manifest=manifest,
                invocation_context=invocation_context,
                cost_semantic_generations=self.cost_semantic_generations,
                metadata_contributors=self._metadata_contributors,
                redact=self._payload_redact,
            )
        except BaseException as exc:
            observation.update(level="ERROR", status_message=_safe_status_message(exc))
            raise
        finally:
            cm.__exit__(None, None, None)

    def flush(self) -> None:
        self.client.flush()

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redacted = self.redactor.redact(metadata)
        if isinstance(redacted, dict):
            return _mark_redacted_metadata(redacted)
        return metadata


class _LangfuseObservationHandle:
    def __init__(
        self,
        observation: Any,
        *,
        redact: Callable[[Any], Any] = _identity_payload,
    ) -> None:
        self._observation = observation
        self._redact = redact

    def update_output(self, output_value: BaseModel) -> None:
        self._observation.update(output=self._redact(_model_payload(output_value)))

    def update_error(self, error: BaseException) -> None:
        self._observation.update(level="ERROR", status_message=_safe_status_message(error))

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        self._observation.update(metadata=self._redact(metadata))

    def update_usage(self, usage: ProviderUsage) -> None:
        # Token counts are operational metadata, never content; cost stays
        # derived by Langfuse from the generation's model plus these counts.
        details = usage.usage_details()
        updates: dict[str, Any] = {}
        if details:
            updates["usage_details"] = details
        if usage.model:
            updates["model"] = usage.model
        if updates:
            self._observation.update(**updates)


class _LangfuseWorkflowInvocation(_LangfuseObservationHandle):
    def __init__(
        self,
        *,
        client: Any,
        observation: Any,
        rollup_sink: _ActivityTraceRollupSink | None = None,
        redact: Callable[[Any], Any] = _identity_payload,
    ) -> None:
        super().__init__(observation, redact=redact)
        self._client = client
        self._metadata: dict[str, Any] | None = None
        self._rollup_sink = rollup_sink
        self._lock = RLock()

    def update_output(self, output_value: Any) -> None:
        output_payload = self._redact(_payload(output_value))
        self._observation.update(output=output_payload)
        _set_trace_io(self._client, output=output_payload)

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            # The unredacted merge state stays internal for rollup merging;
            # only the payload sent to the backend is redacted.
            merged = _preserve_executed_activity_entries(self._metadata, metadata)
            self._metadata = merged
            if self._rollup_sink is not None:
                self._rollup_sink.refresh(self)
            self._observation.update(metadata=self._redact(merged))

    def unregister_rollup(self) -> None:
        if self._rollup_sink is not None:
            self._rollup_sink.unregister(self)

    def merge_activity_manifest(self, activity_manifest: ActivityExecutionManifest) -> None:
        with self._lock:
            if self._metadata is None:
                return
            typeflux = self._metadata.get("typeflux")
            if not isinstance(typeflux, dict):
                return
            execution_manifest = typeflux.get("execution_manifest")
            if not isinstance(execution_manifest, dict):
                return
            updated = dict(self._metadata)
            updated_typeflux = dict(typeflux)
            updated_typeflux["execution_manifest"] = merge_activity_rollup(
                execution_manifest,
                activity_manifest,
            )
            updated["typeflux"] = updated_typeflux
            self.update_metadata(updated)


def _preserve_executed_activity_entries(
    current: dict[str, Any] | None,
    refreshed: dict[str, Any],
) -> dict[str, Any]:
    # Root trace metadata refreshes (such as the post-start run-id refresh)
    # rebuild from the planned activity rollup; executed activity manifests
    # already merged into the trace must survive the rebuild.
    executed = _executed_activity_entries(current)
    if not executed:
        return refreshed
    refreshed_typeflux = refreshed.get("typeflux")
    if not isinstance(refreshed_typeflux, dict):
        return refreshed
    manifest = refreshed_typeflux.get("execution_manifest")
    if not isinstance(manifest, dict):
        return refreshed
    activities = manifest.get("activities")
    if not isinstance(activities, list):
        return refreshed
    merged_activities: list[Any] = []
    changed = False
    for entry in activities:
        name: str | None = None
        if isinstance(entry, dict):
            value = entry.get("activity_name")
            name = value if isinstance(value, str) else None
        elif isinstance(entry, str):
            name = entry
        replacement = executed.get(name) if name is not None else None
        if replacement is not None and not _is_executed_activity_entry(entry):
            merged_activities.append(replacement)
            changed = True
        else:
            merged_activities.append(entry)
    if not changed:
        return refreshed
    updated = dict(refreshed)
    updated_typeflux = dict(refreshed_typeflux)
    updated_manifest = dict(manifest)
    updated_manifest["activities"] = merged_activities
    updated_typeflux["execution_manifest"] = updated_manifest
    updated["typeflux"] = updated_typeflux
    return updated


def _executed_activity_entries(metadata: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if metadata is None:
        return {}
    typeflux = metadata.get("typeflux")
    if not isinstance(typeflux, dict):
        return {}
    manifest = typeflux.get("execution_manifest")
    if not isinstance(manifest, dict):
        return {}
    executed: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("activities") or ():
        if _is_executed_activity_entry(entry):
            name = entry.get("activity_name")
            if isinstance(name, str):
                executed[name] = entry
    return executed


def _is_executed_activity_entry(entry: Any) -> bool:
    # Executed compact activity manifests carry the execution manifest hash
    # under "manifest_hash"; planned rollup entries only have
    # "activity_manifest_hash".
    return isinstance(entry, dict) and isinstance(entry.get("manifest_hash"), str)


class _ActivityTraceRollupSink:
    def __init__(self) -> None:
        self._lock = RLock()
        self._observations: WeakValueDictionary[
            tuple[str | None, str | None],
            _LangfuseWorkflowInvocation,
        ] = WeakValueDictionary()
        self._observation_keys: WeakKeyDictionary[
            _LangfuseWorkflowInvocation,
            set[tuple[str | None, str | None]],
        ] = WeakKeyDictionary()

    def refresh(self, observation: _LangfuseWorkflowInvocation) -> None:
        with self._lock:
            self._remove_locked(observation)
            keys = self._keys_for_observation(observation)
            for key in keys:
                self._observations[key] = observation
            self._observation_keys[observation] = keys

    def lookup(
        self,
        workflow_id: str | None,
        run_id: str | None,
    ) -> _LangfuseWorkflowInvocation | None:
        if not isinstance(workflow_id, str):
            return None
        with self._lock:
            if isinstance(run_id, str):
                observation = self._observations.get((workflow_id, run_id))
                if observation is not None:
                    return observation
            return self._observations.get((workflow_id, None))

    def unregister(self, observation: _LangfuseWorkflowInvocation) -> None:
        with self._lock:
            self._remove_locked(observation)
            self._observation_keys.pop(observation, None)

    def _remove_locked(self, observation: _LangfuseWorkflowInvocation) -> None:
        for key in tuple(self._observation_keys.get(observation, ())):
            if self._observations.get(key) is observation:
                self._observations.pop(key, None)

    def _keys_for_observation(
        self,
        observation: _LangfuseWorkflowInvocation,
    ) -> set[tuple[str | None, str | None]]:
        metadata = observation._metadata or {}
        typeflux = metadata.get("typeflux")
        if not isinstance(typeflux, dict):
            return set()
        execution_manifest = typeflux.get("execution_manifest")
        if not isinstance(execution_manifest, dict):
            return set()
        workflow_id = execution_manifest.get("workflow_id")
        run_id = execution_manifest.get("temporal_run_id")
        if not isinstance(workflow_id, str):
            return set()
        keys: set[tuple[str | None, str | None]] = {(workflow_id, None)}
        if isinstance(run_id, str):
            keys.add((workflow_id, run_id))
        return keys


class _LangfuseActivityObservation(_LangfuseObservationHandle):
    def __init__(
        self,
        *,
        observation: Any,
        manifest: AIActivityManifest,
        invocation_context: AIInvocationContext | None,
        cost_semantic_generations: bool,
        metadata_contributors: Sequence[MetadataContributor],
        redact: Callable[[Any], Any] = _identity_payload,
    ) -> None:
        super().__init__(observation, redact=redact)
        self._manifest = manifest
        self._invocation_context = invocation_context
        self._cost_semantic_generations = cost_semantic_generations
        self._metadata_contributors = tuple(metadata_contributors)

    @contextmanager
    def observe_generation(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        metadata: dict[str, Any],
        validation_attempt: int,
        model: str | None,
        temperature: float | None,
        provider_params: ProviderParams | None = None,
        observation_context: dict[str, Any] | None = None,
    ) -> Iterator[ObservationHandle]:
        start_kwargs: dict[str, Any] = {
            "name": f"{self._manifest.activity_name}.generation",
            "as_type": "generation",
            "input": self._redact(_messages_payload(messages)),
            "metadata": self._redact(
                semantic_metadata(
                    manifest=self._manifest,
                    activity_execution_manifest=_activity_execution_manifest_from_metadata(
                        metadata
                    ),
                    invocation_context=self._invocation_context,
                    level="generation",
                    validation_attempt=validation_attempt,
                    registry_metadata=_registry_metadata(metadata),
                    provider_controls=_provider_controls_metadata(metadata),
                    metadata_contributors=self._metadata_contributors,
                )
            ),
            "model": model if self._cost_semantic_generations else None,
            "model_parameters": _model_parameters(temperature, provider_params)
            if self._cost_semantic_generations
            else None,
        }
        prompt = langfuse_prompt_from_context(observation_context)
        if prompt is not None:
            start_kwargs["prompt"] = prompt
        cm = self._observation.start_as_current_observation(**start_kwargs)
        generation = cm.__enter__()
        try:
            yield _LangfuseObservationHandle(generation, redact=self._redact)
        except BaseException as exc:
            generation.update(level="ERROR", status_message=_safe_status_message(exc))
            raise
        finally:
            cm.__exit__(None, None, None)

    @contextmanager
    def observe_hook(
        self,
        *,
        activity_input: BaseModel,
        llm_output: BaseModel,
        metadata: dict[str, Any],
    ) -> Iterator[ObservationHandle]:
        cm = self._observation.start_as_current_observation(
            name=f"{self._manifest.activity_name}.hook",
            as_type="span",
            input=self._redact(
                {
                    "activity_input": _model_payload(activity_input),
                    "llm_output": _model_payload(llm_output),
                }
            ),
            metadata=self._redact(
                semantic_metadata(
                    manifest=self._manifest,
                    activity_execution_manifest=_activity_execution_manifest_from_metadata(
                        metadata
                    ),
                    invocation_context=self._invocation_context,
                    level="hook",
                    registry_metadata=_registry_metadata(metadata),
                    metadata_contributors=self._metadata_contributors,
                )
            ),
        )
        hook = cm.__enter__()
        try:
            yield _LangfuseObservationHandle(hook, redact=self._redact)
        except BaseException as exc:
            hook.update(level="ERROR", status_message=_safe_status_message(exc))
            raise
        finally:
            cm.__exit__(None, None, None)


def semantic_metadata(
    *,
    manifest: AIActivityManifest,
    activity_execution_manifest: ActivityExecutionManifest | Mapping[str, Any] | None = None,
    invocation_context: AIInvocationContext | None,
    level: str,
    validation_attempt: int | None = None,
    registry_metadata: dict[str, Any] | None = None,
    provider_controls: dict[str, Any] | None = None,
    metadata_contributors: Sequence[MetadataContributor] = (),
) -> dict[str, Any]:
    activity_execution_payload = (
        _activity_execution_manifest_payload(activity_execution_manifest)
        if activity_execution_manifest is not None
        else None
    )
    typeflux: dict[str, Any] = _base_typeflux_metadata(
        level=level,
        activity_name=manifest.activity_name,
        activity_manifest_hash=manifest.manifest_hash,
        activity_execution_manifest_hash=(
            None
            if activity_execution_payload is None
            else activity_execution_payload.get("manifest_hash")
        ),
        invocation_context=invocation_context,
        activity_execution_manifest=activity_execution_manifest,
        metadata_contributors=metadata_contributors,
    )
    if activity_execution_manifest is not None and activity_execution_payload is not None:
        if level == "activity" and "manifest_hash" in activity_execution_payload:
            typeflux["activity_execution_manifest"] = activity_execution_payload
        elif level != "activity":
            typeflux["activity"] = compact_activity_manifest(activity_execution_payload)
    if validation_attempt is not None:
        typeflux["generation"] = {"validation_attempt": validation_attempt}
    if level == "hook":
        typeflux["hook"] = {"hook_name": manifest.hook_name}
    if registry_metadata:
        typeflux["registry"] = registry_metadata
    if provider_controls:
        typeflux["provider_controls"] = provider_controls
    return {"typeflux": _drop_none_deep(typeflux)}


def configure_temporal_langfuse_tracing(
    *,
    host: str | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
    timeout: int | None = 60,
    add_temporal_spans: bool = True,
    export_all_temporal_spans: bool = True,
    redactor: Redactor | None = None,
):
    try:
        from opentelemetry import trace
        from temporalio.contrib.opentelemetry import OpenTelemetryPlugin, create_tracer_provider
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError(
            "temporalio and opentelemetry are required for Temporal tracing"
        ) from exc

    try:
        from langfuse import Langfuse
        from langfuse.span_filter import is_default_export_span
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError("langfuse is required for Langfuse tracing") from exc

    tracer_provider = create_tracer_provider()
    try:
        trace.set_tracer_provider(tracer_provider)
    except Exception:
        pass

    resolved_redactor = redactor or RegexPIIRedactor.default()
    langfuse_client = Langfuse(
        host=host,
        public_key=public_key,
        secret_key=secret_key,
        timeout=timeout,
        mask=_mask(resolved_redactor),
        tracer_provider=tracer_provider,
        should_export_span=(
            _export_default_plus_temporal(is_default_export_span)
            if export_all_temporal_spans
            else is_default_export_span
        ),
    )
    _register_langfuse_redactor(langfuse_client, resolved_redactor)
    return _TemporalLangfuseTracing(
        plugin=OpenTelemetryPlugin(add_temporal_spans=add_temporal_spans),
        client=langfuse_client,
        tracer_provider=tracer_provider,
    )


class _TemporalLangfuseTracing:
    __slots__ = ("plugin", "client", "tracer_provider")

    def __init__(self, *, plugin: Any, client: Any, tracer_provider: Any) -> None:
        self.plugin = plugin
        self.client = client
        self.tracer_provider = tracer_provider

    def __iter__(self):
        yield self.plugin
        yield self.client


def _export_default_plus_temporal(is_default_export_span):
    def should_export_span(span) -> bool:
        if is_default_export_span(span):
            return True
        scope = span.instrumentation_scope.name if span.instrumentation_scope else ""
        return scope.startswith("temporalio.")

    return should_export_span


def _build_langfuse_client(
    *,
    host: str | None,
    public_key: str | None,
    secret_key: str | None,
    timeout: int | None,
    redactor: Redactor | None,
) -> Any:
    try:
        from langfuse import Langfuse
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError("langfuse is required for Langfuse tracing") from exc
    resolved_redactor = redactor or RegexPIIRedactor.default()
    client = Langfuse(
        host=host,
        public_key=public_key,
        secret_key=secret_key,
        timeout=timeout,
        mask=_mask(resolved_redactor),
    )
    _register_langfuse_redactor(client, resolved_redactor)
    return client


def _mask(redactor: Redactor | None):
    resolved_redactor = redactor or RegexPIIRedactor.default()

    def mask(data: Any, **kwargs: Any) -> Any:
        if _is_redacted_metadata(data):
            return dict(data)
        return resolved_redactor.redact(data, **kwargs)

    return mask


def _register_langfuse_redactor(client: Any, redactor: Redactor) -> None:
    with _LANGFUSE_CLIENT_REDACTORS_LOCK:
        try:
            _LANGFUSE_CLIENT_REDACTORS[client] = redactor
            return
        except TypeError:
            client_id = id(client)

            def remove(_client_ref: ReferenceType[Any]) -> None:
                with _LANGFUSE_CLIENT_REDACTORS_LOCK:
                    _LANGFUSE_CLIENT_REDACTORS_BY_ID.pop(client_id, None)

            try:
                _LANGFUSE_CLIENT_REDACTORS_BY_ID[client_id] = (ref(client, remove), redactor)
            except TypeError as exc:
                raise TypeError(
                    "Langfuse clients must support weak references to register a Typeflux redactor"
                ) from exc


def _registered_langfuse_redactor(client: Any | None) -> Redactor | None:
    if client is None:
        return None
    with _LANGFUSE_CLIENT_REDACTORS_LOCK:
        try:
            registered = _LANGFUSE_CLIENT_REDACTORS.get(client)
        except TypeError:
            registered = None
        if registered is not None:
            return registered
        client_id = id(client)
        fallback = _LANGFUSE_CLIENT_REDACTORS_BY_ID.get(client_id)
        if fallback is None:
            return None
        client_ref, redactor = fallback
        if client_ref() is client:
            return redactor
        _LANGFUSE_CLIENT_REDACTORS_BY_ID.pop(client_id, None)
        return None


def _model_payload(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _payload(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _set_trace_io(client: Any, *, input: Any | None = None, output: Any | None = None) -> None:
    setter = getattr(client, "set_current_trace_io", None)
    if setter is None:
        return
    kwargs: dict[str, Any] = {}
    if input is not None:
        kwargs["input"] = input
    if output is not None:
        kwargs["output"] = output
    if kwargs:
        setter(**kwargs)


def _messages_payload(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [
        {"role": message.role, "content": content_part_payload(message.content)}
        for message in messages
    ]


def _model_parameters(
    temperature: float | None,
    provider_params: ProviderParams | None,
) -> dict[str, Any] | None:
    payload = dict(provider_params.to_dict() if provider_params is not None else {})
    payload.pop("model", None)
    if temperature is not None:
        payload.setdefault("temperature", temperature)
    return payload or None


def _registry_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in metadata.items()
        if key != "typeflux" and not key.startswith("typeflux.") and not key.startswith("temporal.")
    }


def _provider_controls_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    typeflux = metadata.get("typeflux")
    if not isinstance(typeflux, Mapping):
        return {}
    provider_controls = typeflux.get("provider_controls")
    return dict(provider_controls) if isinstance(provider_controls, Mapping) else {}


def _activity_execution_manifest_from_metadata(
    metadata: dict[str, Any],
) -> Mapping[str, Any] | None:
    value = metadata.get("typeflux.activity_execution_manifest")
    if isinstance(value, Mapping):
        return value
    typeflux = metadata.get("typeflux")
    if isinstance(typeflux, Mapping):
        activity = typeflux.get("activity_execution_manifest")
        if isinstance(activity, Mapping):
            return activity
        compact = typeflux.get("activity")
        if isinstance(compact, Mapping):
            return compact
    return value if isinstance(value, Mapping) else None


def _activity_execution_manifest_payload(
    manifest: ActivityExecutionManifest | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(manifest, ActivityExecutionManifest):
        return manifest.to_dict()
    return dict(manifest)


def _temporal_payload(invocation_context: AIInvocationContext | None) -> dict[str, Any]:
    if invocation_context is None:
        return {}
    return _drop_none(
        {
            "namespace": invocation_context.temporal_namespace,
            "workflow_type": invocation_context.temporal_workflow_type,
            "workflow_id": invocation_context.temporal_workflow_id,
            "run_id": invocation_context.temporal_run_id,
            "task_queue": invocation_context.temporal_task_queue,
            "activity_type": invocation_context.temporal_activity_type,
            "activity_id": invocation_context.temporal_activity_id,
            "activity_attempt": invocation_context.temporal_activity_attempt,
        }
    )


def _base_typeflux_metadata(
    *,
    level: str,
    activity_name: str,
    activity_manifest_hash: str,
    activity_execution_manifest_hash: str | None,
    invocation_context: AIInvocationContext | None,
    activity_execution_manifest: ActivityExecutionManifest | Mapping[str, Any] | None = None,
    metadata_contributors: Sequence[MetadataContributor] = (),
) -> dict[str, Any]:
    typeflux: dict[str, Any] = {
        "manifest_version": "1",
        "level": level,
        "activity_name": activity_name,
        "join": {
            "activity_manifest_hash": activity_manifest_hash,
            "activity_execution_manifest_hash": activity_execution_manifest_hash,
        },
    }
    contributors = (ActivityContextContributor(), *metadata_contributors)
    contribution = activity_contribution(
        contributors,
        ActivityMetadataContext(
            manifest=None,
            activity_execution_manifest=activity_execution_manifest,
            invocation_context=invocation_context,
            level=level,
        ),
    )
    contributed = contribution.activity_metadata.get("typeflux")
    if isinstance(contributed, Mapping):
        typeflux = _deep_merge(typeflux, dict(contributed))
    return typeflux


def _deep_merge(left: dict[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(merged[key], value)
            continue
        if key in merged and merged[key] != value:
            raise ValueError(f"conflicting metadata contribution for {key!r}")
        merged[key] = value
    return merged


def _map_payload(invocation_context: AIInvocationContext | None) -> dict[str, Any]:
    if invocation_context is None:
        return {}
    return _drop_none(
        {
            "map_step_id": invocation_context.map_step_id,
            "map_index": invocation_context.map_index,
            "map_size": invocation_context.map_size,
            "map_concurrency": invocation_context.map_concurrency,
        }
    )


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _drop_none_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _drop_none_deep(item)) is not None
        }
    if isinstance(value, list):
        return [_drop_none_deep(item) for item in value]
    return value


__all__ = [
    "LangfuseAIActivityObserver",
    "configure_temporal_langfuse_tracing",
    "invocation_metadata",
    "observe_workflow_invocation",
    "semantic_metadata",
]
