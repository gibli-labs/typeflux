"""Subject-scoped DeleteWorkflowExecution driver (#715 slice 4).

Dry-run / execute / conflict (multi-subject, stale-index, unknown-status,
unreadable-subjects) / running paths, driven by a fake Temporal client — no live
server. The fakes yield REAL ``WorkflowExecutionStatus`` enums (the driver classifies
against the enum, never a string compare). Asserts the audit-honest report (counts
only, never other subjects' ids; ``deleted`` outcome distinct from the ``deletable``
plan; ``exclude_none`` serialization).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus

from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE
from typeflux.project.erase_executions import (
    SubjectExecutionDeletionReport,
    delete_executions_for_subject,
)


class _FakeTypedSearchAttributes:
    """Mimics temporalio TypedSearchAttributes.get(key) → value or KeyError.

    ``value`` is returned VERBATIM (not normalized), so tests can feed the driver a
    bare string / int / raising lookup to prove the fail-safe handling.
    """

    def __init__(self, value: Any, *, raise_error: Exception | None = None) -> None:
        self._value = value
        self._raise = raise_error

    def get(self, key: Any) -> Any:
        if self._raise is not None:
            raise self._raise
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
        status: WorkflowExecutionStatus | Any | None,
        subject_ids: Any,
        run_id: str | None = "run-1",
        has_typed: bool = True,
        typed_raises: Exception | None = None,
    ) -> None:
        self.id = execution_id
        self.run_id = run_id
        self.workflow_type = "W.v1"
        self.status = status
        self.typed_search_attributes = (
            _FakeTypedSearchAttributes(subject_ids, raise_error=typed_raises) if has_typed else None
        )


class _FakeWorkflowService:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str | None]] = []
        self.raise_for: set[str] = set()

    async def delete_workflow_execution(self, request: Any) -> Any:
        wf = request.workflow_execution
        if wf.workflow_id in self.raise_for:
            raise RuntimeError("boom")
        self.deleted.append((wf.workflow_id, wf.run_id or None))
        return object()


class _FakeClient:
    def __init__(self, executions: list[_FakeExecution], *, namespace: str = "ns") -> None:
        self._executions = executions
        self.namespace = namespace
        self.workflow_service = _FakeWorkflowService()

    async def list_workflows(self, query: str):  # noqa: ANN201 - async iterator
        self.last_query = query
        for execution in self._executions:
            yield execution


def _closed(execution_id: str, subject_ids: Any, **kwargs: Any) -> _FakeExecution:
    return _FakeExecution(
        execution_id,
        status=WorkflowExecutionStatus.COMPLETED,
        subject_ids=subject_ids,
        **kwargs,
    )


def _run(client: _FakeClient, subject_id: str, **kwargs: Any) -> SubjectExecutionDeletionReport:
    return asyncio.run(delete_executions_for_subject(client, subject_id, **kwargs))


def test_dry_run_default_reports_but_deletes_nothing() -> None:
    client = _FakeClient([_closed("wf-1", ["subject-0001"])])
    report = _run(client, "subject-0001")
    assert report.dry_run is True
    assert report.executions_matched == 1
    assert [d.execution_id for d in report.deletable] == ["wf-1"]
    assert report.deleted == ()
    assert report.deleted_count == 0
    assert client.workflow_service.deleted == []
    # The enumeration used the keyword-list membership query (via the slice-1 seam).
    assert client.last_query == f"{SUBJECT_IDS_SEARCH_ATTRIBUTE} = 'subject-0001'"
    # Always carries the coverage caveat.
    assert "post-slice-1" in report.index_coverage


def test_execute_deletes_only_closed_subject_dedicated() -> None:
    client = _FakeClient(
        [
            _closed("wf-closed", ["subject-0001"]),
            _FakeExecution(
                "wf-running",
                status=WorkflowExecutionStatus.RUNNING,
                subject_ids=["subject-0001"],
            ),
            _FakeExecution(
                "wf-multi",
                status=WorkflowExecutionStatus.FAILED,
                subject_ids=["subject-0001", "subject-0002"],
            ),
        ]
    )
    report = _run(client, "subject-0001", dry_run=False)
    assert report.dry_run is False
    # `deleted` is the authoritative outcome; `deletable` remains the plan set.
    assert [d.execution_id for d in report.deleted] == ["wf-closed"]
    assert report.deleted_count == 1
    assert client.workflow_service.deleted == [("wf-closed", "run-1")]
    # Running reported, never touched.
    assert [r.execution_id for r in report.still_running] == ["wf-running"]
    # Multi-subject conflicted, counted, other ids never leaked.
    assert len(report.conflicted) == 1
    conflict = report.conflicted[0]
    assert conflict.execution_id == "wf-multi"
    assert conflict.reason == "multi_subject"
    assert conflict.other_subject_count == 1
    # The report dict carries no other subject id anywhere.
    assert "subject-0002" not in repr(report.to_dict())


def test_unknown_status_is_excluded_fail_safe() -> None:
    # A falsy/absent/non-enum status must NEVER be treated as closed (the repo
    # `or`-as-default trap): the execution is excluded, not deleted.
    class _WeirdStatus:
        name = ""  # falsy .name — the exact or-trap input

    client = _FakeClient(
        [
            _FakeExecution("wf-none", status=None, subject_ids=["subject-0001"]),
            _FakeExecution("wf-weird", status=_WeirdStatus(), subject_ids=["subject-0001"]),
        ]
    )
    report = _run(client, "subject-0001", dry_run=False)
    assert report.deleted_count == 0
    assert client.workflow_service.deleted == []
    assert {c.execution_id for c in report.conflicted} == {"wf-none", "wf-weird"}
    assert all(c.reason == "unknown_status" for c in report.conflicted)


def test_unreadable_subject_set_is_conflicted_unknown_fail_safe() -> None:
    # Rows whose subject set cannot be read — missing attributes, a non-list value
    # (a bare str would char-iterate; an int is not iterable), or a RAISING typed
    # lookup — are excluded fail-safe WITHOUT aborting the rest of the enumeration.
    client = _FakeClient(
        [
            _closed("wf-missing", None),
            _closed("wf-no-typed", None, has_typed=False),
            _closed("wf-str", "subject-0001"),  # bare str is NOT a subject list
            _closed("wf-int", 42),
            _closed("wf-raises", ["subject-0001"], typed_raises=RuntimeError("decode broke")),
            _closed("wf-good", ["subject-0001"]),
        ]
    )
    report = _run(client, "subject-0001", dry_run=False)
    # The good row still deletes: one bad row never aborts the enumeration.
    assert [d.execution_id for d in report.deleted] == ["wf-good"]
    assert {c.execution_id for c in report.conflicted} == {
        "wf-missing",
        "wf-no-typed",
        "wf-str",
        "wf-int",
        "wf-raises",
    }
    assert all(c.reason == "unreadable_subjects" for c in report.conflicted)
    assert all(c.other_subject_count is None for c in report.conflicted)


def test_readable_set_without_target_is_stale_index_not_unreadable() -> None:
    # A READABLE set that disowns the target is an index-integrity signal — labeled
    # stale_index, never masked as a data-access (unreadable) problem.
    client = _FakeClient([_closed("wf-x", ["subject-9999"])])
    report = _run(client, "subject-0001", dry_run=False)
    assert report.deleted_count == 0
    assert [c.reason for c in report.conflicted] == ["stale_index"]
    assert report.conflicted[0].other_subject_count is None
    # And the foreign subject id is not leaked.
    assert "subject-9999" not in repr(report.to_dict())


def test_delete_failure_is_recorded_not_swallowed() -> None:
    client = _FakeClient(
        [
            _closed("wf-1", ["subject-0001"]),
            _closed("wf-2", ["subject-0001"]),
        ]
    )
    client.workflow_service.raise_for.add("wf-1")
    report = _run(client, "subject-0001", dry_run=False)
    # The plan lists both; the outcome lists only the success.
    assert [d.execution_id for d in report.deletable] == ["wf-1", "wf-2"]
    assert [d.execution_id for d in report.deleted] == ["wf-2"]
    assert report.deleted_count == 1
    assert client.workflow_service.deleted == [("wf-2", "run-1")]
    assert [f.execution_id for f in report.failures] == ["wf-1"]
    assert "boom" in report.failures[0].reason


def test_limit_truncation_warns() -> None:
    client = _FakeClient([_closed(f"wf-{i}", ["subject-0001"]) for i in range(5)])
    report = _run(client, "subject-0001", limit=2)
    assert report.executions_matched == 2
    assert any("hit the limit" in w for w in report.warnings)


def test_exact_limit_does_not_warn() -> None:
    client = _FakeClient([_closed(f"wf-{i}", ["subject-0001"]) for i in range(2)])
    report = _run(client, "subject-0001", limit=2)
    assert report.executions_matched == 2
    assert report.warnings == ()


def test_empty_subject_id_fails_closed() -> None:
    client = _FakeClient([])
    with pytest.raises(ValueError, match="non-empty"):
        _run(client, "")


def test_missing_namespace_fails_closed() -> None:
    client = _FakeClient([], namespace="")
    with pytest.raises(ValueError, match="namespace"):
        _run(client, "subject-0001")


def test_explicit_namespace_overrides_client() -> None:
    client = _FakeClient([_closed("wf-1", ["subject-0001"])], namespace="client-ns")
    report = _run(client, "subject-0001", namespace="explicit-ns", dry_run=False)
    assert report.namespace == "explicit-ns"


def test_serialized_report_omits_absent_optionals() -> None:
    # Cross-edition shape convention (#715 slice 4 fix round): absent optional fields
    # are OMITTED from the serialized report (TS conditional-spread parity), never
    # emitted as explicit nulls.
    client = _FakeClient(
        [
            _closed("wf-ok", ["subject-0001"], run_id=None),
            _closed("wf-unreadable", None, run_id=None),
        ]
    )
    report = _run(client, "subject-0001")
    payload = report.to_dict()
    deletable_keys = set(payload["deletable"][0].keys())
    assert "run_id" not in deletable_keys  # absent → omitted, not null
    conflict_keys = set(payload["conflicted"][0].keys())
    assert conflict_keys == {"execution_id", "reason"}  # no run_id, no null count
