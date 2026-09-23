"""Run listing and trace correlation for the control plane (#251).

The executions list reuses the drain view's fail-safe type-prefix query so
runs that predate the logical-name search attribute are never invisible.
Trace correlation goes through the workflow's *own* configured observability
backend — whatever the YAML dictates is what gets queried; ``none`` is
reported as exactly that.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from typeflux.core.subjects import subject_index_query
from typeflux.project.environment import (
    async_project_environment_context,
    create_workflow_with_subworkflows,
    project_environment_context,
    resolve_project_workflow,
    resolve_subworkflows_for,
)
from typeflux.project.spec import TypefluxProjectSpec


class WorkflowExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    run_id: str | None = None
    workflow_type: str
    #: True when the execution runs the currently-resolved spec version.
    current_version: bool
    status: str
    start_time: str | None = None
    close_time: str | None = None


class WorkflowExecutionList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_workflow: str
    current_workflow_type: str
    executions: tuple[WorkflowExecutionRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SubjectExecutionRef(BaseModel):
    """One execution the ``TypefluxSubjectIds`` index attributes to a subject
    (#715 slice 1) — the primitive later erasure slices' dry-run walks.

    ``subject_ids`` is the execution's OWN indexed subject set read off its typed
    search attributes, or ``None`` when the listing row carried no readable set
    (slice 4: the delete driver treats that as unanswerable and excludes fail-safe).
    ``is_running`` / ``is_closed`` are computed against the actual
    ``WorkflowExecutionStatus`` ENUM (never a string compare — the repo ``or``-trap);
    both ``False`` means the status was absent/unrecognized, which consumers must
    treat fail-safe (report, never delete).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    run_id: str | None = None
    workflow_type: str
    status: str
    is_running: bool = False
    is_closed: bool = False
    subject_ids: tuple[str, ...] | None = None
    start_time: str | None = None
    close_time: str | None = None


class SubjectExecutionEnumeration(BaseModel):
    """The result of a subject enumeration: the refs plus an honest truncation flag.

    ``truncated`` is True when the ``limit`` stopped the walk with more matching
    executions possibly remaining — consumers must surface it (an erasure plan built
    on a truncated enumeration is not a complete plan)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executions: tuple[SubjectExecutionRef, ...] = ()
    truncated: bool = False


def _execution_status_flags(execution: Any) -> tuple[str, bool, bool]:
    """Classify a listed execution's status against the REAL status enum.

    Returns ``(status_name, is_running, is_closed)``. Anything that is not the
    ``WorkflowExecutionStatus`` enum yields ``("UNKNOWN", False, False)`` — the
    fail-safe answer (a consumer must never treat an unknown status as closed).
    """

    from temporalio.client import WorkflowExecutionStatus

    status = getattr(execution, "status", None)
    if not isinstance(status, WorkflowExecutionStatus):
        return "UNKNOWN", False, False
    is_running = status == WorkflowExecutionStatus.RUNNING
    return status.name, is_running, not is_running


def _execution_subject_ids(execution: Any) -> tuple[str, ...] | None:
    """Read the execution's ``TypefluxSubjectIds`` set off its typed search attributes.

    Returns the ordered subject ids, or ``None`` when the row carries no READABLE set:
    missing attributes, a value that is not a real list/tuple (a bare str/bytes is NOT
    a subject list — it would char-iterate), or ANY error from the SDK's typed-attribute
    decode. The whole extraction is wrapped so one undecodable row degrades to
    ``None`` (excluded fail-safe by consumers) instead of aborting the enumeration.
    """

    try:
        typed = getattr(execution, "typed_search_attributes", None)
        if typed is None:
            return None
        from temporalio.common import SearchAttributeKey

        from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE

        value = typed.get(SearchAttributeKey.for_keyword_list(SUBJECT_IDS_SEARCH_ATTRIBUTE))
    except Exception:  # noqa: BLE001 - any per-row decode failure is fail-safe None.
        return None
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(str(item) for item in value)


async def list_executions_for_subject(
    client: Any,
    subject_id: str,
    *,
    limit: int = 1000,
) -> SubjectExecutionEnumeration:
    """Enumerate every execution the subject index attributes to ``subject_id``.

    A thin visibility query over the ``TypefluxSubjectIds`` keyword-list search
    attribute — the same ``list_workflows`` mechanism drain/runs use. THE erasure
    enumeration seam (#715): the slice-4 delete driver and the slice-5 plan both walk
    it; it performs NO mutation. Each ref carries the row's own subject set and
    enum-derived status classification (see :class:`SubjectExecutionRef`); the result
    flags an honest ``truncated`` when ``limit`` stopped the walk. The caller supplies
    a connected Temporal ``client`` (the erase op owns the connection).
    """

    query = subject_index_query(subject_id)
    refs: list[SubjectExecutionRef] = []
    truncated = False
    async for execution in client.list_workflows(query):
        if len(refs) >= limit:
            truncated = True
            break
        status_name, is_running, is_closed = _execution_status_flags(execution)
        refs.append(
            SubjectExecutionRef(
                execution_id=execution.id,
                run_id=getattr(execution, "run_id", None),
                workflow_type=execution.workflow_type,
                status=status_name,
                is_running=is_running,
                is_closed=is_closed,
                subject_ids=_execution_subject_ids(execution),
                start_time=_iso(getattr(execution, "start_time", None)),
                close_time=_iso(getattr(execution, "close_time", None)),
            )
        )
    return SubjectExecutionEnumeration(executions=tuple(refs), truncated=truncated)


class WorkflowChildExecution(BaseModel):
    """One DIRECT child execution of a parent run (#55 §9) — cross-edition DTO:
    exactly ``{workflow_id, workflow_name, status, start_time}``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    #: The child's logical workflow name, from its ``typeflux_workflow`` memo key.
    workflow_name: str | None = None
    #: The Temporal execution status enum name (RUNNING/COMPLETED/...).
    status: str | None = None
    start_time: str | None = None


class WorkflowMigrationProvenance(BaseModel):
    """Where an execution was migrated FROM (#204), read from its own memo.

    Present only on a run that a ``migrate`` operation started: ``run_id`` is the
    terminated old run's id (``typeflux_migrated_from`` memo) and ``version_key``
    is the version it ran under (``typeflux_migrated_from_version`` memo). Links
    the resubmitted run back to the run it replaced.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    version_key: str | None = None


class WorkflowRunCorrelation(BaseModel):
    """Observer-aware reproducibility record for one execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    observer: str
    reachable: bool
    #: ``TraceSummaryView.to_public_dict()`` when the observer yielded a
    #: trace — id, status, manifest hash, git sha, prompt refs, models.
    trace: dict[str, Any] | None = None
    warning: str | None = None
    #: DIRECT children of this execution (#55 §9), joined on the child memo's
    #: ``typeflux_parent_workflow_id`` key. ``[]`` = the Temporal tier answered
    #: and found none (also, without any children SCAN, when the spec references
    #: no sub-workflows — correlation still makes one describe for
    #: ``migrated_from``); ``None`` = the Temporal tier was unreachable under a
    #: composed spec (see ``warning``). The observability ``reachable`` flag is
    #: never overloaded.
    children: tuple[WorkflowChildExecution, ...] | None = None
    #: Migration provenance (#204): set when this run was started by a ``migrate``
    #: operation (its memo carries ``typeflux_migrated_from``). Read via ONE
    #: bounded describe of the execution — composed or not (the documented
    #: contract: correlation makes exactly one Temporal describe for provenance
    #: even when the spec references no sub-workflows). ``None`` (omitted) when
    #: the run was not migrated, or the describe degraded (see ``warning``); the
    #: authoritative provenance always lives in the execution memo and the
    #: migrate result.
    migrated_from: WorkflowMigrationProvenance | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


async def workflow_executions(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    limit: int = 20,
) -> WorkflowExecutionList:
    from typeflux.project.drain import _drain_query
    from typeflux.yaml.runtime import _connect_client

    # resolve_project_workflow takes the process-wide env lock through the
    # SYNC context manager — a blocking acquire that must never run on the
    # event loop (#590: one holder across a Temporal await + one loop-blocked
    # waiter = a permanent, API-wide deadlock). Resolve in a worker thread.
    resolved = await asyncio.to_thread(
        resolve_project_workflow,
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
    )
    workflow_class, _subworkflows, _activities = await asyncio.to_thread(
        create_workflow_with_subworkflows, project, resolved
    )
    async with async_project_environment_context(resolved.application):
        client = await _connect_client(resolved.spec, plugin=None)
    logical = resolved.spec.workflow.name
    current_type: str = getattr(workflow_class, "__typeflux_workflow_type__")
    # The drain query minus the running filter: every status, newest first.
    query = _drain_query(logical).replace(" AND ExecutionStatus = 'Running'", "")
    records: list[WorkflowExecutionRecord] = []
    async for execution in client.list_workflows(query):
        records.append(
            WorkflowExecutionRecord(
                execution_id=execution.id,
                run_id=getattr(execution, "run_id", None),
                workflow_type=execution.workflow_type,
                current_version=execution.workflow_type == current_type,
                status=getattr(getattr(execution, "status", None), "name", None) or "UNKNOWN",
                start_time=_iso(getattr(execution, "start_time", None)),
                close_time=_iso(getattr(execution, "close_time", None)),
            )
        )
        if len(records) >= limit:
            break
    return WorkflowExecutionList(
        logical_workflow=logical,
        current_workflow_type=current_type,
        executions=tuple(records),
    )


#: Same knob the control-plane API bounds every Temporal-tier await with (#581);
#: the correlation card's children tier degrades to ``children=None`` at the bound
#: instead of stalling the card behind a dead cluster.
_TEMPORAL_TIER_TIMEOUT_ENV = "TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"
_TEMPORAL_TIER_TIMEOUT_DEFAULT = 10.0

#: The children collected per correlation card are bounded (#55 §9): a card is a
#: summary, not a full listing — a wider fan-out truncates at this cap.
_CHILDREN_COLLECT_LIMIT = 100


async def workflow_run_correlation(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    execution_id: str,
) -> WorkflowRunCorrelation:
    # Sync resolution acquires the process-wide env lock — worker thread, never the
    # event loop (#590, same discipline as workflow_executions).
    resolved = await asyncio.to_thread(
        resolve_project_workflow,
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
    )
    observability = resolved.spec.runtime.observability
    observer: str = getattr(observability, "type", "none") or "none"
    reachable = True
    trace: dict[str, Any] | None = None
    warnings: list[str] = []
    if observer == "langfuse":

        def _fetch_trace() -> dict[str, Any] | None:
            with project_environment_context(resolved.application):
                return _langfuse_trace_summary(execution_id)

        try:
            trace = await asyncio.to_thread(_fetch_trace)
        except Exception as exc:  # noqa: BLE001 - degrade, never 500 the card.
            reachable = False
            warnings.append(f"observability backend unreachable: {exc}")
    children, migrated_from = await _direct_children_and_provenance(
        project, resolved, execution_id, warnings
    )
    return WorkflowRunCorrelation(
        execution_id=execution_id,
        observer=observer,
        reachable=reachable,
        trace=trace,
        warning="; ".join(warnings) if warnings else None,
        children=children,
        migrated_from=migrated_from,
    )


def _read_migration_provenance(memo: Any) -> WorkflowMigrationProvenance | None:
    # Provenance stamped by a migrate operation (#204): the resubmitted run's
    # memo carries the terminated old run id and its version key. Absent on every
    # run that was not migrated, so correlation omits the field.
    from_run = memo.get("typeflux_migrated_from")
    if not isinstance(from_run, str) or not from_run:
        return None
    from_version = memo.get("typeflux_migrated_from_version")
    return WorkflowMigrationProvenance(
        run_id=from_run,
        version_key=from_version if isinstance(from_version, str) and from_version else None,
    )


async def _direct_children_and_provenance(
    project: TypefluxProjectSpec,
    resolved: Any,
    execution_id: str,
    warnings: list[str],
) -> tuple[tuple[WorkflowChildExecution, ...] | None, WorkflowMigrationProvenance | None]:
    """The DIRECT children of one execution (#55 §9) plus its migration
    provenance (#204), from ONE bounded Temporal connection.

    Provenance is DECOUPLED from the children scan (#204 review): every
    correlation does one describe of the parent execution for its
    ``typeflux_migrated_from`` memo — including the common NON-composed case,
    where that single describe is the only Temporal call (the children answer
    stays ``()`` with no visibility scan). Composed specs additionally run the
    bounded newest-first per-child-type scan filtered client-side on
    ``typeflux_parent_workflow_id``.

    Degrades, never errors: a failed provenance describe omits the field and
    appends a warning; an unreachable tier under a composed spec degrades
    ``children`` to ``None`` exactly as before (non-composed keeps ``()`` —
    children never needed the call).
    """
    from typeflux.project.binding_ts import EXECUTIONS_SCAN_LIMIT
    from typeflux.yaml.runtime import _connect_client, _describe_memo

    subworkflows = await asyncio.to_thread(resolve_subworkflows_for, project, resolved)
    has_children_scan = bool(subworkflows.records)

    async def _scan() -> tuple[
        tuple[WorkflowChildExecution, ...],
        WorkflowMigrationProvenance | None,
        str | None,
    ]:
        async with async_project_environment_context(resolved.application):
            client = await _connect_client(resolved.spec, plugin=None)
        # Provenance of the parent run itself: one describe, decoupled from the
        # children scan so non-composed workflows surface it too (#204 review).
        provenance: WorkflowMigrationProvenance | None = None
        provenance_warning: str | None = None
        try:
            parent = client.get_workflow_handle(execution_id)
            provenance = _read_migration_provenance(await _describe_memo(await parent.describe()))
        except Exception as exc:  # noqa: BLE001 - degrade: omit + warn, never fail the card.
            provenance_warning = f"temporal tier unreachable for the migration provenance: {exc}"
        if not has_children_scan:
            return (), provenance, provenance_warning
        children: list[WorkflowChildExecution] = []
        # ONE scan budget across the child types (bounded best-effort, the
        # executions/drain discipline) plus a collected-children cap.
        scanned = 0
        for record in subworkflows.records.values():
            async for execution in client.list_workflows(
                f"WorkflowType = '{record.workflow_type}'"
            ):
                scanned += 1
                memo = await _describe_memo(execution)
                if memo.get("typeflux_parent_workflow_id") == execution_id:
                    name = memo.get("typeflux_workflow")
                    children.append(
                        WorkflowChildExecution(
                            workflow_id=execution.id,
                            workflow_name=name if isinstance(name, str) else None,
                            status=getattr(getattr(execution, "status", None), "name", None),
                            start_time=_iso(getattr(execution, "start_time", None)),
                        )
                    )
                    if len(children) >= _CHILDREN_COLLECT_LIMIT:
                        return tuple(children), provenance, provenance_warning
                if scanned >= EXECUTIONS_SCAN_LIMIT:
                    return tuple(children), provenance, provenance_warning
        return tuple(children), provenance, provenance_warning

    timeout = float(os.environ.get(_TEMPORAL_TIER_TIMEOUT_ENV, _TEMPORAL_TIER_TIMEOUT_DEFAULT))
    try:
        children, provenance, provenance_warning = await asyncio.wait_for(_scan(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - degrade, never 500.
        detail = f"did not answer within {timeout:g}s" if isinstance(exc, TimeoutError) else exc
        if has_children_scan:
            # Composed: the children listing genuinely needed the tier — degrade
            # children to None (unknown), exactly the pre-#204 contract.
            warnings.append(f"temporal tier unreachable for the children listing: {detail}")
            return None, None
        # Non-composed: only the provenance describe needed the tier; children
        # never did, so the honest answer stays ().
        warnings.append(f"temporal tier unreachable for the migration provenance: {detail}")
        return (), None
    if provenance_warning is not None:
        warnings.append(provenance_warning)
    return children, provenance


def _langfuse_trace_summary(execution_id: str) -> dict[str, Any] | None:
    from typeflux.observability.inspect import TraceSearchQuery, TraceSummaryView
    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    backend = LangfuseObservabilityBackend.from_env()
    # search_traces scans multiple pages for the join key; a plain
    # list_traces(limit=1) would filter workflow_id client-side over a
    # single window and miss traces in busy projects.
    page = backend.reader.search_traces(TraceSearchQuery(workflow_id=execution_id, limit=1))
    if not page.traces:
        return None
    return TraceSummaryView.from_trace(page.traces[0]).to_public_dict()


_Statuses = Literal["RUNNING", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"]


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


__all__ = [
    "WorkflowChildExecution",
    "WorkflowExecutionList",
    "WorkflowExecutionRecord",
    "WorkflowMigrationProvenance",
    "WorkflowRunCorrelation",
    "workflow_executions",
    "workflow_run_correlation",
]
