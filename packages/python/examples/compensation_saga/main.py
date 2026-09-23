from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from examples.compensation_saga.schemas import Fulfillment, OrderRequest
from typeflux.env import load_env
from typeflux.yaml import build_runtime, load_yaml_spec

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "typeflux.yaml"


def sample_order() -> OrderRequest:
    return OrderRequest(
        order_id="ORDER-1",
        idempotency_key="idem-ORDER-1",
        room="Deluxe King",
        amount=100.0,
    )


async def _runtime():
    load_env()
    return await build_runtime(load_yaml_spec(YAML_PATH))


async def run_demo() -> int:
    """Run the happy path against a real dev server: book -> charge -> fulfill, all
    side-effecting steps recorded on the compensation LIFO but never unwound (nothing
    fails). The reverse-order unwind on failure is proven by the test suite and the
    slice-1 replay fixtures."""
    runtime = await _runtime()
    workflow_id = f"compensation-saga-{uuid4().hex[:8]}"
    try:
        async with runtime.worker.build_worker():
            result = await runtime.execute_workflow(
                sample_order(),
                id=workflow_id,
                result_type=Fulfillment,
            )
            print(f"workflow_id={workflow_id}")
            print(result.model_dump_json(indent=2))
    finally:
        runtime.observability.writer.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compensation saga YAML demo (#299).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the saga happy path against a dev server.")
    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(run_demo())
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
