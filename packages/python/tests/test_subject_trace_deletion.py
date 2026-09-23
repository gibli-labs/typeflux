"""Subject-scoped Langfuse trace deletion (#715 slice 2).

The Langfuse surface's erasure primitive: ``delete_traces_for_subject`` queries
by BOTH the portable ``typeflux.subject:{id}`` tag and the native ``userId``,
UNIONs the ids, walks every page, and — unless ``dry_run`` — batches
``delete_multiple`` while reporting per-batch failures. A recording fake pins
the two invariants that matter for compliance: a dry run NEVER mutates, and a
report is honest about what it could and could not reach.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from typeflux.observability import (
    SUBJECT_TRACE_DELETION_UNSUPPORTED,
    SUBJECT_TRACE_INDEX_COVERAGE,
    InMemoryTraceStore,
    LangfuseTraceReader,
    NoOpTraceReader,
)
from typeflux.observability import langfuse as langfuse_module


class _RecordingTraceApi:
    """A Langfuse Trace-API double that records list/delete calls.

    ``tag_pages`` / ``user_pages`` are page-numbered result pages (a list of
    trace-id lists) served to the tag channel and the user_id channel; a page's
    ``meta.totalPages`` drives the pagination walk. ``fail_ids`` makes any batch
    containing one of those ids raise, so partial-failure reporting is testable.
    ``tags_by_id`` overrides a row's ``tags`` field (default ``[]`` — a
    single-subject trace); ``None`` omits the field entirely, modelling a row
    whose tags are unreadable (the conflicted-unknown path).
    """

    def __init__(
        self,
        *,
        tag_pages: list[list[str]] | None = None,
        user_pages: list[list[str]] | None = None,
        fail_ids: frozenset[str] = frozenset(),
        tags_by_id: dict[str, list[str] | None] | None = None,
        user_tags_by_id: dict[str, list[str] | None] | None = None,
    ) -> None:
        self._tag_pages = tag_pages if tag_pages is not None else [[]]
        self._user_pages = user_pages if user_pages is not None else [[]]
        self._fail_ids = frozenset(fail_ids)
        self._tags_by_id = tags_by_id or {}
        # Per-channel override for the user_id channel's rows (defaults to the
        # shared mapping) — lets a test serve DIFFERENT tag views of the same
        # trace per channel (the cross-channel merge scenarios).
        self._user_tags_by_id = user_tags_by_id if user_tags_by_id is not None else None
        self.list_calls: list[dict[str, Any]] = []
        self.delete_batches: list[list[str]] = []
        self.deleted: list[str] = []

    def _row(self, trace_id: str, *, channel: str) -> dict[str, Any]:
        mapping = self._tags_by_id
        if channel == "user" and self._user_tags_by_id is not None:
            mapping = self._user_tags_by_id
        row_tags = mapping.get(trace_id, [])
        if row_tags is None:
            return {"id": trace_id}
        return {"id": trace_id, "tags": row_tags}

    def list(
        self,
        *,
        page: int,
        limit: int,
        tags: list[str] | None = None,
        user_id: str | None = None,
        from_timestamp: Any = None,
        to_timestamp: Any = None,
    ) -> dict[str, Any]:
        self.list_calls.append(
            {
                "page": page,
                "limit": limit,
                "tags": tags,
                "user_id": user_id,
                "from_timestamp": from_timestamp,
                "to_timestamp": to_timestamp,
            }
        )
        channel = "tag" if tags else "user"
        pages = self._tag_pages if tags else self._user_pages
        rows = pages[page - 1] if 1 <= page <= len(pages) else []
        return {
            "data": [self._row(trace_id, channel=channel) for trace_id in rows],
            "meta": {
                "page": page,
                "limit": limit,
                "totalItems": sum(len(p) for p in pages),
                "totalPages": len(pages),
            },
        }

    def delete_multiple(self, *, trace_ids: list[str]) -> dict[str, str]:
        self.delete_batches.append(list(trace_ids))
        failing = [trace_id for trace_id in trace_ids if trace_id in self._fail_ids]
        if failing:
            raise RuntimeError(f"delete rejected: {failing}")
        self.deleted.extend(trace_ids)
        return {"message": f"deleted {len(trace_ids)} traces"}


class _RecordingClient:
    def __init__(self, trace_api: _RecordingTraceApi) -> None:
        self.api = type("_Api", (), {"trace": trace_api})()


def _reader(trace_api: _RecordingTraceApi) -> LangfuseTraceReader:
    return LangfuseTraceReader(client=_RecordingClient(trace_api))


def test_dry_run_lists_union_without_deleting() -> None:
    trace_api = _RecordingTraceApi(
        tag_pages=[["t1", "t2"]],
        user_pages=[["t2", "t3"]],
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=True)

    assert report.dry_run is True
    # UNION of both channels, de-duplicated, tag channel first, order preserved.
    assert report.trace_ids == ("t1", "t2", "t3")
    assert report.matched_by_tag == ("t1", "t2")
    assert report.matched_by_user_id == ("t2", "t3")
    assert report.deleted_count == 0
    assert report.failures == ()
    # The load-bearing invariant: a dry run performs NO deletion.
    assert trace_api.delete_batches == []
    assert trace_api.deleted == []


def test_dual_channel_query_uses_subject_tag_and_native_user_id() -> None:
    trace_api = _RecordingTraceApi(tag_pages=[["t1"]], user_pages=[["t1"]])
    _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=True)

    tag_calls = [call for call in trace_api.list_calls if call["tags"]]
    user_calls = [call for call in trace_api.list_calls if call["user_id"] is not None]
    assert tag_calls, "the tag channel must query trace.list"
    assert tag_calls[0]["tags"] == ["typeflux.subject:subj-1"]
    assert user_calls, "the user_id channel must query trace.list"
    assert user_calls[0]["user_id"] == "subj-1"


def test_union_dedups_traces_present_in_both_channels() -> None:
    trace_api = _RecordingTraceApi(
        tag_pages=[["shared", "tag-only"]],
        user_pages=[["shared", "user-only"]],
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=True)

    assert report.trace_ids == ("shared", "tag-only", "user-only")
    assert report.trace_ids.count("shared") == 1


def test_pagination_walks_every_page() -> None:
    trace_api = _RecordingTraceApi(
        tag_pages=[["a", "b"], ["c", "d"], ["e"]],
        user_pages=[["f"]],
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=True)

    assert report.matched_by_tag == ("a", "b", "c", "d", "e")
    assert report.matched_by_user_id == ("f",)
    tag_pages_requested = [call["page"] for call in trace_api.list_calls if call["tags"]]
    assert tag_pages_requested == [1, 2, 3]


def test_execute_deletes_the_union_and_reports_counts() -> None:
    trace_api = _RecordingTraceApi(
        tag_pages=[["t1", "t2"]],
        user_pages=[["t3"]],
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)

    assert report.dry_run is False
    assert report.trace_ids == ("t1", "t2", "t3")
    assert report.deleted_count == 3
    assert report.failures == ()
    assert sorted(trace_api.deleted) == ["t1", "t2", "t3"]


def test_partial_failure_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One id per batch so a single failing id does not sink its neighbours.
    monkeypatch.setattr(langfuse_module, "_LANGFUSE_TRACE_DELETE_BATCH_SIZE", 1)
    trace_api = _RecordingTraceApi(
        tag_pages=[["ok-1", "boom", "ok-2"]],
        user_pages=[[]],
        fail_ids=frozenset({"boom"}),
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)

    assert report.deleted_count == 2
    assert sorted(trace_api.deleted) == ["ok-1", "ok-2"]
    assert [failure.trace_id for failure in report.failures] == ["boom"]
    assert "delete rejected" in report.failures[0].reason


def test_report_always_carries_the_index_coverage_caveat() -> None:
    trace_api = _RecordingTraceApi(tag_pages=[["t1"]], user_pages=[[]])
    reader = _reader(trace_api)

    dry = reader.delete_traces_for_subject("subj-1", dry_run=True)
    live = reader.delete_traces_for_subject("subj-1", dry_run=False)

    assert dry.index_coverage == SUBJECT_TRACE_INDEX_COVERAGE
    assert live.index_coverage == SUBJECT_TRACE_INDEX_COVERAGE
    assert "post-slice-1" in dry.index_coverage
    assert dry.to_dict()["index_coverage"] == SUBJECT_TRACE_INDEX_COVERAGE


def test_window_bounds_reach_the_list_call() -> None:
    from datetime import UTC, datetime

    trace_api = _RecordingTraceApi(tag_pages=[["t1"]], user_pages=[[]])
    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime(2026, 6, 1, tzinfo=UTC)
    _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=True, since=since, until=until)

    tag_call = next(call for call in trace_api.list_calls if call["tags"])
    assert tag_call["from_timestamp"] == since
    assert tag_call["to_timestamp"] == until


@pytest.mark.parametrize(
    "reader",
    [NoOpTraceReader(), InMemoryTraceStore(())],
    ids=["noop", "in-memory"],
)
def test_backends_without_a_trace_store_raise_not_supported(reader: Any) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        reader.delete_traces_for_subject("subj-1", dry_run=True)
    assert str(excinfo.value) == SUBJECT_TRACE_DELETION_UNSUPPORTED


class _NoTagFilterTraceApi(_RecordingTraceApi):
    """A list API WITHOUT the ``tags`` kwarg — the P1 fail-closed scenario."""

    def list(  # type: ignore[override]
        self,
        *,
        page: int,
        limit: int,
        user_id: str | None = None,
        from_timestamp: Any = None,
        to_timestamp: Any = None,
    ) -> dict[str, Any]:
        return super().list(
            page=page,
            limit=limit,
            tags=None,
            user_id=user_id,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
        )


class _NoFilterTraceApi(_RecordingTraceApi):
    """A list API accepting NEITHER subject-filter kwarg."""

    def list(  # type: ignore[override]
        self,
        *,
        page: int,
        limit: int,
        from_timestamp: Any = None,
        to_timestamp: Any = None,
    ) -> dict[str, Any]:
        return super().list(
            page=page,
            limit=limit,
            tags=None,
            user_id=None,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
        )


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "execute"])
def test_list_api_without_tag_filter_fails_closed(dry_run: bool) -> None:
    # P1: a stripped subject filter would turn the listing UNFILTERED and feed
    # every trace in the window to delete_multiple. The tag channel must raise
    # instead — on dry-run too (its plan would be equally wrong) — and nothing
    # may be deleted.
    trace_api = _NoTagFilterTraceApi(tag_pages=[["t1"]], user_pages=[["t1"]])
    with pytest.raises(RuntimeError, match="subject-filter.*fail-closed") as excinfo:
        _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=dry_run)
    assert "tags" in str(excinfo.value)
    assert trace_api.delete_batches == []
    assert trace_api.deleted == []


def test_list_api_without_either_filter_fails_closed() -> None:
    trace_api = _NoFilterTraceApi(tag_pages=[["t1"]], user_pages=[["t1"]])
    with pytest.raises(RuntimeError, match="fail-closed"):
        _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)
    assert trace_api.deleted == []


def test_optional_kwargs_still_compat_stripped_but_filters_never() -> None:
    # A list API without the date-bound kwargs still works (genuinely optional
    # kwargs keep compat-stripping); only the subject filters are load-bearing.
    class _NoWindowTraceApi(_RecordingTraceApi):
        def list(  # type: ignore[override]
            self,
            *,
            page: int,
            limit: int,
            tags: list[str] | None = None,
            user_id: str | None = None,
        ) -> dict[str, Any]:
            return super().list(page=page, limit=limit, tags=tags, user_id=user_id)

    from datetime import UTC, datetime

    trace_api = _NoWindowTraceApi(tag_pages=[["t1"]], user_pages=[[]])
    report = _reader(trace_api).delete_traces_for_subject(
        "subj-1", dry_run=True, since=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert report.trace_ids == ("t1",)


def test_multi_subject_trace_is_excluded_and_reported_with_count() -> None:
    # A trace carrying OTHER subjects' markers must not be deleted while erasing
    # one subject — that would silently destroy the others' audit trails. It is
    # excluded and reported with a COUNT of other markers (never their ids).
    trace_api = _RecordingTraceApi(
        tag_pages=[["solo", "shared"]],
        user_pages=[[]],
        tags_by_id={
            "solo": ["typeflux.subject:subj-1"],
            "shared": [
                "typeflux.subject:subj-1",
                "typeflux.subject:subj-2",
                "typeflux.subject:subj-3",
            ],
        },
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)

    assert report.trace_ids == ("solo",)
    assert trace_api.deleted == ["solo"]
    assert [(c.trace_id, c.other_subject_count) for c in report.conflicted] == [("shared", 2)]
    # The other subjects' IDs must not appear anywhere in the report.
    assert "subj-2" not in str(report.to_dict())


def test_dry_run_reports_conflicts_identically() -> None:
    trace_api = _RecordingTraceApi(
        tag_pages=[["shared"]],
        user_pages=[[]],
        tags_by_id={"shared": ["typeflux.subject:subj-1", "typeflux.subject:subj-2"]},
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=True)

    assert report.trace_ids == ()
    assert [(c.trace_id, c.other_subject_count) for c in report.conflicted] == [("shared", 1)]
    assert trace_api.delete_batches == []


def test_row_without_tags_is_excluded_as_conflicted_unknown() -> None:
    # No readable tags → the other-subject question is unanswerable; fail-safe
    # exclusion (conflicted with an unknown count), never deletion on a guess.
    trace_api = _RecordingTraceApi(
        tag_pages=[[]],
        user_pages=[["opaque"]],
        tags_by_id={"opaque": None},
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)

    assert report.trace_ids == ()
    assert trace_api.deleted == []
    assert [(c.trace_id, c.other_subject_count) for c in report.conflicted] == [("opaque", None)]


def test_empty_tag_channel_tags_cannot_mask_user_channel_conflict() -> None:
    # The Bugbot HIGH scenario verbatim: the tag channel serves the trace with an
    # EMPTY tags array while the user_id channel's row carries another subject's
    # marker. A first-readable-wins merge would read the trace as single-subject
    # and delete it; the union merge must see the conflict and exclude it.
    trace_api = _RecordingTraceApi(
        tag_pages=[["shared"]],
        user_pages=[["shared"]],
        tags_by_id={"shared": []},
        user_tags_by_id={"shared": ["typeflux.subject:subj-1", "typeflux.subject:subj-2"]},
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)

    assert report.trace_ids == ()
    assert trace_api.deleted == []
    assert [(c.trace_id, c.other_subject_count) for c in report.conflicted] == [("shared", 1)]


def test_disjoint_readable_tag_lists_union_before_counting() -> None:
    # Both channels readable but each carrying a DIFFERENT other-subject marker:
    # the union counts both (de-duplicated), not just one channel's view.
    trace_api = _RecordingTraceApi(
        tag_pages=[["shared"]],
        user_pages=[["shared"]],
        tags_by_id={"shared": ["typeflux.subject:subj-1", "typeflux.subject:subj-2"]},
        user_tags_by_id={"shared": ["typeflux.subject:subj-1", "typeflux.subject:subj-3"]},
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=True)

    assert report.trace_ids == ()
    assert [(c.trace_id, c.other_subject_count) for c in report.conflicted] == [("shared", 2)]


def test_unreadable_channel_does_not_poison_a_readable_union() -> None:
    # One channel's row has no readable tags, the other's is readable: the
    # readable union governs — a clean single-subject list stays deletable and
    # a readable conflict is counted; unknown only when NO channel is readable.
    trace_api = _RecordingTraceApi(
        tag_pages=[["clean", "shared"]],
        user_pages=[["clean", "shared"]],
        tags_by_id={"clean": None, "shared": None},
        user_tags_by_id={
            "clean": ["typeflux.subject:subj-1"],
            "shared": ["typeflux.subject:subj-1", "typeflux.subject:subj-2"],
        },
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)

    assert report.trace_ids == ("clean",)
    assert trace_api.deleted == ["clean"]
    assert [(c.trace_id, c.other_subject_count) for c in report.conflicted] == [("shared", 1)]


def test_page_cap_truncation_warns_and_still_deletes_found_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cap the walk at 2 pages while 4 exist: the report must WARN about the
    # truncation (the completeness story) and still delete what it found.
    monkeypatch.setattr(langfuse_module, "_LANGFUSE_TRACE_LIST_MAX_PAGES", 2)
    trace_api = _RecordingTraceApi(
        tag_pages=[["a"], ["b"], ["c"], ["d"]],
        user_pages=[[]],
    )
    report = _reader(trace_api).delete_traces_for_subject("subj-1", dry_run=False)

    assert report.trace_ids == ("a", "b")
    assert sorted(trace_api.deleted) == ["a", "b"]
    assert any(
        "stopped after scanning 2 pages" in warning and "more candidate pages remain" in warning
        for warning in report.warnings
    )
    # The dry-run plan carries the same warning.
    dry = _reader(
        _RecordingTraceApi(tag_pages=[["a"], ["b"], ["c"], ["d"]], user_pages=[[]])
    ).delete_traces_for_subject("subj-1", dry_run=True)
    assert any("more candidate pages remain" in warning for warning in dry.warnings)


def test_missing_trace_api_raises_clearly() -> None:
    class _NoTraceApi:
        api = type("_Api", (), {"trace": None})()

    reader = LangfuseTraceReader(client=_NoTraceApi())
    with pytest.raises(RuntimeError, match="Trace API is required"):
        reader.delete_traces_for_subject("subj-1", dry_run=True)


def test_langsmith_reader_is_not_a_subject_indexed_surface() -> None:
    # #715 fans subjects to Langfuse only; LangSmith carries no subject carrier,
    # so subject-scoped deletion is a pointed not-supported error, not a no-op.
    from typeflux.observability.langsmith import LangSmithTraceReader

    reader = LangSmithTraceReader(client=None)
    with pytest.raises(RuntimeError, match="not supported by the LangSmith backend"):
        reader.delete_traces_for_subject("subj-1", dry_run=True)


def _missing_live_env() -> list[str]:
    required = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    return [name for name in required if not os.getenv(name)]


@pytest.mark.live
def test_live_langfuse_subject_deletion_round_trip(
    request: pytest.FixtureRequest,
) -> None:
    """LIVE proof against a real Langfuse: list-by-tag/user_id → delete_multiple.

    Gated: needs ``-m live``, ``TYPEFLUX_RUN_LIVE=1``, and Langfuse creds. The
    creds are absent in CI, so this skips with a pointed reason.
    """

    if request.config.option.markexpr != "live":
        pytest.skip("live tests run only when explicitly selected with -m live")
    missing = _missing_live_env()
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1" or missing:
        pytest.skip(
            "set TYPEFLUX_RUN_LIVE=1 plus LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY to run the live Langfuse deletion round-trip"
        )

    import time
    from uuid import uuid4

    # The v4 creation path (the pinned langfuse>=4 has no v3 `client.trace`):
    # spans created under `propagate_attributes` carry the trace-level tags /
    # user_id — the exact write path the slice-1 observer uses.
    from langfuse import Langfuse, propagate_attributes

    subject_id = f"erase-live-{uuid4().hex[:12]}"
    client = Langfuse()
    # Seed two traces carrying the subject via BOTH carriers, then flush.
    with propagate_attributes(tags=[f"typeflux.subject:{subject_id}"]):
        with client.start_as_current_observation(name="typeflux-erase-live-tag", as_type="span"):
            pass
    with propagate_attributes(user_id=subject_id):
        with client.start_as_current_observation(name="typeflux-erase-live-user", as_type="span"):
            pass
    client.flush()

    reader = LangfuseTraceReader(client=client)
    # Langfuse ingestion is asynchronous server-side — poll until the seeded
    # traces become visible to the dry-run plan (bounded).
    deadline = time.monotonic() + 60
    planned = reader.delete_traces_for_subject(subject_id, dry_run=True)
    while not planned.trace_ids and time.monotonic() < deadline:
        time.sleep(3)
        planned = reader.delete_traces_for_subject(subject_id, dry_run=True)
    assert planned.dry_run is True
    assert planned.trace_ids, "seeded traces never became visible to the deletion query"
    performed = reader.delete_traces_for_subject(subject_id, dry_run=False)
    assert performed.deleted_count == len(performed.trace_ids)
    assert performed.failures == ()
