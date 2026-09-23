"""Review-before-side-effect example (#299 D299-5): a human review gate fires BEFORE the
side-effecting disbursement (prevention), and the step still declares a compensate (the
recovery net). Covers the spec shape, require_compensation admission, and that the
disbursement only runs after the review is approved."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from examples.review_before_side_effect.schemas import (
    Assessment,
    ClaimInput,
    Disbursement,
    Reversal,
)
from typeflux.project.policy import TypefluxProjectPolicySpec
from typeflux.project.policy_enforcement import evaluate_risk_tier
from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

YAML_PATH = "examples/review_before_side_effect/typeflux.yaml"


def _workflow() -> Any:
    spec = load_yaml_spec(YAML_PATH)
    return create_workflow(spec, collect_activities(spec))


def test_review_gate_precedes_the_side_effecting_step() -> None:
    spec = load_yaml_spec(YAML_PATH)
    assert spec.workflow.lifecycle is not None and spec.workflow.lifecycle.enabled
    review = spec.workflow.lifecycle.review
    assert review is not None and review.after_step == "assess"
    # The gate routes to `disburse` — the side-effecting step runs only after approval.
    assert review.user_decisions["approve"].route == "disburse"
    disburse_def = next(d for d in spec.activities.definitions if d.name == "disburse_payment")
    assert disburse_def.side_effecting is True
    disburse_step = next(s for s in spec.workflow.steps if s.id == "disburse")
    assert disburse_step.compensate is not None
    assert disburse_step.compensate.activity == "reverse_payment"


def test_require_compensation_admission_passes_and_fails_when_dropped() -> None:
    spec = load_yaml_spec(YAML_PATH)
    payload = TypefluxProjectPolicySpec(
        name="p", risk_tiers={"human_gated": {"require_compensation": True}}
    ).to_payload()
    ok = evaluate_risk_tier(spec, payload)
    assert ok is not None and {r.name: r.satisfied for r in ok.requirements} == {
        "require_compensation": True
    }

    uncovered = spec.model_copy(deep=True)
    next(s for s in uncovered.workflow.steps if s.id == "disburse").compensate = None
    dropped = evaluate_risk_tier(uncovered, payload)
    assert dropped is not None
    assert {r.name: r.satisfied for r in dropped.requirements} == {"require_compensation": False}


@pytest.mark.asyncio
async def test_disbursement_runs_only_after_review_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_cls = _workflow()

    import temporalio.workflow

    calls: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if name == "assess_claim":
            return Assessment(claim_id=arg.claim_id, recommended=True, rationale="ok")
        if name == "disburse_payment":
            return Disbursement(
                claim_id=arg.claim_id,
                transfer_id="XFER-1",
                idempotency_key="idem-CLAIM-1",
                amount=500.0,
            )
        if name == "reverse_payment":
            return Reversal(claim_id=arg.claim_id, reversed=True)
        raise AssertionError(f"unexpected activity: {name}")

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    instance = workflow_cls()
    task = asyncio.create_task(
        instance.run(ClaimInput(claim_id="CLAIM-1", claimant="Avery", amount=500.0))
    )
    await _wait_for_state(instance, "waiting_for_review")
    # The side-effecting disbursement has NOT run yet — the gate holds it.
    assert calls == ["assess_claim"]

    instance.typeflux_submit_review({"user_decision": "approve", "reviewer": "demo"})
    result = await task
    assert calls == ["assess_claim", "disburse_payment"]
    assert result.transfer_id == "XFER-1"
    assert instance._typeflux_lifecycle.status().state == "completed"


def test_disbursement_is_idempotent_on_repeat() -> None:
    # The disbursement carries an idempotency key AND the fake external system dedups on it,
    # so a retry of the same disbursement is a no-op (one real transfer, not two).
    from examples.review_before_side_effect.fakes import ReviewBeforeSideEffectProvider
    from typeflux import ChatMessage

    provider = ReviewBeforeSideEffectProvider()
    request = [ChatMessage(role="user", content="Disburse the approved payment for claim CLAIM-1.")]

    first = provider.structured_call(messages=request, output_schema=Disbursement)
    second = provider.structured_call(messages=request, output_schema=Disbursement)

    assert first.transfer_id == second.transfer_id
    assert provider.transfers_performed == 1  # deduped: the payment happened once, not twice


async def _wait_for_state(instance: Any, state: str) -> None:
    for _ in range(200):
        if instance.typeflux_lifecycle_status().state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"workflow did not reach {state!r}")
