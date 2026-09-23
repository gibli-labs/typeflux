"""The cross-surface erasure seam + ErasureReceipt (#715 slice 5).

Orchestration across all four surface drivers with fakes — no live server: happy path
(dry-run and execute), per-surface failure isolation, skipped-with-reason (missing
dependency AND deselected), dry-run PROVABLY mutation-free (the fakes record every
mutating call and must see zero), empty-subject/actor rejection, windowing
pass-through + the window-less-surface notes, other-subject non-leakage, and the
receipt's exact serialized key sets (the cross-edition shape pin).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus

from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE
from typeflux.execution.cache import InMemoryCacheStore
from typeflux.observability.inspect import SubjectTraceDeletionReport
from typeflux.project.erase import (
    ERASURE_SURFACES,
    UNREACHABLE_SURFACES,
    ErasureReceipt,
    erase_subject,
)
from typeflux.yaml.subject_keystore import (
    InMemorySubjectKeystore,
    subject_key_state,
)

# --- fakes (the slice-4 test conventions) ---------------------------------------------


class _FakeTypedSearchAttributes:
    def __init__(self, value: Any) -> None:
        self._value = value

    def get(self, key: Any) -> Any:
        if getattr(key, "name", None) == SUBJECT_IDS_SEARCH_ATTRIBUTE:
            if self._value is None:
                raise KeyError(key)
            return self._value
        raise KeyError(key)


class _FakeExecution:
    def __init__(
        self,
        execution_id: str,
        *,
        status: Any = WorkflowExecutionStatus.COMPLETED,
        subject_ids: Any = None,
        run_id: str | None = "run-1",
    ) -> None:
        self.id = execution_id
        self.run_id = run_id
        self.workflow_type = "W.v1"
        self.status = status
        self.typed_search_attributes = _FakeTypedSearchAttributes(subject_ids)


class _FakeWorkflowService:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_workflow_execution(self, request: Any) -> Any:
        self.deleted.append(request.workflow_execution.workflow_id)
        return object()


class _FakeClient:
    def __init__(self, executions_by_subject: dict[str, list[_FakeExecution]]) -> None:
        self._by_subject = executions_by_subject
        self.namespace = "ns"
        self.workflow_service = _FakeWorkflowService()

    async def list_workflows(self, query: str):  # noqa: ANN201 - async iterator
        for subject_id, executions in self._by_subject.items():
            if f"'{subject_id}'" in query:
                for execution in executions:
                    yield execution


class _FakeTraceReader:
    """Records every delete_traces_for_subject call; mutates only when dry_run=False."""

    def __init__(self, trace_ids_by_subject: dict[str, list[str]]) -> None:
        self._by_subject = trace_ids_by_subject
        self.calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def delete_traces_for_subject(
        self,
        subject_id: str,
        *,
        dry_run: bool = True,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> SubjectTraceDeletionReport:
        self.calls.append(
            {"subject_id": subject_id, "dry_run": dry_run, "since": since, "until": until}
        )
        trace_ids = tuple(self._by_subject.get(subject_id, []))
        if dry_run:
            return SubjectTraceDeletionReport(
                subject_id=subject_id,
                dry_run=True,
                matched_by_tag=trace_ids,
                trace_ids=trace_ids,
            )
        self.deleted.extend(trace_ids)
        return SubjectTraceDeletionReport(
            subject_id=subject_id,
            dry_run=False,
            matched_by_tag=trace_ids,
            trace_ids=trace_ids,
            deleted_count=len(trace_ids),
        )


class _RaisingTraceReader:
    def delete_traces_for_subject(self, subject_id: str, **kwargs: Any) -> Any:
        raise RuntimeError("langfuse exploded")


def _seeded_cache(subject_id: str) -> InMemoryCacheStore:
    from typeflux.contracts.cache import CacheKey, cache_record

    store = InMemoryCacheStore()
    key = CacheKey(activity="judge", input_hash="a" * 64, scope={})
    store.set(
        key,
        cache_record(
            key=key,
            output={"ok": True},
            created_at=datetime.now(UTC).isoformat(),
            output_schema_hash="b" * 64,
            manifest_hash="c" * 64,
            subjects=(subject_id,),
        ),
    )
    return store


def _erase(**kwargs: Any) -> ErasureReceipt:
    return asyncio.run(erase_subject(**kwargs))


def _full_deps(subject_id: str = "subject-0001") -> dict[str, Any]:
    keystore = InMemorySubjectKeystore()
    keystore.data_key(subject_id, create=True)
    return {
        "subject_keystore": keystore,
        "temporal_client": _FakeClient(
            {subject_id: [_FakeExecution("wf-1", subject_ids=[subject_id])]}
        ),
        "trace_reader": _FakeTraceReader({subject_id: ["trace-1", "trace-2"]}),
        "cache_store": _seeded_cache(subject_id),
    }


# --- happy paths ----------------------------------------------------------------------


def test_dry_run_default_reports_plan_across_all_surfaces() -> None:
    deps = _full_deps()
    receipt = _erase(subject_ids="subject-0001", actor="ops", **deps)

    assert receipt.dry_run is True
    assert receipt.subject_ids == ("subject-0001",)
    assert receipt.actor == "ops"
    assert receipt.failed is False
    # Temporal: the plan is visible, nothing performed.
    assert receipt.temporal.status == "ok"
    assert receipt.temporal.keystore.shreddable_key_records == 1
    assert receipt.temporal.keystore.shredded_key_records == 0
    entry = receipt.temporal.keystore.entries[0]
    assert (entry.state_before, entry.would_shred, entry.shredded) == ("live", True, False)
    execution_report = receipt.temporal.executions.reports[0]
    assert [ref.execution_id for ref in execution_report.deletable] == ["wf-1"]
    assert execution_report.deleted_count == 0
    # Langfuse: the deletable set, nothing deleted.
    trace_report = receipt.langfuse.reports[0]
    assert trace_report.trace_ids == ("trace-1", "trace-2")
    assert trace_report.deleted_count == 0
    # Cache: keys found, nothing deleted.
    cache_report = receipt.cache.reports[0]
    assert cache_report.keys_found == 1
    assert cache_report.keys_deleted == 0
    # The document-only surfaces block is ALWAYS present.
    assert receipt.unreachable == UNREACHABLE_SURFACES
    assert {note.surface for note in receipt.unreachable} == {
        "provider_logs",
        "exported_artifacts",
        "mixed_workflow_payloads",
    }


def test_dry_run_is_provably_mutation_free() -> None:
    deps = _full_deps()
    keystore = deps["subject_keystore"]
    _erase(subject_ids="subject-0001", actor="ops", **deps)

    # Keystore: the record is STILL live (no destroy) and probing an unseen subject
    # minted nothing (the probe is non-minting).
    assert subject_key_state(keystore, "subject-0001") == "live"
    assert keystore.data_key("subject-0001", create=False)  # still decodable
    # Temporal: zero DeleteWorkflowExecution RPCs.
    assert deps["temporal_client"].workflow_service.deleted == []
    # Langfuse: the driver was invoked in dry-run mode only, deleted nothing.
    assert deps["trace_reader"].deleted == []
    assert all(call["dry_run"] is True for call in deps["trace_reader"].calls)
    # Cache: the record survives.
    assert deps["cache_store"].erase_subject("subject-0001", dry_run=True).keys_found == 1


def test_dry_run_probe_never_mints_for_unknown_subject() -> None:
    keystore = InMemorySubjectKeystore()
    receipt = _erase(
        subject_ids="subject-0002",
        actor="ops",
        surfaces=("temporal",),
        subject_keystore=keystore,
    )
    entry = receipt.temporal.keystore.entries[0]
    assert entry.state_before == "absent"
    assert entry.would_shred is False
    # Still absent after the probe: no record, no tombstone.
    assert subject_key_state(keystore, "subject-0002") == "absent"


def test_execute_performs_and_reports_across_all_surfaces() -> None:
    deps = _full_deps()
    receipt = _erase(subject_ids="subject-0001", actor="ops", dry_run=False, **deps)

    assert receipt.dry_run is False
    assert receipt.failed is False
    assert receipt.temporal.keystore.shredded_key_records == 1
    assert receipt.temporal.keystore.entries[0].shredded is True
    assert subject_key_state(deps["subject_keystore"], "subject-0001") == "destroyed"
    assert deps["temporal_client"].workflow_service.deleted == ["wf-1"]
    assert receipt.temporal.executions.reports[0].deleted_count == 1
    assert deps["trace_reader"].deleted == ["trace-1", "trace-2"]
    assert receipt.langfuse.reports[0].deleted_count == 2
    assert receipt.cache.reports[0].keys_deleted == 1


def test_execute_on_already_destroyed_subject_is_idempotent() -> None:
    keystore = InMemorySubjectKeystore()
    keystore.data_key("subject-0001", create=True)
    keystore.destroy_subject_key("subject-0001")
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        dry_run=False,
        surfaces=("temporal",),
        subject_keystore=keystore,
    )
    entry = receipt.temporal.keystore.entries[0]
    assert entry.state_before == "destroyed"
    assert entry.shredded is False
    assert receipt.temporal.keystore.shredded_key_records == 0
    assert receipt.failed is False


def test_multiple_subjects_produce_per_subject_reports() -> None:
    keystore = InMemorySubjectKeystore()
    keystore.data_key("subject-0001", create=True)
    receipt = _erase(
        subject_ids=["subject-0001", "subject-0002", "subject-0001"],  # dupes collapse
        actor="ops",
        surfaces=("temporal", "langfuse"),
        subject_keystore=keystore,
        trace_reader=_FakeTraceReader({"subject-0001": ["trace-1"]}),
    )
    assert receipt.subject_ids == ("subject-0001", "subject-0002")
    states = {entry.subject_id: entry.state_before for entry in receipt.temporal.keystore.entries}
    assert states == {"subject-0001": "live", "subject-0002": "absent"}
    assert [report.subject_id for report in receipt.langfuse.reports] == [
        "subject-0001",
        "subject-0002",
    ]


# --- skipped-with-reason ---------------------------------------------------------------


def test_missing_dependencies_are_skipped_with_reason_never_silent() -> None:
    receipt = _erase(subject_ids="subject-0001", actor="ops")
    assert receipt.temporal.status == "skipped"
    assert "keystore" in (receipt.temporal.keystore.skip_reason or "")
    assert "Temporal client" in (receipt.temporal.executions.skip_reason or "")
    assert receipt.langfuse.status == "skipped"
    assert "trace reader" in (receipt.langfuse.skip_reason or "")
    assert receipt.cache.status == "skipped"
    assert "cache store" in (receipt.cache.skip_reason or "")
    assert receipt.failed is False


def test_deselected_surfaces_are_reported_skipped() -> None:
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("cache",),
        cache_store=InMemoryCacheStore(),
    )
    assert receipt.temporal.status == "skipped"
    assert "not selected" in (receipt.temporal.skip_reason or "")
    assert receipt.langfuse.status == "skipped"
    assert "not selected" in (receipt.langfuse.skip_reason or "")
    assert receipt.cache.status == "ok"


def test_partial_temporal_dependency_yields_mixed_sub_statuses() -> None:
    keystore = InMemorySubjectKeystore()
    keystore.data_key("subject-0001", create=True)
    receipt = _erase(subject_ids="subject-0001", actor="ops", subject_keystore=keystore)
    assert receipt.temporal.status == "ok"  # one half ran
    assert receipt.temporal.keystore.status == "ok"
    assert receipt.temporal.executions.status == "skipped"


def test_unindexed_cache_store_reports_not_supported_and_warns() -> None:
    class _PlainStore:
        def get(self, key: Any) -> Any:
            return None

        def set(self, key: Any, record: Any) -> None:
            pass

    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("cache",),
        cache_store=_PlainStore(),
    )
    report = receipt.cache.reports[0]
    assert report.supported is False
    assert receipt.cache.status == "ok"  # documented fallback, not a failure
    assert any("Flush the whole store" in warning for warning in receipt.warnings)


def test_targeted_requirement_makes_the_incapable_store_a_loud_failure() -> None:
    """#795: runtime.cache_erasure: targeted forbids the full-flush fallback — an
    incapable store is a per-subject FAILURE naming the store class, never a warning."""

    class _PlainStore:
        def get(self, key: Any) -> Any:
            return None

        def set(self, key: Any, record: Any) -> None:
            pass

    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("cache",),
        cache_store=_PlainStore(),
        require_targeted_cache=True,
    )
    assert receipt.cache.status == "failed"
    assert "cache_erasure is 'targeted'" in receipt.cache.failures[0].error
    assert "_PlainStore" in receipt.cache.failures[0].error


def test_targeted_requirement_fails_a_selected_cache_surface_with_no_store() -> None:
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("cache",),
        require_targeted_cache=True,
    )
    assert receipt.cache.status == "failed"
    assert "no cache store was provided" in receipt.cache.failures[0].error


# --- failure isolation -----------------------------------------------------------------


def test_one_bad_surface_does_not_abort_the_others_and_marks_failed() -> None:
    deps = _full_deps()
    deps["trace_reader"] = _RaisingTraceReader()
    receipt = _erase(subject_ids="subject-0001", actor="ops", **deps)

    assert receipt.langfuse.status == "failed"
    assert receipt.langfuse.failures[0].subject_id == "subject-0001"
    assert "langfuse exploded" in receipt.langfuse.failures[0].error
    # The other surfaces still ran to completion.
    assert receipt.temporal.status == "ok"
    assert receipt.cache.status == "ok"
    # The failure is unmistakable.
    assert receipt.failed is True


def test_keystore_failure_is_isolated_per_subject() -> None:
    class _ExplodingKeystore:
        def data_key(self, subject_id: str, *, create: bool) -> bytes:
            raise RuntimeError("kms down")

        def destroy_subject_key(self, subject_id: str) -> Any:
            raise RuntimeError("kms down")

    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("temporal",),
        subject_keystore=_ExplodingKeystore(),
    )
    assert receipt.temporal.keystore.status == "failed"
    assert "kms down" in receipt.temporal.keystore.failures[0].error
    assert receipt.failed is True


def test_executed_driver_report_failures_mark_the_surface_failed() -> None:
    class _FailingDeleteService(_FakeWorkflowService):
        async def delete_workflow_execution(self, request: Any) -> Any:
            raise RuntimeError("delete refused")

    client = _FakeClient({"subject-0001": [_FakeExecution("wf-1", subject_ids=["subject-0001"])]})
    client.workflow_service = _FailingDeleteService()
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        dry_run=False,
        surfaces=("temporal",),
        temporal_client=client,
    )
    assert receipt.temporal.executions.status == "failed"
    assert receipt.temporal.executions.reports[0].failures
    assert receipt.failed is True


# --- input validation ------------------------------------------------------------------


def test_empty_subject_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one subject id"):
        _erase(subject_ids=[], actor="ops")


def test_blank_subject_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        _erase(subject_ids=["  "], actor="ops")


def test_missing_actor_is_rejected() -> None:
    with pytest.raises(ValueError, match="actor"):
        _erase(subject_ids="subject-0001", actor="")
    with pytest.raises(ValueError, match="actor"):
        _erase(subject_ids="subject-0001", actor=" ops ")


def test_unknown_surface_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown erasure surface"):
        _erase(subject_ids="subject-0001", actor="ops", surfaces=("temporal", "provider"))


def test_bare_string_surfaces_is_rejected() -> None:
    with pytest.raises(ValueError, match="bare string"):
        _erase(subject_ids="subject-0001", actor="ops", surfaces="temporal")


def test_empty_surface_selection_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one surface"):
        _erase(subject_ids="subject-0001", actor="ops", surfaces=())


def test_non_positive_execution_limit_is_rejected_at_the_seam() -> None:
    # #715 Bugbot: limit 0 would trip the enumeration's truncation guard on the
    # first match and yield an empty, healthy-looking plan — fail loud instead.
    for bad_limit in (0, -1):
        with pytest.raises(ValueError, match="execution_limit must be >= 1"):
            _erase(subject_ids="subject-0001", actor="ops", execution_limit=bad_limit)


# --- windowing ------------------------------------------------------------------------


def test_window_flows_through_to_the_langfuse_driver() -> None:
    reader = _FakeTraceReader({"subject-0001": []})
    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime(2026, 6, 30, tzinfo=UTC)
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("langfuse",),
        trace_reader=reader,
        since=since,
        until=until,
    )
    assert reader.calls[0]["since"] == since
    assert reader.calls[0]["until"] == until
    assert receipt.failed is False


def test_window_without_langfuse_surface_is_a_loud_error() -> None:
    with pytest.raises(ValueError, match="langfuse surface"):
        _erase(
            subject_ids="subject-0001",
            actor="ops",
            surfaces=("temporal",),
            subject_keystore=InMemorySubjectKeystore(),
            since=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_window_with_windowless_surfaces_adds_explicit_notes() -> None:
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        trace_reader=_FakeTraceReader({}),
        since=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert any(
        warning.startswith("temporal:") and "window-less" in warning for warning in receipt.warnings
    )
    assert any(
        warning.startswith("cache:") and "window-less" in warning for warning in receipt.warnings
    )


# --- aggregated warnings + non-leakage -------------------------------------------------


def test_conflicted_and_running_executions_surface_as_warnings() -> None:
    client = _FakeClient(
        {
            "subject-0001": [
                _FakeExecution("wf-multi", subject_ids=["subject-0001", "subject-0002"]),
                _FakeExecution(
                    "wf-running",
                    status=WorkflowExecutionStatus.RUNNING,
                    subject_ids=["subject-0001"],
                ),
            ]
        }
    )
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("temporal",),
        temporal_client=client,
    )
    assert any("excluded fail-safe" in warning for warning in receipt.warnings)
    assert any("NOT touched" in warning for warning in receipt.warnings)


def test_receipt_never_leaks_other_subjects_ids() -> None:
    # wf-multi carries subject-0002 as well; the receipt (whole serialized form) must
    # carry it only as a COUNT, never the id.
    client = _FakeClient(
        {
            "subject-0001": [
                _FakeExecution("wf-multi", subject_ids=["subject-0001", "other-subject-9"])
            ]
        }
    )
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("temporal",),
        temporal_client=client,
    )
    serialized = json.dumps(receipt.to_dict())
    assert "other-subject-9" not in serialized
    conflict = receipt.temporal.executions.reports[0].conflicted[0]
    assert conflict.reason == "multi_subject"
    assert conflict.other_subject_count == 1


# --- serialized shape (the cross-edition pin) ------------------------------------------


def test_receipt_to_dict_pins_the_exact_key_sets() -> None:
    deps = _full_deps()
    receipt = _erase(subject_ids="subject-0001", actor="ops", **deps)
    data = receipt.to_dict()
    assert set(data) == {
        "subject_ids",
        "executed_at",
        "actor",
        "dry_run",
        "surfaces",
        "unreachable",
        "warnings",
    }
    assert set(data["surfaces"]) == {"temporal", "langfuse", "cache"}
    assert set(data["surfaces"]["temporal"]) == {"status", "keystore", "executions"}
    assert set(data["surfaces"]["temporal"]["keystore"]) == {
        "status",
        "entries",
        "shreddable_key_records",
        "shredded_key_records",
        "failures",
    }
    assert set(data["surfaces"]["temporal"]["keystore"]["entries"][0]) == {
        "subject_id",
        "state_before",
        "would_shred",
        "shredded",
    }
    assert set(data["surfaces"]["temporal"]["executions"]) == {"status", "reports", "failures"}
    assert set(data["surfaces"]["langfuse"]) == {"status", "reports", "failures"}
    assert set(data["surfaces"]["cache"]) == {"status", "reports", "failures"}
    assert all(set(note) == {"surface", "note"} for note in data["unreachable"])
    # ISO-8601 executed_at round-trips.
    datetime.fromisoformat(data["executed_at"])


def test_skipped_sections_serialize_to_status_plus_reason_only() -> None:
    receipt = _erase(subject_ids="subject-0001", actor="ops")
    data = receipt.to_dict()
    assert set(data["surfaces"]["langfuse"]) == {"status", "skip_reason"}
    assert set(data["surfaces"]["cache"]) == {"status", "skip_reason"}
    assert set(data["surfaces"]["temporal"]) == {"status", "keystore", "executions"}
    assert set(data["surfaces"]["temporal"]["keystore"]) == {"status", "skip_reason"}


def test_surface_order_is_canonical_regardless_of_selection_order() -> None:
    receipt = _erase(
        subject_ids="subject-0001",
        actor="ops",
        surfaces=("cache", "temporal", "langfuse"),
        cache_store=InMemoryCacheStore(),
        subject_keystore=InMemorySubjectKeystore(),
        trace_reader=_FakeTraceReader({}),
    )
    assert list(receipt.to_dict()["surfaces"]) == list(ERASURE_SURFACES)
