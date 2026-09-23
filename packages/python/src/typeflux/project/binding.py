"""Binding drivers: the operate tier per Temporal binding profile (#618).

The temporal-binding contract (``contracts/temporal-binding/binding.v1.json``)
describes the two deliberately divergent execution ABIs as profiles; a
control plane operates a project by selecting the driver for its profile.
``PythonVersionedTypeDriver`` is the extraction of the pre-existing
``WorkflowOperations`` behavior, byte-identical. The ``ts-plan-argument``
driver arrives with the epic's slice 4 — selecting it today fails closed
with a structured pointer error, never garbage.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ValidationError

from typeflux.core.contracts import ReviewCommand, WorkflowLifecycleStatus
from typeflux.core.errors import LifecycleBindingError, TypefluxError
from typeflux.project.environment import (
    ProjectResolvedWorkflow,
    async_project_environment_context,
    resolve_project_workflow,
    resolve_subworkflows_for,
    run_while_env_lock_held,
)
from typeflux.project.migrate import MigratedInputError
from typeflux.project.policy_enforcement import (
    ProjectPolicyEnforcementError,
    build_project_policy_runtime_guard,
)
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.runtime import (
    TypefluxYamlRuntime,
    build_runtime,
    prepare_runtime_build,
)

if TYPE_CHECKING:
    from typeflux.project.migrate import WorkflowMigrateResult
    from typeflux.project.operations import (
        WorkflowOperationStatus,
        WorkflowStartReceipt,
    )

#: Registry ``runtime`` → binding profile (the contract's profile names).
BINDING_PROFILE_FOR_RUNTIME: dict[str, str] = {
    "python": "python-versioned-type",
    "typescript": "ts-plan-argument",
}


class UnsupportedBindingProfileError(TypefluxError):
    """This control plane has no driver for the selected binding profile."""


class PolicyGuardUnavailableError(TypefluxError, ValueError):
    """The request asked for policy enforcement a plan-less driver cannot
    compose (needs resolution) — a 422-class client error, not a server
    fault."""


@runtime_checkable
class BindingDriver(Protocol):
    """The operate-tier surface a control plane consumes, per profile.

    Binding verification (the type/memo identity check before lifecycle
    dispatch, 409 on mismatch) is each driver's obligation per its profile's
    rules — see the temporal-binding contract.
    """

    resolved: ProjectResolvedWorkflow | None

    async def start(
        self,
        input_value: Any,
        *,
        workflow_id: str,
        task_queue: str | None = None,
        **start_kwargs: Any,
    ) -> WorkflowStartReceipt: ...

    async def status(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
        trace: bool = False,
    ) -> WorkflowOperationStatus: ...

    def valid_user_decisions(self) -> dict[str, str]: ...

    async def submit_review(
        self,
        workflow_id: str,
        command: ReviewCommand | dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> None: ...

    async def request_cancel(
        self,
        workflow_id: str,
        reason: str | None = None,
        *,
        run_id: str | None = None,
    ) -> None: ...

    async def migrate(
        self,
        execution_id: str,
        *,
        run_id: str | None = None,
        abandon_gates: bool = False,
        reason: str | None = None,
        dry_run: bool = False,
    ) -> WorkflowMigrateResult: ...

    def shutdown(self) -> None: ...


class PythonVersionedTypeDriver:
    """The ``python-versioned-type`` binding: a pinned in-process runtime.

    Identity and frozen-version live in the registered workflow type; the
    runtime's lifecycle helpers perform the binding verification (#320).
    """

    profile = "python-versioned-type"

    def __init__(
        self,
        *,
        runtime: TypefluxYamlRuntime,
        resolved: ProjectResolvedWorkflow | None = None,
    ) -> None:
        self.runtime = runtime
        self.resolved = resolved

    @classmethod
    async def for_project_workflow(
        cls,
        project: TypefluxProjectSpec,
        *,
        workflow_id: str,
        environment_id: str,
        policy_ids: tuple[str, ...] = (),
        expected_policy_hash: str | None = None,
    ) -> PythonVersionedTypeDriver:
        """Build the pinned runtime for one workflow/environment selection.

        Mirrors the ``project submit`` construction: resolve, apply the
        environment profile, compose the policy guard, and verify the
        expected policy hash before any Temporal connection — control-plane
        operations fail closed on policy drift exactly like workers and
        submitters.
        """
        # The sync env-lock acquire inside resolve_project_workflow must never
        # run on the event loop (#590) — resolve in a worker thread.
        resolved = await asyncio.to_thread(
            resolve_project_workflow,
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
        )
        # Sub-workflows (#55 §3.4): resolve the parent's references BEFORE entering
        # the async env context (resolution acquires the sync env lock internally per
        # child — never while this coroutine holds the async context) so the pinned
        # runtime co-registers the transitive child classes + activities; operating a
        # `workflow:`-step workflow through the driver must not hit the standalone
        # rejection. Same discipline as runs.py/drain.py.
        subworkflows = await asyncio.to_thread(resolve_subworkflows_for, project, resolved)
        async with async_project_environment_context(resolved.application):
            policy_guard = build_project_policy_runtime_guard(
                project=project,
                resolved=resolved,
                policy_ids=policy_ids,
                enforcement_mode="project_submit",
            )
            _verify_expected_policy_hash(
                expected_policy_hash,
                policy_hash=(None if policy_guard is None else policy_guard.policy.policy_hash),
            )
            # The build's sync prelude (module imports, observability/provider
            # client construction) is ~1s of blocking work that must not run
            # on the event loop (#585). It runs on the env-lock holder's
            # dedicated worker, not asyncio.to_thread: the default executor
            # can be full of threads blocked acquiring this very lock, and
            # queueing the holder's work behind them would deadlock.
            prepared = await run_while_env_lock_held(
                prepare_runtime_build,
                resolved.spec,
                policy_guard=policy_guard,
                subworkflow_records=subworkflows.records,
                child_workflow_classes=subworkflows.workflow_classes,
                child_activities=subworkflows.activities,
                child_registry_specs=tuple(subworkflows.child_specs.items()),
            )
            runtime = await build_runtime(
                resolved.spec, policy_guard=policy_guard, prepared=prepared
            )
        return cls(runtime=runtime, resolved=resolved)

    async def start(
        self,
        input_value: Any,
        *,
        workflow_id: str,
        task_queue: str | None = None,
        **start_kwargs: Any,
    ) -> WorkflowStartReceipt:
        from typeflux.project.operations import WorkflowStartReceipt

        handle = await self.runtime.start_workflow(
            input_value,
            id=workflow_id,
            task_queue=task_queue,
            **start_kwargs,
        )
        run_id = getattr(handle, "run_id", None)
        workflow_cls = self.runtime.workflow_class
        return WorkflowStartReceipt(
            workflow_id=workflow_id,
            run_id=run_id if isinstance(run_id, str) else None,
            workflow_name=self.runtime.spec.workflow.name,
            workflow_type=getattr(workflow_cls, "__typeflux_workflow_type__"),
            spec_digest=getattr(workflow_cls, "__typeflux_spec_digest__"),
            task_queue=task_queue or self.runtime.spec.task_queue,
            trace_query_hint={"workflow_id": workflow_id, "limit": 1},
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
        from typeflux.project.operations import (
            WorkflowOperationStatus,
            effective_valid_user_decisions,
        )

        status = await self.runtime.query_lifecycle_status(
            workflow_id,
            run_id=run_id,
            trace=trace,
        )
        return WorkflowOperationStatus(
            workflow_id=workflow_id,
            run_id=run_id,
            status=status,
            # Prefer the running execution's own waiting-gate decisions over the resolved spec
            # (#55 §6 drift caveat); identical for single-gate workflows.
            valid_user_decisions=effective_valid_user_decisions(status, self.valid_user_decisions),
        )

    def valid_user_decisions(self) -> dict[str, str]:
        # The resolved-spec fallback (used when the execution reports no waiting gate): the
        # single review's decisions, or the union over all `gates` (#55 slice 4), sorted.
        lifecycle = self.runtime.spec.workflow.lifecycle
        if lifecycle is None:
            return {}
        merged: dict[str, str] = {}
        for gate in lifecycle.resolved_gates():
            for decision, route in gate.user_decisions.items():
                merged[decision] = route.route
        return {decision: merged[decision] for decision in sorted(merged)}

    async def submit_review(
        self,
        workflow_id: str,
        command: ReviewCommand | dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> None:
        await self.runtime.submit_lifecycle_review(workflow_id, command, run_id=run_id)

    async def request_cancel(
        self,
        workflow_id: str,
        reason: str | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        await self.runtime.request_lifecycle_cancel(workflow_id, reason, run_id=run_id)

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

        ``dry_run`` (#791) runs every preflight below — binding, same-version,
        pollers, gates, input decode — and returns the preview instead of
        terminating: the identical code path with one early return, so the
        preview can never drift from the real operation.

        This driver is pinned to the CURRENT resolved version; the execution
        being migrated runs an OLDER versioned type, so — unlike start/status/
        review/cancel — the handle is NOT bound through
        ``_verify_execution_binding`` (which requires the execution's type to
        equal the pinned type; a cross-version migrate never would). Instead the
        old execution is verified to belong to the same project AND the same
        logical workflow, and its running type differs (the same-version guard).
        Frozen-version enforcement already ran when this runtime was built, so
        the start leg reuses the normal identity-memo path (#204).
        """
        from typeflux.project.migrate import (
            MigrateExecutionClosedError,
            NoServingWorkersError,
            SameVersionMigrateError,
            WaitingGateMigrateError,
            WorkflowMigrateResult,
            is_execution_closed_error,
            migrate_partial_error,
            migrate_termination_reason,
            read_start_event_input,
        )
        from typeflux.project.workers import _poller_count
        from typeflux.yaml.runtime import _coerce_lifecycle_status, _describe_memo

        client = self.runtime.client
        workflow_class = self.runtime.workflow_class
        new_version_key = getattr(workflow_class, "__typeflux_workflow_type__")
        expected_project = getattr(workflow_class, "__typeflux_project__", None)
        expected_logical = self.runtime.spec.workflow.name

        handle = client.get_workflow_handle(execution_id, run_id=run_id)
        description = await handle.describe()
        old_version_key = getattr(description, "workflow_type", None)
        memo = await _describe_memo(description)
        actual_project = memo.get("typeflux_project")
        actual_logical = memo.get("typeflux_workflow")
        if actual_project != expected_project:
            raise LifecycleBindingError(
                f"migrate refused: execution {execution_id!r} belongs to project "
                f"{actual_project!r}, not the bound project {expected_project!r}"
            )
        if actual_logical != expected_logical:
            raise LifecycleBindingError(
                f"migrate refused: execution {execution_id!r} runs logical workflow "
                f"{actual_logical!r}, not the bound workflow {expected_logical!r}"
            )
        described_run_id = getattr(description, "run_id", None)
        if run_id is None and isinstance(described_run_id, str):
            # Pin to the run describe() verified so a workflow-id reuse cannot
            # swap the target between verification and terminate/start.
            handle = client.get_workflow_handle(execution_id, run_id=described_run_id)
        old_run_id = described_run_id if isinstance(described_run_id, str) else run_id
        if not isinstance(old_version_key, str):
            raise LifecycleBindingError(
                f"migrate refused: execution {execution_id!r} has no readable workflow type"
            )
        if old_version_key == new_version_key:
            raise SameVersionMigrateError(
                f"migrate refused: execution {execution_id!r} already runs the current "
                f"version {new_version_key!r}; migrating onto the same graph version is a "
                "no-op — nothing to migrate to"
            )

        # Fail-closed BEFORE terminating: the new run targets the current
        # version's task queue, and starting it onto a queue no worker polls
        # would trade a running execution for a permanently pending one.
        target_queue = self.runtime.spec.task_queue
        namespace = self.runtime.spec.runtime.temporal.namespace
        pollers = await _poller_count(client, namespace, target_queue)
        if pollers <= 0:
            raise NoServingWorkersError(
                f"migrate refused: no workers are polling the target task queue "
                f"{target_queue!r} for version {new_version_key!r}; deploy the new "
                "version's workers before migrating (the new run would otherwise sit "
                "pending forever)"
            )

        status = _coerce_lifecycle_status(
            await handle.query(
                "typeflux_lifecycle_status",
                result_type=WorkflowLifecycleStatus,
            )
        )
        abandoned_gate_ids: tuple[str, ...] = ()
        if status.waiting_gates:
            gate_ids = tuple(gate.gate_id for gate in status.waiting_gates)
            if not abandon_gates:
                raise WaitingGateMigrateError(
                    f"migrate refused: execution {execution_id!r} is waiting at review "
                    f"gate(s) {', '.join(gate_ids)}; deciding the gate first preserves the "
                    "human decision, or pass abandon_gates to acknowledge that terminate-"
                    "and-resubmit discards the pending review"
                )
            abandoned_gate_ids = gate_ids

        # Input carry-over: read AND validate the original input BEFORE
        # terminating, so an undecodable/incompatible input never leaves the old
        # run dead with no replacement. (This driver's frozen-version check ran
        # when the pinned runtime was built, so no further start-leg
        # precondition remains — after the terminate below, only a genuine
        # transport/race error can fail.)
        raw_input = await read_start_event_input(handle, client.data_converter)
        input_value = self._coerce_migrated_input(raw_input, execution_id=execution_id)

        if dry_run:
            # Preview (#791): every preflight above ran; stop before the mutation.
            return WorkflowMigrateResult(
                execution_id=execution_id,
                old_run_id=old_run_id if isinstance(old_run_id, str) else "",
                new_run_id=None,
                old_version_key=old_version_key,
                new_version_key=new_version_key,
                abandoned_gate_ids=abandoned_gate_ids,
                dry_run=True,
            )

        try:
            await handle.terminate(migrate_termination_reason(new_version_key, reason))
        except Exception as exc:
            if is_execution_closed_error(exc):
                # The race, not a fault: the execution closed (completed, or a
                # concurrent migrate terminated it) between preflight and
                # terminate. Nothing was started; fail with the 409 shape.
                raise MigrateExecutionClosedError(
                    f"migrate conflict: execution {execution_id!r} (run "
                    f"{old_run_id or '<unknown>'}) is already closed — it may have "
                    "completed or been migrated concurrently; nothing was terminated "
                    "or started"
                ) from exc
            raise

        provenance_memo: dict[str, Any] = {"typeflux_migrated_from_version": old_version_key}
        if isinstance(old_run_id, str):
            provenance_memo["typeflux_migrated_from"] = old_run_id
        try:
            new_handle = await self.runtime.start_workflow(
                input_value,
                id=execution_id,
                task_queue=None,
                memo=provenance_memo,
            )
        except Exception as exc:
            # The old run is gone and the replacement did not start: surface the
            # DISTINGUISHED partial-failure shape, never a refusal 422 (#204
            # review). Every static precondition was preflighted above, so this
            # is a genuine transport/race error.
            raise migrate_partial_error(execution_id, old_run_id, exc) from exc
        new_run_id = getattr(new_handle, "run_id", None)
        return WorkflowMigrateResult(
            execution_id=execution_id,
            old_run_id=old_run_id if isinstance(old_run_id, str) else "",
            new_run_id=new_run_id if isinstance(new_run_id, str) else None,
            old_version_key=old_version_key,
            new_version_key=new_version_key,
            abandoned_gate_ids=abandoned_gate_ids,
        )

    def _coerce_migrated_input(self, raw_input: Any, *, execution_id: str) -> Any:
        # Validate the carried-over input against the CURRENT version's input
        # model, failing fast (422) rather than dispatching a new run that would
        # only fail on the worker. Mirrors the control-plane start coercion.
        run = getattr(self.runtime.workflow_class, "run")
        input_model = run.__annotations__.get("input_value")
        if input_model is None or not hasattr(input_model, "model_validate"):
            return raw_input
        try:
            return input_model.model_validate(raw_input)
        except ValidationError as exc:
            raise MigratedInputError(
                f"migrate refused: execution {execution_id!r} original input is not valid "
                f"for the current version's input model {input_model.__name__}: {exc}"
            ) from exc

    def shutdown(self) -> None:
        self.runtime.observability.writer.shutdown()


async def driver_for_profile(
    profile: str,
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    policy_ids: tuple[str, ...] = (),
    expected_policy_hash: str | None = None,
    resolver: Any = None,
) -> BindingDriver:
    """Build the binding driver for one profile, failing closed otherwise.

    ``resolver`` is the routed runtime's contract resolver when the control
    plane holds one (#642). The ts-plan-argument driver needs it to start
    (``resolve_plan``) and to honor a policy selection
    (``resolve_bundle``/``validate_project`` — the CP-admission half of the
    #663 split; per-call enforcement stays the worker's); ``None`` keeps the
    historical fail-closed behavior.
    """
    if profile == "python-versioned-type":
        return await PythonVersionedTypeDriver.for_project_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=policy_ids,
            expected_policy_hash=expected_policy_hash,
        )
    if profile == "ts-plan-argument":
        # Selection is pure manifest logic (this edition owns it): explicit
        # ids win, else the project's validation.targets select — the SAME
        # default-enforcement rule both editions' own operate tiers apply
        # (#663). Only the composition/admission needs the foreign resolver.
        from typeflux.project.policy_enforcement import (
            select_project_policy_ids_for_workflow,
        )

        selected = select_project_policy_ids_for_workflow(
            project,
            environment_id=environment_id,
            workflow_id=workflow_id,
            explicit_policy_ids=policy_ids,
        )
        if resolver is not None:
            # #788: verify whenever a resolver is configured — the TS resolver's
            # validate emits the risk_tier_binding verdict for an unbound-elevated
            # workflow, which the verdict filters honor. For an UNBOUND selection a
            # resolver-transport failure degrades to unverified (the pre-#788 shape:
            # e.g. a schema-fetch failure must keep surfacing as start's own 422, and
            # the TS worker's guard build remains the backstop) — a genuine binding
            # verdict arrives as a FAILED CHECK on a successful call and still raises.
            # A project that SELECTS policies keeps the strict behavior: any failure
            # here refuses.
            try:
                verified_bundle = await _verify_ts_policy_via_resolver(
                    resolver,
                    project,
                    workflow_id=workflow_id,
                    environment_id=environment_id,
                    policy_ids=policy_ids,
                    expected_policy_hash=expected_policy_hash,
                )
            except ProjectPolicyEnforcementError as exc:
                # For an UNBOUND selection only the #788 binding verdict may block —
                # an unrelated project-level policy defect (a broken UNUSED policy or
                # target elsewhere) must not stop an ungoverned start it never governed.
                if selected or expected_policy_hash is not None or "risk_tier_binding" in str(exc):
                    raise
                verified_bundle = None
            except Exception:
                if selected or expected_policy_hash is not None:
                    raise
                verified_bundle = None
        elif selected or expected_policy_hash is not None:
            # Policy-guard composition needs resolution; silently skipping
            # enforcement would be worse than refusing (#618 slice 4) —
            # and a project whose targets DECLARE policies must never
            # operate ungoverned just because no resolver is configured.
            # 422-class: the caller/operator can fix the configuration.
            raise PolicyGuardUnavailableError(
                "policy-guarded operations are not available for "
                "ts-plan-argument executions on this server: this "
                "workflow selects project policies "
                f"({', '.join(selected) or 'via expected_policy_hash'}) "
                "and policy enforcement requires resolution — configure "
                "the typescript subprocess resolver (#642)"
            )
        else:
            verified_bundle = None
        from typeflux.project.binding_ts import TsPlanArgumentDriver

        return await TsPlanArgumentDriver.for_project_workflow(
            project.manifest_path,
            workflow_id=workflow_id,
            environment_id=environment_id,
            resolver=resolver,
            policy_ids=policy_ids,
            prefetched_bundle=verified_bundle,
        )
    raise UnsupportedBindingProfileError(
        f"unknown binding profile {profile!r} "
        f"(known: {', '.join(sorted((*BINDING_PROFILE_FOR_RUNTIME.values(),)))})"
    )


async def _verify_ts_policy_via_resolver(
    resolver: Any,
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    policy_ids: tuple[str, ...],
    expected_policy_hash: str | None,
) -> Any:
    """Honor a ts-plan-argument policy selection through the resolver (#642).

    Returns the verified bundle so the driver pin reuses it (the input JSON
    Schema, #673): one subprocess round-trip, and the pinned schema is the one
    resolved UNDER the admitted policy selection.

    The CP-admission half of the #663 split, without reconstructing Python
    spec objects from a foreign edition: the resolver's ``resolve_bundle``
    composes the selection (its ``policy.policy_hash`` is the closure hash the
    expected-hash check pins — reusing ``_verify_expected_policy_hash`` for
    message parity), and ``validate_project`` runs the resolved compliance
    checks — any FAILED check refuses the operation, fail closed. Per-call
    enforcement remains the worker's half, exactly as in the TS edition's own
    operate tier.
    """
    manifest_path = str(project.manifest_path)
    # The resolver protocol is sync (the subprocess client blocks on the
    # pipe); never run it on the event loop.
    bundle = await asyncio.to_thread(
        lambda: resolver.resolve_bundle(
            manifest_path,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=policy_ids,
        )
    )
    policy = getattr(bundle, "policy", None)
    _verify_expected_policy_hash(
        expected_policy_hash,
        policy_hash=None if policy is None else policy.policy_hash,
    )
    report = await asyncio.to_thread(
        lambda: resolver.validate_project(
            manifest_path,
            environment_id=environment_id,
            workflow_ids=(workflow_id,),
            policy_ids=policy_ids,
        )
    )
    # A policy selection was made, so the report must POSITIVELY prove
    # admission — fail closed on anything less:
    #   1. a top-level policy-scoped issue (unknown_validation_policy, a
    #      missing policy source, a broken composition) means the selection
    #      itself is invalid — proceeding would silently drop it;
    #   2. NO resolved workflow means validation bailed before the compliance
    #      checks ran (e.g. a manifest defect) — admission is unprovable;
    #   3. a failed POLICY-coded check (policy_allowlists/policy_secrets/…)
    #      is a real violation. A failed graph/import check is a project
    #      defect, not a policy violation — it surfaces with its real error
    #      at resolve_plan/start, and mislabeling it "policy enforcement
    #      failed" would send operators to the wrong config.
    policy_issues = [
        issue
        for issue in report.issues
        if "policy" in issue.code or "risk_tier_binding" in issue.code
    ]
    if policy_issues:
        parts = "; ".join(f"{issue.code}: {issue.message}" for issue in policy_issues)
        raise ProjectPolicyEnforcementError(f"project policy enforcement failed: {parts}")
    if not report.resolved_workflows:
        detail = "; ".join(f"{issue.code}: {issue.message}" for issue in report.issues)
        raise ProjectPolicyEnforcementError(
            "project policy enforcement failed: validation produced no resolved "
            "workflow to admit the policy selection against" + (f" ({detail})" if detail else "")
        )
    failures = [
        check
        for resolved in report.resolved_workflows
        for check in resolved.checks
        if (
            check.status == "failed"
            and (check.code.startswith("policy") or check.code == "risk_tier_binding")
        )
        # #788: validation SOFTENS a tier bound only in other environments to
        # `skipped` (multi-env validate must not fail prod-only workflows) — but this
        # verifier IS the guard before a control-plane start in THIS environment, so
        # the softened verdict refuses here exactly like the Python-runtime guard.
        or (
            check.status == "skipped"
            and check.code == "risk_tier_binding"
            and "bound only in other environments" in (check.message or "")
        )
    ]
    if failures:
        parts = "; ".join(
            f"{check.code}: {check.message or f'policy check failed: {check.code}'}"
            for check in failures
        )
        raise ProjectPolicyEnforcementError(
            f"project policy enforcement failed: {parts}", checks=tuple(failures)
        )
    return bundle


def _verify_expected_policy_hash(
    expected_policy_hash: str | None,
    *,
    policy_hash: str | None,
) -> None:
    if expected_policy_hash is None:
        return
    if policy_hash is None:
        raise ProjectPolicyEnforcementError(
            "expected project policy hash was provided, but no project policy was selected"
        )
    if policy_hash != expected_policy_hash:
        raise ProjectPolicyEnforcementError(
            "selected project policy hash does not match expected deployment policy hash "
            f"(expected={expected_policy_hash}, actual={policy_hash})"
        )


__all__ = [
    "BINDING_PROFILE_FOR_RUNTIME",
    "BindingDriver",
    "PolicyGuardUnavailableError",
    "PythonVersionedTypeDriver",
    "UnsupportedBindingProfileError",
    "driver_for_profile",
]
