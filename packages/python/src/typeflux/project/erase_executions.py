"""Subject-scoped ``DeleteWorkflowExecution`` driver (#715 slice 4, option iii).

The crypto-shred keystore (``yaml/subject_keystore.py``) makes a subject's Temporal
history permanently UNREADABLE; this driver is the complement that REMOVES the history
outright for CLOSED subject-dedicated executions — ``DeleteWorkflowExecution`` deletes an
execution's whole history + visibility record (async, closed-execution-oriented in the
Temporal service).

``DeleteWorkflowExecution`` is BLUNT: it deletes the entire execution, every subject on
it. So it is applied ONLY where the ``TypefluxSubjectIds`` index (slice 1) confirms the
execution's subject set is EXACTLY the target subject (subject-dedicated). Everything
else is excluded FAIL-SAFE and reported by category (multi-subject by COUNT, never the
other subjects' ids — matching the slice-2 conflicted-trace convention). Running
executions are reported, never touched — terminating a running execution is slice-5 /
CLI territory — and an execution whose status is absent/unrecognized is excluded too
(never deleted on a status guess).

Enumeration goes through the slice-1 seam (``runs.list_executions_for_subject``) — one
query/limit/status/subject-extraction implementation, not a drifting copy.

Dry-run defaults ON: the driver reports what it WOULD delete (the compliance plan an
operator reviews) and mutates nothing unless ``dry_run=False``. The report is
audit-honest: the ``deletable`` PLAN set, the authoritative ``deleted`` outcome,
conflicted-excluded by category, still-running, the index-coverage caveat, and any
enumeration-truncation warning. Serialized reports OMIT absent optional fields
(``exclude_none``), matching the TS edition's shape.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from typeflux.project.runs import (
    SubjectExecutionRef,
    list_executions_for_subject,
)

__all__ = [
    "SUBJECT_EXECUTION_INDEX_COVERAGE",
    "DeletedExecutionRef",
    "SubjectExecutionDeletionFailure",
    "SubjectExecutionDeletionReport",
    "SubjectExecutionConflict",
    "delete_executions_for_subject",
]

#: The audit-honesty caveat every execution-deletion report carries (#715 slice 4).
#: The subject->execution index (the ``TypefluxSubjectIds`` search attribute) only
#: stamps executions started since slice 1: an execution that touched a subject's data
#: before subject plumbing, or through a path that declared no subject, is invisible to
#: this enumeration. This report is complete for post-slice-1 executions only, never a
#: proof that no older executions exist.
SUBJECT_EXECUTION_INDEX_COVERAGE = (
    "Only executions stamped with the TypefluxSubjectIds index (emitted since #715 "
    "slice 1) are visible to this enumeration; executions started before subject "
    "plumbing, or that touched this subject's data through a path that declared no "
    "subject, carry no index entry and are invisible here. This report is complete for "
    "post-slice-1 executions only — not proof that no older executions exist."
)


class DeletedExecutionRef(BaseModel):
    """One execution id/run id — a plan entry (``deletable``) or an outcome entry
    (``deleted``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    run_id: str | None = None
    status: str | None = None


class SubjectExecutionConflict(BaseModel):
    """A matched execution EXCLUDED from deletion, fail-safe. ``reason``:

    * ``"multi_subject"`` — its ``TypefluxSubjectIds`` set carries OTHER subjects;
      deleting it would destroy their history.
    * ``"unreadable_subjects"`` — the subject set could not be read from the listing
      row, so the subject-dedicated question is UNANSWERABLE.
    * ``"stale_index"`` — the set WAS readable but does not contain the target subject:
      the index matched a row its own attributes disown. Split from unreadable so an
      index-integrity bug is never masked as a data-access problem.
    * ``"unknown_status"`` — the execution's status was absent/unrecognized, so
      closed-ness cannot be proven; never deleted on a status guess.

    ``other_subject_count`` is the number of OTHER subjects on the execution — a COUNT,
    never the other subject ids: listing them would leak other subjects' presence into
    this subject's erasure report (a per-subject compliance artifact must not become a
    subject directory). Present only for ``multi_subject`` (serialization omits it
    otherwise).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    run_id: str | None = None
    reason: Literal["multi_subject", "unreadable_subjects", "stale_index", "unknown_status"]
    other_subject_count: int | None = None


class SubjectExecutionDeletionFailure(BaseModel):
    """One execution the delete call could not remove — id plus the failure reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    run_id: str | None = None
    reason: str


class SubjectExecutionDeletionReport(BaseModel):
    """The outcome of a subject-scoped execution deletion — ids/counts only, never PII.

    ``deletable`` is the PLAN: the closed subject-DEDICATED executions the enumeration
    identified — on a dry run, exactly what an execute WOULD delete; on an executed
    report it remains the plan set (attempts), NOT the outcome. The authoritative
    outcome is ``deleted`` (the executions actually removed) / ``deleted_count`` plus
    ``failures`` — a consumer must never read ``len(deletable)`` as an erasure result.

    ``conflicted`` lists matched executions excluded fail-safe (see
    :class:`SubjectExecutionConflict`); ``still_running`` lists running executions,
    which the driver REPORTS but never touches (termination is slice 5).
    ``index_coverage`` is the always-present audit caveat; ``warnings`` carries
    scan-completeness caveats (e.g. the enumeration limit was hit).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str
    dry_run: bool
    namespace: str
    executions_matched: int = 0
    deletable: tuple[DeletedExecutionRef, ...] = ()
    deleted: tuple[DeletedExecutionRef, ...] = ()
    deleted_count: int = 0
    conflicted: tuple[SubjectExecutionConflict, ...] = ()
    still_running: tuple[DeletedExecutionRef, ...] = ()
    failures: tuple[SubjectExecutionDeletionFailure, ...] = ()
    index_coverage: str = SUBJECT_EXECUTION_INDEX_COVERAGE
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict for the erasure receipt (slice 5) and ``--json`` output.

        Omits ``None``-valued optional fields (``exclude_none``) so the serialized
        shape matches the TS edition's (which omits absent keys)."""

        return self.model_dump(mode="json", exclude_none=True)


def _classify(
    ref: SubjectExecutionRef,
    subject_id: str,
) -> tuple[str, SubjectExecutionConflict | None]:
    """Classify one enumerated execution: ``running`` | ``deletable`` | ``conflicted``."""

    if ref.is_running:
        return "running", None
    if not ref.is_closed:
        # Neither running nor provably closed (absent/unrecognized status) — never
        # delete on a status guess.
        return "conflicted", SubjectExecutionConflict(
            execution_id=ref.execution_id,
            run_id=ref.run_id,
            reason="unknown_status",
        )
    if ref.subject_ids is None:
        return "conflicted", SubjectExecutionConflict(
            execution_id=ref.execution_id,
            run_id=ref.run_id,
            reason="unreadable_subjects",
        )
    if subject_id not in ref.subject_ids:
        # Readable set that disowns the target: the index matched a row its own
        # attributes contradict — an index-integrity signal, not a data-access one.
        return "conflicted", SubjectExecutionConflict(
            execution_id=ref.execution_id,
            run_id=ref.run_id,
            reason="stale_index",
        )
    if len(ref.subject_ids) != 1:
        # Multi-subject: deleting the whole execution would destroy OTHER subjects'
        # history. Excluded; report the COUNT of others, never their ids.
        return "conflicted", SubjectExecutionConflict(
            execution_id=ref.execution_id,
            run_id=ref.run_id,
            reason="multi_subject",
            other_subject_count=len(ref.subject_ids) - 1,
        )
    return "deletable", None


async def delete_executions_for_subject(
    client: Any,
    subject_id: str,
    *,
    namespace: str | None = None,
    dry_run: bool = True,
    limit: int = 1000,
) -> SubjectExecutionDeletionReport:
    """Delete CLOSED subject-DEDICATED executions for ``subject_id`` (dry-run by default).

    Enumerates via the slice-1 ``TypefluxSubjectIds`` seam, classifies each execution,
    and — only when ``dry_run`` is False — calls ``DeleteWorkflowExecution`` for the
    closed subject-dedicated executions. Everything not provably closed AND
    subject-dedicated is excluded fail-safe and reported (counts only); running
    executions are reported, never touched. The caller supplies a connected Temporal
    ``client`` (the erase op owns the connection).
    """

    if not subject_id or subject_id.strip() != subject_id:
        # Fail closed: an empty/untrimmed subject would enumerate the wrong set.
        raise ValueError("delete_executions_for_subject requires a non-empty, trimmed subject id")

    resolved_namespace = namespace if namespace is not None else getattr(client, "namespace", None)
    if not resolved_namespace:
        # Never issue a namespace-less delete — fail closed rather than guess.
        raise ValueError(
            "delete_executions_for_subject could not resolve a namespace (pass namespace= or a "
            "client exposing .namespace)"
        )

    enumeration = await list_executions_for_subject(client, subject_id, limit=limit)
    deletable: list[DeletedExecutionRef] = []
    conflicted: list[SubjectExecutionConflict] = []
    still_running: list[DeletedExecutionRef] = []
    warnings: list[str] = []

    for ref in enumeration.executions:
        kind, conflict = _classify(ref, subject_id)
        as_deleted_ref = DeletedExecutionRef(
            execution_id=ref.execution_id, run_id=ref.run_id, status=ref.status
        )
        if kind == "running":
            still_running.append(as_deleted_ref)
        elif kind == "deletable":
            deletable.append(as_deleted_ref)
        else:
            assert conflict is not None
            conflicted.append(conflict)

    if enumeration.truncated:
        warnings.append(
            f"execution enumeration for {subject_id!r} hit the limit ({limit}); more matching "
            "executions may remain. Raise the limit or rerun to reach the rest — the deletion "
            "covers only the executions enumerated."
        )

    if dry_run:
        return SubjectExecutionDeletionReport(
            subject_id=subject_id,
            dry_run=True,
            namespace=resolved_namespace,
            executions_matched=len(enumeration.executions),
            deletable=tuple(deletable),
            deleted=(),
            deleted_count=0,
            conflicted=tuple(conflicted),
            still_running=tuple(still_running),
            warnings=tuple(warnings),
        )

    deleted: list[DeletedExecutionRef] = []
    failures: list[SubjectExecutionDeletionFailure] = []
    # Deletes run sequentially: subject-dedicated sets are small in practice and the
    # server-side delete is itself asynchronous; bounded fan-out (the
    # PLAN_PR_LOOKUP_CONCURRENCY pattern) is deferred until a real large-scale need.
    for planned in deletable:
        try:
            await _delete_execution(client, resolved_namespace, planned)
            deleted.append(planned)
        except Exception as exc:  # noqa: BLE001 - a failed delete is recorded, never swallowed.
            failures.append(
                SubjectExecutionDeletionFailure(
                    execution_id=planned.execution_id,
                    run_id=planned.run_id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
    return SubjectExecutionDeletionReport(
        subject_id=subject_id,
        dry_run=False,
        namespace=resolved_namespace,
        executions_matched=len(enumeration.executions),
        deletable=tuple(deletable),
        deleted=tuple(deleted),
        deleted_count=len(deleted),
        conflicted=tuple(conflicted),
        still_running=tuple(still_running),
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


async def _delete_execution(client: Any, namespace: str, ref: DeletedExecutionRef) -> None:
    """Issue one ``DeleteWorkflowExecution`` RPC for a closed subject-dedicated run."""

    from temporalio.api.common.v1 import WorkflowExecution as WorkflowExecutionProto
    from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest

    execution_proto = WorkflowExecutionProto(workflow_id=ref.execution_id)
    if ref.run_id:
        execution_proto.run_id = ref.run_id
    request = DeleteWorkflowExecutionRequest(
        namespace=namespace,
        workflow_execution=execution_proto,
    )
    await client.workflow_service.delete_workflow_execution(request)
