"""Long-drain migration: terminate-and-resubmit across graph versions (#204).

The supported primitive for an execution that cannot be waited out is
terminate-and-resubmit with input carry-over (D204-1): continue-as-new handoff
across graph versions is unsound (mapping in-flight interpreter state onto an
arbitrarily edited graph is the graph-identity problem #191 pinned executions
against), so migration is deliberate and provenance-stamped, and it restarts the
new run from step zero.

``migrate`` reads the running execution's original input from its start event
(no operator re-typing), refuses when the current resolved version equals the
execution's (a same-version migrate is a no-op error), requires serving workers
on the target task queue (fail-closed), and refuses an execution parked at a
review gate unless the operator acknowledges the human-state loss with
``abandon_gates``. It then terminates the old run with a canonical reason and
starts a new run against the current spec — carrying ``typeflux_migrated_from``
(the old run id) and ``typeflux_migrated_from_version`` (the old version key) in
the identity memo, so the audit trail links the pair (correlation-visible).

EVERY start-leg precondition is preflighted BEFORE the terminate (#204 review):
the carried input is decoded and validated against the current version's input
model/schema, and the frozen-``workflow.version`` check runs read-only, while
the old run is still alive — a static failure refuses the migration instead of
leaving the execution dead with no replacement. After the terminate, only two
failures remain, each with a distinguished shape: a terminate racing an
already-closed execution is :class:`MigrateExecutionClosedError` (409), and a
failed replacement start is :class:`MigratePartialError` (500-class, naming the
already-terminated run; the carried input is intact — resubmit via a normal
start).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from typeflux.core.errors import LifecycleBindingError, TypefluxError

#: Canonical prefix for the termination reason recorded on the old run; the live
#: proof and audit tooling key on it. An operator note (the ``reason`` argument)
#: is appended after a colon when supplied.
MIGRATE_TERMINATION_REASON_PREFIX = "typeflux migrate to"


class SameVersionMigrateError(TypefluxError, ValueError):
    """The execution already runs the currently-resolved version (422).

    Migrating onto the same graph version is a no-op that would needlessly
    terminate and re-run every activity, so it is refused rather than performed.
    """


class NoServingWorkersError(TypefluxError, ValueError):
    """No worker is polling the target task queue (422, fail-closed).

    Starting the new run would leave it pending forever, so migration refuses
    before terminating the old run — the operator keeps a running execution
    instead of trading it for a stuck one.
    """


class WaitingGateMigrateError(TypefluxError, ValueError):
    """The execution is parked at a review gate and ``abandon_gates`` was not
    set (422).

    A waiting gate is human state; terminate-and-resubmit drops it. The operator
    must either decide the gate first or explicitly acknowledge the loss with
    ``abandon_gates`` — migration never silently discards a pending review.
    """


class MigratedInputError(TypefluxError, ValueError):
    """The execution's carried-over input is not valid for the current version's
    input model (422).

    Read before terminating the old run, so an incompatible input refuses the
    migration rather than leaving the old run dead with an un-startable
    replacement.
    """


class MigrateExecutionClosedError(LifecycleBindingError):
    """The terminate raced an execution that is already closed (409).

    The execution completed naturally — or another migrate got there first —
    between the preflight and the terminate. Nothing was lost: no replacement
    was started, and the closed run is intact. Inspect it and re-run migrate if
    it is genuinely still an old-version straggler.
    """


class MigratePartialError(TypefluxError):
    """The old run was terminated but the replacement start failed (500-class).

    A DISTINGUISHED partial-failure signal (never the refusal 422 shape): the
    message states which run was already terminated, why the start leg failed,
    and that the carried input is intact — the operator resubmits via a normal
    start. Only a genuine transport/race error on the start leg can reach this
    (every static precondition is preflighted before the terminate).
    """


def migrate_partial_error(
    execution_id: str, old_run_id: str | None, cause: BaseException
) -> MigratePartialError:
    """The canonical partial-failure error (shared by both drivers)."""
    run = old_run_id or "<unknown>"
    return MigratePartialError(
        f"migrate partially completed: old run {run} of execution {execution_id!r} was "
        f"already terminated; the replacement start failed: {cause}. The carried input "
        "is intact in the terminated run's history — resubmit via a normal start."
    )


def is_execution_closed_error(exc: BaseException) -> bool:
    """True when a terminate failed because the execution is already closed.

    Temporal surfaces a terminate against a completed/terminated execution as an
    RPCError with status NOT_FOUND (the mutable-state lookup misses), sometimes
    phrased "workflow execution already completed". Both shapes classify as the
    409 race, never an opaque 500.
    """
    try:
        from temporalio.service import RPCError, RPCStatusCode
    except ModuleNotFoundError:  # pragma: no cover - temporalio is a core dependency.
        pass
    else:
        if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
            return True
    message = str(exc).lower()
    return "already completed" in message or "workflow not found" in message


class WorkflowMigrateResult(BaseModel):
    """Identity returned by a control-plane migrate operation.

    Links the terminated old run to the freshly-started new run and records the
    version keys either side of the migration plus the gate ids abandoned (empty
    unless the execution was parked at a gate and ``abandon_gates`` was set).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The Temporal workflow id; the new run reuses it (terminate-and-resubmit).
    execution_id: str
    old_run_id: str
    new_run_id: str | None
    #: True for a preview (#791): every preflight ran — binding, same-version, pollers,
    #: gates, input decode — and nothing was terminated or started (new_run_id is None).
    dry_run: bool = False
    #: The version key (registered workflow type) the terminated run ran under.
    old_version_key: str
    #: The version key the new run runs under (the currently-resolved spec).
    new_version_key: str
    #: Gate ids that were open on the old run and dropped by the migration;
    #: non-empty only when ``abandon_gates`` acknowledged an open gate.
    abandoned_gate_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def migrate_termination_reason(new_version_key: str, reason: str | None) -> str:
    """The reason recorded when the old run is terminated.

    Always begins with ``typeflux migrate to {new version key}`` so the audit
    record is machine-recognizable; an operator note is appended after a colon.
    """
    text = f"{MIGRATE_TERMINATION_REASON_PREFIX} {new_version_key}"
    note = (reason or "").strip()
    return f"{text}: {note}" if note else text


async def read_start_event_input(handle: Any, data_converter: Any, *, input_index: int = 0) -> Any:
    """Decode the workflow input from an execution's ``WorkflowExecutionStarted``
    event (input carry-over).

    Reads the first history event's input payloads through the client's data
    converter and returns the positional argument at ``input_index`` the workflow
    was started with. ``input_index`` is 0 for the Python versioned-type profile
    (``run(input)``) and 1 for the ts-plan-argument profile (``run(plan, input)``,
    where the plan is arg 0). Returns ``None`` when the start carried no argument
    at that index. Raises if the start event is missing — an execution with no
    start event is not one we can migrate.
    """
    from temporalio.api.enums.v1 import EventType

    async for event in handle.fetch_history_events():
        if event.event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            continue
        attributes = event.workflow_execution_started_event_attributes
        payloads = list(getattr(getattr(attributes, "input", None), "payloads", ()) or ())
        if not payloads:
            return None
        decoded = await data_converter.decode(payloads)
        return decoded[input_index] if len(decoded) > input_index else None
    raise TypefluxError(
        "cannot migrate: the execution has no WorkflowExecutionStarted event in history"
    )


__all__ = [
    "MIGRATE_TERMINATION_REASON_PREFIX",
    "MigrateExecutionClosedError",
    "MigratePartialError",
    "MigratedInputError",
    "NoServingWorkersError",
    "SameVersionMigrateError",
    "WaitingGateMigrateError",
    "WorkflowMigrateResult",
    "is_execution_closed_error",
    "migrate_partial_error",
    "migrate_termination_reason",
    "read_start_event_input",
]
