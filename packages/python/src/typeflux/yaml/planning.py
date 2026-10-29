"""Shared typed YAML graph validation and predicates; no Temporal execution."""

from __future__ import annotations

import json
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Union, cast, get_args, get_origin

from pydantic import BaseModel

from typeflux.core.contracts import (
    AIActivity,
    YamlWorkflowActivity,
)
from typeflux.yaml.imports import import_type_ref
from typeflux.yaml.spec import (
    TypefluxYamlSpec,
    WhenAllSpec,
    WhenAnySpec,
    WhenLeafSpec,
    WhenSpec,
    WorkflowAnyStepSpec,
    WorkflowMapStepSpec,
    WorkflowParallelStepSpec,
    WorkflowSubworkflowStepSpec,
)


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


def _resolve_context_path(context: Mapping[str, Any], path: str) -> Any:
    parts = path.split(".")
    current = context[parts[0]]
    for field_name in parts[1:]:
        if isinstance(current, Mapping):
            current = current[field_name]
        else:
            current = getattr(current, field_name)
    return current


@dataclass(frozen=True)
class WorkflowPlan:
    """Validated computational shape, before an engine binds execution options.

    This does not attest runtime capabilities or construct a Temporal workflow.
    User model/activity imports retain the existing trusted-code boundary.
    """

    input_type: type[BaseModel]
    output_type: type[BaseModel]
    steps: tuple[WorkflowAnyStepSpec, ...]


def plan_workflow(
    spec: TypefluxYamlSpec,
    activities: Mapping[str, YamlWorkflowActivity],
    *,
    subworkflows: Mapping[str, ResolvedSubworkflow] | None = None,
) -> WorkflowPlan:
    """Resolve input/output models and validate the shared graph without an SDK."""
    input_type = import_type_ref(spec.project, spec.workflow.input)
    output_type = import_type_ref(spec.project, spec.workflow.output)
    _validate_model_type(input_type, "workflow.input")
    _validate_model_type(output_type, "workflow.output")
    _validate_workflow_graph(spec, activities, input_type, output_type, subworkflows)
    return WorkflowPlan(input_type, output_type, tuple(spec.workflow.steps))
