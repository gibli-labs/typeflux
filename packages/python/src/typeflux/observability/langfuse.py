from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from typeflux.core.subjects import subject_trace_tags
from typeflux.execution.observer import AIActivityObserver
from typeflux.metadata import MetadataContributor
from typeflux.observability.backend import (
    TraceReader,
    TraceWriter,
    WorkflowObservation,
)
from typeflux.observability.inspect import (
    ObservationRecord,
    SubjectTraceDeletionConflict,
    SubjectTraceDeletionFailure,
    SubjectTraceDeletionReport,
    TraceListQuery,
    TracePage,
    TraceRecord,
    TraceRetrievalInfo,
    TraceSearchQuery,
    _apply_list_filters,
    _as_utc_aware,
    _trace_matches_search,
)
from typeflux.observability.redaction import (
    Redactor,
    RegexPIIRedactor,
    _mark_redacted_metadata,
)

_LANGFUSE_DEFAULT_LOOKBACK = timedelta(hours=24)
_LANGFUSE_LIST_FIELDS = "core,basic,metadata,model,prompt,trace_context,usage"
_LANGFUSE_DETAIL_FIELDS = f"{_LANGFUSE_LIST_FIELDS},io"
_LANGFUSE_MAX_OBSERVATION_LIMIT = 1000
_LANGFUSE_DEFAULT_DETAIL_MAX_PAGES = 100
#: Page size for the observations discovery scan, and the batch size for
#: ``trace.delete_multiple`` (#715 slice 2). Observations v2 uses opaque cursors;
#: discovery and unfiltered ownership walks preserve every cursor.
_LANGFUSE_TRACE_LIST_PAGE_SIZE = 100
_LANGFUSE_TRACE_DELETE_BATCH_SIZE = 100
#: A hard ceiling on the pagination walk so a backend that never advances
#: repeats a cursor cannot spin forever.
_LANGFUSE_TRACE_LIST_MAX_PAGES = 10_000


class LangfuseTraceWriter:
    enabled = True

    def __init__(
        self,
        *,
        client: Any | None = None,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        timeout: int | None = 60,
        add_temporal_spans: bool = True,
        export_all_temporal_spans: bool = True,
        redactor: Redactor | None = None,
    ) -> None:
        self.client = client
        self.host = host
        self.public_key = public_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.add_temporal_spans = add_temporal_spans
        self.export_all_temporal_spans = export_all_temporal_spans
        from typeflux.observability.semantic import (
            _ActivityTraceRollupSink,
            _registered_langfuse_redactor,
        )

        self.redactor = (
            redactor or _registered_langfuse_redactor(client) or RegexPIIRedactor.default()
        )
        self._rollup_sink = _ActivityTraceRollupSink()
        self._plugin: Any | None = None
        self._tracer_provider: Any | None = None

    def observe_workflow_invocation(
        self,
        *,
        workflow_name: str,
        input_value: Any,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        subject_ids: Sequence[str] | None = None,
    ) -> AbstractContextManager[WorkflowObservation]:
        from typeflux.observability.semantic import observe_workflow_invocation

        return observe_workflow_invocation(
            client=self._require_client(),
            workflow_name=workflow_name,
            input_value=input_value,
            metadata=metadata,
            tags=tags,
            subject_ids=subject_ids,
            rollup_sink=self._rollup_sink,
            redactor=self.redactor,
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
        from typeflux.observability.semantic import observe_lifecycle_operation

        return observe_lifecycle_operation(
            client=self._require_client(),
            operation_type=operation_type,
            operation_name=operation_name,
            metadata=metadata,
            tags=tags,
            redactor=self.redactor,
        )

    def create_activity_observer(
        self,
        *,
        metadata_contributors: Sequence[MetadataContributor] = (),
    ) -> AIActivityObserver:
        from typeflux.observability.semantic import LangfuseAIActivityObserver

        if self.client is not None:
            return LangfuseAIActivityObserver(
                client=self.client,
                redactor=self.redactor,
                rollup_sink=self._rollup_sink,
                metadata_contributors=metadata_contributors,
            )
        observer = LangfuseAIActivityObserver(
            host=self.host,
            public_key=self.public_key,
            secret_key=self.secret_key,
            timeout=self.timeout,
            redactor=self.redactor,
            rollup_sink=self._rollup_sink,
            metadata_contributors=metadata_contributors,
        )
        self.client = observer.client
        return observer

    def configure_temporal_plugin(self) -> Any | None:
        if self._plugin is not None:
            return self._plugin
        if self.client is not None:
            return None

        from typeflux.observability.semantic import configure_temporal_langfuse_tracing

        tracing = configure_temporal_langfuse_tracing(
            host=self.host,
            public_key=self.public_key,
            secret_key=self.secret_key,
            timeout=self.timeout,
            add_temporal_spans=self.add_temporal_spans,
            export_all_temporal_spans=self.export_all_temporal_spans,
            redactor=self.redactor,
        )
        self._plugin = tracing.plugin
        self.client = tracing.client
        self._tracer_provider = tracing.tracer_provider
        return self._plugin

    def flush(self) -> None:
        client = self.client
        if client is not None and hasattr(client, "flush"):
            client.flush()

    def shutdown(self) -> None:
        self.flush()
        tracer_provider = self._tracer_provider
        if tracer_provider is None:
            return
        force_flush = getattr(tracer_provider, "force_flush", None)
        if force_flush is not None:
            force_flush()
        shutdown = getattr(tracer_provider, "shutdown", None)
        if shutdown is not None:
            shutdown()

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redacted = self.redactor.redact(metadata)
        if isinstance(redacted, dict):
            return _mark_redacted_metadata(redacted)
        return metadata

    def _require_client(self) -> Any:
        if self.client is None:
            self.configure_temporal_plugin()
        if self.client is None:
            self.create_activity_observer()
        if self.client is None:
            raise RuntimeError("Langfuse client is not configured")
        return self.client


class LangfuseTraceReader:
    def __init__(
        self,
        *,
        client: Any | None = None,
        detail_page_size: int = _LANGFUSE_MAX_OBSERVATION_LIMIT,
        max_detail_pages: int = _LANGFUSE_DEFAULT_DETAIL_MAX_PAGES,
        **client_kwargs: Any,
    ) -> None:
        self.detail_page_size = _validate_detail_page_size(detail_page_size)
        self.max_detail_pages = _validate_detail_max_pages(max_detail_pages)
        self.client = client
        self._client_kwargs = client_kwargs

    def get_trace(
        self,
        trace_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_detail_pages: int | None = None,
    ) -> TraceRecord:
        query = TraceListQuery(limit=1, since=since, until=until)
        resolved_max_pages = (
            self.max_detail_pages
            if max_detail_pages is None
            else _validate_detail_max_pages(max_detail_pages)
        )
        rows: list[Any] = []
        cursor: str | None = None
        pages_read = 0
        seen_cursors: set[str] = set()
        complete = True
        while pages_read < resolved_max_pages:
            row_page = self._observation_rows_page(
                query,
                fields=_LANGFUSE_DETAIL_FIELDS,
                trace_id=trace_id,
                observation_limit=self.detail_page_size,
                cursor=cursor,
                default_lookback=False,
            )
            rows.extend(row_page.rows)
            pages_read += 1
            cursor = row_page.next_cursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise RuntimeError("Langfuse observation cursor repeated during trace retrieval")
            seen_cursors.add(cursor)
        else:
            if cursor is not None:
                complete = False

        retrieval_warnings = _retrieval_warnings(
            complete=complete,
            trace_id=trace_id,
            pages_read=pages_read,
            observations_read=len(rows),
            max_pages=resolved_max_pages,
            next_cursor=cursor,
        )
        retrieval = TraceRetrievalInfo(
            backend="langfuse",
            complete=complete,
            pages_read=pages_read,
            observations_read=len(rows),
            page_size=self.detail_page_size,
            max_pages=resolved_max_pages,
            next_cursor=cursor,
            warnings=retrieval_warnings,
        )
        for trace in _traces_from_observations(rows):
            if trace.trace_id == trace_id:
                return trace.model_copy(update={"retrieval": retrieval})
        raise KeyError(trace_id)

    def list_traces(self, query: TraceListQuery) -> TracePage:
        return self._list_traces_page(query)

    def _list_traces_page(
        self,
        query: TraceListQuery,
        *,
        backend_filter: Any | None = None,
        server_tags: Sequence[str] | None = None,
    ) -> TracePage:
        page = self._observations_page(
            query,
            fields=_LANGFUSE_LIST_FIELDS,
            backend_filter=backend_filter,
            server_tags=server_tags,
        )
        traces = page.traces
        if query.workflow_id:
            traces = self._hydrate_trace_page_candidates(traces, query)
        traces = _apply_list_filters(traces, query)
        return TracePage(traces=traces[: query.limit], next_cursor=page.next_cursor)

    def _hydrate_trace_page_candidates(
        self,
        traces: Sequence[TraceRecord],
        query: TraceListQuery,
    ) -> tuple[TraceRecord, ...]:
        hydrated: list[TraceRecord] = []
        seen: set[str] = set()
        for trace in traces:
            if trace.trace_id in seen:
                continue
            seen.add(trace.trace_id)
            try:
                hydrated.append(
                    self.get_trace(trace.trace_id, since=query.since, until=query.until)
                )
            except KeyError:
                hydrated.append(trace)
        return tuple(hydrated)

    def search_traces(self, query: TraceSearchQuery) -> TracePage:
        return self._search_candidates(query, backend_filter=query.backend_filter)

    def delete_traces_for_subject(
        self,
        subject_id: str,
        *,
        dry_run: bool = True,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> SubjectTraceDeletionReport:
        """Erase every trace attributed to ``subject_id`` on Langfuse (#715 slice 2).

        DUAL-CHANNEL query per the ratified decision: both the portable
        ``typeflux.subject:{id}`` tag and the native ``userId`` carry the subject
        (slice 1 always sets both), so this lists by BOTH and UNIONs the ids —
        neither channel alone is authoritative (a hand-written trace may set only
        one). Each channel's pages are walked FULLY (the list API is
        cursor-paginated); hitting the page cap yields an explicit warning, never a
        silently partial set. The subject-filter kwargs are FAIL-CLOSED: a list
        API that cannot accept a channel's filter raises rather than listing (and
        deleting from) an UNFILTERED page. Multi-subject traces are EXCLUDED and
        reported as ``conflicted`` — deleting them wholesale would destroy other
        subjects' audit trails. ``dry_run`` (default True) returns the set it
        WOULD delete without mutating; a real run batches ``delete_multiple`` and
        records per-batch failures. The report always carries the index-coverage
        caveat: pre-slice-1 traces carry neither carrier and are invisible here.
        """

        trace_api = _trace_api(self._require_client())
        observations_getter = _observations_getter(self._require_client())
        window_since = _as_utc_aware(since) if since is not None else None
        window_until = _as_utc_aware(until) if until is not None else None
        (own_tag,) = subject_trace_tags([subject_id])
        tag_listing = _list_subject_trace_rows(
            observations_getter,
            tags=[own_tag],
            user_id=None,
            since=window_since,
            until=window_until,
        )
        user_listing = _list_subject_trace_rows(
            observations_getter,
            tags=None,
            user_id=subject_id,
            since=window_since,
            until=window_until,
        )
        warnings = [
            *_channel_truncation_warnings(subject_id, "tag", tag_listing),
            *_channel_truncation_warnings(subject_id, "user_id", user_listing),
        ]

        # Merge per-id tag knowledge across channels by UNIONING every READABLE
        # tags list (Bugbot HIGH): keeping only the FIRST readable list would let
        # a tag-channel row with an empty/partial list MASK the user-id channel's
        # richer list carrying other subjects' markers — reading multi-subject as
        # single-subject and re-opening the collateral-deletion hole. A channel
        # with unreadable tags contributes nothing but does NOT poison a readable
        # union; only when NO channel yielded readable tags is the row
        # conflicted-unknown.
        tags_by_id: dict[str, list[str] | None] = {}
        for trace_id, row_tags in (*tag_listing.rows, *user_listing.rows):
            if row_tags is None:
                tags_by_id.setdefault(trace_id, None)
                continue
            known = tags_by_id.get(trace_id)
            if known is None:
                tags_by_id[trace_id] = list(row_tags)
            else:
                known.extend(tag for tag in row_tags if tag not in known)

        union: list[str] = []
        seen: set[str] = set()
        for trace_id in (*tag_listing.trace_ids, *user_listing.trace_ids):
            if trace_id not in seen:
                seen.add(trace_id)
                union.append(trace_id)

        deletable: list[str] = []
        conflicted: list[SubjectTraceDeletionConflict] = []
        for trace_id in union:
            # A filtered observation cannot establish ownership of the whole trace.
            # Hydrate every candidate without subject/time filters before deleting.
            try:
                detail = self.get_trace(trace_id)
            except KeyError:
                # Retention or a concurrent erasure may remove a discovered trace.
                conflicted.append(SubjectTraceDeletionConflict(trace_id=trace_id))
                warnings.append(
                    f"Langfuse ownership unavailable for trace {trace_id}; not deleted."
                )
                continue
            if detail.retrieval is None or not detail.retrieval.complete:
                conflicted.append(SubjectTraceDeletionConflict(trace_id=trace_id))
                warnings.append(
                    f"Langfuse ownership scan incomplete for trace {trace_id}; not deleted."
                )
                continue
            detail_tags = [_row_tags(row) for row in detail.raw]
            if not detail_tags or any(tags is None for tags in detail_tags):
                conflicted.append(SubjectTraceDeletionConflict(trace_id=trace_id))
                continue
            row_tags = list(tags_by_id.get(trace_id) or [])
            for tags in detail_tags:
                row_tags.extend(tag for tag in (tags or []) if tag not in row_tags)
            other_subjects = sum(
                1 for tag in row_tags if tag.startswith(_SUBJECT_TAG_PREFIX) and tag != own_tag
            )
            if other_subjects:
                conflicted.append(
                    SubjectTraceDeletionConflict(
                        trace_id=trace_id, other_subject_count=other_subjects
                    )
                )
            else:
                deletable.append(trace_id)

        if dry_run:
            return SubjectTraceDeletionReport(
                subject_id=subject_id,
                dry_run=True,
                matched_by_tag=tuple(tag_listing.trace_ids),
                matched_by_user_id=tuple(user_listing.trace_ids),
                trace_ids=tuple(deletable),
                conflicted=tuple(conflicted),
                warnings=tuple(warnings),
            )

        deleted = 0
        failures: list[SubjectTraceDeletionFailure] = []
        for batch in _chunked(deletable, _LANGFUSE_TRACE_DELETE_BATCH_SIZE):
            try:
                trace_api.delete_multiple(trace_ids=list(batch))
            except Exception as exc:  # noqa: BLE001 - report, do not swallow, any batch failure.
                reason = f"{type(exc).__name__}: {exc}"
                failures.extend(
                    SubjectTraceDeletionFailure(trace_id=trace_id, reason=reason)
                    for trace_id in batch
                )
            else:
                deleted += len(batch)
        return SubjectTraceDeletionReport(
            subject_id=subject_id,
            dry_run=False,
            matched_by_tag=tuple(tag_listing.trace_ids),
            matched_by_user_id=tuple(user_listing.trace_ids),
            trace_ids=tuple(deletable),
            deleted_count=deleted,
            failures=tuple(failures),
            conflicted=tuple(conflicted),
            warnings=tuple(warnings),
        )

    def _search_candidates(
        self,
        query: TraceSearchQuery,
        *,
        backend_filter: Any | None,
    ) -> TracePage:
        matches: list[TraceRecord] = []
        cursor = query.cursor
        next_cursor: str | None = None
        max_pages = max(query.scan_pages, 1)
        server_tags = _server_tags_for_query(query)
        pages_scanned = 0
        for _ in range(max_pages):
            page = self._list_traces_page(
                TraceListQuery(
                    limit=query.limit,
                    cursor=cursor,
                    since=query.since,
                    until=query.until,
                    workflow_name=query.workflow_name,
                    workflow_id=query.workflow_id,
                    status=query.status,
                ),
                backend_filter=backend_filter,
                server_tags=server_tags,
            )
            pages_scanned += 1
            for trace in page.traces:
                if _trace_matches_search(trace, query):
                    matches.append(trace)
                    if len(matches) >= query.limit:
                        return TracePage(traces=tuple(matches), next_cursor=page.next_cursor)
            next_cursor = page.next_cursor
            if not next_cursor:
                break
            cursor = next_cursor
        complete = next_cursor is None
        return TracePage(
            traces=tuple(matches),
            next_cursor=next_cursor,
            complete=complete,
            warnings=_scan_warnings(
                complete=complete,
                pages_scanned=pages_scanned,
                matches=len(matches),
                scan_pages=max_pages,
            ),
        )

    def _observations_page(
        self,
        query: TraceListQuery,
        *,
        fields: str,
        backend_filter: Any | None = None,
        trace_id: str | None = None,
        observation_limit: int | None = None,
        server_tags: Sequence[str] | None = None,
    ) -> TracePage:
        page = self._observation_rows_page(
            query,
            fields=fields,
            backend_filter=backend_filter,
            trace_id=trace_id,
            observation_limit=observation_limit,
            server_tags=server_tags,
        )
        return TracePage(
            traces=_traces_from_observations(page.rows),
            next_cursor=page.next_cursor,
        )

    def _observation_rows_page(
        self,
        query: TraceListQuery,
        *,
        fields: str,
        backend_filter: Any | None = None,
        trace_id: str | None = None,
        observation_limit: int | None = None,
        cursor: str | None = None,
        default_lookback: bool = True,
        server_tags: Sequence[str] | None = None,
    ) -> _ObservationRowsPage:
        getter = _observations_getter(self._require_client())
        kwargs: dict[str, Any] = {
            "fields": fields,
            "expand_metadata": "typeflux,typeflux_moderation,typeflux.execution_manifest,typeflux.activity_execution_manifest",
            "limit": observation_limit or _observation_limit(query.limit),
        }
        if default_lookback:
            since, until = _bounded_window(query.since, query.until)
            kwargs["from_start_time"] = since
            kwargs["to_start_time"] = until
        else:
            # Exact trace-id lookups must not lose known traces to the default
            # lookback window; only caller-supplied bounds apply.
            if query.since is not None:
                kwargs["from_start_time"] = _as_utc_aware(query.since)
            if query.until is not None:
                kwargs["to_start_time"] = _as_utc_aware(query.until)
        resolved_cursor = cursor or query.cursor
        if resolved_cursor:
            kwargs["cursor"] = resolved_cursor
        if trace_id is not None:
            kwargs["trace_id"] = trace_id
        if backend_filter is not None or server_tags:
            filters = (
                json.loads(_serialize_backend_filter(backend_filter))
                if backend_filter is not None
                else []
            )
            if not isinstance(filters, list):
                raise ValueError("Langfuse backend_filter must be an array of conditions")
            if server_tags:
                filters.append(
                    {
                        "type": "arrayOptions",
                        "column": "traceTags",
                        "operator": "all of",
                        "value": list(server_tags),
                    }
                )
            for key, operator in (("from_start_time", ">="), ("to_start_time", "<")):
                if key in kwargs:
                    filters.append(
                        {
                            "type": "datetime",
                            "column": "startTime",
                            "operator": operator,
                            "value": kwargs.pop(key).isoformat(),
                        }
                    )
            if "trace_id" in kwargs:
                filters.append(
                    {
                        "type": "string",
                        "column": "traceId",
                        "operator": "=",
                        "value": kwargs.pop("trace_id"),
                    }
                )
            kwargs["filter"] = json.dumps(filters)
        raw_page = getter(**kwargs)
        return _ObservationRowsPage(
            rows=_page_items(raw_page),
            next_cursor=_page_cursor(raw_page),
        )

    def _require_client(self) -> Any:
        if self.client is None:
            self.client = _build_client(**self._client_kwargs)
        return self.client


@dataclass(frozen=True)
class _ObservationRowsPage:
    rows: list[Any]
    next_cursor: str | None


def _stripped_env(name: str) -> str | None:
    """An env value stripped of whitespace; unset/whitespace-only ⇒ ``None``.

    The one resolution both :meth:`LangfuseObservabilityBackend.is_configured` and
    :meth:`LangfuseObservabilityBackend.from_env` read, so what the check accepts is
    byte-for-byte what construction uses (#715 Bugbot: a whitespace-only key must
    read as unconfigured, never build a client that fails at runtime)."""

    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


@dataclass(frozen=True)
class LangfuseObservabilityBackend:
    writer: TraceWriter
    reader: TraceReader

    @classmethod
    def is_configured(cls, *, public_key: str | None = None, secret_key: str | None = None) -> bool:
        """Whether the environment carries the credential pair :meth:`from_env` needs.

        THE one configuredness check (colocated with the resolution it mirrors, #715
        slice-5 fix round): callers deciding whether to construct a backend at all —
        the observability CLI's default-backend pick, the erase CLI's
        skipped-vs-driven Langfuse surface — ask this instead of hand-rolling env-var
        reads that can drift from what ``from_env`` actually requires. Values are
        STRIPPED before the check (a whitespace-only key is not a credential — it
        would build a client that fails at runtime instead of the surface being
        honestly skipped), and :meth:`from_env` resolves through the same helper so
        the value checked is exactly the value used. The host is deliberately NOT
        part of the check (cloud default applies when unset)."""

        # Explicit values (#793: spec-resolved credentials) participate PER FIELD, exactly
        # like from_env's fallback — a mixed spec+env pair is configured.
        return bool(public_key or _stripped_env("LANGFUSE_PUBLIC_KEY")) and bool(
            secret_key or _stripped_env("LANGFUSE_SECRET_KEY")
        )

    @classmethod
    def from_env(
        cls,
        *,
        redactor: Redactor | None = None,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        timeout: int | None = 60,
    ) -> LangfuseObservabilityBackend:
        # The SAME stripped resolution is_configured checks: check-vs-use must never
        # diverge (a padded key would otherwise pass the check and then fail auth).
        # Explicit values (#793: spec-declared credentials, resolved by the caller) win;
        # unset falls back to the standard env vars, so declaring them is additive.
        resolved_host = host or _stripped_env("LANGFUSE_HOST") or _stripped_env("LANGFUSE_BASE_URL")
        public_key = public_key or _stripped_env("LANGFUSE_PUBLIC_KEY")
        secret_key = secret_key or _stripped_env("LANGFUSE_SECRET_KEY")
        writer = LangfuseTraceWriter(
            host=resolved_host,
            public_key=public_key,
            secret_key=secret_key,
            timeout=timeout,
            redactor=redactor,
        )
        return cls(
            writer=writer,
            reader=LangfuseTraceReader(
                host=resolved_host,
                public_key=public_key,
                secret_key=secret_key,
                timeout=timeout,
            ),
        )

    @classmethod
    def from_client(
        cls,
        client: Any,
        *,
        redactor: Redactor | None = None,
    ) -> LangfuseObservabilityBackend:
        writer = LangfuseTraceWriter(client=client, redactor=redactor)
        return cls(writer=writer, reader=LangfuseTraceReader(client=client))


def _build_client(**kwargs: Any) -> Any:
    try:
        from langfuse import Langfuse
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError("langfuse is required for Langfuse trace reading") from exc

    clean_kwargs = {key: value for key, value in kwargs.items() if value is not None}
    return Langfuse(**clean_kwargs)


def _validate_detail_page_size(value: int) -> int:
    if value < 1:
        raise ValueError("detail_page_size must be >= 1")
    if value > _LANGFUSE_MAX_OBSERVATION_LIMIT:
        raise ValueError(
            f"detail_page_size must be <= {_LANGFUSE_MAX_OBSERVATION_LIMIT} for Langfuse"
        )
    return value


def _validate_detail_max_pages(value: int) -> int:
    if value < 1:
        raise ValueError("max_detail_pages must be >= 1")
    return value


def _retrieval_warnings(
    *,
    complete: bool,
    trace_id: str,
    pages_read: int,
    observations_read: int,
    max_pages: int,
    next_cursor: str | None,
) -> tuple[str, ...]:
    if complete:
        return ()
    cursor_status = "next cursor remains" if next_cursor else "cursor status unknown"
    return (
        "Langfuse trace retrieval incomplete for "
        f"{trace_id!r}: stopped after {pages_read} pages and "
        f"{observations_read} observations because max_detail_pages={max_pages}; "
        f"{cursor_status}. Increase --max-detail-pages or narrow --since/--until.",
    )


def _scan_warnings(
    *,
    complete: bool,
    pages_scanned: int,
    matches: int,
    scan_pages: int,
) -> tuple[str, ...]:
    if complete:
        return ()
    return (
        "Langfuse trace search incomplete: stopped after scanning "
        f"{pages_scanned} pages with {matches} matches because "
        f"scan_pages={scan_pages}; more candidate pages remain. "
        "Increase --scan-pages, narrow --since/--until, or rerun with --cursor.",
    )


def _observations_getter(client: Any) -> Any:
    api = getattr(client, "api", client)
    observations = getattr(api, "observations", None)
    getter = getattr(observations, "get_many", None)
    if getter is None:
        raise RuntimeError("Langfuse Observations API v2 is required for trace reading")
    return getter


def _traces_from_observations(rows: list[Any]) -> tuple[TraceRecord, ...]:
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        data = _dump(row)
        trace_id = _optional_str(_first_present(data, ("traceId", "trace_id", "trace_id")))
        if trace_id:
            grouped.setdefault(trace_id, []).append(row)
    return tuple(
        _trace_from_observation_rows(trace_id, items) for trace_id, items in grouped.items()
    )


def _trace_from_observation_rows(trace_id: str, rows: list[Any]) -> TraceRecord:
    observations = tuple(_normalize_observation(row) for row in rows)
    root = _root_observation(observations)
    first = root or (observations[0] if observations else None)
    first_row = _dump(rows[0]) if rows else {}
    metadata = dict(first.metadata) if first is not None else {}
    tags = _first_trace_context_value(rows, "tags")
    if tags is not None and "tags" not in metadata:
        metadata["tags"] = tags
    trace_name = (
        _optional_str(_first_trace_context_value(rows, "traceName"))
        or _optional_str(_first_trace_context_value(rows, "trace_name"))
        or (first.name if first is not None and _is_workflow_observation(first) else None)
    )
    return TraceRecord(
        trace_id=trace_id,
        name=trace_name,
        timestamp=(None if first is None else first.start_time)
        or _optional_datetime(first_row.get("startTime")),
        input=None if first is None else first.input,
        output=None if first is None else first.output,
        metadata=metadata,
        observations=observations,
        raw=rows,
    )


def _root_observation(observations: tuple[ObservationRecord, ...]) -> ObservationRecord | None:
    for observation in observations:
        if _is_workflow_observation(observation):
            return observation
    for observation in observations:
        if observation.name and observation.name.startswith("TypefluxWorkflow:"):
            return observation
    return None


def _is_workflow_observation(observation: ObservationRecord) -> bool:
    typeflux = observation.metadata.get("typeflux")
    return isinstance(typeflux, dict) and typeflux.get("level") == "workflow"


def _first_trace_context_value(rows: list[Any], key: str) -> Any:
    for row in rows:
        data = _dump(row)
        value = data.get(key)
        if value is not None:
            return value
    return None


def _normalize_trace(payload: Any) -> TraceRecord:
    data = _dump(payload)
    observations = tuple(_normalize_observation(item) for item in _observations(data, payload))
    return TraceRecord(
        trace_id=str(_first_present(data, ("id", "trace_id", "traceId")) or ""),
        name=_optional_str(_first_present(data, ("name",))),
        timestamp=_optional_datetime(
            _first_present(data, ("timestamp", "created_at", "createdAt"))
        ),
        input=_first_present(data, ("input",)),
        output=_first_present(data, ("output",)),
        metadata=_dict(_first_present(data, ("metadata",))),
        observations=observations,
        raw=payload,
    )


def _normalize_observation(payload: Any) -> ObservationRecord:
    data = _dump(payload)
    return ObservationRecord(
        observation_id=_optional_str(
            _first_present(data, ("id", "observation_id", "observationId"))
        ),
        name=_optional_str(_first_present(data, ("name",))),
        type=_optional_str(_first_present(data, ("type", "as_type", "asType"))),
        level=_optional_str(_first_present(data, ("level",))),
        input=_parse_jsonish(_first_present(data, ("input",))),
        output=_parse_jsonish(_first_present(data, ("output",))),
        metadata=_dict(_first_present(data, ("metadata",))),
        start_time=_optional_datetime(_first_present(data, ("start_time", "startTime"))),
        end_time=_optional_datetime(_first_present(data, ("end_time", "endTime"))),
        raw=payload,
    )


def _observations(data: dict[str, Any], payload: Any) -> list[Any]:
    for key in ("observations", "spans", "generations"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    value = getattr(payload, "observations", None)
    if isinstance(value, list):
        return value
    return []


def _page_items(payload: Any) -> list[Any]:
    data = _dump(payload)
    for key in ("data", "items", "traces"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    if isinstance(payload, list):
        return payload
    return []


def _page_cursor(payload: Any) -> str | None:
    meta = _dump(payload).get("meta")
    if not isinstance(meta, dict):
        raise ValueError("Invalid Langfuse Observations v2 page; completeness is unknown")
    cursor = meta.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("Invalid Langfuse observation cursor")
    return cursor or None


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _unsupported_keyword_from_type_error(exc: TypeError) -> str | None:
    message = str(exc)
    if "keyword" not in message:
        return None
    if not any(marker in message for marker in ("unexpected", "unsupported", "unknown")):
        return None
    match = re.search(
        r"(?:unexpected|unsupported|unknown) keyword(?: argument)? ['\"]([^'\"]+)['\"]",
        message,
    )
    return None if match is None else match.group(1)


def _first_present(value: Any, keys: tuple[str, ...]) -> Any:
    data = _dump(value)
    for key in keys:
        if key in data:
            return data[key]
    return None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc_aware(value)
    if isinstance(value, str):
        try:
            return _as_utc_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bounded_window(
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime, datetime]:
    end = _as_utc_aware(until if until is not None else _utc_now())
    if end is None:
        end = _utc_now()
    start = _as_utc_aware(since) if since is not None else end - _LANGFUSE_DEFAULT_LOOKBACK
    if start is None:
        start = end - _LANGFUSE_DEFAULT_LOOKBACK
    return start, end


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _observation_limit(trace_limit: int) -> int:
    return min(max(trace_limit * 10, 50), _LANGFUSE_MAX_OBSERVATION_LIMIT)


def _serialize_backend_filter(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    raise TypeError("backend_filter must be a JSON string, list, or dict")


def _trace_api(client: Any) -> Any:
    """The Langfuse Trace API surface (``client.api.trace``) — list + delete."""

    api = getattr(client, "api", client)
    trace = getattr(api, "trace", None)
    if trace is None:
        raise RuntimeError("Langfuse Trace API is required for subject-trace deletion")
    return trace


#: The tag prefix that marks a subject carrier on a trace (#715 slice 1).
_SUBJECT_TAG_PREFIX = "typeflux.subject:"


@dataclass(frozen=True)
class _SubjectChannelListing:
    """One query channel's full page walk: (trace_id, tags-or-None) rows plus
    whether the walk COMPLETED or was truncated by the page cap."""

    rows: tuple[tuple[str, list[str] | None], ...]
    complete: bool
    pages_scanned: int

    @property
    def trace_ids(self) -> list[str]:
        return list(dict.fromkeys(trace_id for trace_id, _ in self.rows))


def _subject_filter_unsupported_error(keys: Sequence[str]) -> RuntimeError:
    names = ", ".join(sorted(keys))
    return RuntimeError(
        f"the Langfuse observations API does not accept the subject-filter "
        f"keyword(s) {names}; refusing to list (and delete from) an UNFILTERED "
        "trace page — a subject-scoped erasure must never widen to every trace "
        "in the window (fail-closed). Upgrade the langfuse client or use a "
        "backend whose observations API supports the required filters."
    )


def _call_subject_query(func: Any, kwargs: dict[str, Any]) -> Any:
    """Call the current SDK contract once; never retry without identity or cursor filters."""
    try:
        return func(**kwargs)
    except TypeError as exc:
        unsupported = _unsupported_keyword_from_type_error(exc)
        if unsupported in kwargs:
            raise _subject_filter_unsupported_error([unsupported]) from exc
        raise


def _list_subject_trace_rows(
    getter: Any,
    *,
    tags: Sequence[str] | None,
    user_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> _SubjectChannelListing:
    """List subject candidates through bounded Observations v2 cursor pages.

    JSON filters override simple query filters, so identity AND time bounds
    belong to the same array. No filter or cursor may be compatibility-stripped.
    """
    filters: list[dict[str, Any]] = []
    if tags:
        filters.append(
            {
                "type": "arrayOptions",
                "column": "traceTags",
                "operator": "all of",
                "value": list(tags),
            }
        )
    elif user_id is not None:
        filters.append({"type": "string", "column": "userId", "operator": "=", "value": user_id})
    else:
        raise ValueError("subject erasure requires a tag or user filter")
    if since is not None:
        filters.append(
            {
                "type": "datetime",
                "column": "startTime",
                "operator": ">=",
                "value": since.isoformat(),
            }
        )
    if until is not None:
        filters.append(
            {"type": "datetime", "column": "startTime", "operator": "<", "value": until.isoformat()}
        )
    rows: list[tuple[str, list[str] | None]] = []
    cursor: str | None = None
    cursors: set[str] = set()
    complete = False
    pages_scanned = 0
    for _ in range(_LANGFUSE_TRACE_LIST_MAX_PAGES):
        kwargs: dict[str, Any] = {
            "limit": _LANGFUSE_TRACE_LIST_PAGE_SIZE,
            "fields": "core,basic,trace_context",
            "filter": json.dumps(filters),
        }
        if cursor is not None:
            kwargs["cursor"] = cursor
        response = _call_subject_query(getter, kwargs)
        pages_scanned += 1
        for row in _page_items(response):
            trace_id = _optional_str(_first_present(row, ("trace_id", "traceId")))
            if trace_id:
                rows.append((trace_id, _row_tags(row)))
        cursor = _page_cursor(response)
        if cursor is None:
            complete = True
            break
        if cursor in cursors:
            raise RuntimeError(
                "Langfuse observation cursor repeated; refusing incomplete subject erasure"
            )
        cursors.add(cursor)
    return _SubjectChannelListing(rows=tuple(rows), complete=complete, pages_scanned=pages_scanned)


def _row_tags(row: Any) -> list[str] | None:
    """A row's tags list, or ``None`` when the field is absent/unreadable."""

    value = _first_present(row, ("tags",))
    if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
        return None
    tags = list(value)
    user_id = _first_present(row, ("user_id", "userId"))
    if isinstance(user_id, str) and user_id:
        native_tag = f"typeflux.subject:{user_id}"
        if native_tag not in tags:
            tags.append(native_tag)
    return tags


def _channel_truncation_warnings(
    subject_id: str,
    channel: str,
    listing: _SubjectChannelListing,
) -> tuple[str, ...]:
    if listing.complete:
        return ()
    return (
        f"Langfuse subject-trace listing incomplete for {subject_id!r} on the "
        f"{channel} channel: stopped after scanning {listing.pages_scanned} pages "
        f"because the page cap ({_LANGFUSE_TRACE_LIST_MAX_PAGES}) was reached; "
        "more candidate pages remain. The deletion covers only the traces found "
        "— narrow --since/--until and rerun to reach the rest.",
    )


def _chunked(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _server_tags_for_query(query: TraceSearchQuery) -> list[str]:
    tags = []
    if query.workflow_name:
        tags.append(f"typeflux.workflow:{query.workflow_name}")
    if query.activity_name:
        tags.append(f"typeflux.activity:{query.activity_name}")
    if query.prompt_ref:
        tags.append(f"typeflux.prompt:{query.prompt_ref}")
    if query.provider_model:
        tags.append(f"typeflux.model:{query.provider_model}")
    if query.environment:
        tags.append(f"typeflux.env:{query.environment}")
    return tags


__all__ = [
    "LangfuseObservabilityBackend",
    "LangfuseTraceReader",
    "LangfuseTraceWriter",
]
