"""Live regulated claims adjudication demo operated through WorkflowOperations.

Runs the YAML workflow against real Temporal + Anthropic + Langfuse:

1. Non-blocking control-plane start (``WorkflowOperations.start`` receipt).
2. Untraced wait for the review gate (``wait_for_lifecycle_state``).
3. One deliberate traced status check with the valid review decisions.
4. Review decision submitted through the ops API (reviewer/notes stay out of
   ``typeflux.*`` metadata).
5. Result awaited via the raw Temporal handle, then the run is verified in
   Langfuse: generation usage details, literal special-character rendering,
   curated lifecycle operations, and plain Temporal activities recorded as
   planned names only.

Requires ``.env`` with ANTHROPIC_API_KEY, LANGFUSE_PUBLIC_KEY/SECRET_KEY, and
a Temporal server at TEMPORAL_ADDRESS.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from examples.regulated_claims_adjudication.schemas import ClaimInput, FinalAdjudication
from typeflux import ReviewCommand
from typeflux.env import load_env
from typeflux.project import WorkflowOperations
from typeflux.yaml import build_runtime, load_yaml_spec

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "typeflux.anthropic.yaml"

CLAIM = ClaimInput(
    claim_id="CLM-2026-0617",
    claimant="Smith & Sons <Underwriting> Ltd",
    policy_number="POL-88-1142",
    amount_usd=18250.0,
    description=(
        "Water damage to server room after sprinkler fault; invoice notes say "
        '"replace 3 racks & re-certify <hot aisle> containment". '
        "No prior claims on this policy."
    ),
)


async def main() -> int:
    load_env()
    spec = load_yaml_spec(YAML_PATH)
    runtime = await build_runtime(spec)
    ops = WorkflowOperations(runtime=runtime)
    workflow_id = f"regulated-claims-{uuid4().hex[:8]}"
    try:
        async with runtime.worker.build_worker():
            receipt = await ops.start(CLAIM, workflow_id=workflow_id)
            print("--- start receipt ---")
            print(receipt.model_dump_json(indent=2))

            await runtime.wait_for_lifecycle_state(
                workflow_id,
                "waiting_for_review",
                timeout_seconds=180.0,
            )

            status = await ops.status(workflow_id, trace=True)
            print("--- status at review gate ---")
            print(status.model_dump_json(indent=2))
            assert status.status.state == "waiting_for_review"
            assert status.valid_user_decisions == {
                "approve_payment": "finalize_adjudication",
                "escalate_fraud": "escalate_case",
            }

            await ops.submit_review(
                workflow_id,
                ReviewCommand(
                    user_decision="approve_payment",
                    reviewer="compliance.officer@example.com",
                    notes="Within authority limit; call 555-123-4567 with questions.",
                ),
            )

            handle = runtime.client.get_workflow_handle(
                workflow_id,
                run_id=receipt.run_id,
                result_type=FinalAdjudication,
            )
            result = await handle.result()
            print("--- final adjudication ---")
            print(result.model_dump_json(indent=2))
            assert result.decision == "approved_for_payment"
            assert result.escalated is False
            assert result.audit_code.startswith(f"REG-{CLAIM.claim_id}-")
    finally:
        runtime.observability.writer.shutdown()

    verified = await _verify_in_langfuse(runtime, workflow_id=workflow_id)
    print("--- langfuse verification ---")
    print(json.dumps(verified, indent=2, sort_keys=True))
    # Exit non-zero when verification fails so a runner or CI can tell a clean
    # run from a broken one.
    return 0 if verified.get("ok") else 1


async def _verify_in_langfuse(runtime, *, workflow_id: str) -> dict:
    """Poll Langfuse until the run's traces are queryable, then verify them."""
    import datetime

    from langfuse import Langfuse

    client = Langfuse()
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
    deadline = time.monotonic() + 180.0
    checks: dict = {}
    while True:
        page = runtime.list_traces(workflow_id=workflow_id, limit=50, since=since)
        full_traces = []
        for item in page.traces:
            try:
                trace = client.api.trace.get(item.trace_id)
            except Exception:  # noqa: BLE001 - ingestion lag returns 404s.
                continue
            full_traces.append(trace.dict() if hasattr(trace, "dict") else dict(trace.__dict__))
        checks = _evaluate(full_traces, workflow_id=workflow_id)
        if checks.get("ok") or time.monotonic() >= deadline:
            checks["traces_scanned"] = len(full_traces)
            return checks
        await asyncio.sleep(10.0)


def _evaluate(full_traces: list[dict], *, workflow_id: str) -> dict:
    # A trace belongs to this run when the workflow id appears anywhere in it
    # (lifecycle-operation metadata, Temporal span attributes, or inputs);
    # observation-level metadata alone is not enough because the generation
    # carries only join keys.
    run_traces = [trace for trace in full_traces if workflow_id in json.dumps(trace, default=str)]
    observations = [
        obs
        for trace in run_traces
        for obs in trace.get("observations") or ()
        if isinstance(obs, dict)
    ]
    by_name: dict[str, list[dict]] = {}
    for obs in observations:
        by_name.setdefault(obs.get("name") or "", []).append(obs)

    generation = next(iter(by_name.get("assess_claim.generation", [])), None)
    usage = (generation or {}).get("usageDetails") or (generation or {}).get("usage")
    generation_input = json.dumps((generation or {}).get("input"), default=str)
    all_text = json.dumps(observations, default=str)

    plain_activity_spans = sorted(
        name
        for name in by_name
        if name in ("RunActivity:apply_compliance_policy", "RunActivity:finalize_adjudication")
    )

    checks = {
        "anthropic_generation_found": generation is not None,
        "generation_usage_present": bool(usage),
        "generation_usage": usage,
        "literal_special_chars_in_prompt": (
            "Smith & Sons" in generation_input and "&amp;" not in generation_input
        ),
        "interpolation_placeholder_literal": "${AUDIT_REGION}" in generation_input,
        "traced_status_query_exactly_once": (
            len(by_name.get("TypefluxLifecycleQuery:typeflux_lifecycle_status", [])) == 1
        ),
        "review_signal_traced": (
            len(by_name.get("TypefluxLifecycleSignal:typeflux_submit_review", [])) == 1
        ),
        "plain_activities_executed": plain_activity_spans,
        "reviewer_email_absent": "compliance.officer@example.com" not in all_text,
        "reviewer_phone_absent": "555-123-4567" not in all_text,
    }
    checks["ok"] = bool(
        checks["anthropic_generation_found"]
        and checks["generation_usage_present"]
        and checks["literal_special_chars_in_prompt"]
        and checks["interpolation_placeholder_literal"]
        and checks["traced_status_query_exactly_once"]
        and checks["review_signal_traced"]
        and len(plain_activity_spans) == 2
        and checks["reviewer_email_absent"]
        and checks["reviewer_phone_absent"]
    )
    return checks


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
