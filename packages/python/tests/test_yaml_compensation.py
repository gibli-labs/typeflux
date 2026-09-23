"""First-class YAML compensation (#299 SLICE 1) — Python edition.

Load-time rejections, digest present-only, and the in-process interpreter LIFO (reverse-order
unwind on failure AND cancellation, per-item map reverse order, parallel-sibling participation,
when-skip pushes nothing, input_from resolution, and compensation-failure = partial with the
original error preserved). The end-to-end live proof is in test_live_compensation.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "replay_fixtures"

# Inline activity definitions shared by the specs below (types come from replay_demo_project.schemas).
_DEFS = """
    - { name: book_hotel, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: book_flight, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: charge, input: schemas:MiddleModel, output: schemas:FailModel, prompt: t }
    - { name: cancel_hotel, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: cancel_flight, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: cancel_wrong, input: schemas:FailModel, output: schemas:FailModel, prompt: t }
"""


def _prime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]


def _spec_text(
    steps: str,
    *,
    output: str = "schemas:FailModel",
    input_ref: str = "schemas:MiddleModel",
    defs: str = _DEFS,
) -> str:
    return f"""
project: replay_demo_project
name: unit_saga
task_queue: q
runtime:
  temporal: {{}}
  registry: {{ type: inline, prompts: {{ t: hello }} }}
  provider: {{ type: custom, class: replay_demo_project.fakes:ReplaySagaProvider }}
  imports: {{ allow_provider_class: true }}
  observability: {{ type: none }}
activities:
  definitions:{defs}
workflow:
  name: UnitSaga
  input: {input_ref}
  output: {output}
  lifecycle: {{ enabled: true }}
  steps:
{steps}
"""


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: str, **kw: Any) -> type:
    _prime(monkeypatch)
    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

    path = tmp_path / "typeflux.yaml"
    path.write_text(_spec_text(steps, **kw), encoding="utf-8")
    spec = load_yaml_spec(path, load_dotenv=False)
    return create_workflow(spec, collect_activities(spec))


def _record_activities(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any], *, fail: set[str] = frozenset()
) -> list[tuple[str, Any]]:
    import temporalio.workflow

    calls: list[tuple[str, Any]] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append((name, arg))
        if name in fail:
            raise RuntimeError(f"{name} failed")
        return outputs[name](arg)

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)
    return calls


# ---------------------------------------------------------------------------
# Interpreter LIFO
# ---------------------------------------------------------------------------

_SAGA_STEPS = """    - id: book_hotel
      activity: book_hotel
      compensate: { activity: cancel_hotel }
    - id: book_flight
      activity: book_flight
      compensate: { activity: cancel_flight, retry: { maximum_attempts: 1 } }
    - id: charge
      activity: charge
"""


@pytest.mark.asyncio
async def test_failure_unwinds_in_reverse_and_records_complete(tmp_path, monkeypatch) -> None:
    cls = _build(tmp_path, monkeypatch, _SAGA_STEPS)
    from replay_demo_project.schemas import MiddleModel

    calls = _record_activities(
        monkeypatch,
        {
            "book_hotel": lambda a: MiddleModel(value="hotel"),
            "book_flight": lambda a: MiddleModel(value="flight"),
            "cancel_hotel": lambda a: MiddleModel(value="undone"),
            "cancel_flight": lambda a: MiddleModel(value="undone"),
        },
        fail={"charge"},
    )
    instance = cls()
    with pytest.raises(RuntimeError, match="charge failed"):
        await instance.run(MiddleModel(value="start"))
    # Compensations ran in REVERSE step order (LIFO), each on the compensated step's own output.
    comp = [(n, a.value) for (n, a) in calls if n.startswith("cancel_")]
    assert comp == [("cancel_flight", "flight"), ("cancel_hotel", "hotel")]
    status = instance._typeflux_lifecycle.status()
    assert status.terminal_status == "failed"
    assert status.compensation_status == "complete"
    events = [(e.event, e.step_id) for e in status.events if e.event.startswith("compensation_")]
    assert events == [
        ("compensation_started", "book_flight"),
        ("compensation_completed", "book_flight"),
        ("compensation_started", "book_hotel"),
        ("compensation_completed", "book_hotel"),
    ]


@pytest.mark.asyncio
async def test_compensation_failure_is_partial_and_preserves_original_error(
    tmp_path, monkeypatch
) -> None:
    cls = _build(tmp_path, monkeypatch, _SAGA_STEPS)
    from replay_demo_project.schemas import MiddleModel

    _record_activities(
        monkeypatch,
        {
            "book_hotel": lambda a: MiddleModel(value="hotel"),
            "book_flight": lambda a: MiddleModel(value="flight"),
            "cancel_hotel": lambda a: MiddleModel(value="undone"),
        },
        fail={"charge", "cancel_flight"},
    )
    instance = cls()
    # The ORIGINAL error (charge), never the compensation failure, propagates.
    with pytest.raises(RuntimeError, match="charge failed"):
        await instance.run(MiddleModel(value="start"))
    status = instance._typeflux_lifecycle.status()
    assert status.compensation_status == "partial"
    events = [(e.event, e.step_id) for e in status.events if e.event.startswith("compensation_")]
    assert events == [
        ("compensation_started", "book_flight"),
        ("compensation_failed", "book_flight"),
        ("compensation_started", "book_hotel"),
        ("compensation_completed", "book_hotel"),
    ]


@pytest.mark.asyncio
async def test_cancellation_also_unwinds(tmp_path, monkeypatch) -> None:
    cls = _build(tmp_path, monkeypatch, _SAGA_STEPS)
    from replay_demo_project.schemas import MiddleModel

    instance = cls()

    def flight(_a: Any) -> MiddleModel:
        # A cancel signal lands while book_flight runs; the next loop check unwinds (D299-2a).
        instance._typeflux_lifecycle.cancellation_requested = True
        return MiddleModel(value="flight")

    calls = _record_activities(
        monkeypatch,
        {
            "book_hotel": lambda a: MiddleModel(value="hotel"),
            "book_flight": flight,
            "cancel_hotel": lambda a: MiddleModel(value="undone"),
            "cancel_flight": lambda a: MiddleModel(value="undone"),
        },
    )
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        await instance.run(MiddleModel(value="start"))
    assert getattr(excinfo.value, "type", None) == "TypefluxWorkflowCancelled"
    comp = [n for (n, _a) in calls if n.startswith("cancel_")]
    assert comp == ["cancel_flight", "cancel_hotel"]
    status = instance._typeflux_lifecycle.status()
    assert status.terminal_status == "cancelled"
    assert status.compensation_status == "complete"


@pytest.mark.asyncio
async def test_input_from_overrides_the_default_own_output(tmp_path, monkeypatch) -> None:
    steps = """    - id: book_hotel
      activity: book_hotel
      compensate: { activity: cancel_hotel, input_from: input }
    - id: charge
      activity: charge
"""
    cls = _build(tmp_path, monkeypatch, steps)
    from replay_demo_project.schemas import MiddleModel

    calls = _record_activities(
        monkeypatch,
        {
            "book_hotel": lambda a: MiddleModel(value="hotel"),
            "cancel_hotel": lambda a: MiddleModel(value="undone"),
        },
        fail={"charge"},
    )
    with pytest.raises(RuntimeError, match="charge failed"):
        await cls().run(MiddleModel(value="ORIGINAL"))
    # cancel_hotel received the WORKFLOW INPUT (input_from: input), not book_hotel's output.
    cancel_arg = next(a for (n, a) in calls if n == "cancel_hotel")
    assert cancel_arg.value == "ORIGINAL"


@pytest.mark.asyncio
async def test_when_skipped_step_pushes_no_compensation(tmp_path, monkeypatch) -> None:
    # A parallel block: branch A completes (compensation recorded); branch B's only step is
    # gated OUT (pushes nothing). A failing tail then unwinds ONLY branch A's compensation.
    steps = """    - id: fan
      parallel:
        branches:
          - id: a
            steps:
              - { id: book_hotel, activity: book_hotel, compensate: { activity: cancel_hotel } }
          - id: b
            steps:
              - id: book_flight
                activity: book_flight
                when: { path: input.value, eq: "RUNB" }
                compensate: { activity: cancel_flight }
        collect: { output: schemas:FanModel }
    - id: charge
      activity: charge
"""
    defs = (
        _DEFS
        + "    - { name: charge2, input: schemas:FanModel, output: schemas:FailModel, prompt: t }\n"
    )
    # charge consumes the collect object, so retype it to the fan collect model.
    steps = steps.replace("activity: charge", "activity: charge2")
    cls = _build(tmp_path, monkeypatch, steps, input_ref="schemas:MiddleModel", defs=defs)
    from replay_demo_project.schemas import MiddleModel

    calls = _record_activities(
        monkeypatch,
        {
            "book_hotel": lambda a: MiddleModel(value="hotel"),
            "cancel_hotel": lambda a: MiddleModel(value="undone"),
        },
        fail={"charge2"},
    )
    with pytest.raises(RuntimeError, match="charge2 failed"):
        await cls().run(MiddleModel(value="not-runb"))
    comp = [n for (n, _a) in calls if n.startswith("cancel_")]
    # book_flight was gated out -> cancel_flight NEVER runs; only branch A's cancel_hotel does.
    assert comp == ["cancel_hotel"]


@pytest.mark.asyncio
async def test_map_compensates_each_item_in_reverse_item_order(tmp_path, monkeypatch) -> None:
    import temporalio.workflow

    steps = """    - id: review
      map:
        activity: review_item
        over: input.items
        concurrency: 3
        collect: { output: schemas:BatchModel, field: results }
      compensate: { activity: cancel_item }
    - id: charge
      activity: charge3
"""
    defs = """
    - { name: review_item, input: schemas:InputModel, output: schemas:MiddleModel, prompt: t }
    - { name: cancel_item, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: charge3, input: schemas:BatchModel, output: schemas:FailModel, prompt: t }
"""
    cls = _build(tmp_path, monkeypatch, steps, input_ref="schemas:BatchInputModel", defs=defs)
    from replay_demo_project.schemas import BatchInputModel, InputModel, MiddleModel

    order: list[str] = []

    async def fake_map_activity(name: str, arg: Any, ctx: Any = None, **kwargs: Any) -> Any:
        del ctx
        return MiddleModel(value=f"r{arg.value}")

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "charge3":
            raise RuntimeError("charge3 failed")
        if name == "cancel_item":
            order.append(arg.value)
            return MiddleModel(value="undone")
        raise AssertionError(name)

    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)
    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    with pytest.raises(RuntimeError, match="charge3 failed"):
        await cls().run(
            BatchInputModel(
                items=[InputModel(value="0"), InputModel(value="1"), InputModel(value="2")]
            )
        )
    # Per-item compensation in REVERSE item order (each on its own item result).
    assert order == ["r2", "r1", "r0"]


@pytest.mark.asyncio
async def test_map_mid_fanout_failure_compensates_already_succeeded_items(
    tmp_path, monkeypatch
) -> None:
    # #299 review MUST-FIX 1a: item 0 succeeds, item 1 FAILS mid-fan-out. The succeeded item 0
    # must still be compensated (the pre-fix code pushed only after the whole map collected, so a
    # mid-fan-out failure orphaned item 0). Serial (concurrency 1) for a deterministic order.
    import temporalio.workflow

    steps = """    - id: review
      map:
        activity: review_item
        over: input.items
        concurrency: 1
        collect: { output: schemas:BatchModel, field: results }
      compensate: { activity: cancel_item }
"""
    defs = """
    - { name: review_item, input: schemas:InputModel, output: schemas:MiddleModel, prompt: t }
    - { name: cancel_item, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
"""
    cls = _build(
        tmp_path,
        monkeypatch,
        steps,
        input_ref="schemas:BatchInputModel",
        output="schemas:BatchModel",
        defs=defs,
    )
    from replay_demo_project.schemas import BatchInputModel, InputModel, MiddleModel

    order: list[str] = []

    async def fake_map_activity(name: str, arg: Any, ctx: Any = None, **kwargs: Any) -> Any:
        if arg.value == "1":
            raise RuntimeError("item 1 failed")
        return MiddleModel(value=f"r{arg.value}")

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        assert name == "cancel_item"
        order.append(arg.value)
        return MiddleModel(value="undone")

    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)
    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    with pytest.raises(RuntimeError, match="item 1 failed"):
        await cls().run(
            BatchInputModel(
                items=[InputModel(value="0"), InputModel(value="1"), InputModel(value="2")]
            )
        )
    # Item 0 succeeded before item 1 failed -> compensated; item 1 (failed) and 2 (never ran) not.
    assert order == ["r0"]


@pytest.mark.asyncio
async def test_map_same_wait_batch_failure_still_compensates_batch_successes(
    tmp_path, monkeypatch
) -> None:
    # #299 verify round, edge 1: at concurrency 3 all three items land in the SAME
    # asyncio.wait(FIRST_COMPLETED) batch. Two succeed, one ("boom") fails. If the failure is drained
    # first, the pre-fix code cancelled the batch's other successes WITHOUT recording them. The drain
    # must record every SUCCESS in the batch before propagating the failure.
    import temporalio.workflow

    steps = """    - id: review
      map:
        activity: review_item
        over: input.items
        concurrency: 3
        collect: { output: schemas:BatchModel, field: results }
      compensate: { activity: cancel_item }
"""
    defs = """
    - { name: review_item, input: schemas:InputModel, output: schemas:MiddleModel, prompt: t }
    - { name: cancel_item, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
"""
    cls = _build(
        tmp_path,
        monkeypatch,
        steps,
        input_ref="schemas:BatchInputModel",
        output="schemas:BatchModel",
        defs=defs,
    )
    from replay_demo_project.schemas import BatchInputModel, InputModel, MiddleModel

    order: list[str] = []

    async def fake_map_activity(name: str, arg: Any, ctx: Any = None, **kwargs: Any) -> Any:
        # No await point before the check, so all three tasks settle in ONE loop step -> same batch.
        if arg.value == "boom":
            raise RuntimeError("boom failed")
        return MiddleModel(value=f"r{arg.value}")

    def fake_start_activity(name: str, arg: Any = None, **kwargs: Any):
        args = kwargs.pop("args")
        return asyncio.create_task(fake_map_activity(name, *args, **kwargs))

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        assert name == "cancel_item"
        order.append(arg.value)
        return MiddleModel(value="undone")

    monkeypatch.setattr(temporalio.workflow, "start_activity", fake_start_activity)
    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    with pytest.raises(RuntimeError, match="boom failed"):
        await cls().run(
            BatchInputModel(
                items=[InputModel(value="ok0"), InputModel(value="boom"), InputModel(value="ok2")]
            )
        )
    # BOTH batch successes (items 0 and 2) are compensated, in reverse item order.
    assert order == ["rok2", "rok0"]


@pytest.mark.asyncio
async def test_parallel_both_completed_branches_compensate_on_later_failure(
    tmp_path, monkeypatch
) -> None:
    # #299 review MUST-FIX 1b + D299-3 flat stack: TWO branches both complete (both push), then a
    # tail step fails -> the unwind compensates BOTH branches.
    steps = """    - id: fan
      parallel:
        branches:
          - id: a
            steps:
              - { id: book_hotel, activity: book_hotel, compensate: { activity: cancel_hotel } }
          - id: b
            steps:
              - { id: book_flight, activity: book_flight, compensate: { activity: cancel_flight } }
        collect: { output: schemas:FanModel }
    - id: charge2
      activity: charge2
"""
    defs = (
        _DEFS
        + "    - { name: charge2, input: schemas:FanModel, output: schemas:FailModel, prompt: t }\n"
    )
    cls = _build(tmp_path, monkeypatch, steps, input_ref="schemas:MiddleModel", defs=defs)
    from replay_demo_project.schemas import MiddleModel

    calls = _record_activities(
        monkeypatch,
        {
            "book_hotel": lambda a: MiddleModel(value="hotel"),
            "book_flight": lambda a: MiddleModel(value="flight"),
            "cancel_hotel": lambda a: MiddleModel(value="undone"),
            "cancel_flight": lambda a: MiddleModel(value="undone"),
        },
        fail={"charge2"},
    )
    instance = cls()
    with pytest.raises(RuntimeError, match="charge2 failed"):
        await instance.run(MiddleModel(value="start"))
    compensated = {n for (n, _a) in calls if n.startswith("cancel_")}
    assert compensated == {"cancel_hotel", "cancel_flight"}
    assert instance._typeflux_lifecycle.status().compensation_status == "complete"


@pytest.mark.asyncio
async def test_native_cancel_mid_unwind_preserves_original_error(tmp_path, monkeypatch) -> None:
    # #299 review MUST-FIX 2: a NATIVE Temporal cancel delivers asyncio.CancelledError INTO an
    # `await execute_activity` in the unwind. It must NOT escape the handler, orphan the remaining
    # entries, or mask the ORIGINAL business error. Simulated by cancel_flight raising CancelledError.
    import temporalio.workflow

    cls = _build(tmp_path, monkeypatch, _SAGA_STEPS)
    from replay_demo_project.schemas import MiddleModel

    calls: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if name == "charge":
            raise RuntimeError("charge failed")
        if name == "cancel_flight":
            raise asyncio.CancelledError  # a native cancel delivered mid-unwind
        return MiddleModel(value="ok")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)
    instance = cls()
    # The ORIGINAL business error (charge), never the CancelledError, is the outcome.
    with pytest.raises(RuntimeError, match="charge failed"):
        await instance.run(MiddleModel(value="start"))
    # The unwind CONTINUED past the cancelled compensation to book_hotel's compensation.
    assert "cancel_flight" in calls and "cancel_hotel" in calls
    status = instance._typeflux_lifecycle.status()
    assert (
        status.terminal_status == "failed"
    )  # terminal event recorded despite the mid-unwind cancel
    assert status.compensation_status == "partial"


# ---------------------------------------------------------------------------
# Load-time validation
# ---------------------------------------------------------------------------


def test_review_route_skipping_a_step_rejects_its_compensation_input_from(
    tmp_path, monkeypatch
) -> None:
    # #299 review MUST-FIX 3: a review gate routing A->C SKIPS B, so C's compensate.input_from: B
    # can't resolve on the routed path. The §3.3 routed-tail validation must reject it at load.
    _prime(monkeypatch)
    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

    spec_text = """
project: replay_demo_project
name: unit_route
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { t: hello } }
  provider: { type: custom, class: replay_demo_project.fakes:ReplaySagaProvider }
  imports: { allow_provider_class: true }
  observability: { type: none }
activities:
  definitions:
    - { name: a_step, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: b_step, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: c_step, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: cancel_c, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
workflow:
  name: UnitRoute
  input: schemas:MiddleModel
  output: schemas:MiddleModel
  lifecycle:
    enabled: true
    review:
      after_step: A
      user_decisions:
        approve: { route: C }
  steps:
    - { id: A, activity: a_step }
    - { id: B, activity: b_step }
    - id: C
      activity: c_step
      compensate: { activity: cancel_c, input_from: B }
"""
    path = tmp_path / "typeflux.yaml"
    path.write_text(spec_text, encoding="utf-8")
    spec = load_yaml_spec(path, load_dotenv=False)
    with pytest.raises(ValueError, match="routes past step 'B'.*compensate.input_from"):
        create_workflow(spec, collect_activities(spec))


def test_map_self_referential_input_from_is_rejected(tmp_path, monkeypatch) -> None:
    # #299 verify round, edge 2: a map whose compensate.input_from is its OWN id reads the map's
    # collected output, which does not exist when its items compensate -> reject at load.
    steps = """    - id: review
      map:
        activity: review_item
        over: input.items
        concurrency: 1
        collect: { output: schemas:BatchModel, field: results }
      compensate: { activity: cancel_item, input_from: review }
"""
    defs = """
    - { name: review_item, input: schemas:InputModel, output: schemas:MiddleModel, prompt: t }
    - { name: cancel_item, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
"""
    with pytest.raises(ValueError, match="its own collected output"):
        _build(
            tmp_path,
            monkeypatch,
            steps,
            input_ref="schemas:BatchInputModel",
            output="schemas:BatchModel",
            defs=defs,
        )


def test_branch_compensate_referencing_the_parallel_collect_is_rejected(
    tmp_path, monkeypatch
) -> None:
    # #299 verify round, edge 3: a branch step's compensate must not read the ENCLOSING parallel
    # collect (context[fan]) — it does not exist while the branch runs. Python already copies
    # branch_types without the parallel id (_validate_parallel_step); confirm the rejection.
    steps = """    - id: fan
      parallel:
        branches:
          - id: a
            steps:
              - { id: book_hotel, activity: book_hotel, compensate: { activity: cancel_hotel, input_from: fan } }
          - id: b
            steps:
              - { id: book_flight, activity: book_flight }
        collect: { output: schemas:FanModel }
    - id: charge2
      activity: charge2
"""
    defs = (
        _DEFS
        + "    - { name: charge2, input: schemas:FanModel, output: schemas:FailModel, prompt: t }\n"
    )
    with pytest.raises(ValueError, match="'fan' is not available|routes past"):
        _build(tmp_path, monkeypatch, steps, input_ref="schemas:MiddleModel", defs=defs)


def test_top_level_compensate_referencing_a_guaranteed_branch_step_is_accepted(
    tmp_path, monkeypatch
) -> None:
    # #299 verify round, edge 3: a guaranteed (ungated) post-parallel branch step IS addressable by
    # a later top-level compensation — Python propagates the parallel's guaranteed interior to later
    # steps' context_types.
    steps = """    - id: fan
      parallel:
        branches:
          - id: a
            steps:
              - { id: book_hotel, activity: book_hotel }
          - id: b
            steps:
              - { id: book_flight, activity: book_flight }
        collect: { output: schemas:FanModel }
    - id: after
      activity: charge2
      compensate: { activity: cancel_hotel, input_from: book_hotel }
"""
    defs = (
        _DEFS
        + "    - { name: charge2, input: schemas:FanModel, output: schemas:MiddleModel, prompt: t }\n"
    )
    # Builds without error: book_hotel (an ungated branch step) is guaranteed present after the block.
    cls = _build(
        tmp_path,
        monkeypatch,
        steps,
        input_ref="schemas:MiddleModel",
        output="schemas:MiddleModel",
        defs=defs,
    )
    assert cls is not None


def test_undeclared_compensation_activity_is_rejected(tmp_path, monkeypatch) -> None:
    steps = """    - id: book_hotel
      activity: book_hotel
      compensate: { activity: nonexistent }
    - id: charge
      activity: charge
"""
    with pytest.raises(ValueError, match="not a declared activity"):
        _build(tmp_path, monkeypatch, steps)


def test_unresolvable_input_from_is_rejected(tmp_path, monkeypatch) -> None:
    steps = """    - id: book_hotel
      activity: book_hotel
      compensate: { activity: cancel_hotel, input_from: ghost }
    - id: charge
      activity: charge
"""
    with pytest.raises(ValueError, match="is not available there"):
        _build(tmp_path, monkeypatch, steps)


def test_compensation_input_type_mismatch_is_rejected(tmp_path, monkeypatch) -> None:
    # cancel_wrong expects FailModel, but book_hotel's own output is MiddleModel.
    steps = """    - id: book_hotel
      activity: book_hotel
      compensate: { activity: cancel_wrong }
    - id: charge
      activity: charge
"""
    with pytest.raises(TypeError, match="compensate runs activity 'cancel_wrong'"):
        _build(tmp_path, monkeypatch, steps)


def test_compensate_on_parallel_step_is_rejected_by_the_schema(tmp_path, monkeypatch) -> None:
    # WorkflowParallelStepSpec has no `compensate` field (extra="forbid").
    steps = """    - id: fan
      compensate: { activity: cancel_hotel }
      parallel:
        branches:
          - id: a
            steps:
              - { id: book_hotel, activity: book_hotel }
        collect: { output: schemas:FanModel }
    - id: charge
      activity: charge
"""
    with pytest.raises(Exception):  # noqa: B017,PT011 - pydantic ValidationError (extra forbidden)
        _build(tmp_path, monkeypatch, steps)


# ---------------------------------------------------------------------------
# Sub-workflow compensation (#299 D299-3, review bug 7)
# ---------------------------------------------------------------------------


def _resolved_child(input_model: Any, output_model: Any):
    from typeflux.yaml.workflow import ResolvedSubworkflow

    return ResolvedSubworkflow(
        workflow_id="child",
        workflow_type="ChildWorkflow.000000000000",
        workflow_name="ChildWorkflow",
        project="replay_demo_project",
        spec_digest="0" * 64,
        input_type=input_model,
        output_type=output_model,
    )


def _build_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: str, subworkflows: dict, **kw: Any
) -> type:
    # The caller must have primed already (so the child types passed in `subworkflows` come from
    # the SAME import as the spec's resolved types — re-priming would reload the module and break
    # `is`-identity type checks).
    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

    path = tmp_path / "typeflux.yaml"
    path.write_text(_spec_text(steps, **kw), encoding="utf-8")
    spec = load_yaml_spec(path, load_dotenv=False)
    return create_workflow(spec, collect_activities(spec), subworkflows=subworkflows)


@pytest.mark.asyncio
async def test_subworkflow_step_parent_side_compensate_fires_on_later_failure(
    tmp_path, monkeypatch
) -> None:
    # #299 review bug 7 + D299-3: a `workflow:` step carries a PARENT-SIDE compensate (the child
    # unwinds its OWN stack internally — that is the same failure-unwind code a child run executes;
    # here we prove the parent's inverse fires on a later parent-side failure, on the child's result).
    import temporalio.workflow

    _prime(monkeypatch)
    from replay_demo_project.schemas import MiddleModel

    steps = """    - id: sub
      workflow: child
      compensate: { activity: cancel_sub }
    - id: charge_sub
      activity: charge_sub
"""
    defs = """
    - { name: cancel_sub, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
    - { name: charge_sub, input: schemas:MiddleModel, output: schemas:FailModel, prompt: t }
"""
    cls = _build_sub(
        tmp_path,
        monkeypatch,
        steps,
        {"child": _resolved_child(MiddleModel, MiddleModel)},
        input_ref="schemas:MiddleModel",
        output="schemas:FailModel",
        defs=defs,
    )
    calls: list[tuple[str, Any]] = []

    class _FakeInfo:
        workflow_id = "wf-parent"

    async def fake_execute_child(_wtype: str, value: Any, **kwargs: Any) -> Any:
        calls.append(("child", value))
        return MiddleModel(value="child-done")

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append((name, arg))
        if name == "charge_sub":
            raise RuntimeError("charge failed")
        return MiddleModel(value="undone")

    monkeypatch.setattr(temporalio.workflow, "execute_child_workflow", fake_execute_child)
    monkeypatch.setattr(temporalio.workflow, "info", lambda: _FakeInfo())
    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    instance = cls()
    with pytest.raises(RuntimeError, match="charge failed"):
        await instance.run(MiddleModel(value="start"))
    cancel_arg = next(a for (n, a) in calls if n == "cancel_sub")
    # The parent-side compensation ran on the CHILD's result (the default compensation input).
    assert cancel_arg.value == "child-done"
    assert instance._typeflux_lifecycle.status().compensation_status == "complete"


def test_map_workflow_step_compensate_is_rejected(tmp_path, monkeypatch) -> None:
    # #299 review bug 7: a `map.workflow` (child fan-out) carrying compensate is deferred; Python
    # must reject it at load exactly as TS does at plan build.
    _prime(monkeypatch)
    from replay_demo_project.schemas import InputModel, MiddleModel

    steps = """    - id: fan
      map:
        workflow: child
        over: input.items
        concurrency: 2
        collect: { output: schemas:BatchModel, field: results }
      compensate: { activity: cancel_item }
"""
    defs = """
    - { name: cancel_item, input: schemas:MiddleModel, output: schemas:MiddleModel, prompt: t }
"""
    with pytest.raises(ValueError, match="cannot carry"):
        _build_sub(
            tmp_path,
            monkeypatch,
            steps,
            {"child": _resolved_child(InputModel, MiddleModel)},
            input_ref="schemas:BatchInputModel",
            output="schemas:BatchModel",
            defs=defs,
        )


# ---------------------------------------------------------------------------
# Digest present-only
# ---------------------------------------------------------------------------


def test_compensation_digest_is_present_only(tmp_path, monkeypatch) -> None:
    from typeflux.yaml.identity import _call_payload

    without = _build(
        tmp_path,
        monkeypatch,
        """    - id: book_hotel
      activity: book_hotel
    - id: charge
      activity: charge
""",
    )
    with_comp = _build(
        tmp_path,
        monkeypatch,
        """    - id: book_hotel
      activity: book_hotel
      compensate: { activity: cancel_hotel, input_from: input }
    - id: charge
      activity: charge
""",
    )
    d_without = getattr(without, "__typeflux_spec_digest__")
    d_with = getattr(with_comp, "__typeflux_spec_digest__")
    assert d_without != d_with
    calls_without = getattr(without, "__typeflux_activity_calls__")
    calls_with = getattr(with_comp, "__typeflux_activity_calls__")
    # The compensate key is present ONLY on the compensated step's payload; input_from participates.
    assert "compensate" not in _call_payload(calls_without[0])
    assert _call_payload(calls_with[0])["compensate"] == {
        "activity": "cancel_hotel",
        "input_from": "input",
    }
    # A retry override does NOT enter the digest (parity with a normal activity's retry).
    assert "retry" not in _call_payload(calls_with[0])["compensate"]
