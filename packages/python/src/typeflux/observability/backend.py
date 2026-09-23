from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from typeflux.execution.observer import AIActivityObserver, NoOpObserver
from typeflux.metadata import MetadataContributor
from typeflux.observability.inspect import (
    SubjectTraceDeletionReport,
    TraceListQuery,
    TracePage,
    TraceReader,
    TraceRecord,
    TraceSearchQuery,
    _apply_list_filters,
    _trace_matches_search,
)

#: Raised when a subject-trace deletion is requested against a backend that has
#: no deletable trace store (NoOp / in-memory fixtures). A pointed error — never
#: a silent no-op, which would falsely report a subject erased (#715 slice 2).
SUBJECT_TRACE_DELETION_UNSUPPORTED = (
    "subject-trace deletion is not supported by this observability backend "
    "(no deletable trace store); configure the Langfuse backend to erase a "
    "subject's traces (see docs/privacy.md 'Retention & Erasure')."
)


class WorkflowObservation(Protocol):
    def update_output(self, output_value: Any) -> None: ...

    def update_error(self, error: BaseException) -> None: ...

    def update_metadata(self, metadata: dict[str, Any]) -> None: ...


class TraceWriter(Protocol):
    enabled: bool

    def observe_workflow_invocation(
        self,
        *,
        workflow_name: str,
        input_value: Any,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        subject_ids: Sequence[str] | None = None,
    ) -> AbstractContextManager[WorkflowObservation]: ...

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
    ) -> AbstractContextManager[WorkflowObservation]: ...

    def create_activity_observer(
        self,
        *,
        metadata_contributors: Sequence[MetadataContributor] = (),
    ) -> AIActivityObserver: ...

    def configure_temporal_plugin(self) -> Any | None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]: ...


class ObservabilityBackend(Protocol):
    @property
    def writer(self) -> TraceWriter: ...

    @property
    def reader(self) -> TraceReader: ...


class NoOpWorkflowObservation:
    def update_output(self, output_value: Any) -> None:
        return None

    def update_error(self, error: BaseException) -> None:
        return None

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        return None


class _NoOpWorkflowContext:
    def __enter__(self) -> NoOpWorkflowObservation:
        return NoOpWorkflowObservation()

    def __exit__(self, exc_type, exc, traceback) -> Literal[False]:
        return False


class NoOpTraceWriter:
    enabled = False

    def observe_workflow_invocation(
        self,
        *,
        workflow_name: str,
        input_value: Any,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        subject_ids: Sequence[str] | None = None,
    ) -> AbstractContextManager[WorkflowObservation]:
        del workflow_name, input_value, metadata, tags, subject_ids
        return _NoOpWorkflowContext()

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
        del operation_type, operation_name, workflow_name, workflow_id, run_id, metadata, tags
        return _NoOpWorkflowContext()

    def create_activity_observer(
        self,
        *,
        metadata_contributors: Sequence[MetadataContributor] = (),
    ) -> AIActivityObserver:
        del metadata_contributors
        return NoOpObserver()

    def configure_temporal_plugin(self) -> Any | None:
        return None

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


class NoOpTraceReader:
    def get_trace(
        self,
        trace_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_detail_pages: int | None = None,
    ) -> TraceRecord:
        del since, until, max_detail_pages
        raise RuntimeError("trace reading is not configured")

    def list_traces(self, query: TraceListQuery) -> TracePage:
        return TracePage(traces=())

    def search_traces(self, query: TraceSearchQuery) -> TracePage:
        if query.backend_filter is not None:
            raise RuntimeError("backend_filter is not supported by this trace reader")
        return TracePage(traces=())

    def delete_traces_for_subject(
        self,
        subject_id: str,
        *,
        dry_run: bool = True,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> SubjectTraceDeletionReport:
        del subject_id, dry_run, since, until
        raise RuntimeError(SUBJECT_TRACE_DELETION_UNSUPPORTED)


@dataclass(frozen=True)
class NoOpObservabilityBackend:
    writer: TraceWriter = field(default_factory=NoOpTraceWriter)
    reader: TraceReader = field(default_factory=NoOpTraceReader)


class InMemoryTraceStore:
    def __init__(self, traces: Sequence[TraceRecord] = ()) -> None:
        self._traces = tuple(traces)

    def get_trace(
        self,
        trace_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_detail_pages: int | None = None,
    ) -> TraceRecord:
        del since, until, max_detail_pages
        for trace in self._traces:
            if trace.trace_id == trace_id:
                return trace
        raise KeyError(trace_id)

    def list_traces(self, query: TraceListQuery) -> TracePage:
        traces = _apply_list_filters(self._traces, query)
        return TracePage(traces=traces[: query.limit])

    def search_traces(self, query: TraceSearchQuery) -> TracePage:
        if query.backend_filter is not None:
            raise RuntimeError("backend_filter is not supported by this trace reader")
        traces = [trace for trace in self._traces if _trace_matches_search(trace, query)]
        return TracePage(traces=tuple(traces[: query.limit]))

    def delete_traces_for_subject(
        self,
        subject_id: str,
        *,
        dry_run: bool = True,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> SubjectTraceDeletionReport:
        # An immutable read-only fixture store — deletion is a provider-side
        # mutation it cannot honestly perform (#715 slice 2).
        del subject_id, dry_run, since, until
        raise RuntimeError(SUBJECT_TRACE_DELETION_UNSUPPORTED)


__all__ = [
    "SUBJECT_TRACE_DELETION_UNSUPPORTED",
    "InMemoryTraceStore",
    "NoOpObservabilityBackend",
    "NoOpTraceReader",
    "NoOpTraceWriter",
    "ObservabilityBackend",
    "TraceReader",
    "TraceWriter",
    "WorkflowObservation",
]
