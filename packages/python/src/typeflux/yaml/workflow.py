from __future__ import annotations

import asyncio
import json
import re
import types
from collections import deque
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Union, cast, get_args, get_origin

from pydantic import BaseModel

from typeflux.core.contracts import (
    AIActivity,
    CachedSessionHandle,
    MapActivityContext,
    ReviewCommand,
    SessionCacheConfig,
    WaitingGate,
    WorkflowLifecycleEvent,
    WorkflowLifecycleStatus,
    YamlWorkflowActivity,
)
from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE
from typeflux.execution.session_cache import (
    cache_prep_activity_name,
    cache_release_activity_name,
)
from typeflux.yaml.identity import registered_workflow_type, workflow_spec_digest
from typeflux.yaml.imports import import_type_ref
from typeflux.yaml.spec import (
    TypefluxYamlSpec,
    WhenAllSpec,
    WhenAnySpec,
    WhenLeafSpec,
    WhenSpec,
    WorkflowAnyStepSpec,
    WorkflowLifecycleGateSpec,
    WorkflowLifecycleSpec,
    WorkflowMapStepSpec,
    WorkflowParallelStepSpec,
    WorkflowSubworkflowStepSpec,
)

DEFAULT_START_TO_CLOSE_TIMEOUT = timedelta(minutes=2)
DEFAULT_ACTIVITY_RETRY_MAXIMUM_ATTEMPTS = 5
YAML_WORKFLOW_MODULE = __name__


@dataclass(frozen=True)
class WhenLeaf:
    """One normalized leaf predicate of a ``when:`` gate: compare the context value
    at ``path`` against ``value`` with ``op`` (TS ``PlanWhenLeaf`` parity)."""

    path: str
    op: str
    value: Any


@dataclass(frozen=True)
class WhenGate:
    """A normalized ``when:`` gate (#55 §3.2): one leaf, or exactly one ``all``/``any``
    composition level over leaves (decision D1). Pure data, so it folds into the
    workflow digest; V1 specs never carry one (TS ``PlanWhen`` parity)."""

    mode: str  # "leaf" | "all" | "any"
    predicates: tuple[WhenLeaf, ...]


def _when_leaf_from_spec(leaf: WhenLeafSpec) -> WhenLeaf:
    op, value = leaf.operator()
    return WhenLeaf(path=leaf.path, op=op, value=value)


def when_gate_from_spec(when: WhenSpec) -> WhenGate:
    """Normalize a spec ``when:`` into the ``{mode, predicates}`` shape (TS
    ``planWhenFromSpec`` parity)."""
    if isinstance(when, WhenAllSpec):
        return WhenGate(
            mode="all", predicates=tuple(_when_leaf_from_spec(leaf) for leaf in when.all)
        )
    if isinstance(when, WhenAnySpec):
        return WhenGate(
            mode="any", predicates=tuple(_when_leaf_from_spec(leaf) for leaf in when.any)
        )
    if not isinstance(when, WhenLeafSpec):
        # The predicate stub never validates; defend against a hand-built spec.
        raise ValueError("when.predicate (named injected predicates) is not supported (#55)")
    return WhenGate(mode="leaf", predicates=(_when_leaf_from_spec(when),))


def _literals_equal(resolved: Any, literal: Any) -> bool:
    # JSON equality semantics (TS `===` parity): a boolean equals only a boolean —
    # Python's `True == 1` must not read as a match.
    if isinstance(resolved, bool) != isinstance(literal, bool):
        return False
    return bool(resolved == literal)


def _is_orderable(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _resolve_when_path(context: Mapping[str, Any], path: str) -> Any:
    """Resolve a ``when:`` predicate path against recorded context. Stricter than
    the shared ``_resolve_context_path``: a Pydantic model segment resolves only
    DECLARED fields — TS's ``resolveContextPath`` reads plain wire objects with
    ``Object.hasOwn``, so a Python model attribute that is not a data field
    (``copy``, ``model_dump``, …) must not resolve here either (``exists`` would
    otherwise answer true for names that are code, not data)."""
    current: Any = context
    for part in path.split("."):
        if isinstance(current, BaseModel):
            if part not in type(current).model_fields:
                raise KeyError(part)
            current = getattr(current, part)
        elif isinstance(current, Mapping):
            current = current[part]
        else:
            raise TypeError(
                f"cannot resolve workflow path {path!r} at {part!r}: "
                f"{type(current).__name__} is not traversable"
            )
    return current


def _evaluate_when_leaf(leaf: WhenLeaf, context: Mapping[str, Any]) -> bool:
    """Evaluate one leaf against recorded context. Comparisons are strict and loud
    (TS ``evaluateWhenLeaf`` parity): an unresolvable path under a non-``exists``
    operator raises, and an ordering comparison over mismatched or unorderable types
    raises rather than silently gating false."""
    if leaf.op == "exists":
        exists = True
        try:
            _resolve_when_path(context, leaf.path)
        except (KeyError, TypeError):
            exists = False
        return exists == (leaf.value is True)
    resolved = _resolve_when_path(context, leaf.path)
    if leaf.op == "eq":
        return _literals_equal(resolved, leaf.value)
    if leaf.op == "neq":
        return not _literals_equal(resolved, leaf.value)
    if leaf.op == "in":
        candidates = leaf.value if isinstance(leaf.value, (list, tuple)) else ()
        return any(_literals_equal(resolved, candidate) for candidate in candidates)
    if (
        not _is_orderable(resolved)
        or not _is_orderable(leaf.value)
        or isinstance(resolved, str) != isinstance(leaf.value, str)
    ):
        raise TypeError(
            f"when predicate {leaf.path!r} {leaf.op} {leaf.value!r} cannot order "
            f"a {type(resolved).__name__} against a {type(leaf.value).__name__}"
        )
    if leaf.op == "lt":
        return bool(resolved < leaf.value)
    if leaf.op == "lte":
        return bool(resolved <= leaf.value)
    if leaf.op == "gt":
        return bool(resolved > leaf.value)
    return bool(resolved >= leaf.value)


def evaluate_when_gate(gate: WhenGate, context: Mapping[str, Any]) -> bool:
    """Evaluate a normalized ``when:`` gate against recorded context (deterministic:
    data only; TS ``evaluatePlanWhen`` parity)."""
    if gate.mode == "any":
        return any(_evaluate_when_leaf(leaf, context) for leaf in gate.predicates)
    # "leaf" carries exactly one predicate, so `all` covers both modes.
    return all(_evaluate_when_leaf(leaf, context) for leaf in gate.predicates)


def _render_js_number(value: float) -> str:
    """Format a float exactly as ECMAScript ``Number::toString`` (JSON.stringify)
    does: shortest round-trip digits, fixed notation for decimal exponents in
    (-6, 21], exponential (unpadded, explicit sign) otherwise. Python's ``repr``
    disagrees on the notation window (1e-5 -> '1e-05' vs JS '0.00001') and pads
    exponents ('1e-07' vs '1e-7'), and the rendered text is cross-edition wire
    data — so the JS rules are reimplemented here."""
    if value != value or value in (float("inf"), float("-inf")):
        # JSON.stringify(NaN/Infinity) is "null"; unreachable for YAML literals.
        return "null"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude == 0:
        return "0"
    # repr() already gives the shortest round-trip digits; Decimal extracts them.
    from decimal import Decimal

    _, digit_tuple, exponent10 = Decimal(repr(magnitude)).as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0") or "0"
    stripped = len(digit_tuple) - len(digits)
    # value == 0.<digits> * 10**n with no leading/trailing zeros in <digits>.
    n = int(exponent10) + stripped + len(digits)
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    exponent = n - 1
    mantissa_text = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{mantissa_text}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"


def _render_when_literal(value: Any) -> str:
    # JSON.stringify parity: the rendered text is cross-edition wire data (it rides
    # `step_skipped` events and the topology projection's `condition`), so both
    # editions must produce identical strings for the same YAML.
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        return _render_js_number(value)
    if isinstance(value, int):
        # JS renders integers >= 1e21 in exponential notation (they are floats
        # there); smaller integers keep their exact fixed form.
        return _render_js_number(float(value)) if abs(value) >= 10**21 else str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_render_when_literal(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _render_when_leaf(leaf: WhenLeaf) -> str:
    rendered = _render_when_literal(leaf.value)
    symbols = {"eq": "==", "neq": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
    if leaf.op in symbols:
        return f"{leaf.path} {symbols[leaf.op]} {rendered}"
    if leaf.op == "in":
        return f"{leaf.path} in {rendered}"
    return f"{leaf.path} exists" if leaf.value is True else f"{leaf.path} not exists"


def render_when_gate(gate: WhenGate) -> str:
    """The canonical text of a ``when:`` gate — shared by ``step_skipped`` lifecycle
    events and the topology projection's ``condition`` edges (#55 §3.3/§7), identical
    to the TS edition's ``renderPlanWhen`` for the same YAML."""
    parts = [_render_when_leaf(leaf) for leaf in gate.predicates]
    if gate.mode == "leaf":
        return parts[0]
    return f"{gate.mode}({', '.join(parts)})"


@dataclass(frozen=True)
class CompensateCallSpec:
    """Resolved compensation for a completed step (#299 D299-1): the compensating activity
    and the Temporal kwargs it schedules with. ``input_from`` is a context dot-path (None =
    the compensated step's OWN output — per item for a map step); ``retry_policy`` is the
    per-compensation override (None = the default bounded retry, resolved at build)."""

    activity_name: str
    input_from: str | None
    task_queue: str | None
    start_to_close_timeout: timedelta
    retry_policy: Any | None
    heartbeat_timeout: timedelta | None = None


@dataclass(frozen=True)
class _CompensationEntry:
    """One recorded compensation to run on the unwind (#299 D299-2): the ORIGINAL compensated
    step id (carried on every compensation event), the resolved {@link CompensateCallSpec}, and
    the compensation input resolved AT PUSH TIME."""

    step_id: str
    compensate: CompensateCallSpec
    input_value: Any


@dataclass(frozen=True)
class ActivityCallSpec:
    step_id: str
    activity_name: str
    task_queue: str | None
    start_to_close_timeout: timedelta
    retry_policy: Any | None
    heartbeat_timeout: timedelta | None = None
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenGate | None = None
    #: Compensation for this completed step (#299 D299-1).
    compensate: CompensateCallSpec | None = None
    #: True when the step targets an AI activity (the Typeflux wrapper, which accepts
    #: the optional per-call context envelope). A PLAIN ``@activity.defn`` callable
    #: declares exactly one parameter, so the runner must never pass it a second
    #: argument — baked at generation time (#715 slice 1 review).
    ai: bool = True


@dataclass(frozen=True)
class MapCollectCallSpec:
    output_type: type[BaseModel]
    field: str
    max_bytes: int = 0


@dataclass(frozen=True)
class MapCallSpec:
    step_id: str
    activity_name: str
    over: str
    concurrency: int
    collect: MapCollectCallSpec
    task_queue: str | None
    start_to_close_timeout: timedelta
    retry_policy: Any | None
    heartbeat_timeout: timedelta | None = None
    #: Resolved from the per-item activity's opt-in caching request (#60). When
    #: present + enabled the generator emits a cache-prep activity before the
    #: fan-out, which changes the command sequence — so it participates in the
    #: workflow digest (see identity._call_payload).
    session_cache: SessionCacheConfig | None = None
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenGate | None = None
    #: Compensation per completed item, reverse item order (#299 D299-1).
    compensate: CompensateCallSpec | None = None


@dataclass(frozen=True)
class ParallelCollectCallSpec:
    """The typed merge of a parallel block (#55 §3.1): the collect object's fields
    ARE the branch ids (decision D4); ``max_bytes`` guards the merged payload
    exactly like a map's collect (0 disables)."""

    output_type: type[BaseModel]
    max_bytes: int = 0


@dataclass(frozen=True)
class ParallelBranchCallSpec:
    """One branch of a parallel block: a nested call sequence, optionally gated.
    A gated-out branch contributes None to its collect field (#55 §3.3)."""

    branch_id: str
    calls: tuple[WorkflowCallSpec, ...]
    when: WhenGate | None = None


@dataclass(frozen=True)
class ParallelCallSpec:
    """Run branches concurrently over the running value, then merge their terminal
    values into the collect object keyed by branch id (#55 §3.1; TS
    ``ParallelPlanStep`` parity)."""

    step_id: str
    branches: tuple[ParallelBranchCallSpec, ...]
    collect: ParallelCollectCallSpec
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenGate | None = None


@dataclass(frozen=True)
class ResolvedSubworkflow:
    """A sibling PROJECT workflow resolved for use as a child (#55 §3.4). Built by the
    project layer (``project.environment.resolve_subworkflows_for``, which computes
    the child's registered type + digest recursively with cycle detection) and passed
    INTO ``create_workflow`` — the yaml package never imports ``project.*`` (the sandbox
    boundary), so the resolved identity travels as plain data instead. A child's
    ``workflow.version`` label is not carried separately: the label (or digest prefix)
    is already encoded in ``workflow_type`` (``registered_workflow_type``), and the
    child identity memo mirrors Python's top-level identity-memo key set, which has
    no version key (see ``_child_identity_memo``)."""

    #: Manifest workflow id (the ``workflow:`` reference value).
    workflow_id: str
    #: The child's registered Temporal workflow type (``{name}.{suffix}``).
    workflow_type: str
    #: The child's logical workflow name (its own drain/visibility identity).
    workflow_name: str
    project: str
    spec_digest: str
    input_type: type[BaseModel]
    output_type: type[BaseModel]


@dataclass(frozen=True)
class SubworkflowCallSpec:
    """Run a sibling project workflow as a Temporal CHILD workflow (#55 §3.4): a
    deterministic child id ``{parent_workflow_id}.{step_id}``, the child's own identity
    memo plus the parent-link key, ALLOW_DUPLICATE reuse + TERMINATE parent-close, and
    the parent's task queue inherited. The child's registered type + digest are BAKED
    IN at generation time, so a child-graph edit moves the parent digest."""

    step_id: str
    #: The child's MANIFEST workflow id (topology projection; not the registered type).
    child_workflow_id: str
    child_workflow_type: str
    child_workflow_name: str
    child_project: str
    child_digest: str
    #: The child's ``workflow.output`` model — the child-execution ``result_type``.
    output_type: type[BaseModel]
    #: Pre-built ``TypedSearchAttributes`` stamping the child's OWN name into the
    #: configured attribute, or None (built at generation time, outside the sandbox).
    search_attributes: Any | None
    #: Pre-built Temporal start-option enums (built outside the sandbox).
    id_reuse_policy: Any
    parent_close_policy: Any
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenGate | None = None
    #: Parent-side compensation for having invoked the child (#299 D299-1/D299-3).
    compensate: CompensateCallSpec | None = None


@dataclass(frozen=True)
class MapSubworkflowCallSpec:
    """Fan a sibling project workflow over the items at ``over`` with V1 map semantics
    (#55 §3.4 ``map.workflow``): a child execution per item, child ids
    ``{parent_workflow_id}.{step_id}-{index}``, bounded concurrency, and the map
    collect/payload-guard shape exactly — with a child start in place of an activity."""

    step_id: str
    child_workflow_id: str
    child_workflow_type: str
    child_workflow_name: str
    child_project: str
    child_digest: str
    #: The child's ``workflow.output`` model — the per-item child ``result_type``.
    output_type: type[BaseModel]
    over: str
    concurrency: int
    collect: MapCollectCallSpec
    search_attributes: Any | None
    id_reuse_policy: Any
    parent_close_policy: Any
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenGate | None = None


WorkflowCallSpec = (
    ActivityCallSpec | MapCallSpec | ParallelCallSpec | SubworkflowCallSpec | MapSubworkflowCallSpec
)


def flatten_call_specs(
    calls: Sequence[WorkflowCallSpec],
) -> tuple[ActivityCallSpec | MapCallSpec, ...]:
    """Every LEAF (activity/map) call of a call tree, depth-first in declared order —
    parallel nodes contribute their branches' calls, not themselves (TS
    ``flattenPlanSteps`` parity). The one walk shared by every consumer that reasons
    per-activity (bundle/catalog projections, run metadata), so none can miss a
    nested step. Sub-workflow calls contribute NOTHING: they invoke no activity of the
    PARENT's — the child's activities belong to its own plan/worker map (#55 §6)."""
    leaves: list[ActivityCallSpec | MapCallSpec] = []
    for call in calls:
        if isinstance(call, ParallelCallSpec):
            for branch in call.branches:
                leaves.extend(flatten_call_specs(branch.calls))
        elif isinstance(call, (SubworkflowCallSpec, MapSubworkflowCallSpec)):
            continue
        else:
            leaves.append(call)
    return tuple(leaves)


def collect_subworkflow_references(spec: TypefluxYamlSpec) -> tuple[str, ...]:
    """Every sibling workflow id referenced by a spec (``workflow:`` steps +
    ``map.workflow`` fan-outs, recursively through parallel branches), de-duplicated in
    first-seen order. The project layer resolves these into ``ResolvedSubworkflow``
    records to pass into ``create_workflow`` (#55 §3.4)."""
    seen: list[str] = []

    def add(ref: str) -> None:
        if ref not in seen:
            seen.append(ref)

    def walk(steps: Sequence[Any]) -> None:
        for step in steps:
            if isinstance(step, WorkflowSubworkflowStepSpec):
                add(step.workflow)
            elif isinstance(step, WorkflowMapStepSpec) and step.map.workflow is not None:
                add(step.map.workflow)
            elif isinstance(step, WorkflowParallelStepSpec):
                for branch in step.parallel.branches:
                    walk(branch.steps)

    walk(spec.workflow.steps)
    return tuple(seen)


#: Sentinel distinguishing "no inherited search attribute passed" (use this spec's own
#: ``runtime.temporal.workflow_search_attribute``) from an inherited ``None`` (the ROOT
#: of the sub-workflow chain configures none, so no descendant stamps one). #55 §6.
_INHERITED_SEARCH_ATTRIBUTE_UNSET: Any = object()


def create_workflow(
    spec: TypefluxYamlSpec,
    activities: Mapping[str, YamlWorkflowActivity],
    *,
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
    inherited_search_attribute: Any = _INHERITED_SEARCH_ATTRIBUTE_UNSET,
) -> type:
    input_type = import_type_ref(spec.project, spec.workflow.input)
    output_type = import_type_ref(spec.project, spec.workflow.output)
    _validate_model_type(input_type, "workflow.input")
    _validate_model_type(output_type, "workflow.output")
    _validate_workflow_graph(spec, activities, input_type, output_type, subworkflows)

    # The search-attribute NAME children are stamped under (#55 §6, TS start-context
    # parity): the ROOT deployment's configured name propagates down the WHOLE chain —
    # when this class is built as a descendant (the project resolver passes
    # `inherited_search_attribute`), the root's value (possibly None) OVERRIDES this
    # spec's own config; only a root build (sentinel default) reads its own spec.
    # Deployment config, not graph identity: never folded into `_call_payload`.
    child_search_attribute: str | None = (
        spec.runtime.temporal.workflow_search_attribute
        if inherited_search_attribute is _INHERITED_SEARCH_ATTRIBUTE_UNSET
        else inherited_search_attribute
    )

    calls = tuple(
        _build_call_spec(spec, step, activities, subworkflows, child_search_attribute)
        for step in spec.workflow.steps
    )
    call_indexes = {call.step_id: index for index, call in enumerate(calls)}
    lifecycle_spec = (
        spec.workflow.lifecycle if _lifecycle_enabled(spec.workflow.lifecycle) else None
    )

    async def run(self, input_value):
        from temporalio import workflow

        context: dict[str, Any] = {"input": input_value}
        # The run's subject ids (#715 slice 1), read once off this execution's OWN
        # `TypefluxSubjectIds` search attribute (stamped at start / inherited from
        # the parent) — deterministic in the sandbox. Threaded into every AI
        # activity call's context envelope so the activity-side invocation context
        # (and through it the cross-run cache record) carries them.
        run_subject_ids = _run_subject_ids(workflow)
        lifecycle = getattr(self, "_typeflux_lifecycle", None)
        if lifecycle is not None:
            lifecycle.start(
                workflow,
                total_units=_lifecycle_total_units(calls, context)
                if lifecycle.progress_enabled
                else 0,
            )
        current = input_value

        # The workflow-local compensation LIFO (#299 D299-2): each `compensate:`-bearing step
        # pushes on completion, with its input resolved AT PUSH TIME (the context value exists
        # then and is replay-stable). On failure OR cancellation the outer handler walks this in
        # reverse. One flat stack per run — completed sibling branches of a parallel block and
        # per-item map compensations all live here (#299 D299-3).
        compensation_stack: list[_CompensationEntry] = []
        compensation_counter = 0

        def push_compensation(call: WorkflowCallSpec, value: Any) -> None:
            # Record an ATOMIC step's compensation. Called AFTER context[step_id] is set, so
            # `input_from` — including the step's own output by id — resolves. Activity /
            # sub-workflow steps only: a MAP step self-records PER COMPLETED ITEM from inside
            # _execute_map_call (so a mid-fan-out failure still compensates the succeeded items),
            # and a parallel step never carries compensate (its branch steps push as they run).
            if isinstance(call, MapCallSpec):
                return
            compensate = getattr(call, "compensate", None)
            if compensate is None:
                return
            resolved = (
                _resolve_context_path(context, compensate.input_from)
                if compensate.input_from is not None
                else value
            )
            compensation_stack.append(_CompensationEntry(call.step_id, compensate, resolved))

        def make_map_compensation_recorder(call: MapCallSpec):
            # A per-completed-item recorder (#299) passed INTO _execute_map_call: it appends one
            # compensation entry per item result. The map executor calls it in ITEM order for
            # every COMPLETED item (on success AND on a mid-fan-out failure), so a partial map
            # still compensates its succeeded items and the flat LIFO unwinds in reverse item order.
            if call.compensate is None:
                return None
            compensate = call.compensate

            def record(item_result: Any) -> None:
                resolved = (
                    _resolve_context_path(context, compensate.input_from)
                    if compensate.input_from is not None
                    else item_result
                )
                compensation_stack.append(_CompensationEntry(call.step_id, compensate, resolved))

            return record

        async def run_compensations() -> str:
            # Walk the compensation LIFO in reverse (#299 D299-2). Each compensating activity runs
            # with a bounded per-compensation timeout and the per-compensation retry override; a
            # compensation failure records `compensation_failed` and the unwind CONTINUES
            # (best-effort, loud). Returns the terminal compensation_status; NEVER raises.
            #
            # temporalio exposes no workflow-safe non-cancellable scope (asyncio.shield is unsafe
            # in the deterministic workflow sandbox), so the unwind runs synchronously in the outer
            # handler BEFORE the original error re-raises. Typeflux's OWN cancel is cooperative — a
            # flag raises TypefluxWorkflowCancelled (an ApplicationError, never CancelledError), so
            # that path schedules compensation activities normally. But a NATIVE Temporal cancel
            # (operator/client CancelWorkflowExecution, independent of the typeflux signal) can
            # deliver asyncio.CancelledError into the `await` below. We catch BaseException per
            # compensation — recording compensation_failed and continuing — so a mid-unwind
            # CancelledError can NEVER escape this handler, orphan the remaining entries, or mask
            # the ORIGINAL business error (which the caller always re-raises). This is the
            # best-effort floor the design accepts absent a real shield (#299 review, Finder A#1).
            nonlocal compensation_counter
            if not compensation_stack:
                return "none"
            any_failed = False
            while compensation_stack:
                entry = compensation_stack.pop()
                if lifecycle is not None:
                    lifecycle.compensation_started(workflow, entry.step_id)
                activity_id = f"{entry.step_id}.__compensate__.{compensation_counter}"
                compensation_counter += 1
                try:
                    await workflow.execute_activity(
                        entry.compensate.activity_name,
                        entry.input_value,
                        **_compensate_activity_kwargs(entry.compensate, activity_id),
                    )
                    if lifecycle is not None:
                        lifecycle.compensation_completed(workflow, entry.step_id)
                except BaseException:  # noqa: BLE001 - see the docstring: a native cancel must not escape
                    # Best-effort per the saga precedent, but LOUD: record and keep unwinding so one
                    # broken compensation (or a native cancel delivered mid-unwind) never strands the
                    # rest. The ORIGINAL failure is what the caller re-raises.
                    any_failed = True
                    if lifecycle is not None:
                        lifecycle.compensation_failed(workflow, entry.step_id)
            return "partial" if any_failed else "complete"

        async def execute_call(call: WorkflowCallSpec, value: Any) -> Any:
            if isinstance(call, ActivityCallSpec):
                if call.ai and run_subject_ids:
                    # The context envelope rides ONLY on AI activity calls (a plain
                    # @activity.defn callable declares exactly one parameter) and
                    # ONLY when the run has subjects, so subject-free histories
                    # stay byte-identical (#715 slice 1).
                    result = await workflow.execute_activity(
                        call.activity_name,
                        args=[value, MapActivityContext(subject_ids=run_subject_ids)],
                        **_activity_kwargs(call, call.step_id),
                    )
                else:
                    result = await workflow.execute_activity(
                        call.activity_name,
                        value,
                        **_activity_kwargs(call, call.step_id),
                    )
                if lifecycle is not None:
                    lifecycle.unit_completed(workflow, call.step_id)
                return result
            if isinstance(call, MapCallSpec):
                items = _resolve_context_path(context, call.over)
                if lifecycle is not None and _context_path_root(call.over) != "input":
                    lifecycle.add_total_units(_sequence_length(items))
                return await _execute_map_call(
                    workflow,
                    call,
                    items,
                    lifecycle=lifecycle,
                    record_compensation=make_map_compensation_recorder(call),
                    subject_ids=run_subject_ids,
                )
            if isinstance(call, SubworkflowCallSpec):
                result = await _execute_subworkflow_call(workflow, call, value)
                if lifecycle is not None:
                    lifecycle.unit_completed(workflow, call.step_id)
                return result
            if isinstance(call, MapSubworkflowCallSpec):
                items = _resolve_context_path(context, call.over)
                if lifecycle is not None and _context_path_root(call.over) != "input":
                    lifecycle.add_total_units(_sequence_length(items))
                return await _execute_subworkflow_map_call(
                    workflow, call, items, lifecycle=lifecycle
                )
            return await _execute_parallel_call(
                workflow,
                call,
                value,
                context=context,
                lifecycle=lifecycle,
                run_branch=run_branch_sequence,
            )

        async def run_branch_sequence(branch_calls: Sequence[WorkflowCallSpec], entry: Any) -> Any:
            # One branch's step sequence (#55 §5.1): steps in order, threading the
            # branch's running value and recording every result into the SHARED flat
            # context (unique ids make concurrent sibling writes collision-free). A
            # false `when` gate skips the step and the REMAINDER of the sequence —
            # the branch's contribution is the running value at the gate (§3.3).
            value = entry
            for index, call in enumerate(branch_calls):
                _raise_if_cancelled(workflow, lifecycle)
                if call.when is not None and not evaluate_when_gate(call.when, context):
                    if lifecycle is not None:
                        lifecycle.step_skipped(workflow, call.step_id, render_when_gate(call.when))
                        lifecycle.skip_units(_lifecycle_total_units(branch_calls[index:], context))
                    return value
                if lifecycle is not None:
                    lifecycle.step_started(workflow, call.step_id)
                value = await execute_call(call, value)
                context[call.step_id] = value
                # A completed step inside a parallel branch pushes onto the SAME flat LIFO
                # (#299 D299-3): a later failure unwinds every completed sibling branch too.
                push_compensation(call, value)
            return value

        try:
            call_index = 0
            while call_index < len(calls):
                call = calls[call_index]
                _raise_if_cancelled(workflow, lifecycle)
                # Top-level `when` gating is EARLY EXIT (#55 §3.3): a false gate skips
                # this step and the remainder of the sequence, completing the workflow
                # with the running value at the gate (load-validated to match
                # workflow.output).
                if call.when is not None and not evaluate_when_gate(call.when, context):
                    if lifecycle is not None:
                        lifecycle.step_skipped(workflow, call.step_id, render_when_gate(call.when))
                        lifecycle.skip_units(_lifecycle_total_units(calls[call_index:], context))
                    break
                if lifecycle is not None:
                    lifecycle.step_started(workflow, call.step_id)
                current = await execute_call(call, current)
                context[call.step_id] = current
                push_compensation(call, current)
                route_target = await _maybe_wait_for_review(workflow, lifecycle, call.step_id)
                if route_target is not None:
                    route_index = call_indexes[route_target]
                    if lifecycle is not None:
                        lifecycle.skip_units(
                            _lifecycle_total_units(calls[call_index + 1 : route_index], context)
                        )
                    call_index = route_index
                    continue
                call_index += 1
            if lifecycle is not None:
                lifecycle.completed(workflow)
            return current
        except BaseException as exc:
            # Compensation unwind (#299 D299-2): BOTH failure and cancellation walk the LIFO in
            # reverse, then the terminal event carries the resulting compensation_status. The
            # ORIGINAL error is always re-raised — a compensation failure never masks it.
            compensation_status = await run_compensations()
            if lifecycle is not None:
                if _is_typeflux_cancellation(exc):
                    lifecycle.cancelled(workflow, compensation_status)
                else:
                    lifecycle.failed(workflow, exc, compensation_status)
            raise

    def __init__(self):
        if lifecycle_spec is not None:
            self._typeflux_lifecycle = _LifecycleRuntime(lifecycle_spec)

    spec_digest = workflow_spec_digest(spec, calls)
    workflow_type = registered_workflow_type(spec, spec_digest)
    class_name = _generated_workflow_class_name(spec, spec_digest)
    run.__name__ = "run"
    run.__qualname__ = f"{class_name}.run"
    run.__annotations__ = {"input_value": input_type, "return": output_type}

    from temporalio import workflow

    workflow_run = workflow.run(run)
    attrs: dict[str, Any] = {
        "__module__": __name__,
        "run": workflow_run,
        "__typeflux_activity_calls__": calls,
        "__typeflux_workflow_name__": spec.workflow.name,
        "__typeflux_workflow_type__": workflow_type,
        "__typeflux_workflow_version_label__": spec.workflow.version,
        "__typeflux_spec_digest__": spec_digest,
        "__typeflux_project__": spec.project,
        "__typeflux_yaml_name__": spec.name,
        "__typeflux_lifecycle_spec__": lifecycle_spec,
        # The resolved workflow I/O models — read by the project layer when this
        # class is resolved AS A CHILD (its input/output type identity feeds a
        # parent's sub-workflow type-graph check without a re-import). #55 §3.4
        "__typeflux_input_type__": input_type,
        "__typeflux_output_type__": output_type,
    }
    # The lifecycle surface is always registered (#618, binding contract): a
    # lifecycle-less workflow answers the status query with state "disabled"
    # and accepts the signals as no-ops, matching the TS edition — a control
    # plane never needs to know whether a spec configured a lifecycle before
    # polling. Only the lifecycle state itself is conditional.
    attrs["typeflux_lifecycle_status"] = _lifecycle_query(class_name)
    attrs["typeflux_request_cancel"] = _lifecycle_cancel_signal(class_name)
    attrs["typeflux_submit_review"] = _lifecycle_review_signal(class_name)
    if lifecycle_spec is not None:
        __init__.__qualname__ = f"{class_name}.__init__"
        attrs["__init__"] = __init__
    workflow_class = type(
        class_name,
        (),
        attrs,
    )
    globals()[class_name] = workflow_class
    # Executions register under an immutable versioned workflow type so a
    # changed YAML graph can never replay an old history; the logical
    # spec.workflow.name stays the human-facing identity.
    return workflow.defn(name=workflow_type)(workflow_class)


def validate_unique_yaml_workflow_names(specs: Sequence[TypefluxYamlSpec]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for spec in specs:
        workflow_name = spec.workflow.name
        if workflow_name in seen:
            duplicates.add(workflow_name)
        seen.add(workflow_name)
    if duplicates:
        raise ValueError(f"duplicate YAML workflow name(s): {', '.join(sorted(duplicates))}")


def create_yaml_workflow_runner():
    from temporalio.worker.workflow_sandbox import (
        SandboxedWorkflowRunner,
        SandboxRestrictions,
    )

    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(YAML_WORKFLOW_MODULE)
    )


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """``T | None`` -> ``(T, True)``; anything else -> ``(annotation, False)``."""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1 and len(get_args(annotation)) == 2:
            return args[0], True
    return annotation, False


def _check_when_roots(
    gate: WhenGate,
    context_types: Mapping[str, type],
    gate_label: str,
    *,
    route_label: str | None = None,
) -> None:
    """Load-check every ``when`` path root against the context roots available at the
    gated step/branch (#55 §4.5, the review walk's discipline): workflow input plus
    the results of steps already completed on every path to it — sibling-branch
    results are not addressable (they may not exist yet). Mirrors TS
    ``validateWhenPaths`` / ``checkGateRoots``."""
    for leaf in gate.predicates:
        root = leaf.path.split(".", 1)[0]
        if root in context_types:
            continue
        if route_label is not None:
            raise ValueError(
                f"{route_label} routes past step {root!r}, but the when predicate on "
                f"{gate_label} reads {leaf.path!r} — the route would skip the step "
                "that produces it"
            )
        raise ValueError(
            f"when predicate on {gate_label} reads {leaf.path!r}, but {root!r} is not "
            "available there (available: the workflow input and steps completed on "
            "every path to the gate; sibling-branch results are not addressable)"
        )


def _walk_sequence_types(
    spec: TypefluxYamlSpec,
    steps: Sequence[WorkflowAnyStepSpec],
    activities: Mapping[str, YamlWorkflowActivity],
    *,
    entry_type: type,
    context_types: dict[str, type],
    route_label: str | None = None,
    route_entry_desc: str | None = None,
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
) -> tuple[type, list[tuple[str, type]], list[tuple[str, dict[str, type]]]]:
    """Walk one sequence (the top level, a parallel branch, or a routed tail) over
    RESOLVED Pydantic types — the Python edition of TS ``walkSequence`` (#55 §4).
    Threads the running type through activity steps, resumes at map/parallel collect
    outputs, validates ``when`` path roots progressively, and returns
    ``(terminal_type, gate_points, per-step contributions)``.

    ``context_types`` is mutated progressively with each step's GUARANTEED
    contribution: the step's own id (a false gate on it skips the remainder, so any
    later step that ran saw it run) plus — for parallel blocks — only the interior
    ids guaranteed on every path (ungated branches' ungated prefixes, recursively).
    A gated branch's inner results are conditionally absent even when the block
    completed, so they never become roots downstream (#55 codex round).
    """
    current = entry_type
    gates: list[tuple[str, type]] = []
    contributions: list[tuple[str, dict[str, type]]] = []
    first = True
    for step in steps:
        when = getattr(step, "when", None)
        if when is not None:
            gate = when_gate_from_spec(when)
            _check_when_roots(gate, context_types, f"step {step.id!r}", route_label=route_label)
            gates.append((step.id, current))
        if isinstance(step, WorkflowMapStepSpec):
            current = _validate_map_step(
                spec, step, activities, context_types=context_types, subworkflows=subworkflows
            )
            contribution = {step.id: current}
        elif isinstance(step, WorkflowSubworkflowStepSpec):
            record = _resolve_subworkflow_record(subworkflows, step.workflow, step.id)
            if record.input_type is not current:
                if route_label is not None and first and route_entry_desc is not None:
                    raise TypeError(
                        f"{route_label} routes from {route_entry_desc!r} to step "
                        f"{step.id!r}, which runs sibling workflow {step.workflow!r} "
                        f"expecting {record.input_type.__name__}, but review checkpoint "
                        f"output is {current.__name__}"
                    )
                raise TypeError(
                    f"workflow step {step.id!r} runs sibling workflow {step.workflow!r} "
                    f"expecting {record.input_type.__name__}, but previous output is "
                    f"{current.__name__}"
                )
            current = record.output_type
            contribution = {step.id: current}
        elif isinstance(step, WorkflowParallelStepSpec):
            current, interior = _validate_parallel_step(
                spec,
                step,
                activities,
                entry_type=current,
                context_types=context_types,
                route_label=route_label,
                route_entry_desc=route_entry_desc if first else None,
                subworkflows=subworkflows,
            )
            contribution = {step.id: current, **interior}
        else:
            activity = activities.get(step.activity)
            if activity is None:
                raise ValueError(
                    f"workflow step {step.id!r} references unknown activity: {step.activity}"
                )
            if activity.input_type is not current:
                if route_label is not None and first and route_entry_desc is not None:
                    raise TypeError(
                        f"{route_label} routes from {route_entry_desc!r} to step "
                        f"{step.id!r}, which expects {activity.input_type.__name__}, "
                        f"but review checkpoint output is {current.__name__}"
                    )
                raise TypeError(
                    f"workflow step {step.id!r} expects {activity.input_type.__name__}, "
                    f"but previous output is {current.__name__}"
                )
            current = activity.output_type
            contribution = {step.id: current}
        context_types.update(contribution)
        # Compensation is validated AFTER the step's own contribution joins context_types, so
        # input_from may reference the step's own output by id and — on a routed tail — a
        # reference to a step the route skipped is rejected (#299, route/branch-aware).
        if getattr(step, "compensate", None) is not None:
            _check_compensate(
                spec,
                step,
                activities,
                own_output_type=current,
                context_types=context_types,
                route_label=route_label,
            )
        contributions.append((step.id, contribution))
        first = False
    return current, gates, contributions


def _validate_parallel_step(
    spec: TypefluxYamlSpec,
    step: WorkflowParallelStepSpec,
    activities: Mapping[str, YamlWorkflowActivity],
    *,
    entry_type: type,
    context_types: Mapping[str, type],
    route_label: str | None = None,
    route_entry_desc: str | None = None,
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
) -> tuple[type, dict[str, type]]:
    """Validate a parallel block against resolved types (#55 §4.1/§4.3): each branch
    is a nested sequence whose first step consumes the block input; the collect
    output's declared fields are EXACTLY the branch ids (decision D4), each field's
    type equal to its branch's terminal output type — Optional exactly where the
    branch is ``when``-gated (a skipped branch contributes None). Returns the collect
    type plus the guaranteed interior ids (ungated branches' ungated prefixes,
    recursively) that may become context roots after the block."""
    collect_output = cast(
        "type[BaseModel]", import_type_ref(spec.project, step.parallel.collect.output)
    )
    _validate_model_type(collect_output, f"workflow step {step.id!r} collect.output")
    fields = collect_output.model_fields
    branch_ids = [branch.id for branch in step.parallel.branches]
    missing = sorted(set(branch_ids) - set(fields))
    extra = sorted(set(fields) - set(branch_ids))
    if missing or extra:
        raise TypeError(
            f"workflow step {step.id!r} collect.output {collect_output.__name__} fields "
            f"must be exactly the branch ids (#55 decision D4); missing: "
            f"{', '.join(missing) or 'none'}; extra: {', '.join(extra) or 'none'}"
        )
    guaranteed: dict[str, type] = {}
    for branch in step.parallel.branches:
        if branch.when is not None:
            _check_when_roots(
                when_gate_from_spec(branch.when),
                context_types,
                f"branch {branch.id!r}",
                route_label=route_label,
            )
        # Each branch sees the block-entry roots plus its OWN earlier steps (the
        # copy keeps sibling branches invisible to each other). In a routed tail,
        # `route_entry_desc` threads to the branches' FIRST steps only — they
        # consume the checkpoint output instead of the block's natural predecessor
        # (TS checkBranchTail parity).
        branch_types = dict(context_types)
        terminal, gate_points, contributions = _walk_sequence_types(
            spec,
            branch.steps,
            activities,
            entry_type=entry_type,
            context_types=branch_types,
            route_label=route_label,
            route_entry_desc=route_entry_desc,
            subworkflows=subworkflows,
        )
        # A mid-branch gate exits the branch with the running value as its
        # contribution, so that value must match the branch's terminal type.
        for gate_id, gate_type in gate_points:
            if gate_type is not terminal:
                raise TypeError(
                    f"when gate on step {gate_id!r} makes {gate_type.__name__} a "
                    f"potential result of branch {branch.id!r}, which terminates with "
                    f"{terminal.__name__} — a gated sequence must exit with its "
                    "declared result type"
                )
        base_type, optional = _unwrap_optional(fields[branch.id].annotation)
        if base_type is not terminal:
            raise TypeError(
                f"workflow step {step.id!r} collect field {branch.id!r} expects "
                f"{getattr(base_type, '__name__', base_type)!s}, but branch "
                f"{branch.id!r} terminates with {terminal.__name__}"
            )
        if branch.when is not None and not optional:
            raise TypeError(
                f"workflow step {step.id!r} collect field {branch.id!r} must be "
                f"Optional: branch {branch.id!r} is when-gated and contributes None "
                "when skipped (#55 §3.1)"
            )
        if branch.when is None:
            # The guaranteed interior: this branch always runs, so its ungated
            # prefix (and, recursively, those steps' own guaranteed interiors) is
            # present on every path once the block completed.
            for nested, contribution in zip(branch.steps, contributions, strict=True):
                if getattr(nested, "when", None) is not None:
                    break
                guaranteed.update(contribution[1])
    return collect_output, guaranteed


def _validate_workflow_graph(
    spec: TypefluxYamlSpec,
    activities: Mapping[str, YamlWorkflowActivity],
    input_type: type,
    output_type: type,
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
) -> None:
    context_types: dict[str, type] = {"input": input_type}
    current_type, gates, contributions = _walk_sequence_types(
        spec,
        spec.workflow.steps,
        activities,
        entry_type=input_type,
        context_types=context_types,
        subworkflows=subworkflows,
    )
    if current_type is not output_type:
        raise TypeError(
            f"workflow output expects {output_type.__name__}, but final step returns {current_type.__name__}"
        )
    # A top-level gate is early exit (#55 §3.3): the workflow completes with the
    # running value at the gate, so that value must match workflow.output (a gated
    # FIRST step therefore requires workflow.input == workflow.output).
    for gate_id, gate_type in gates:
        if gate_type is not output_type:
            raise TypeError(
                f"when gate on step {gate_id!r} would complete the workflow with "
                f"{gate_type.__name__}, but workflow.output is {output_type.__name__} — "
                "a gated sequence must exit with its declared result type"
            )
    _validate_review_routes(
        spec,
        activities,
        input_type=input_type,
        output_type=output_type,
        step_contributions=dict(contributions),
        subworkflows=subworkflows,
    )


def _check_compensate(
    spec: TypefluxYamlSpec,
    step: Any,
    activities: Mapping[str, YamlWorkflowActivity],
    *,
    own_output_type: type,
    context_types: Mapping[str, type],
    route_label: str | None,
) -> None:
    """Load-validate a step's ``compensate:`` (#299 D299-1). Called from ``_walk_sequence_types``
    AFTER the step's own contribution joins ``context_types``, so it is route/branch-aware by
    construction: on a routed tail (``route_label`` set) a ``input_from`` referencing a step the
    route SKIPPED is rejected exactly like a ``when`` path would be (the A->C-skips-B case). The
    compensating activity must be declared, and — for the DEFAULT and bare-step-id references —
    its input type must equal the referenced value's type (dotted field paths validate only root
    availability, conservative — matching the TS edition). ``map.workflow`` (child fan-out)
    compensation is deferred; parallel steps cannot carry compensate (schema-forbidden)."""
    compensate = step.compensate
    if isinstance(step, WorkflowMapStepSpec) and step.map.workflow is not None:
        raise ValueError(
            f"workflow step {step.id!r} fans a sub-workflow (`map.workflow`) and cannot carry "
            "`compensate:` in this release — compensation covers activity / map / sub-workflow "
            "steps (#299)"
        )
    # The per-item compensation input of a map step is the per-ITEM activity output, not the
    # collect object; every other kind's own output is the running type.
    if isinstance(step, WorkflowMapStepSpec) and step.map.activity is not None:
        map_activity = activities.get(step.map.activity)
        own_type: type | None = map_activity.output_type if map_activity is not None else None
    else:
        own_type = own_output_type

    referenced: type | None
    if compensate.input_from is None:
        referenced = own_type
    else:
        root = compensate.input_from.split(".", 1)[0]
        # A map step's compensations are recorded PER ITEM from inside the map executor, BEFORE the
        # map's own collected output is written to context — so a self-rooted map input_from can
        # never resolve at per-item record time (#299 review). Reject it at load, naming the
        # constraint; omit input_from to compensate each item on its own result.
        if isinstance(step, WorkflowMapStepSpec) and root == step.id:
            raise ValueError(
                f"workflow step {step.id!r} compensate.input_from reads {compensate.input_from!r} "
                "(its own collected output), which is not available when its items compensate — omit "
                "input_from to compensate each item on its own result (#299)"
            )
        if root not in context_types:
            if route_label is not None:
                raise ValueError(
                    f"{route_label} routes past step {root!r}, but step {step.id!r} "
                    f"compensate.input_from reads {compensate.input_from!r} — the route would "
                    "skip the step whose output the compensation needs (#299)"
                )
            raise ValueError(
                f"workflow step {step.id!r} compensate.input_from reads {compensate.input_from!r}, "
                f"but {root!r} is not available there (available: the workflow input and steps "
                "completed on every path to this step, including the step itself) (#299)"
            )
        referenced = None if "." in compensate.input_from else context_types.get(root)

    comp_activity = activities.get(compensate.activity)
    if comp_activity is None:
        raise ValueError(
            f"workflow step {step.id!r} compensate references activity {compensate.activity!r}, "
            "which is not a declared activity (#299)"
        )
    if referenced is not None and comp_activity.input_type is not referenced:
        referenced_desc = (
            "the step's own output"
            if compensate.input_from is None
            else repr(compensate.input_from)
        )
        raise TypeError(
            f"workflow step {step.id!r} compensate runs activity {compensate.activity!r}, which "
            f"expects {comp_activity.input_type.__name__}, but the referenced value "
            f"({referenced_desc}) is {referenced.__name__} (#299)"
        )


def _validate_review_routes(
    spec: TypefluxYamlSpec,
    activities: Mapping[str, YamlWorkflowActivity],
    *,
    input_type: type,
    output_type: type,
    step_contributions: Mapping[str, dict[str, type]],
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
) -> None:
    lifecycle = spec.workflow.lifecycle
    if lifecycle is None:
        return
    steps = spec.workflow.steps
    step_indexes = {step.id: index for index, step in enumerate(steps)}
    # The single `review` keeps its exact V1 error prose; named `gates` are gate-scoped. Both
    # share the same forward-only type-chain walk (#55 §8). after_step existence + forward-only
    # were already checked in spec.py; here each gate's route TAIL must type-chain to workflow.output.
    single_review = lifecycle.review is not None

    def _check_route(
        route_step_id: str, label: str, after_step: str, after_output_type: type
    ) -> None:
        after_index = step_indexes[after_step]
        if route_step_id not in step_indexes:
            raise ValueError(f"{label} routes to unknown step: {route_step_id!r}")
        route_index = step_indexes[route_step_id]
        # Available roots at the jump: input + every step up to the checkpoint,
        # contributing only their GUARANTEED subtrees — a gated branch's inner
        # results are conditionally absent even when the block completed, so a
        # routed map/gate must not read them (they would fail at runtime).
        context_types: dict[str, type] = {"input": input_type}
        for step in steps[: after_index + 1]:
            context_types.update(step_contributions[step.id])
        current_type, tail_gates, _ = _walk_sequence_types(
            spec,
            steps[route_index:],
            activities,
            entry_type=after_output_type,
            context_types=context_types,
            route_label=label,
            route_entry_desc=after_step,
            subworkflows=subworkflows,
        )
        # The §3.3 gate-typing rule applies to routed tails too: a false gate on a
        # tail step completes the workflow with the value flowing AT that gate —
        # for the route target that is the CHECKPOINT output, not its natural
        # predecessor's output. A when-gated MAP step consumes no running value,
        # so the tail chain check alone would accept a route whose false gate
        # returns the checkpoint type despite workflow.output (codex round).
        for gate_id, gate_type in tail_gates:
            if gate_type is not output_type:
                raise TypeError(
                    f"{label} routes into a when-gated tail: a false gate on step "
                    f"{gate_id!r} would complete the workflow with {gate_type.__name__}, "
                    f"but workflow.output is {output_type.__name__} — a gated sequence "
                    "must exit with its declared result type"
                )
        if current_type is not output_type:
            raise TypeError(
                f"{label} route expects workflow output {output_type.__name__}, "
                f"but routed tail returns {current_type.__name__}"
            )

    for gate in lifecycle.resolved_gates():
        prefix = "review" if single_review else f"gate {gate.id!r}"
        after_output_type = step_contributions[gate.after_step][gate.after_step]
        for user_decision, route in gate.user_decisions.items():
            _check_route(
                route.route,
                f"{prefix} user_decision {user_decision!r}",
                gate.after_step,
                after_output_type,
            )
        # The timeout's route-to-step action is type-checked the same way (#297).
        timeout = gate.timeout
        if timeout is not None and timeout.on_timeout == "route" and timeout.route is not None:
            _check_route(timeout.route, f"{prefix} timeout", gate.after_step, after_output_type)


def _validate_map_step(
    spec: TypefluxYamlSpec,
    step: WorkflowMapStepSpec,
    activities: Mapping[str, YamlWorkflowActivity],
    *,
    context_types: Mapping[str, type],
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
) -> type:
    if step.map.workflow is not None:
        return _validate_subworkflow_map_step(
            spec, step, context_types=context_types, subworkflows=subworkflows
        )
    # The spec validator enforces exactly one of map.activity/map.workflow; the workflow
    # arm returned above, so activity is set here (mypy can't see the model-level XOR).
    assert step.map.activity is not None
    activity = activities.get(step.map.activity)
    if activity is None:
        raise ValueError(
            f"workflow step {step.id!r} references unknown activity: {step.map.activity}"
        )
    if not isinstance(activity, AIActivity):
        # Map calls schedule the activity with (item, MapActivityContext); only
        # the Typeflux AI wrapper accepts the context argument. A plain
        # @activity.defn target would pass validation here and then fail at
        # runtime with an argument-count error.
        raise TypeError(
            f"workflow step {step.id!r} maps plain Temporal activity "
            f"{activity.name!r}; map steps support AI activities only"
        )
    collection_type = _resolve_type_path(context_types, step.map.over)
    item_type = _list_item_type(collection_type)
    if item_type is None:
        raise TypeError(f"workflow step {step.id!r} map.over must resolve to a list field")
    if item_type is not activity.input_type:
        raise TypeError(
            f"workflow step {step.id!r} maps {activity.name!r} over {item_type.__name__}, "
            f"but activity expects {activity.input_type.__name__}"
        )
    collect_output = import_type_ref(spec.project, step.map.collect.output)
    _validate_model_type(collect_output, f"workflow step {step.id!r} collect.output")
    collect_item_type = _collect_field_item_type(collect_output, step.map.collect.field)
    if collect_item_type is None:
        raise TypeError(
            f"workflow step {step.id!r} collect.field {step.map.collect.field!r} must be a list field"
        )
    if collect_item_type is not activity.output_type:
        raise TypeError(
            f"workflow step {step.id!r} collect.field {step.map.collect.field!r} "
            f"expects {collect_item_type.__name__}, but activity returns {activity.output_type.__name__}"
        )
    return collect_output


def _validate_subworkflow_map_step(
    spec: TypefluxYamlSpec,
    step: WorkflowMapStepSpec,
    *,
    context_types: Mapping[str, type],
    subworkflows: Mapping[str, ResolvedSubworkflow] | None,
) -> type:
    # `map.workflow` fan-out typing (#55 §4.2): the child's `workflow.input` stands in
    # for a definition's input, its `workflow.output` for the definition's output.
    assert step.map.workflow is not None  # narrowed by the caller
    record = _resolve_subworkflow_record(subworkflows, step.map.workflow, step.id)
    collection_type = _resolve_type_path(context_types, step.map.over)
    item_type = _list_item_type(collection_type)
    if item_type is None:
        raise TypeError(f"workflow step {step.id!r} map.over must resolve to a list field")
    if item_type is not record.input_type:
        raise TypeError(
            f"workflow step {step.id!r} maps sibling workflow {step.map.workflow!r} over "
            f"{item_type.__name__}, but that workflow expects {record.input_type.__name__}"
        )
    collect_output = import_type_ref(spec.project, step.map.collect.output)
    _validate_model_type(collect_output, f"workflow step {step.id!r} collect.output")
    collect_item_type = _collect_field_item_type(collect_output, step.map.collect.field)
    if collect_item_type is None:
        raise TypeError(
            f"workflow step {step.id!r} collect.field {step.map.collect.field!r} must be a list field"
        )
    if collect_item_type is not record.output_type:
        raise TypeError(
            f"workflow step {step.id!r} collect.field {step.map.collect.field!r} expects "
            f"{collect_item_type.__name__}, but sibling workflow {step.map.workflow!r} "
            f"returns {record.output_type.__name__}"
        )
    return collect_output


def _resolve_type_path(context_types: Mapping[str, type], path: str) -> Any:
    parts = path.split(".")
    if len(parts) < 2 or not all(parts):
        raise ValueError(f"workflow map path must use '<context>.<field>': {path}")
    root = context_types.get(parts[0])
    if root is None:
        raise ValueError(f"workflow map path references unknown context value: {parts[0]}")
    current: Any = root
    for field_name in parts[1:]:
        if not _is_basemodel_type(current):
            raise TypeError(f"workflow map path cannot access {field_name!r} on {current!r}")
        fields = current.model_fields
        field = fields.get(field_name)
        if field is None:
            raise ValueError(
                f"workflow map path {path!r} references unknown field {field_name!r} on {current.__name__}"
            )
        current = field.annotation
    return current


def _collect_field_item_type(model_type: type[BaseModel], field_name: str) -> type | None:
    field = model_type.model_fields.get(field_name)
    if field is None:
        raise ValueError(f"collect.output {model_type.__name__} has no field {field_name!r}")
    return _list_item_type(field.annotation)


def _list_item_type(value: Any) -> type | None:
    origin = get_origin(value)
    if origin is not list:
        return None
    args = get_args(value)
    if len(args) != 1 or not isinstance(args[0], type):
        return None
    return args[0]


def _validate_model_type(value: type, field_name: str) -> None:
    if not _is_basemodel_type(value):
        raise TypeError(f"{field_name} must resolve to a Pydantic BaseModel type")


def _is_basemodel_type(value: Any) -> bool:
    return isinstance(value, type) and issubclass(value, BaseModel)


def _build_call_spec(
    spec: TypefluxYamlSpec,
    step: Any,
    activities: Mapping[str, YamlWorkflowActivity],
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
    child_search_attribute: str | None = None,
) -> WorkflowCallSpec:
    when = when_gate_from_spec(step.when) if getattr(step, "when", None) is not None else None
    compensate = _build_compensate_call_spec(spec, step, activities)
    if isinstance(step, WorkflowParallelStepSpec):
        return ParallelCallSpec(
            step_id=step.id,
            branches=tuple(
                ParallelBranchCallSpec(
                    branch_id=branch.id,
                    calls=tuple(
                        _build_call_spec(
                            spec, nested, activities, subworkflows, child_search_attribute
                        )
                        for nested in branch.steps
                    ),
                    when=(when_gate_from_spec(branch.when) if branch.when is not None else None),
                )
                for branch in step.parallel.branches
            ),
            collect=ParallelCollectCallSpec(
                output_type=import_type_ref(spec.project, step.parallel.collect.output),
                max_bytes=step.parallel.collect.max_bytes,
            ),
            when=when,
        )
    if isinstance(step, WorkflowSubworkflowStepSpec):
        record = _resolve_subworkflow_record(subworkflows, step.workflow, step.id)
        return SubworkflowCallSpec(
            step_id=step.id,
            child_workflow_id=step.workflow,
            child_workflow_type=record.workflow_type,
            child_workflow_name=record.workflow_name,
            child_project=record.project,
            child_digest=record.spec_digest,
            output_type=record.output_type,
            search_attributes=_child_search_attributes(child_search_attribute, record),
            id_reuse_policy=_child_id_reuse_policy(),
            parent_close_policy=_child_parent_close_policy(),
            when=when,
            compensate=compensate,
        )
    if isinstance(step, WorkflowMapStepSpec) and step.map.workflow is not None:
        if compensate is not None:
            # A `map.workflow` (child fan-out) carrying compensate is deferred to a later slice —
            # compensation covers activity / map / sub-workflow steps (#299). Reject loud.
            raise ValueError(
                f"workflow step {step.id!r} fans a sub-workflow (`map.workflow`) and cannot carry "
                "`compensate:` in this release — compensation covers activity / map / sub-workflow "
                "steps (#299)"
            )
        record = _resolve_subworkflow_record(subworkflows, step.map.workflow, step.id)
        return MapSubworkflowCallSpec(
            step_id=step.id,
            child_workflow_id=step.map.workflow,
            child_workflow_type=record.workflow_type,
            child_workflow_name=record.workflow_name,
            child_project=record.project,
            child_digest=record.spec_digest,
            output_type=record.output_type,
            over=step.map.over,
            concurrency=step.map.concurrency,
            collect=MapCollectCallSpec(
                output_type=import_type_ref(spec.project, step.map.collect.output),
                field=step.map.collect.field,
                max_bytes=step.map.collect.max_bytes,
            ),
            search_attributes=_child_search_attributes(child_search_attribute, record),
            id_reuse_policy=_child_id_reuse_policy(),
            parent_close_policy=_child_parent_close_policy(),
            when=when,
        )
    if isinstance(step, WorkflowMapStepSpec):
        # The map.workflow arm returned above; a plain map has activity set (model-level XOR).
        assert step.map.activity is not None
        activity = activities[step.map.activity]
        output_type = import_type_ref(spec.project, step.map.collect.output)
        return MapCallSpec(
            step_id=step.id,
            activity_name=activity.name,
            over=step.map.over,
            concurrency=step.map.concurrency,
            collect=MapCollectCallSpec(
                output_type=output_type,
                field=step.map.collect.field,
                max_bytes=step.map.collect.max_bytes,
            ),
            task_queue=activity.task_queue,
            start_to_close_timeout=activity.start_to_close_timeout
            or DEFAULT_START_TO_CLOSE_TIMEOUT,
            retry_policy=_resolve_retry_policy(spec, activity),
            heartbeat_timeout=activity.heartbeat_timeout,
            session_cache=getattr(activity, "session_cache", None),
            when=when,
            compensate=compensate,
        )
    activity = activities[step.activity]
    return ActivityCallSpec(
        step_id=step.id,
        activity_name=activity.name,
        task_queue=activity.task_queue,
        start_to_close_timeout=activity.start_to_close_timeout or DEFAULT_START_TO_CLOSE_TIMEOUT,
        retry_policy=_resolve_retry_policy(spec, activity),
        heartbeat_timeout=activity.heartbeat_timeout,
        when=when,
        compensate=compensate,
        ai=isinstance(activity, AIActivity),
    )


def _build_compensate_call_spec(
    spec: TypefluxYamlSpec,
    step: Any,
    activities: Mapping[str, YamlWorkflowActivity],
) -> CompensateCallSpec | None:
    """Resolve a step's ``compensate:`` into a {@link CompensateCallSpec} (#299 D299-1): the
    compensating activity must be declared (an undeclared name is a load error); its retry is
    the per-compensation override when set, else the compensating activity's own resolved
    bounded retry; timeouts/queue come from the compensating activity's definition."""
    compensate = getattr(step, "compensate", None)
    if compensate is None:
        return None
    comp_activity = activities.get(compensate.activity)
    if comp_activity is None:
        raise ValueError(
            f"workflow step {step.id!r} compensate references activity {compensate.activity!r}, "
            "which is not a declared activity (#299)"
        )
    retry_policy = (
        compensate.retry.to_retry_policy()
        if compensate.retry is not None
        else _resolve_retry_policy(spec, comp_activity)
    )
    return CompensateCallSpec(
        activity_name=comp_activity.name,
        input_from=compensate.input_from,
        task_queue=comp_activity.task_queue,
        start_to_close_timeout=comp_activity.start_to_close_timeout
        or DEFAULT_START_TO_CLOSE_TIMEOUT,
        retry_policy=retry_policy,
        heartbeat_timeout=comp_activity.heartbeat_timeout,
    )


def _resolve_subworkflow_record(
    subworkflows: Mapping[str, ResolvedSubworkflow] | None,
    ref: str,
    step_id: str,
) -> ResolvedSubworkflow:
    # Standalone rejection (#55 §1): a `workflow:` / `map.workflow` reference resolves
    # ONLY through a project manifest. `subworkflows is None` marks a spec loaded
    # standalone; a project-loaded spec always carries a (possibly empty) mapping whose
    # keys are the references collected from the spec, so a missing key is a genuinely
    # undeclared sibling.
    if subworkflows is None:
        raise ValueError(
            f"workflow step {step_id!r} references sibling workflow {ref!r}, but this spec "
            "was loaded standalone — sub-workflow references resolve through a project "
            "manifest (the `workflow:` value is a workflow id declared in "
            "typeflux.project.yaml), so load this workflow via its project (#55)"
        )
    record = subworkflows.get(ref)
    if record is None:
        raise ValueError(
            f"workflow step {step_id!r} references sibling workflow {ref!r}, which is not "
            "declared in the project manifest (#55)"
        )
    return record


def _child_search_attributes(attribute_name: str | None, record: ResolvedSubworkflow) -> Any | None:
    # A child is its OWN workflow, so it stamps its OWN logical name into the configured
    # search attribute (#55 §6) — keeping wide child fan-outs OUT of the parent's frozen
    # scan. The NAME is the ROOT deployment's config, inherited down the whole chain
    # (TS start-context parity — see `create_workflow`'s `inherited_search_attribute`);
    # None ⇒ no attribute, exactly like a top-level start with no
    # `workflow_search_attribute`. Built here (at generation time, outside the sandbox);
    # mirrors runtime._workflow_start_search_attributes.
    if attribute_name is None:
        return None
    from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

    key = SearchAttributeKey.for_keyword(attribute_name)
    return TypedSearchAttributes([SearchAttributePair(key, record.workflow_name)])


def _child_id_reuse_policy() -> Any:
    from temporalio.common import WorkflowIDReusePolicy

    return WorkflowIDReusePolicy.ALLOW_DUPLICATE


def _child_parent_close_policy() -> Any:
    from temporalio.workflow import ParentClosePolicy

    return ParentClosePolicy.TERMINATE


def _child_identity_memo(
    call: SubworkflowCallSpec | MapSubworkflowCallSpec,
    parent_workflow_id: str,
) -> dict[str, str]:
    # The child's NORMAL identity memo (the keys Python stamps at a top-level start —
    # see runtime._workflow_identity_memo) PLUS the parent-link key (decision D2).
    # Temporal's ParentWorkflowExecution stays authoritative; the memo just spares
    # memo-only correlation readers (the CP `children` listing) a round-trip.
    # DRIFT GUARD: this literal key set must stay identity-memo ∪
    # {"typeflux_parent_workflow_id"} — runtime._workflow_identity_memo carries the
    # mirrored comment, and test_yaml_subworkflows pins the sets equal, because the
    # `children` correlation listing joins on these exact keys (a rename here without
    # the counterpart silently breaks parent->child correlation).
    return {
        "typeflux_spec_digest": call.child_digest,
        "typeflux_workflow": call.child_workflow_name,
        "typeflux_project": call.child_project,
        "typeflux_parent_workflow_id": parent_workflow_id,
    }


def _child_start_kwargs(
    workflow: Any,
    call: SubworkflowCallSpec | MapSubworkflowCallSpec,
    child_id: str,
) -> dict[str, Any]:
    info = workflow.info()
    parent_workflow_id = info.workflow_id
    kwargs: dict[str, Any] = {
        "id": child_id,
        "result_type": call.output_type,
        "id_reuse_policy": call.id_reuse_policy,
        "parent_close_policy": call.parent_close_policy,
        "memo": _child_identity_memo(call, parent_workflow_id),
    }
    # A child inherits the parent's SUBJECT ids (#715 slice 1): the children of a
    # subject's review process that subject's data, so the child's execution must
    # carry the same `TypefluxSubjectIds` index entry — otherwise erasure would
    # enumerate the parent but miss its children. Subject ids are only known at
    # runtime (the parent reads them off its OWN search attributes, deterministic
    # in the sandbox), so — unlike the child's logical-name attribute, baked at
    # generation time in `call.search_attributes` — this stamp happens here.
    search_attributes = _child_search_attributes_with_subjects(info, call.search_attributes)
    if search_attributes is not None:
        kwargs["search_attributes"] = search_attributes
    return kwargs


def _run_subject_ids(workflow: Any) -> tuple[str, ...]:
    """The running execution's own subject ids (#715 slice 1), or () outside a
    workflow event loop (unit harnesses drive the generated ``run`` directly)."""

    try:
        info = workflow.info()
    except Exception:  # noqa: BLE001 - only the not-in-workflow harness path lands here
        return ()
    return _parent_subject_ids(info)


def _parent_subject_ids(info: Any) -> tuple[str, ...]:
    """Read the parent execution's subject ids off its own search attributes.

    Deterministic in the workflow sandbox (reading own info is replay-safe). The
    keyword-LIST value comes back as a list of strings; missing ⇒ no subjects."""

    typed = getattr(info, "typed_search_attributes", None)
    if typed is None:
        return ()
    from temporalio.common import SearchAttributeKey

    try:
        value = typed.get(SearchAttributeKey.for_keyword_list(SUBJECT_IDS_SEARCH_ATTRIBUTE))
    except (KeyError, TypeError):
        return ()
    if not value:
        return ()
    return tuple(str(item) for item in value)


def _child_search_attributes_with_subjects(info: Any, base: Any | None) -> Any | None:
    """Merge the inherited ``TypefluxSubjectIds`` into the child's search attributes."""

    subject_ids = _parent_subject_ids(info)
    if not subject_ids:
        return base
    from temporalio.common import (
        SearchAttributeKey,
        SearchAttributePair,
        TypedSearchAttributes,
    )

    subject_pair = SearchAttributePair(
        SearchAttributeKey.for_keyword_list(SUBJECT_IDS_SEARCH_ATTRIBUTE),
        list(subject_ids),
    )
    pairs: list[Any] = []
    if base is not None:
        pairs.extend(pair for pair in base if pair.key.name != SUBJECT_IDS_SEARCH_ATTRIBUTE)
    pairs.append(subject_pair)
    return TypedSearchAttributes(pairs)


async def _execute_subworkflow_call(
    workflow: Any,
    call: SubworkflowCallSpec,
    value: Any,
) -> Any:
    # One sub-workflow step (#55 §3.4): start the child on the running value under a
    # deterministic id `{parent}.{step_id}`, inheriting the parent's task queue (no
    # task_queue kwarg). A failing child fails this branch, exactly like an activity.
    child_id = f"{workflow.info().workflow_id}.{call.step_id}"
    return await workflow.execute_child_workflow(
        call.child_workflow_type,
        value,
        **_child_start_kwargs(workflow, call, child_id),
    )


async def _execute_subworkflow_map_call(
    workflow: Any,
    call: MapSubworkflowCallSpec,
    items: Sequence[Any],
    *,
    lifecycle: _LifecycleRuntime | None = None,
) -> BaseModel:
    # `map.workflow` fan-out (#55 §3.4): V1 map semantics with a child execution per
    # item in place of an activity. Child ids `{parent}.{step_id}-{index}`, bounded
    # concurrency, the same asyncio.wait FIRST_COMPLETED + cancel-wait discipline as the
    # activity map runner (a failing/cancelled fan-out cancels in-flight child tasks).
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise TypeError(f"workflow step {call.step_id!r} map.over did not resolve to a sequence")
    parent_workflow_id = workflow.info().workflow_id
    results: list[Any] = [None] * len(items)
    next_index = 0
    running: dict[Any, int] = {}

    async def _run_child(index: int) -> Any:
        child_id = f"{parent_workflow_id}.{call.step_id}-{index}"
        return await workflow.execute_child_workflow(
            call.child_workflow_type,
            items[index],
            **_child_start_kwargs(workflow, call, child_id),
        )

    cancel_wait = (
        asyncio.create_task(_wait_for_lifecycle_cancel(workflow, lifecycle))
        if lifecycle is not None and lifecycle.cancellation_enabled
        else None
    )
    try:
        while next_index < len(items) or running:
            _raise_if_cancelled(workflow, lifecycle)
            while next_index < len(items) and len(running) < call.concurrency:
                _raise_if_cancelled(workflow, lifecycle)
                task = asyncio.create_task(_run_child(next_index))
                running[task] = next_index
                next_index += 1
            wait_for = set(running.keys())
            if cancel_wait is not None:
                wait_for.add(cancel_wait)
            done, _pending = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait is not None and cancel_wait in done:
                _raise_if_cancelled(workflow, lifecycle)
            # Same batch-drain discipline as the activity map runner (#299 review; ported by
            # #787): FIRST_COMPLETED can return a FAILING child AND already-succeeded children
            # in the SAME done batch. Drain every successful one into results (and its
            # lifecycle progress tick) FIRST, then raise the held error — a batch's successes
            # are never discarded by a racing sibling failure. Keeps the two map runners
            # behaviorally identical, and is load-bearing the day map.workflow grows per-item
            # compensation.
            batch_error: BaseException | None = None
            for task in done:
                if task is cancel_wait:
                    continue
                index = running.pop(task)
                try:
                    results[index] = await task
                except Exception as exc:  # noqa: BLE001 - a business failure; drain the batch's successes first
                    if batch_error is None:
                        batch_error = exc
                    continue
                if lifecycle is not None:
                    lifecycle.unit_completed(workflow, call.step_id)
            if batch_error is not None:
                raise batch_error
    except BaseException:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        raise
    finally:
        if cancel_wait is not None:
            cancel_wait.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_wait
    collected = call.collect.output_type(**{call.collect.field: results})
    _enforce_collect_payload_limit(
        kind="map",
        step_id=call.step_id,
        collected=collected,
        limit=call.collect.max_bytes,
        reduce_hint="reduce fan-out, shrink child outputs,",
    )
    return collected


def _resolve_retry_policy(spec: TypefluxYamlSpec, activity: YamlWorkflowActivity) -> Any:
    # Generated activity calls always carry an explicit bounded retry policy;
    # Temporal's own default is unlimited attempts, which lets a deterministic
    # provider failure retry (and spend) without bound.
    if activity.retry_policy is not None:
        return activity.retry_policy
    if spec.runtime.activity_retry is not None:
        return spec.runtime.activity_retry.to_retry_policy()
    return _default_activity_retry_policy()


def _default_activity_retry_policy() -> Any:
    from temporalio.common import RetryPolicy

    return RetryPolicy(
        maximum_attempts=DEFAULT_ACTIVITY_RETRY_MAXIMUM_ATTEMPTS,
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(seconds=60),
        backoff_coefficient=2.0,
    )


def _activity_kwargs(call: ActivityCallSpec | MapCallSpec, activity_id: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "activity_id": activity_id,
        "start_to_close_timeout": call.start_to_close_timeout,
    }
    if call.heartbeat_timeout is not None:
        kwargs["heartbeat_timeout"] = call.heartbeat_timeout
    if call.task_queue is not None:
        kwargs["task_queue"] = call.task_queue
    if call.retry_policy is not None:
        kwargs["retry_policy"] = call.retry_policy
    return kwargs


def _compensate_activity_kwargs(compensate: CompensateCallSpec, activity_id: str) -> dict[str, Any]:
    # The compensating activity is ordinary (#299): a bounded per-compensation timeout, the
    # per-compensation retry override (or the compensating activity's own resolved bounded retry),
    # and its declared queue/heartbeat.
    kwargs: dict[str, Any] = {
        "activity_id": activity_id,
        "start_to_close_timeout": compensate.start_to_close_timeout,
    }
    if compensate.heartbeat_timeout is not None:
        kwargs["heartbeat_timeout"] = compensate.heartbeat_timeout
    if compensate.task_queue is not None:
        kwargs["task_queue"] = compensate.task_queue
    if compensate.retry_policy is not None:
        kwargs["retry_policy"] = compensate.retry_policy
    return kwargs


def _cache_activity_kwargs(call: MapCallSpec, activity_id: str) -> dict[str, Any]:
    # The cache prep/release activities reuse the map's timeout/retry/queue but
    # NOT its heartbeat_timeout: they emit no heartbeats, so inheriting one would
    # have Temporal kill them (a failure the in-activity fail-soft cannot catch,
    # defeating "caching never fails the workflow"). #60/#368
    kwargs: dict[str, Any] = {
        "activity_id": activity_id,
        "start_to_close_timeout": call.start_to_close_timeout,
    }
    if call.task_queue is not None:
        kwargs["task_queue"] = call.task_queue
    if call.retry_policy is not None:
        kwargs["retry_policy"] = call.retry_policy
    return kwargs


def _resolve_context_path(context: Mapping[str, Any], path: str) -> Any:
    parts = path.split(".")
    current = context[parts[0]]
    for field_name in parts[1:]:
        if isinstance(current, Mapping):
            current = current[field_name]
        else:
            current = getattr(current, field_name)
    return current


async def _execute_map_call(
    workflow: Any,
    call: MapCallSpec,
    items: Sequence[Any],
    *,
    lifecycle: _LifecycleRuntime | None = None,
    record_compensation: Callable[[Any], None] | None = None,
    subject_ids: tuple[str, ...] = (),
) -> BaseModel:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise TypeError(f"workflow step {call.step_id!r} map.over did not resolve to a sequence")
    results: list[Any] = [None] * len(items)
    # (index, result) recorded AS EACH ITEM SETTLES (#299): a mid-fan-out failure still
    # compensates the succeeded items, and the `finally` below records them in ITEM order.
    completed: list[tuple[int, Any]] = []
    next_index = 0
    running: dict[Any, int] = {}
    # Prepare a provider-side cached session once for the whole fan-out (#60).
    # Skip entirely for empty maps (no per-item calls to accelerate). The prep
    # activity fails soft to a supported=False handle when the provider has no
    # caching, so every item still runs — only cost/latency differ.
    cached_session: CachedSessionHandle | None = None
    if call.session_cache is not None and call.session_cache.enabled and items:
        cached_session = await workflow.execute_activity(
            cache_prep_activity_name(call.activity_name),
            # A representative item: the prep activity resolves the step's
            # cache: reference artifacts from it (identical across items). #363
            items[0],
            result_type=CachedSessionHandle,
            **_cache_activity_kwargs(call, f"{call.step_id}.__prepare_cache__"),
        )
    cancel_wait = (
        asyncio.create_task(_wait_for_lifecycle_cancel(workflow, lifecycle))
        if lifecycle is not None and lifecycle.cancellation_enabled
        else None
    )
    try:
        while next_index < len(items) or running:
            _raise_if_cancelled(workflow, lifecycle)
            while next_index < len(items) and len(running) < call.concurrency:
                _raise_if_cancelled(workflow, lifecycle)
                handle = workflow.start_activity(
                    call.activity_name,
                    args=[
                        items[next_index],
                        MapActivityContext(
                            map_step_id=call.step_id,
                            map_index=next_index,
                            map_size=len(items),
                            map_concurrency=call.concurrency,
                            cached_session=cached_session,
                            subject_ids=subject_ids,
                        ),
                    ],
                    **_activity_kwargs(call, f"{call.step_id}-{next_index}"),
                )
                running[handle] = next_index
                next_index += 1
            wait_for = set(running.keys())
            if cancel_wait is not None:
                wait_for.add(cancel_wait)
            done, _pending = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait is not None and cancel_wait in done:
                _raise_if_cancelled(workflow, lifecycle)
            # `FIRST_COMPLETED` can return a FAILING handle AND already-succeeded handles in the
            # SAME `done` batch. Drain every SUCCESSFUL one into `completed` FIRST (#299 review): if
            # we let the first failure raise mid-iteration, the except would cancel the batch's other
            # successes without recording their per-item compensations. Hold the first error and
            # raise it only after the whole batch is drained.
            batch_error: BaseException | None = None
            for handle in done:
                if handle is cancel_wait:
                    continue
                index = running.pop(handle)
                try:
                    results[index] = await handle
                except Exception as exc:  # noqa: BLE001 - a business failure; drain the batch's successes first
                    if batch_error is None:
                        batch_error = exc
                    continue
                completed.append((index, results[index]))
                if lifecycle is not None:
                    lifecycle.unit_completed(workflow, call.step_id)
            if batch_error is not None:
                raise batch_error
    except BaseException:
        for handle in running:
            handle.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        raise
    finally:
        if cancel_wait is not None:
            cancel_wait.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_wait
        # Record per-item compensations for every COMPLETED item IN ITEM order (#299), on BOTH
        # the success and failure paths — so a mid-fan-out failure still compensates the items
        # that succeeded, and the flat LIFO unwinds them in reverse item order.
        if record_compensation is not None:
            for _index, item_result in sorted(completed, key=lambda entry: entry[0]):
                record_compensation(item_result)
    # Best-effort release of a reference-style cache (one with a server-side
    # object, i.e. a cache_id) now that the fan-out is done, so it doesn't linger
    # until TTL (#368). Success path only: scheduling an activity during workflow
    # cancellation/failure isn't reliable, and the cache TTL-expires regardless.
    # The release activity already swallows provider errors; suppress any
    # activity-level failure too so cleanup never fails the workflow.
    if cached_session is not None and cached_session.cache_id is not None:
        with suppress(Exception):
            await workflow.execute_activity(
                cache_release_activity_name(call.activity_name),
                cached_session,
                **_cache_activity_kwargs(call, f"{call.step_id}.__release_cache__"),
            )
    collected = call.collect.output_type(**{call.collect.field: results})
    _enforce_collect_payload_limit(
        kind="map",
        step_id=call.step_id,
        collected=collected,
        limit=call.collect.max_bytes,
        reduce_hint="reduce fan-out, shrink item outputs,",
    )
    return collected


async def _execute_parallel_call(
    workflow: Any,
    call: ParallelCallSpec,
    entry: Any,
    *,
    context: Mapping[str, Any],
    lifecycle: _LifecycleRuntime | None,
    run_branch: Callable[[Sequence[WorkflowCallSpec], Any], Coroutine[Any, Any, Any]],
) -> BaseModel:
    """Execute a parallel block (#55 §5.1): evaluate branch gates in declared order
    against recorded context, run the gated-in branches concurrently under the map
    runner's structured-concurrency discipline (a failing branch cancels in-flight
    siblings and propagates; the cancel-wait task honors cooperative cancellation),
    then assemble the collect object keyed by branch id in DECLARED order (a
    gated-out branch's field is None) and enforce ``collect.max_bytes`` exactly like
    a map's."""
    results: dict[str, Any] = {}
    gated_in: list[ParallelBranchCallSpec] = []
    for branch in call.branches:
        if branch.when is not None and not evaluate_when_gate(branch.when, context):
            # Whole-branch gating: the collect field is None (its schema field is
            # load-validated Optional — see _validate_parallel_step).
            if lifecycle is not None:
                lifecycle.step_skipped(workflow, branch.branch_id, render_when_gate(branch.when))
                lifecycle.skip_units(_lifecycle_total_units(branch.calls, context))
            results[branch.branch_id] = None
        else:
            gated_in.append(branch)
    # All gated-in branches run concurrently (no branch-count concurrency knob in
    # v1: branches are static, few, and heterogeneous). Branch tasks start in
    # declared order; progress is driven solely by recorded completions, so replay
    # is stable.
    tasks: dict[Any, str] = {}
    cancel_wait = (
        asyncio.create_task(_wait_for_lifecycle_cancel(workflow, lifecycle))
        if lifecycle is not None and lifecycle.cancellation_enabled
        else None
    )
    try:
        for branch in gated_in:
            tasks[asyncio.create_task(run_branch(branch.calls, entry))] = branch.branch_id
        pending = set(tasks)
        while pending:
            wait_for = set(pending)
            if cancel_wait is not None:
                wait_for.add(cancel_wait)
            done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait is not None and cancel_wait in done:
                _raise_if_cancelled(workflow, lifecycle)
            for task in done:
                if task is cancel_wait:
                    continue
                pending.discard(task)
                results[tasks[task]] = await task
    except BaseException:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        if cancel_wait is not None:
            cancel_wait.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_wait
    collected = call.collect.output_type(
        **{branch.branch_id: results[branch.branch_id] for branch in call.branches}
    )
    _enforce_collect_payload_limit(
        kind="parallel",
        step_id=call.step_id,
        collected=collected,
        limit=call.collect.max_bytes,
        reduce_hint="shrink branch outputs",
    )
    return collected


def _enforce_collect_payload_limit(
    *,
    kind: str,
    step_id: str,
    collected: BaseModel,
    limit: int,
    reduce_hint: str,
) -> None:
    # An actionable failure before Temporal's opaque ~2MB per-payload limit,
    # shared by map and parallel collects (#495/#55). The size is computed from
    # workflow data, so the check replays deterministically; max_bytes
    # participates in the spec digest because it changes control flow for a
    # given history. The failure type stays per-step-kind (cross-edition wire
    # contract; TS enforceCollectPayloadLimit).
    if limit <= 0:
        return
    size = len(collected.model_dump_json().encode("utf-8"))
    if size <= limit:
        return
    raise _workflow_application_error(
        f"{kind} step {step_id!r} collected payload is {size} bytes, exceeding "
        f"collect.max_bytes={limit}; {reduce_hint} or raise collect.max_bytes",
        error_type=(
            "TypefluxMapCollectPayloadTooLarge"
            if kind == "map"
            else "TypefluxParallelCollectPayloadTooLarge"
        ),
    )


def _lifecycle_enabled(lifecycle: WorkflowLifecycleSpec | None) -> bool:
    return lifecycle is not None and lifecycle.enabled


class _GateState:
    """One gate's mutable runtime state (#55 slice 4). A V1 ``review`` normalizes to a single
    gate named ``"review"``; ``gates`` yields one per declared gate.

    ``open_seq`` orders concurrent waits. NOTE: under DS4-1 (distinct after_step per gate) at
    most ONE gate waits at a time in a v1 sequence, so the plural-open-gate machinery
    (``open_seq`` ordering, ``_waiting_gates`` sorting, the decision union) is unreachable
    beyond one element today. It is kept deliberately as the v2 substrate — gates inside
    parallel branches would wait concurrently — do not simplify it away."""

    def __init__(self, gate: WorkflowLifecycleGateSpec) -> None:
        self.id = gate.id
        self.after_step = gate.after_step
        self.routes = {decision: route.route for decision, route in gate.user_decisions.items()}
        self.invalid_policy = gate.invalid_user_decision
        self.timeout_seconds = gate.timeout.seconds if gate.timeout is not None else None
        self.timeout_action = gate.timeout.on_timeout if gate.timeout is not None else None
        self.timeout_route = gate.timeout.route if gate.timeout is not None else None
        self.waiting = False
        self.open_seq: int | None = None
        self.decision: str | None = None
        self.route_target: str | None = None
        self.fired = False
        # Set when THIS gate's `fail` invalid-decision policy trips; its wait loop raises on
        # the next poll. Per-gate — never runtime-wide — so an invalid decision on one gate can
        # never fail a LATER gate's wait (#55 slice 4 review round, item 3).
        self.invalid_failed = False


class _LifecycleRuntime:
    def __init__(self, spec: WorkflowLifecycleSpec) -> None:
        self.progress_enabled = spec.progress
        self.cancellation_enabled = spec.cancellation
        # Normalize `review`/`gates` to one gate list; `multi_gate` decides whether events
        # carry a gate_id (byte-parity: V1 single-`review` events omit it).
        self._gates = [_GateState(gate) for gate in spec.resolved_gates()]
        # Gate lookups keyed once (no per-signal/per-step linear scans). Unique ids and
        # distinct after_step are load-validated (spec.py), so both maps are total.
        self._gates_by_id = {gate.id: gate for gate in self._gates}
        self._gates_by_after_step = {gate.after_step: gate for gate in self._gates}
        self._multi_gate = spec.gates is not None
        self.status_event_limit = spec.history.status_event_limit
        self.state = "pending"
        self.current_step: str | None = None
        self.completed_units = 0
        self.total_units = 0
        self.cancellation_requested = False
        self.cancellation_reason: str | None = None
        self.waiting_checkpoint: str | None = None
        self.review_command: ReviewCommand | None = None
        self.review_user_decision: str | None = None
        self.review_route_target: str | None = None
        self._review_signal_sequence = 0
        self.terminal_status: str | None = None
        # The terminal compensation outcome (#299 D299-2), set once the unwind runs; None
        # otherwise. Surfaced in status() and, once set, on every subsequently-recorded event.
        self.compensation_status: str | None = None
        self._events: deque[WorkflowLifecycleEvent] = deque(maxlen=self.status_event_limit)
        self._event_count = 0
        self._sequence = 0

    def _gate_id_field(self, gate: _GateState) -> str | None:
        """The gate_id an event carries: the gate's id in multi-gate mode, None otherwise."""
        return gate.id if self._multi_gate else None

    def pending_gate_after(self, step_id: str) -> _GateState | None:
        """The not-yet-fired gate to open after ``step_id`` completes, or None (distinct
        after_step, DS4-1). A gate PRE-DECIDED before its checkpoint (route_target set, not
        fired) is still returned, so the interpreter opens it and routes immediately — V1
        pre-submission parity."""
        gate = self._gates_by_after_step.get(step_id)
        return gate if gate is not None and not gate.fired else None

    def _earliest_waiting_gate(self) -> _GateState | None:
        """The earliest-opened still-waiting gate (drives ``waiting_checkpoint``), or None."""
        earliest: _GateState | None = None
        for gate in self._gates:
            if gate.waiting and (
                earliest is None or (gate.open_seq or 0) < (earliest.open_seq or 0)
            ):
                earliest = gate
        return earliest

    def _recompute_checkpoint(self) -> None:
        """Recompute ``waiting_checkpoint`` WITHOUT touching ``state`` (mid-event, state must hold)."""
        earliest = self._earliest_waiting_gate()
        self.waiting_checkpoint = earliest.after_step if earliest is not None else None

    def _refresh_waiting(self) -> None:
        """Recompute the earliest-opened still-waiting gate's checkpoint + waiting/running state."""
        earliest = self._earliest_waiting_gate()
        if earliest is not None:
            self.state = "waiting_for_review"
            self.waiting_checkpoint = earliest.after_step
        else:
            if self.state == "waiting_for_review":
                self.state = "running"
            self.waiting_checkpoint = None

    def start(self, workflow: Any, *, total_units: int) -> None:
        self.total_units = total_units
        self.state = "running"
        self._record(workflow, event="workflow_started")

    def step_started(self, workflow: Any, step_id: str) -> None:
        self.current_step = step_id
        self.state = "running"
        self._record(workflow, event="step_started", step_id=step_id)

    def step_skipped(self, workflow: Any, step_id: str, condition: str) -> None:
        # A `when:` gate skipped a step or branch (#55 §3.3): provenance for "what
        # did not run and why". `step_id` is the SKIPPED id (current_step is
        # untouched — the step never runs) and `condition` carries the rendered
        # predicate. Skipped work releases its progress units via the caller's
        # skip_units (the existing clamp). TS LifecycleRuntime.stepSkipped parity.
        self._record(workflow, event="step_skipped", step_id=step_id, condition=condition)

    def unit_completed(self, workflow: Any, step_id: str) -> None:
        if self.progress_enabled:
            self.completed_units += 1
        self._record(workflow, event="progress", step_id=step_id)

    def add_total_units(self, count: int | None) -> None:
        if self.progress_enabled and count is not None:
            self.total_units += count

    def skip_units(self, count: int | None) -> None:
        if self.progress_enabled and count is not None:
            self.total_units = max(self.completed_units, self.total_units - count)

    def request_cancel(self, workflow: Any, reason: str | None) -> None:
        if not self.cancellation_enabled:
            return
        self.cancellation_requested = True
        self.cancellation_reason = reason
        self.state = "cancelling"
        self._record(workflow, event="cancellation_requested")

    def _target_gate(self, gate_id: str | None) -> _GateState | None:
        """Resolve which gate a submit_review command targets (#55 §8, decision DS4-2):
        explicit id ⇒ that gate, provided it has NOT already fired — a decision can be
        pre-staged on a not-yet-open gate, but a routed/timed-out gate is closed and is not
        targetable (never-guess rule; #55 slice 4 review round, item 3a). Unknown ids likewise.
        absent + a single-gate workflow ⇒ that one gate (V1 pre-submission verbatim);
        absent + several gates ⇒ the sole currently-waiting gate, or None (ambiguous)."""
        if gate_id is not None:
            explicit = self._gates_by_id.get(gate_id)
            return explicit if explicit is not None and not explicit.fired else None
        if len(self._gates) == 1:
            return self._gates[0]
        waiting = [gate for gate in self._gates if gate.waiting]
        return waiting[0] if len(waiting) == 1 else None

    def submit_review(self, workflow: Any, command: ReviewCommand | dict[str, Any]) -> None:
        self._review_signal_sequence += 1
        try:
            review_command = (
                command
                if isinstance(command, ReviewCommand)
                else ReviewCommand.model_validate(command)
            )
        except Exception:
            # Malformed shape: attribute the fail policy only when there's a single unambiguous gate.
            sole = self._gates[0] if len(self._gates) == 1 else None
            if sole is not None and sole.invalid_policy == "fail":
                sole.invalid_failed = True
            self._record(workflow, event="review_invalid_user_decision")
            return
        gate = self._target_gate(review_command.gate)
        if gate is None:
            # Unknown/closed gate id, or ambiguous/absent target with several gates — record,
            # never guess. The ATTEMPTED gate id (when the command carried one) joins the event
            # so the audit trail distinguishes a mistyped/closed gate from an unknown decision.
            self._record(
                workflow,
                event="review_invalid_user_decision",
                gate_id=review_command.gate,
            )
            return
        route_target = gate.routes.get(review_command.user_decision)
        if route_target is None:
            if gate.invalid_policy == "fail":
                gate.invalid_failed = True
            self._record(
                workflow,
                event="review_invalid_user_decision",
                gate_id=self._gate_id_field(gate),
            )
            return
        gate.decision = review_command.user_decision
        gate.route_target = route_target
        self.review_command = review_command
        # Singleton wire fields reflect the just-submitted decision, so a status query in the
        # post-submit/pre-route window shows it exactly as the V1 single gate did (byte-parity).
        self.review_user_decision = review_command.user_decision
        self.review_route_target = route_target
        self._record(
            workflow,
            event="review_submitted",
            review_user_decision=review_command.user_decision,
            review_route_target=route_target,
            gate_id=self._gate_id_field(gate),
        )

    def waiting_for_gate(self, workflow: Any, gate: _GateState) -> None:
        gate.waiting = True
        gate.open_seq = self._sequence + 1  # the sequence this event will carry
        self._refresh_waiting()
        self._record(
            workflow,
            event="waiting_for_review",
            step_id=gate.after_step,
            gate_id=self._gate_id_field(gate),
        )

    def gate_routed(self, workflow: Any, gate: _GateState) -> str:
        gate.waiting = False
        gate.open_seq = None
        gate.fired = True
        self.review_user_decision = gate.decision
        self.review_route_target = gate.route_target
        self._refresh_waiting()
        route_target = gate.route_target
        self._record(
            workflow,
            event="review_routed",
            step_id=gate.after_step,
            review_user_decision=gate.decision,
            review_route_target=route_target,
            gate_id=self._gate_id_field(gate),
        )
        if route_target is None:
            raise _workflow_application_error(
                "workflow review route missing",
                error_type="TypefluxReviewRouteMissing",
            )
        return route_target

    def gate_timed_out(self, workflow: Any, gate: _GateState) -> str | None:
        """Apply a gate's timeout action (#297). Returns a route target for the
        ``route`` action; raises for ``fail``; marks cancellation for ``cancel``
        (the caller's cancel check then terminates). Records a distinct event so
        status/audit show the timeout (never reviewer notes)."""
        gate.waiting = False
        gate.open_seq = None
        gate.fired = True
        # The gate stops driving the checkpoint, but ``state`` stays "waiting_for_review" for the
        # review_timed_out event — the action flips state only AFTER (V1 single-gate byte-parity).
        self._recompute_checkpoint()
        self._record(
            workflow,
            event="review_timed_out",
            step_id=gate.after_step,
            gate_id=self._gate_id_field(gate),
        )
        if gate.timeout_action == "route":
            route_target = gate.timeout_route
            # Reflect the selected route in status (#297 review): status queries
            # and status-derived metadata read review_route_target, so it must be
            # set — like a submitted decision route — not only recorded on the event.
            # The singleton decision clears with it: an earlier gate's submitted
            # decision must not pair with this gate's timeout route in status
            # (no-op single-gate — a timed-out gate was never decided).
            gate.decision = None
            gate.route_target = route_target
            self.review_user_decision = None
            self.review_route_target = route_target
            # Back to running only when no gate still waits (another gate keeps waiting_for_review).
            self._refresh_waiting()
            self._record(
                workflow,
                event="review_routed",
                step_id=gate.after_step,
                review_route_target=route_target,
                gate_id=self._gate_id_field(gate),
            )
            if route_target is None:  # validated at load; defensive
                raise _workflow_application_error(
                    "workflow review timeout route missing",
                    error_type="TypefluxReviewRouteMissing",
                )
            return route_target
        if gate.timeout_action == "cancel":
            self.cancellation_requested = True
            self.cancellation_reason = "review timed out"
            self.state = "cancelling"
            return None
        # fail-closed (default)
        self.state = "failed"
        self.terminal_status = "failed"
        raise _workflow_application_error(
            f"workflow review timed out at {gate.after_step}",
            error_type="TypefluxReviewTimeout",
        )

    def invalid_review_failed(self, workflow: Any) -> None:
        self.state = "failed"
        self.terminal_status = "failed"
        self._record(workflow, event="review_invalid_user_decision_failed")

    def compensation_started(self, workflow: Any, step_id: str) -> None:
        # `step_id` is the ORIGINAL compensated step id (always present on compensation events,
        # never inheriting current_step) (#299 D299-2).
        self._record(workflow, event="compensation_started", step_id=step_id)

    def compensation_completed(self, workflow: Any, step_id: str) -> None:
        self._record(workflow, event="compensation_completed", step_id=step_id)

    def compensation_failed(self, workflow: Any, step_id: str) -> None:
        # Recorded, but the unwind continues (best-effort, loud) (#299 D299-2).
        self._record(workflow, event="compensation_failed", step_id=step_id)

    def cancelled(self, workflow: Any, compensation_status: str = "none") -> None:
        # The unwind ran BEFORE this terminal event (#299 D299-2a); carry its outcome.
        self.compensation_status = compensation_status
        self.state = "cancelled"
        self.terminal_status = "cancelled"
        self._record(workflow, event="workflow_cancelled")

    def completed(self, workflow: Any) -> None:
        self.state = "completed"
        self.terminal_status = "completed"
        self.current_step = None
        self._record(workflow, event="workflow_completed")

    def failed(self, workflow: Any, exc: BaseException, compensation_status: str = "none") -> None:
        # The compensation outcome is recorded even when a terminal state was already set (e.g. a
        # review-timeout `fail`): status queries then still surface how the unwind resolved (#299).
        if self.compensation_status is None:
            self.compensation_status = compensation_status
        if self.terminal_status is not None:
            return
        self.state = "failed"
        self.terminal_status = "failed"
        self._record(workflow, event="workflow_failed", error_type=type(exc).__name__)

    def _waiting_gates(self) -> tuple[WaitingGate, ...]:
        """The execution-reported waiting gates, ordered by open time — ``()`` when none
        (the status-field convention, like ``events``; #55 §8)."""
        open_gates = sorted(
            (gate for gate in self._gates if gate.waiting),
            key=lambda gate: gate.open_seq or 0,
        )
        if not open_gates:
            return ()
        return tuple(
            WaitingGate(
                gate_id=gate.id,
                after_step=gate.after_step,
                # Sorted, like the resolved-spec valid_user_decisions — stable wire order.
                valid_user_decisions={
                    decision: gate.routes[decision] for decision in sorted(gate.routes)
                },
            )
            for gate in open_gates
        )

    def status(self) -> WorkflowLifecycleStatus:
        return WorkflowLifecycleStatus(
            state=self.state,
            current_step=self.current_step,
            completed_units=self.completed_units,
            total_units=self.total_units,
            cancellation_requested=self.cancellation_requested,
            cancellation_reason=self.cancellation_reason,
            waiting_checkpoint=self.waiting_checkpoint,
            review_user_decision=self.review_user_decision,
            review_route_target=self.review_route_target,
            terminal_status=self.terminal_status,
            event_count=self._event_count,
            events_truncated=self._event_count > len(self._events),
            oldest_event_sequence=self._events[0].sequence if self._events else None,
            latest_event_sequence=self._sequence or None,
            events=tuple(self._events),
            waiting_gates=self._waiting_gates(),
            compensation_status=self.compensation_status,
        )

    def _record(
        self,
        workflow: Any,
        *,
        event: str,
        step_id: str | None = None,
        review_user_decision: str | None = None,
        review_route_target: str | None = None,
        error_type: str | None = None,
        condition: str | None = None,
        gate_id: str | None = None,
    ) -> None:
        self._sequence += 1
        self._event_count += 1
        self._events.append(
            WorkflowLifecycleEvent(
                sequence=self._sequence,
                state=self.state,
                event=event,
                timestamp=_workflow_timestamp(workflow),
                step_id=step_id or self.current_step,
                completed_units=self.completed_units,
                total_units=self.total_units,
                cancellation_requested=self.cancellation_requested,
                cancellation_reason=self.cancellation_reason,
                waiting_checkpoint=self.waiting_checkpoint,
                review_user_decision=review_user_decision,
                review_route_target=review_route_target,
                terminal_status=self.terminal_status or error_type,
                condition=condition,
                gate_id=gate_id,
                # Present-only (#299): None (and excluded) until the unwind sets it, so
                # non-compensating event streams are byte-identical; carried on the terminal event.
                compensation_status=self.compensation_status,
            )
        )


def _lifecycle_query(class_name: str):
    def typeflux_lifecycle_status(self) -> WorkflowLifecycleStatus:
        lifecycle = getattr(self, "_typeflux_lifecycle", None)
        if lifecycle is None:
            return WorkflowLifecycleStatus(state="disabled")
        return lifecycle.status()

    typeflux_lifecycle_status.__name__ = "typeflux_lifecycle_status"
    typeflux_lifecycle_status.__qualname__ = f"{class_name}.typeflux_lifecycle_status"
    typeflux_lifecycle_status.__annotations__ = {"return": WorkflowLifecycleStatus}

    from temporalio import workflow

    return workflow.query(name="typeflux_lifecycle_status")(typeflux_lifecycle_status)


def _lifecycle_cancel_signal(class_name: str):
    def typeflux_request_cancel(self, reason: str | None = None) -> None:
        from temporalio import workflow

        lifecycle = getattr(self, "_typeflux_lifecycle", None)
        if lifecycle is not None:
            lifecycle.request_cancel(workflow, reason)

    typeflux_request_cancel.__name__ = "typeflux_request_cancel"
    typeflux_request_cancel.__qualname__ = f"{class_name}.typeflux_request_cancel"
    typeflux_request_cancel.__annotations__ = {"reason": str | None, "return": None}

    from temporalio import workflow

    return workflow.signal(name="typeflux_request_cancel")(typeflux_request_cancel)


def _lifecycle_review_signal(class_name: str):
    def typeflux_submit_review(self, command: ReviewCommand | dict[str, Any]) -> None:
        from temporalio import workflow

        lifecycle = getattr(self, "_typeflux_lifecycle", None)
        if lifecycle is not None:
            lifecycle.submit_review(workflow, command)

    typeflux_submit_review.__name__ = "typeflux_submit_review"
    typeflux_submit_review.__qualname__ = f"{class_name}.typeflux_submit_review"
    typeflux_submit_review.__annotations__ = {
        "command": ReviewCommand | dict[str, Any],
        "return": None,
    }

    from temporalio import workflow

    return workflow.signal(name="typeflux_submit_review")(typeflux_submit_review)


def _lifecycle_total_units(calls: Sequence[WorkflowCallSpec], context: Mapping[str, Any]) -> int:
    # Recursive (#55 §8): 1 per activity, resolvable map lengths, and a parallel
    # block's total is the SUM of its branches' — so branch subsequences count
    # exactly like inline steps (TS totalUnitsOf parity).
    total = 0
    for call in calls:
        if isinstance(call, ActivityCallSpec):
            total += 1
        elif isinstance(call, SubworkflowCallSpec):
            # A plain sub-workflow step is one parent unit (its interior progress is
            # the child execution's own lifecycle) (#55 §8).
            total += 1
        elif isinstance(call, ParallelCallSpec):
            for branch in call.branches:
                total += _lifecycle_total_units(branch.calls, context)
        else:
            # MapCallSpec / MapSubworkflowCallSpec: one unit per resolvable item.
            total += _sequence_length(_try_resolve_context_path(context, call.over)) or 0
    return total


def _try_resolve_context_path(context: Mapping[str, Any], path: str) -> Any | None:
    try:
        return _resolve_context_path(context, path)
    except (AttributeError, KeyError, TypeError):
        return None


def _sequence_length(value: Any) -> int | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    return None


def _context_path_root(path: str) -> str | None:
    parts = path.split(".", 1)
    return parts[0] if parts and parts[0] else None


async def _maybe_wait_for_review(
    workflow: Any,
    lifecycle: _LifecycleRuntime | None,
    step_id: str,
) -> str | None:
    gate = lifecycle.pending_gate_after(step_id) if lifecycle is not None else None
    if lifecycle is None or gate is None:
        return None
    lifecycle.waiting_for_gate(workflow, gate)
    observed_review_sequence = lifecycle._review_signal_sequence
    # An ABSOLUTE deadline, fixed once (#297 review): each wait passes the
    # *remaining* time, so signals that don't resolve the gate (e.g. repeated
    # invalid "warn" decisions) can't refresh the timer and defer the bound
    # forever. _review_clock is workflow.now() in a real (replay-safe) workflow.
    review_deadline: float | None = None
    if gate.timeout_seconds is not None:
        review_deadline = _review_clock(workflow) + gate.timeout_seconds
    while True:
        _raise_if_cancelled(workflow, lifecycle)
        # THIS gate's fail flag — per-gate, so an invalid decision on another (already-closed)
        # fail-policy gate can never fail this gate's wait (#55 slice 4 review round).
        if gate.invalid_failed:
            lifecycle.invalid_review_failed(workflow)
            raise _workflow_application_error(
                f"workflow review received invalid user_decision at {step_id}",
                error_type="TypefluxInvalidReviewDecision",
            )
        # The gate's OWN route target — not the singleton (last-resolved across gates).
        if gate.route_target is not None:
            return lifecycle.gate_routed(workflow, gate)
        remaining: timedelta | None = None
        if review_deadline is not None:
            remaining_seconds = review_deadline - _review_clock(workflow)
            if remaining_seconds <= 0:
                return _apply_review_timeout(workflow, lifecycle, gate)
            remaining = timedelta(seconds=remaining_seconds)
        try:
            await _wait_lifecycle_condition(
                workflow,
                lambda: (
                    lifecycle._review_signal_sequence > observed_review_sequence
                    or lifecycle.cancellation_requested
                ),
                timeout=remaining,
            )
        except TimeoutError:
            return _apply_review_timeout(workflow, lifecycle, gate)
        observed_review_sequence = lifecycle._review_signal_sequence


def _review_clock(workflow: Any) -> float:
    # Reference time for the review-timeout deadline. In a real workflow this is
    # workflow.now() (deterministic + replay-safe); the in-process test fallback
    # uses the event-loop clock. Only used within one wait, so the two clocks
    # never mix across a run.
    now = getattr(workflow, "now", None)
    if callable(now):
        try:
            value = now()
            if isinstance(value, datetime):
                return value.timestamp()
        except Exception:
            pass
    return asyncio.get_running_loop().time()


def _apply_review_timeout(
    workflow: Any, lifecycle: _LifecycleRuntime, gate: _GateState
) -> str | None:
    # The timeout action (fail / cancel / route). For cancel, gate_timed_out
    # marks cancellation and the check below terminates the workflow.
    route_target = lifecycle.gate_timed_out(workflow, gate)
    _raise_if_cancelled(workflow, lifecycle)
    return route_target


async def _wait_for_lifecycle_cancel(
    workflow: Any,
    lifecycle: _LifecycleRuntime | None,
) -> None:
    if lifecycle is None:
        return
    await _wait_lifecycle_condition(workflow, lambda: lifecycle.cancellation_requested)


async def _wait_lifecycle_condition(
    workflow: Any, predicate, timeout: timedelta | None = None
) -> None:
    # A timeout uses Temporal's durable, replay-safe timer; on expiry
    # wait_condition raises TimeoutError (asyncio.TimeoutError is its alias on
    # 3.11+), which the caller maps to the review-timeout action (#297).
    wait_condition = getattr(workflow, "wait_condition", None)
    if wait_condition is not None and _in_workflow_context(workflow):
        if timeout is not None:
            await wait_condition(predicate, timeout=timeout)
        else:
            await wait_condition(predicate)
        return
    # In-process test fallback only: poll, honoring the timeout against the event
    # loop clock (real workflows never take this path; see _in_workflow_context).
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout.total_seconds()
    while not predicate():
        if deadline is not None and loop.time() >= deadline:
            raise TimeoutError("lifecycle condition wait timed out")
        await asyncio.sleep(0.01)


def _in_workflow_context(workflow: Any) -> bool:
    # In-process test instances call run() without a Temporal workflow event
    # loop; only they may use the polling fallback. Inside a real workflow,
    # wait_condition failures must propagate instead of degrading into a
    # short-interval durable timer loop.
    in_workflow = getattr(workflow, "in_workflow", None)
    if not callable(in_workflow):
        return True
    try:
        return bool(in_workflow())
    except Exception:
        return False


def _raise_if_cancelled(workflow: Any, lifecycle: _LifecycleRuntime | None) -> None:
    if lifecycle is None or not lifecycle.cancellation_requested:
        return
    # The terminal `workflow_cancelled` event is NO LONGER recorded here (#299 D299-2a):
    # cancellation ALSO unwinds the compensation LIFO, and the terminal event must carry the
    # resulting compensation_status, so the single outer handler records it AFTER the unwind.
    raise _workflow_application_error(
        "workflow cancellation requested",
        error_type="TypefluxWorkflowCancelled",
    )


def _is_typeflux_cancellation(exc: BaseException) -> bool:
    """True for the cooperative TypefluxWorkflowCancelled (an ApplicationError with that type,
    or the temporalio-less RuntimeError fallback) — distinct from a workflow FAILURE."""
    if getattr(exc, "type", None) == "TypefluxWorkflowCancelled":
        return True
    return isinstance(exc, RuntimeError) and str(exc).startswith("TypefluxWorkflowCancelled:")


def _workflow_application_error(message: str, *, error_type: str) -> BaseException:
    try:
        from temporalio.exceptions import ApplicationError

        return ApplicationError(message, type=error_type, non_retryable=True)
    except ModuleNotFoundError:
        return RuntimeError(f"{error_type}: {message}")


def _workflow_timestamp(workflow: Any) -> str | None:
    now = getattr(workflow, "now", None)
    if now is not None:
        try:
            value = now()
            if isinstance(value, datetime):
                return value.isoformat()
        except Exception:
            pass
    return None


def _generated_workflow_class_name(spec: TypefluxYamlSpec, spec_digest: str) -> str:
    digest = spec_digest[:12]
    return f"{_identifier(spec.project)}_{_identifier(spec.name)}_{_identifier(spec.workflow.name)}_{digest}"


def _identifier(value: str) -> str:
    identifier = re.sub(r"\W+", "_", value).strip("_")
    if not identifier:
        return "workflow"
    if identifier[0].isdigit():
        return f"_{identifier}"
    return identifier


__all__ = [
    "ActivityCallSpec",
    "DEFAULT_START_TO_CLOSE_TIMEOUT",
    "MapCallSpec",
    "MapCollectCallSpec",
    "MapSubworkflowCallSpec",
    "ParallelBranchCallSpec",
    "ParallelCallSpec",
    "ParallelCollectCallSpec",
    "ResolvedSubworkflow",
    "SubworkflowCallSpec",
    "WhenGate",
    "WhenLeaf",
    "WorkflowCallSpec",
    "YAML_WORKFLOW_MODULE",
    "collect_subworkflow_references",
    "create_workflow",
    "create_yaml_workflow_runner",
    "evaluate_when_gate",
    "flatten_call_specs",
    "render_when_gate",
    "validate_unique_yaml_workflow_names",
    "when_gate_from_spec",
]
