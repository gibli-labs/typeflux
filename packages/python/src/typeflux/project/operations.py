from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from typeflux.core.contracts import ReviewCommand, WorkflowLifecycleStatus
from typeflux.project.binding import (
    BindingDriver,
    PythonVersionedTypeDriver,
    driver_for_profile,
)
from typeflux.project.environment import ProjectResolvedWorkflow
from typeflux.project.migrate import WorkflowMigrateResult
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.runtime import TypefluxYamlRuntime

#: Control-plane status polling cadence floor. UI clients should poll at this
#: interval or slower (1-5s), or use user-triggered refresh; faster polling
#: only adds raw Temporal query spans without fresher lifecycle data.
RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS = 1.0


def effective_valid_user_decisions(
    status: WorkflowLifecycleStatus, resolved: Callable[[], dict[str, str]]
) -> dict[str, str]:
    """The review decisions the control plane offers for a status snapshot (#55 §6).

    Prefer the EXECUTION-reported set — the running workflow's own
    ``waiting_gates[].valid_user_decisions`` — over the resolved-spec set: an old
    (drifted) execution pins the plan it started with, which an edited spec no
    longer reflects. When the execution reports >=1 waiting gate, union their
    decisions (sorted, stable wire order); otherwise fall back to the resolved spec.
    ``resolved`` is a THUNK so the fallback is computed only when actually needed —
    never on the waiting-gate hot path. For a single-gate workflow the two coincide,
    so the wire is byte-identical.
    """
    if status.waiting_gates:
        merged: dict[str, str] = {}
        for gate in status.waiting_gates:
            merged.update(gate.valid_user_decisions)
        return {decision: merged[decision] for decision in sorted(merged)}
    return resolved()


class WorkflowStartReceipt(BaseModel):
    """Identity returned by a control-plane workflow start."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    run_id: str | None
    workflow_name: str
    workflow_type: str
    spec_digest: str
    task_queue: str
    #: Keyword arguments for ``TraceListQuery`` to find the execution's traces.
    trace_query_hint: dict[str, Any]


class RuntimePinInfo(BaseModel):
    """Which resolved runtime version the mutating operations are bound to.

    Operations pin the resolved runtime at first use and reuse it until ``serve``
    restarts (or an explicit repin), while read views re-resolve per request — so
    an operator could otherwise see a new manifest in reads while start/review/
    cancel still run against the stale pinned runtime, with no indication (#324).
    The control plane fills this in; the operations layer leaves it unset.

    This reports the *identity* of the pinned runtime (when it was pinned, and
    its workflow-graph spec digest) for visibility; it deliberately does not
    assert a freshness verdict, because ``spec_digest`` covers only the graph —
    a runtime-config edit (task queue, Temporal profile, policy) would not change
    it, so a digest comparison could falsely report "fresh". To refresh the pin,
    use the repin operation rather than inferring staleness from this digest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Workflow-graph spec digest the pinned runtime was resolved from. Identity
    #: only — not a full resolved-config hash, so do not treat equality as proof
    #: the pinned runtime is current (see the class docstring).
    spec_digest: str | None = None
    #: ISO-8601 UTC time the runtime was pinned (first use of this selection).
    pinned_at: str | None = None


class WorkflowOperationStatus(BaseModel):
    """Lifecycle snapshot plus the review decisions valid for this version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    run_id: str | None
    status: WorkflowLifecycleStatus
    #: Valid review decision -> route target step for the resolved version.
    valid_user_decisions: dict[str, str]
    recommended_poll_interval_seconds: float = RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS
    #: Which pinned runtime version mutating ops are bound to (control-plane
    #: fills this; None when served outside the pinned-operations control plane).
    runtime_pin: RuntimePinInfo | None = None


class WorkflowOperations:
    """Control-plane operations over one resolved project workflow.

    A facade over the selected :class:`BindingDriver` (#618): the Temporal
    binding contract describes the per-edition execution ABIs, and each
    driver implements the operate tier per its profile. Wraps the driver in
    UI-safe DTOs. These are control-plane actions: they never enter workflow
    execution manifests, and reviewer identity, freeform notes, and
    cancellation reasons stay out of ``typeflux.*`` metadata (enforced by
    the wrapped helpers). Those fields are still sent as Temporal signal
    payloads, though — they persist in workflow history and the cancel
    reason is returned to ``inspect`` callers via status, so they are not
    client-side (#325). For the in-process Python driver the underlying
    runtime stays reachable via ``ops.runtime`` for advanced callers that
    want raw Temporal handles.
    """

    def __init__(
        self,
        *,
        runtime: TypefluxYamlRuntime | None = None,
        resolved: ProjectResolvedWorkflow | None = None,
        driver: BindingDriver | None = None,
    ) -> None:
        if driver is None:
            if runtime is None:
                raise ValueError("WorkflowOperations needs a driver or an in-process runtime")
            driver = PythonVersionedTypeDriver(runtime=runtime, resolved=resolved)
        self.driver = driver
        self.resolved = resolved if resolved is not None else driver.resolved

    @property
    def runtime(self) -> TypefluxYamlRuntime:
        """The in-process runtime, where the driver has one.

        Only the ``python-versioned-type`` driver carries an in-process
        runtime; other profiles fail closed here rather than answering with
        a wrong-edition handle.
        """
        runtime = getattr(self.driver, "runtime", None)
        if runtime is None:
            raise AttributeError(
                f"the {type(self.driver).__name__} binding driver exposes no in-process runtime"
            )
        return runtime

    @classmethod
    async def for_project_workflow(
        cls,
        project: TypefluxProjectSpec,
        *,
        workflow_id: str,
        environment_id: str,
        policy_ids: tuple[str, ...] = (),
        expected_policy_hash: str | None = None,
        binding_profile: str = "python-versioned-type",
        resolver: Any = None,
    ) -> WorkflowOperations:
        """Build operations for one workflow/environment selection.

        ``binding_profile`` selects the driver (the registry's ``runtime``
        field maps to a profile, #619); the default is this edition's own
        profile. Resolution/policy behavior is the selected driver's — the
        Python driver mirrors the ``project submit`` construction and fails
        closed on policy drift exactly like workers and submitters.
        ``resolver`` is the routed runtime's contract resolver when the server
        holds one (#642): the plan-argument driver starts and honors policy
        selections through it; ``None`` keeps both fail-closed.
        """
        driver = await driver_for_profile(
            binding_profile,
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=policy_ids,
            expected_policy_hash=expected_policy_hash,
            resolver=resolver,
        )
        return cls(driver=driver)

    async def start(
        self,
        input_value: Any,
        *,
        workflow_id: str,
        task_queue: str | None = None,
        **start_kwargs: Any,
    ) -> WorkflowStartReceipt:
        return await self.driver.start(
            input_value, workflow_id=workflow_id, task_queue=task_queue, **start_kwargs
        )

    async def status(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
        trace: bool = False,
    ) -> WorkflowOperationStatus:
        """Query lifecycle status plus the valid review decisions.

        Polling defaults to untraced so UI refresh loops do not flood the
        audit trail; pass ``trace=True`` for a deliberate, auditable check.
        """
        return await self.driver.status(workflow_id, run_id=run_id, trace=trace)

    def valid_user_decisions(self) -> dict[str, str]:
        return self.driver.valid_user_decisions()

    async def submit_review(
        self,
        workflow_id: str,
        command: ReviewCommand | dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> None:
        await self.driver.submit_review(workflow_id, command, run_id=run_id)

    async def request_cancel(
        self,
        workflow_id: str,
        reason: str | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        await self.driver.request_cancel(workflow_id, reason, run_id=run_id)

    async def migrate(
        self,
        execution_id: str,
        *,
        run_id: str | None = None,
        abandon_gates: bool = False,
        reason: str | None = None,
        dry_run: bool = False,
    ) -> WorkflowMigrateResult:
        """Terminate a running execution and resubmit it against this version.

        The supported long-drain primitive (#204): terminate-and-resubmit with
        input carry-over. Refuses a same-version migrate, a target queue with no
        serving workers, and (unless ``abandon_gates``) an execution parked at a
        review gate; stamps ``typeflux_migrated_from`` provenance on the new run.
        """
        return await self.driver.migrate(
            execution_id,
            run_id=run_id,
            abandon_gates=abandon_gates,
            reason=reason,
            dry_run=dry_run,
        )

    def shutdown(self) -> None:
        self.driver.shutdown()


__all__ = [
    "RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS",
    "RuntimePinInfo",
    "WorkflowMigrateResult",
    "WorkflowOperationStatus",
    "WorkflowOperations",
    "WorkflowStartReceipt",
]
