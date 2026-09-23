"""Generic OpenTelemetry trace writer (#59 follow-up).

A vendor-neutral :class:`TraceWriter` that emits Typeflux observations
(workflow / activity / generation / hook / lifecycle) as OpenTelemetry spans and
exports them through any OTLP-ingesting backend. The vendor is a thin
:class:`OtelVendorProfile`: it supplies the OTLP span processor (endpoint +
auth) and the span-attribute conventions (run-type key, input/output keys,
metadata prefix, …). LangSmith is the first profile; adding another OTLP backend
is a profile, not a rewrite.

The writer reuses Temporal's ``create_tracer_provider`` + ``OpenTelemetryPlugin``
so the native Temporal workflow/activity spans and the Typeflux observation spans
share one tracer provider and correlate into a single backend trace.

Nested Typeflux metadata (``typeflux.execution_manifest`` and friends) cannot be
an OTEL attribute directly — attributes are flat scalars — so non-scalar
metadata values are JSON-encoded per top-level key under the profile's metadata
prefix. The matching reader decodes them back into nested dicts so the trace CLI
sees the same ``typeflux.*`` shape it gets from Langfuse.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from typeflux.core.contracts import AIActivity, ChatMessage, ProviderParams
from typeflux.execution.observer import (
    ActivityObservation,
    AIActivityObserver,
    ObservationHandle,
)
from typeflux.manifests import (
    ActivityExecutionManifest,
    AIActivityManifest,
    AIInvocationContext,
)
from typeflux.metadata import MetadataContributor
from typeflux.observability.backend import WorkflowObservation
from typeflux.observability.redaction import (
    Redactor,
    RegexPIIRedactor,
    _is_redacted_metadata,
    _mark_redacted_metadata,
)
from typeflux.observability.semantic import (
    _activity_execution_manifest_from_metadata,
    _messages_payload,
    _model_payload,
    _payload,
    _provider_controls_metadata,
    _registry_metadata,
    semantic_metadata,
    workflow_trace_name,
)

# LangSmith run-type vocabulary, mapped onto the Typeflux observation levels.
KIND_CHAIN = "chain"
KIND_LLM = "llm"
KIND_TOOL = "tool"


@dataclass(frozen=True)
class OtelAttributeConventions:
    """Span-attribute keys a backend recognizes when mapping spans to runs."""

    span_kind_key: str
    input_key: str
    output_key: str
    metadata_prefix: str
    trace_name_key: str
    tags_key: str
    usage_input_key: str
    usage_output_key: str
    usage_total_key: str


class OtelVendorProfile(Protocol):
    name: str
    conventions: OtelAttributeConventions

    def build_span_processor(self) -> Any:
        """Return an OTEL ``SpanProcessor`` exporting to the vendor's OTLP API."""
        ...


def _attribute_value(value: Any) -> Any:
    # OTEL attributes are flat scalars (or scalar sequences); anything richer is
    # JSON-encoded so it survives the round trip and the reader can restore it.
    if isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, default=str, sort_keys=True)


# High-value Typeflux scalars to surface as first-class flat metadata keys, each
# with the candidate dotted paths to look up in the observation metadata (paths
# differ by level: workflow root carries the execution manifest, activity and
# generation spans carry the per-activity manifest / temporal context). The first
# scalar found wins.
_PROMOTED_SCALARS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workflow_name", ("typeflux.execution_manifest.workflow_name",)),
    (
        "workflow_id",
        ("typeflux.execution_manifest.workflow_id", "typeflux.temporal.workflow_id"),
    ),
    (
        "task_queue",
        ("typeflux.execution_manifest.task_queue", "typeflux.temporal.task_queue"),
    ),
    (
        "temporal_run_id",
        ("typeflux.execution_manifest.temporal_run_id", "typeflux.temporal.run_id"),
    ),
    ("activity_name", ("typeflux.activity_name",)),
    (
        "provider_model",
        (
            "typeflux.activity_execution_manifest.provider_model",
            "typeflux.activity.provider_model",
        ),
    ),
    (
        "prompt_ref",
        (
            "typeflux.activity_execution_manifest.prompt_ref.name",
            "typeflux.activity.prompt_ref.name",
        ),
    ),
    (
        "resolved_prompt_version",
        (
            "typeflux.activity_execution_manifest.resolved_prompt_version",
            "typeflux.activity.resolved_prompt_version",
            "typeflux.provider.resolved_prompt_version",
            "typeflux.registry.langsmith.prompt_commit",
        ),
    ),
    (
        "activity_manifest_hash",
        (
            "typeflux.activity_execution_manifest.manifest_hash",
            "typeflux.join.activity_manifest_hash",
        ),
    ),
    ("manifest_hash", ("typeflux.execution_manifest.manifest_hash",)),
    ("workflow_contract_hash", ("typeflux.execution_manifest.workflow_contract_hash",)),
    ("git_sha", ("typeflux.execution_manifest.code_provenance.git_sha",)),
    ("git_ref", ("typeflux.execution_manifest.code_provenance.git_ref",)),
    (
        "spec_digest",
        # The YAML contributor writes spec_digest at typeflux.yaml.* (and mirrors
        # it into the manifest's contributions), NOT execution_manifest.yaml.* —
        # the old single path never resolved, so this field was silently absent
        # from traces (#330).
        (
            "typeflux.yaml.spec_digest",
            "typeflux.execution_manifest.contributions.yaml.spec_digest",
        ),
    ),
)


def _dig(metadata: Any, parts: tuple[str, ...]) -> Any:
    value: Any = metadata
    for part in parts:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _promoted_scalars(metadata: dict[str, Any]) -> dict[str, Any]:
    promoted: dict[str, Any] = {}
    for name, paths in _PROMOTED_SCALARS:
        for path in paths:
            value = _dig(metadata, tuple(path.split(".")))
            if isinstance(value, (bool, int, float, str)):
                promoted[name] = value
                break
    return promoted


class _OtelObservationHandle:
    def __init__(
        self, span: Any, conventions: OtelAttributeConventions, redactor: Redactor
    ) -> None:
        self._span = span
        self._conventions = conventions
        self._redactor = redactor

    def _redact(self, payload: Any) -> Any:
        if _is_redacted_metadata(payload):
            return payload
        return self._redactor.redact(payload)

    def _set_io(self, key: str, payload: Any) -> None:
        self._span.set_attribute(key, json.dumps(self._redact(payload), default=str))

    def _set_metadata(self, metadata: dict[str, Any]) -> None:
        redacted = self._redact(metadata)
        if not isinstance(redacted, dict):
            return
        for key, value in redacted.items():
            if value is None:
                continue
            self._span.set_attribute(
                f"{self._conventions.metadata_prefix}.{key}", _attribute_value(value)
            )
        # OTEL attributes can't be nested, so the full typeflux metadata rides as
        # a JSON string under one key. Promote the high-value scalars to
        # first-class flat metadata keys too, so they are searchable and render
        # as proper fields in the backend UI (not buried in the blob).
        for name, value in _promoted_scalars(redacted).items():
            self._span.set_attribute(f"{self._conventions.metadata_prefix}.{name}", value)

    def update_output(self, output_value: Any) -> None:
        self._set_io(self._conventions.output_key, _payload(output_value))

    def update_error(self, error: BaseException) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._span.record_exception(error)
        self._span.set_status(Status(StatusCode.ERROR, _safe_message(error)))

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        self._set_metadata(metadata)

    def update_usage(self, usage: Any) -> None:
        # Token counts are operational metadata, never content.
        details = usage.usage_details() if hasattr(usage, "usage_details") else {}
        details = details if isinstance(details, dict) else {}
        input_tokens = _first_int(details, ("input", "input_tokens", "prompt_tokens"))
        output_tokens = _first_int(details, ("output", "output_tokens", "completion_tokens"))
        total = _first_int(details, ("total", "total_tokens"))
        if total is None and input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens
        if input_tokens is not None:
            self._span.set_attribute(self._conventions.usage_input_key, input_tokens)
        if output_tokens is not None:
            self._span.set_attribute(self._conventions.usage_output_key, output_tokens)
        if total is not None:
            self._span.set_attribute(self._conventions.usage_total_key, total)


class _OtelActivityObservation(_OtelObservationHandle):
    def __init__(
        self,
        *,
        span: Any,
        tracer: Any,
        conventions: OtelAttributeConventions,
        redactor: Redactor,
        manifest: AIActivityManifest,
        invocation_context: AIInvocationContext | None,
        metadata_contributors: Sequence[MetadataContributor],
    ) -> None:
        super().__init__(span, conventions, redactor)
        self._tracer = tracer
        self._manifest = manifest
        self._invocation_context = invocation_context
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
        del output_schema, temperature, provider_params, observation_context
        generation_metadata = semantic_metadata(
            manifest=self._manifest,
            activity_execution_manifest=_activity_execution_manifest_from_metadata(metadata),
            invocation_context=self._invocation_context,
            level="generation",
            validation_attempt=validation_attempt,
            registry_metadata=_registry_metadata(metadata),
            provider_controls=_provider_controls_metadata(metadata),
            metadata_contributors=self._metadata_contributors,
        )
        with self._tracer.start_as_current_span(
            f"{self._manifest.activity_name}.generation"
        ) as span:
            span.set_attribute(self._conventions.span_kind_key, KIND_LLM)
            if model:
                span.set_attribute(f"{self._conventions.metadata_prefix}.model", model)
            handle = _OtelObservationHandle(span, self._conventions, self._redactor)
            handle._set_io(self._conventions.input_key, _messages_payload(messages))
            handle._set_metadata(generation_metadata)
            try:
                yield handle
            except BaseException as exc:
                handle.update_error(exc)
                raise

    @contextmanager
    def observe_hook(
        self,
        *,
        activity_input: BaseModel,
        llm_output: BaseModel,
        metadata: dict[str, Any],
    ) -> Iterator[ObservationHandle]:
        hook_metadata = semantic_metadata(
            manifest=self._manifest,
            activity_execution_manifest=_activity_execution_manifest_from_metadata(metadata),
            invocation_context=self._invocation_context,
            level="hook",
            registry_metadata=_registry_metadata(metadata),
            metadata_contributors=self._metadata_contributors,
        )
        with self._tracer.start_as_current_span(f"{self._manifest.activity_name}.hook") as span:
            span.set_attribute(self._conventions.span_kind_key, KIND_TOOL)
            handle = _OtelObservationHandle(span, self._conventions, self._redactor)
            handle._set_io(
                self._conventions.input_key,
                {
                    "activity_input": _model_payload(activity_input),
                    "llm_output": _model_payload(llm_output),
                },
            )
            handle._set_metadata(hook_metadata)
            try:
                yield handle
            except BaseException as exc:
                handle.update_error(exc)
                raise


class OtelAIActivityObserver:
    def __init__(
        self,
        *,
        tracer: Any,
        conventions: OtelAttributeConventions,
        redactor: Redactor,
        metadata_contributors: Sequence[MetadataContributor] = (),
    ) -> None:
        self._tracer = tracer
        self._conventions = conventions
        self.redactor = redactor
        self._metadata_contributors = tuple(metadata_contributors)

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
        with self._tracer.start_as_current_span(activity.name) as span:
            span.set_attribute(self._conventions.span_kind_key, KIND_CHAIN)
            observation = _OtelActivityObservation(
                span=span,
                tracer=self._tracer,
                conventions=self._conventions,
                redactor=self.redactor,
                manifest=manifest,
                invocation_context=invocation_context,
                metadata_contributors=self._metadata_contributors,
            )
            observation._set_io(self._conventions.input_key, _model_payload(input_value))
            observation._set_metadata(metadata)
            try:
                yield observation
            except BaseException as exc:
                observation.update_error(exc)
                raise

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redacted = self.redactor.redact(metadata)
        if isinstance(redacted, dict):
            return _mark_redacted_metadata(redacted)
        return metadata


class OtelTraceWriter:
    enabled = True

    def __init__(
        self,
        *,
        profile: OtelVendorProfile,
        redactor: Redactor | None = None,
        add_temporal_spans: bool = True,
    ) -> None:
        self._profile = profile
        self.redactor = redactor or RegexPIIRedactor.default()
        self._add_temporal_spans = add_temporal_spans
        self._tracer: Any | None = None
        self._tracer_provider: Any | None = None
        self._plugin: Any | None = None
        # Langfuse-only seam: the runtime reads ``writer.client`` to set root
        # trace IO through the Langfuse SDK. The OTEL writer carries everything
        # on spans, so there is no side-channel client.
        self.client = None

    def configure_temporal_plugin(self) -> Any | None:
        if self._plugin is not None:
            return self._plugin
        try:
            from opentelemetry import trace
            from temporalio.contrib.opentelemetry import (
                OpenTelemetryPlugin,
                create_tracer_provider,
            )
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
            raise RuntimeError(
                "temporalio and opentelemetry are required for OTEL tracing"
            ) from exc

        tracer_provider = create_tracer_provider()
        tracer_provider.add_span_processor(self._profile.build_span_processor())
        # Setting the global provider lets Temporal's interceptor emit its spans
        # to this exporter. But our own observation spans must come from *this*
        # provider, not the global one: a process that already set a global
        # provider (e.g. the Langfuse SDK) would otherwise capture our spans and
        # they'd never reach this vendor. Take the tracer from the local provider.
        try:
            trace.set_tracer_provider(tracer_provider)
        except Exception:
            pass
        self._tracer_provider = tracer_provider
        self._tracer = tracer_provider.get_tracer(f"typeflux.{self._profile.name}")
        self._plugin = OpenTelemetryPlugin(add_temporal_spans=self._add_temporal_spans)
        return self._plugin

    def observe_workflow_invocation(
        self,
        *,
        workflow_name: str,
        input_value: Any,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        subject_ids: Sequence[str] | None = None,
    ) -> AbstractContextManager[WorkflowObservation]:
        return self._workflow_span(
            name=workflow_trace_name(workflow_name),
            input_value=input_value,
            metadata=metadata,
            tags=tags,
            subject_ids=subject_ids,
        )

    def observe_lifecycle_operation(
        self,
        *,
        operation_type: str,
        operation_name: str,
        workflow_name: str | None,
        workflow_id: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> AbstractContextManager[WorkflowObservation]:
        del workflow_name, workflow_id, run_id
        label = "Query" if operation_type == "query" else "Signal"
        return self._workflow_span(
            name=f"TypefluxLifecycle{label}:{operation_name}",
            input_value=None,
            metadata=metadata,
            tags=tags,
        )

    @contextmanager
    def _workflow_span(
        self,
        *,
        name: str,
        input_value: Any,
        metadata: dict[str, Any] | None,
        tags: list[str] | None,
        subject_ids: Sequence[str] | None = None,
    ) -> Iterator[WorkflowObservation]:
        tracer = self._require_tracer()
        conventions = self._profile.conventions
        with tracer.start_as_current_span(name) as span:
            span.set_attribute(conventions.span_kind_key, KIND_CHAIN)
            span.set_attribute(conventions.trace_name_key, name)
            if tags:
                span.set_attribute(conventions.tags_key, ",".join(tags))
            # #715 slice 1: the primary subject is the span's end-user identity
            # (the OTel-native mirror of the Langfuse userId); the full list rides
            # in the `typeflux.subject:{id}` tags already folded into `tags`.
            if subject_ids:
                span.set_attribute("enduser.id", subject_ids[0])
            handle = _OtelObservationHandle(span, conventions, self.redactor)
            if input_value is not None:
                handle._set_io(conventions.input_key, _payload(input_value))
            if metadata is not None:
                handle._set_metadata(metadata)
            try:
                yield handle
            except BaseException as exc:
                handle.update_error(exc)
                raise

    def create_activity_observer(
        self,
        *,
        metadata_contributors: Sequence[MetadataContributor] = (),
    ) -> AIActivityObserver:
        return OtelAIActivityObserver(
            tracer=self._require_tracer(),
            conventions=self._profile.conventions,
            redactor=self.redactor,
            metadata_contributors=metadata_contributors,
        )

    def flush(self) -> None:
        provider = self._tracer_provider
        if provider is not None and hasattr(provider, "force_flush"):
            provider.force_flush()

    def shutdown(self) -> None:
        self.flush()
        provider = self._tracer_provider
        if provider is not None and hasattr(provider, "shutdown"):
            provider.shutdown()

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redacted = self.redactor.redact(metadata)
        if isinstance(redacted, dict):
            return _mark_redacted_metadata(redacted)
        return metadata

    def _require_tracer(self) -> Any:
        if self._tracer is None:
            self.configure_temporal_plugin()
        if self._tracer is None:  # pragma: no cover - defensive.
            raise RuntimeError("OTEL tracer is not configured")
        return self._tracer


def _first_int(details: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = details.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _safe_message(error: BaseException) -> str:
    text = str(error) or type(error).__name__
    return text[:512]


__all__ = [
    "KIND_CHAIN",
    "KIND_LLM",
    "KIND_TOOL",
    "OtelAIActivityObserver",
    "OtelAttributeConventions",
    "OtelTraceWriter",
    "OtelVendorProfile",
]
