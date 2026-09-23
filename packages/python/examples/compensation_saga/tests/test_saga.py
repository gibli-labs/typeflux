"""Compensation saga example (#299 D299-5): the idempotent-external-activity + saga
pattern. Covers the spec shape (side_effecting + compensate), require_compensation
admission (satisfied here; failing when a compensate is dropped), and the interpreter
LIFO unwinding book/charge in reverse when the finalizer fails."""

from __future__ import annotations

from typing import Any

import pytest

from examples.compensation_saga.schemas import (
    Booking,
    CancelResult,
    ChargeResult,
    OrderRequest,
    RefundResult,
)
from typeflux.project.policy import TypefluxProjectPolicySpec
from typeflux.project.policy_enforcement import evaluate_risk_tier
from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

YAML_PATH = "examples/compensation_saga/typeflux.yaml"


def test_side_effecting_activities_declare_compensation() -> None:
    spec = load_yaml_spec(YAML_PATH)
    side_effecting = {d.name for d in spec.activities.definitions if d.side_effecting}
    assert side_effecting == {"book_room", "cancel_room", "charge_card", "refund_card"}
    # The finalizer is deliberately NOT side-effecting.
    assert not next(
        d for d in spec.activities.definitions if d.name == "fulfill_order"
    ).side_effecting
    # Every side-effecting activity STEP carries a compensate.
    steps = {s.id: s for s in spec.workflow.steps}
    assert (
        steps["book"].compensate is not None and steps["book"].compensate.activity == "cancel_room"
    )
    assert (
        steps["charge"].compensate is not None
        and steps["charge"].compensate.activity == "refund_card"
    )
    assert steps["fulfill"].compensate is None  # non-side-effecting finalizer


def test_require_compensation_admission_passes_and_fails_when_dropped() -> None:
    spec = load_yaml_spec(YAML_PATH)
    payload = TypefluxProjectPolicySpec(
        name="p", risk_tiers={"human_gated": {"require_compensation": True}}
    ).to_payload()

    evaluation = evaluate_risk_tier(spec, payload)
    assert evaluation is not None
    reqs = {r.name: r.satisfied for r in evaluation.requirements}
    assert reqs == {"require_compensation": True}

    # Drop the charge step's compensate: the side-effecting step is now uncovered.
    uncovered = spec.model_copy(deep=True)
    charge = next(s for s in uncovered.workflow.steps if s.id == "charge")
    charge.compensate = None
    dropped = evaluate_risk_tier(uncovered, payload)
    assert dropped is not None
    assert {r.name: r.satisfied for r in dropped.requirements} == {"require_compensation": False}


@pytest.mark.asyncio
async def test_finalizer_failure_unwinds_book_and_charge_in_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = load_yaml_spec(YAML_PATH)
    workflow_cls = create_workflow(spec, collect_activities(spec))

    import temporalio.workflow

    calls: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if name == "book_room":
            return Booking(
                order_id=arg.order_id, confirmation_id="CONF-1", idempotency_key=arg.idempotency_key
            )
        if name == "charge_card":
            return ChargeResult(
                order_id=arg.order_id,
                charge_id="CHG-1",
                idempotency_key=arg.idempotency_key,
                amount=100.0,
            )
        if name == "fulfill_order":
            raise RuntimeError("fulfillment inventory check failed")
        if name == "refund_card":
            return RefundResult(order_id=arg.order_id, refunded=True)
        if name == "cancel_room":
            return CancelResult(order_id=arg.order_id, cancelled=True)
        raise AssertionError(f"unexpected activity: {name}")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    instance = workflow_cls()
    with pytest.raises(RuntimeError, match="fulfillment inventory check failed"):
        await instance.run(
            OrderRequest(
                order_id="ORDER-1", idempotency_key="idem-ORDER-1", room="Deluxe King", amount=100.0
            )
        )

    # Compensations ran in REVERSE step order: refund (charge) THEN cancel (book).
    compensations = [c for c in calls if c in {"refund_card", "cancel_room"}]
    assert compensations == ["refund_card", "cancel_room"]
    status = instance._typeflux_lifecycle.status()
    assert status.terminal_status == "failed"
    assert status.compensation_status == "complete"


def test_external_write_is_idempotent_on_repeat() -> None:
    # The whole point of the idempotent-external-activity pattern: a repeated request with
    # the same idempotency key performs the side effect ONCE. The scripted provider models
    # a genuinely idempotent external system (a dedup ledger keyed on the rendered prompt,
    # which threads the idempotency key), so two identical charge requests yield the same
    # charge_id and only ONE real external write.
    from examples.compensation_saga.fakes import CompensationSagaProvider
    from typeflux import ChatMessage

    provider = CompensationSagaProvider()
    request = [ChatMessage(role="user", content="Charge order ORDER-1 with key idem-ORDER-1.")]

    first = provider.structured_call(messages=request, output_schema=ChargeResult)
    second = provider.structured_call(messages=request, output_schema=ChargeResult)

    assert first.charge_id == second.charge_id
    assert provider.external_writes["charge_card"] == 1  # deduped: one real effect, not two

    # A DIFFERENT key is a distinct request and does perform its own write.
    other = [ChatMessage(role="user", content="Charge order ORDER-2 with key idem-ORDER-2.")]
    provider.structured_call(messages=other, output_schema=ChargeResult)
    assert provider.external_writes["charge_card"] == 2


def test_workflow_type_is_side_effecting_digest_invariant() -> None:
    # side_effecting is governance metadata, never a digest input: stripping every
    # side_effecting flag registers the SAME workflow type.
    spec = load_yaml_spec(YAML_PATH)
    baseline = create_workflow(spec, collect_activities(spec)).__typeflux_spec_digest__

    plain = spec.model_copy(deep=True)
    for definition in plain.activities.definitions:
        definition.side_effecting = False
    stripped = create_workflow(plain, collect_activities(plain)).__typeflux_spec_digest__
    assert stripped == baseline
