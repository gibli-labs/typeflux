from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from examples.review_before_side_effect.schemas import ClaimInput, Disbursement
from typeflux import ReviewCommand
from typeflux.env import load_env
from typeflux.yaml import build_runtime, load_yaml_spec

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "typeflux.yaml"


def sample_claim() -> ClaimInput:
    return ClaimInput(claim_id="CLAIM-1", claimant="Avery Morgan", amount=500.0)


async def _runtime():
    load_env()
    return await build_runtime(load_yaml_spec(YAML_PATH))


async def run_demo() -> int:
    """Run against a real dev server: assess -> [human review gate] -> disburse. The
    side-effecting disbursement only fires AFTER the review is approved; its compensate
    (reverse_payment) is the recovery net for a later failure the gate can't foresee."""
    runtime = await _runtime()
    workflow_id = f"review-before-side-effect-{uuid4().hex[:8]}"
    try:
        async with runtime.worker.build_worker():
            handle = await runtime.start_workflow(
                sample_claim(),
                id=workflow_id,
                result_type=Disbursement,
            )
            print(f"workflow_id={workflow_id}")
            waiting = await runtime.wait_for_lifecycle_state(workflow_id, "waiting_for_review")
            print(f"state={waiting.state} (paused BEFORE the side-effecting disbursement)")
            await runtime.submit_lifecycle_review(
                workflow_id,
                ReviewCommand(user_decision="approve", reviewer="demo"),
            )
            result = await handle.result()
            print(result.model_dump_json(indent=2))
    finally:
        runtime.observability.writer.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review-before-side-effect YAML demo (#299).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run assess -> review -> disburse against a dev server.")
    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(run_demo())
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
