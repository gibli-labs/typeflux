"""LangSmith observability backend (#59 follow-up).

A first-class ``observability.type: langsmith`` backend. The writer is the
generic :class:`OtelTraceWriter` configured with a LangSmith
:class:`OtelVendorProfile`; spans are exported to LangSmith's OTLP endpoint and
correlate with the native Temporal spans into one trace (per LangSmith's
"trace with Temporal" guide). The reader reconstructs Typeflux ``TraceRecord``s
from LangSmith runs (``Client.list_runs`` / ``read_run``) so the ``trace``
CLI — list / inspect / search / diff / export — works against LangSmith too.

Switching a project from Langfuse to LangSmith is a one-line YAML change
(``observability.type``); nothing else in the spec moves.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any

from typeflux.observability.backend import TraceReader, TraceWriter
from typeflux.observability.inspect import (
    ObservationRecord,
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
from typeflux.observability.otel import (
    OtelAttributeConventions,
    OtelTraceWriter,
)
from typeflux.observability.redaction import Redactor, RegexPIIRedactor

logger = logging.getLogger(__name__)

#: LangSmith is NOT a subject-indexed erasure surface in #715: slice 1 fans the
#: subject identity to Langfuse ONLY (native ``userId`` + the
#: ``typeflux.subject:{id}`` tag), never to LangSmith runs. Without a subject
#: carrier there is nothing to query, so subject-scoped deletion here is a hard
#: not-supported error — never a silent no-op that would falsely report erasure.
_LANGSMITH_SUBJECT_DELETION_UNSUPPORTED = (
    "subject-trace deletion is not supported by the LangSmith backend: #715 "
    "indexes subjects on Langfuse only (native userId + the typeflux.subject:<id> "
    "tag), so LangSmith runs carry no subject to target. Use the Langfuse backend "
    "to erase a subject's traces, or your LangSmith run-retention policy (see "
    "docs/privacy.md 'Retention & Erasure')."
)

_SENTINEL = object()

_DEFAULT_LOOKBACK = timedelta(hours=24)
# LangSmith's /runs/query rejects limits above 100.
_MAX_RUN_LIMIT = 100
_DEFAULT_SCAN_LIMIT = _MAX_RUN_LIMIT
# Per-trace span budget for one batched child-hydration call. The total cap is
# this × the number of traces on the page, so a normal Typeflux trace (a handful
# of spans) is never truncated; it only guards against a pathological trace with
# thousands of spans (e.g. runaway retries).
_MAX_HYDRATED_RUNS_PER_TRACE = 200

# LangSmith's OTEL ingestion conventions (docs.langchain.com/langsmith).
_LANGSMITH_CONVENTIONS = OtelAttributeConventions(
    span_kind_key="langsmith.span.kind",
    input_key="input.value",
    output_key="output.value",
    metadata_prefix="langsmith.metadata",
    trace_name_key="langsmith.trace.name",
    tags_key="langsmith.span.tags",
    usage_input_key="gen_ai.usage.input_tokens",
    usage_output_key="gen_ai.usage.output_tokens",
    usage_total_key="gen_ai.usage.total_tokens",
)


class LangSmithOtelProfile:
    name = "langsmith"
    conventions = _LANGSMITH_CONVENTIONS

    def __init__(
        self,
        *,
        api_key: str | None = None,
        project: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._project = project
        self._endpoint = endpoint

    def build_span_processor(self) -> Any:
        try:
            from langsmith.integrations.otel import OtelSpanProcessor
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
            raise RuntimeError(
                "langsmith[otel] is required for the LangSmith observability backend"
            ) from exc
        from opentelemetry.sdk.trace import SpanProcessor

        # opentelemetry-sdk >=1.40 calls ``_on_ending`` on every registered span
        # processor when a span ends. langsmith's ``OtelSpanProcessor`` predates
        # that hook and is not a ``SpanProcessor`` subclass, so under the current
        # SDK it raises ``AttributeError`` at span end and breaks every traced
        # workflow (#349). Mixing in the SDK base contributes its no-op
        # ``_on_ending`` (and any future SDK hooks) by inheritance, while
        # langsmith's own ``on_start``/``on_end``/``shutdown``/``force_flush`` keep
        # doing the real export. The base has no abstract methods, so this adds no
        # obligations and no behavior change beyond satisfying the new hook.
        class _CompatOtelSpanProcessor(OtelSpanProcessor, SpanProcessor):
            pass

        return _CompatOtelSpanProcessor(
            api_key=self._api_key,
            project=self._project,
            url=self._endpoint,
        )


class LangSmithTraceReader:
    def __init__(
        self,
        *,
        client: Any | None = None,
        project: str | None = None,
        api_key: str | None = None,
        endpoint: str | None = None,
        scan_limit: int = _DEFAULT_SCAN_LIMIT,
    ) -> None:
        self.client = client
        self._project = project
        self._api_key = api_key
        self._endpoint = endpoint
        self._scan_limit = scan_limit

    def get_trace(
        self,
        trace_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_detail_pages: int | None = None,
    ) -> TraceRecord:
        del since, until, max_detail_pages
        runs = list(self._list_runs(trace_id=trace_id, limit=self._scan_limit))
        if not runs:
            raise KeyError(trace_id)
        trace = _trace_from_runs(trace_id, runs)
        retrieval = TraceRetrievalInfo(
            backend="langsmith",
            complete=len(runs) < self._scan_limit,
            pages_read=1,
            observations_read=len(runs),
            page_size=self._scan_limit,
            max_pages=1,
            warnings=(
                ()
                if len(runs) < self._scan_limit
                else (
                    f"LangSmith trace {trace_id!r} returned the {self._scan_limit}-run scan "
                    "limit; some observations may be missing.",
                )
            ),
        )
        return trace.model_copy(update={"retrieval": retrieval})

    def list_traces(self, query: TraceListQuery) -> TracePage:
        offset = _decode_cursor(query.cursor)
        roots, has_more = self._root_runs_page(
            offset=offset, limit=query.limit, start_time=_lower_bound(query.since)
        )
        # An unfiltered list never hydrates children. status needs them (a child
        # error the root did not surface), and so does workflow_id: the matcher
        # checks observation metadata (include_observation_metadata=True), and a
        # workflow started without an execution manifest can carry workflow_id
        # only on its child temporal spans. workflow_name/timestamp stay root-only.
        needs_children = query.status is not None or query.workflow_id is not None
        traces = self._traces_from_roots(roots, needs_children=needs_children)
        traces = _apply_list_filters(traces, query)
        next_cursor = _encode_cursor(offset + query.limit) if has_more else None
        return TracePage(traces=traces[: query.limit], next_cursor=next_cursor)

    def delete_traces_for_subject(
        self,
        subject_id: str,
        *,
        dry_run: bool = True,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> SubjectTraceDeletionReport:
        del subject_id, dry_run, since, until
        raise RuntimeError(_LANGSMITH_SUBJECT_DELETION_UNSUPPORTED)

    def search_traces(self, query: TraceSearchQuery) -> TracePage:
        if query.backend_filter is not None:
            raise RuntimeError("backend_filter is not supported by the LangSmith trace reader")
        needs_children = _search_needs_children(query)
        offset = _decode_cursor(query.cursor)
        scan_pages = max(query.scan_pages, 1)
        # Walk the root stream once across the whole scan (chunked into pages),
        # rather than re-opening it per page — re-opening would rescan from 0 each
        # time, so a deep multi-page search would re-fetch the same rows N times.
        runs = self._iter_root_runs(start_time=_lower_bound(query.since))
        deque(islice(runs, offset), maxlen=0)  # drop the already-consumed prefix once
        matches: list[TraceRecord] = []
        consumed = offset
        for _ in range(scan_pages):
            page = list(islice(runs, query.limit))
            if not page:
                break
            traces = self._traces_from_roots(page, needs_children=needs_children)
            for index, trace in enumerate(traces, start=1):
                consumed += 1
                if _trace_matches_search(trace, query):
                    matches.append(trace)
                    if len(matches) >= query.limit:
                        next_cursor = (
                            _encode_cursor(consumed)
                            if index < len(page)
                            else self._cursor_if_more(runs, consumed)
                        )
                        return TracePage(
                            traces=tuple(matches),
                            next_cursor=next_cursor,
                        )
            if len(page) < query.limit:
                break  # stream exhausted within this page
        next_cursor = self._cursor_if_more(runs, consumed)
        complete = next_cursor is None
        warnings = (
            ()
            if complete
            else (
                f"scanned {scan_pages} page(s) without filling limit={query.limit}; "
                f"pass cursor={next_cursor!r} to continue.",
            )
        )
        return TracePage(
            traces=tuple(matches),
            next_cursor=next_cursor,
            complete=complete,
            warnings=warnings,
        )

    @staticmethod
    def _cursor_if_more(runs: Any, consumed: int) -> str | None:
        # Peek one row past what we consumed: if the stream still has a root, a
        # next page exists and the caller resumes from `consumed`. The peeked row
        # is re-fetched by that next call (it reopens the stream from the offset),
        # so discarding it here loses nothing.
        return _encode_cursor(consumed) if next(runs, _SENTINEL) is not _SENTINEL else None

    def _traces_from_roots(
        self, roots: list[Any], *, needs_children: bool
    ) -> tuple[TraceRecord, ...]:
        if not needs_children:
            return tuple(
                _trace_from_runs(_optional_str(_run_attr(run, "trace_id")) or "", [run])
                for run in roots
            )
        trace_ids = [
            trace_id for run in roots if (trace_id := _optional_str(_run_attr(run, "trace_id")))
        ]
        runs_by_trace = self._runs_for_traces(trace_ids)
        traces: list[TraceRecord] = []
        for run in roots:
            trace_id = _optional_str(_run_attr(run, "trace_id")) or ""
            traces.append(_trace_from_runs(trace_id, runs_by_trace.get(trace_id) or [run]))
        return tuple(traces)

    def _root_runs_page(
        self, *, offset: int, limit: int, start_time: datetime
    ) -> tuple[list[Any], bool]:
        """One page of root runs by offset, plus whether more remain.

        LangSmith's ``/runs/query`` rejects a body ``limit`` above 100 and exposes
        no server-side offset, so we lazily walk the unbounded root stream (the
        SDK pages it internally via its own cursor) and slice the window. Reading
        one row past the page tells us whether a next page exists.
        """
        window = list(
            islice(self._iter_root_runs(start_time=start_time), offset, offset + limit + 1)
        )
        return window[:limit], len(window) > limit

    def _iter_root_runs(self, *, start_time: datetime) -> Any:
        # limit=None → the SDK pages the stream internally (<=100 per request) via
        # its own server cursor; we slice the offset window from the lazy result.
        return self._require_client().list_runs(
            project_name=self._project, is_root=True, start_time=start_time, limit=None
        )

    def _runs_for_traces(self, trace_ids: list[str]) -> dict[str, list[Any]]:
        """All runs for the given traces in a single query (avoids per-root N+1)."""
        if not trace_ids:
            return {}
        client = self._require_client()
        quoted = ", ".join(f'"{trace_id}"' for trace_id in trace_ids)
        runs_by_trace: dict[str, list[Any]] = {trace_id: [] for trace_id in trace_ids}
        runs = client.list_runs(
            project_name=self._project, filter=f"in(trace_id, [{quoted}])", limit=None
        )
        # Budget scales with the page so normal traces (a handful of spans each)
        # are never truncated; the cap only guards against a pathological trace
        # with thousands of spans. Truncation would drop the *trailing* traces'
        # children (mis-filtering them), so it must not happen in normal use.
        cap = len(trace_ids) * _MAX_HYDRATED_RUNS_PER_TRACE
        for run in islice(runs, cap):
            trace_id = _optional_str(_run_attr(run, "trace_id"))
            if trace_id in runs_by_trace:
                runs_by_trace[trace_id].append(run)
        if next(runs, _SENTINEL) is not _SENTINEL:
            # Hit the hydration budget; reconstructed traces may be partial.
            logger.warning(
                "LangSmith child hydration hit the %d-run budget for %d traces; "
                "some observations may be missing from the reconstructed traces.",
                cap,
                len(trace_ids),
            )
        return runs_by_trace

    def _list_runs(self, **kwargs: Any) -> Any:
        client = self._require_client()
        params: dict[str, Any] = {"project_name": self._project}
        params.update({key: value for key, value in kwargs.items() if value is not None})
        if "limit" in params:
            params["limit"] = min(params["limit"], _MAX_RUN_LIMIT)
        return client.list_runs(**params)

    def _require_client(self) -> Any:
        if self.client is None:
            self.client = _build_client(api_key=self._api_key, endpoint=self._endpoint)
        return self.client


@dataclass(frozen=True)
class LangSmithObservabilityBackend:
    writer: TraceWriter
    reader: TraceReader

    @classmethod
    def from_env(
        cls,
        *,
        redactor: Redactor | None = None,
        project: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> LangSmithObservabilityBackend:
        # Explicit values (#793: spec-declared credentials, resolved by the caller) win;
        # unset falls back to the standard env vars, so declaring them is additive.
        resolved_project = project or os.getenv("LANGSMITH_PROJECT") or "default"
        resolved_endpoint = endpoint or os.getenv("LANGSMITH_ENDPOINT")
        api_key = api_key or os.getenv("LANGSMITH_API_KEY")
        profile = LangSmithOtelProfile(
            api_key=api_key,
            project=resolved_project,
            endpoint=resolved_endpoint,
        )
        writer = OtelTraceWriter(
            profile=profile,
            redactor=redactor or RegexPIIRedactor.default(),
        )
        reader = LangSmithTraceReader(
            project=resolved_project,
            api_key=api_key,
            endpoint=resolved_endpoint,
        )
        return cls(writer=writer, reader=reader)


def _build_client(*, api_key: str | None, endpoint: str | None) -> Any:
    try:
        from langsmith import Client
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError("langsmith is required for LangSmith trace reading") from exc
    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if endpoint:
        kwargs["api_url"] = endpoint
    return Client(**kwargs)


def _trace_from_runs(trace_id: str, runs: list[Any]) -> TraceRecord:
    observations = tuple(_observation_from_run(run) for run in runs)
    root = _root_observation(observations) or (observations[0] if observations else None)
    return TraceRecord(
        trace_id=trace_id,
        name=root.name if root is not None else None,
        timestamp=root.start_time if root is not None else None,
        input=root.input if root is not None else None,
        output=root.output if root is not None else None,
        metadata=dict(root.metadata) if root is not None else {},
        observations=observations,
        raw=runs,
    )


def _root_observation(observations: tuple[ObservationRecord, ...]) -> ObservationRecord | None:
    for observation in observations:
        typeflux = observation.metadata.get("typeflux")
        if isinstance(typeflux, dict) and typeflux.get("level") == "workflow":
            return observation
    # Fall back to the workflow span by name if the typeflux metadata blob did
    # not decode (mirrors the Langfuse reader); avoids picking an arbitrary
    # child run as the trace root.
    for observation in observations:
        if observation.name and observation.name.startswith("TypefluxWorkflow:"):
            return observation
    return None


def _observation_from_run(run: Any) -> ObservationRecord:
    return ObservationRecord(
        observation_id=_optional_str(_run_attr(run, "id")),
        name=_optional_str(_run_attr(run, "name")),
        type=_optional_str(_run_attr(run, "run_type")),
        level="ERROR" if _run_attr(run, "error") else None,
        input=_io_value(_run_attr(run, "inputs")),
        output=_io_value(_run_attr(run, "outputs")),
        metadata=_run_metadata(run),
        start_time=_optional_datetime(_run_attr(run, "start_time")),
        end_time=_optional_datetime(_run_attr(run, "end_time")),
        raw=run,
    )


#: The OTEL writer JSON-encodes exactly one metadata key — the nested
#: ``typeflux`` blob (``semantic_metadata`` wraps everything under it). Promoted
#: scalars and ``model`` are written as plain scalars, and tags ride a separate
#: span attribute, not metadata. So the reader must decode *only* this key: a
#: user-supplied (or promoted) metadata value that merely looks like JSON — e.g.
#: the literal string ``'{"id": 1}'`` — must round-trip verbatim, not be parsed
#: into a dict (#330).
_STRUCTURED_METADATA_KEYS = frozenset({"typeflux"})


def _run_metadata(run: Any) -> dict[str, Any]:
    extra = _run_attr(run, "extra")
    extra = extra if isinstance(extra, dict) else {}
    metadata = extra.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        key: (_maybe_json(value) if key in _STRUCTURED_METADATA_KEYS else value)
        for key, value in metadata.items()
    }


def _io_value(value: Any) -> Any:
    # The OTEL writer sets the whole payload under ``input.value`` / ``output.value``;
    # LangSmith may surface it parsed, as a string, or wrapped in a ``value`` key.
    if isinstance(value, dict) and set(value.keys()) == {"value"}:
        return _maybe_json(value["value"])
    return _maybe_json(value)


def _maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _run_attr(run: Any, name: str) -> Any:
    if isinstance(run, dict):
        return run.get(name)
    return getattr(run, name, None)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc_aware(value)
    if isinstance(value, str):
        try:
            return _as_utc_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _lower_bound(since: datetime | None) -> datetime:
    if since is not None:
        return _as_utc_aware(since) or (datetime.now(UTC) - _DEFAULT_LOOKBACK)
    return datetime.now(UTC) - _DEFAULT_LOOKBACK


def _search_needs_children(query: TraceSearchQuery) -> bool:
    """Whether a search filter inspects activity/generation (child) observations.

    Direct attribute access (not getattr) so a renamed/removed query field fails
    loudly here instead of silently skipping hydration. Every other search filter
    (workflow id/name, manifest/contract hashes, code provenance, temporal /
    runtime / policy) reads the root run's own metadata and needs no children.
    """
    return bool(
        query.status is not None
        or query.include_untagged_fallback
        or query.activity_name
        or query.activity_manifest_hash
        or query.resolved_prompt_version
        or query.provider_model
        or query.prompt_ref
        or query.input_schema_hash
        or query.output_schema_hash
    )


def _encode_cursor(offset: int) -> str:
    return str(offset)


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except (TypeError, ValueError):
        return 0
    return offset if offset > 0 else 0


__all__ = [
    "LangSmithObservabilityBackend",
    "LangSmithOtelProfile",
    "LangSmithTraceReader",
]
