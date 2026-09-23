from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from examples.lifecycle_review.schemas import CaseInput, FinalDecision
from typeflux import (
    ReviewCommand,
    WorkflowLifecycleStatus,
    export_workflow_lifecycle_audit,
)
from typeflux.env import load_env
from typeflux.observability.redaction import RegexPIIRedactor
from typeflux.yaml import build_runtime, load_yaml_spec

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "typeflux.yaml"


def sample_case() -> CaseInput:
    return CaseInput(
        case_id="CASE-2026-0101",
        customer_name="Avery Morgan",
        request="Approve expedited replacement shipment for a high-value device.",
        risk_notes=["manual review", "expedited replacement"],
    )


async def run_demo() -> int:
    runtime = await _runtime()
    workflow_id = f"lifecycle-review-{uuid4().hex[:8]}"
    try:
        async with runtime.worker.build_worker():
            handle = await runtime.client.start_workflow(
                runtime.workflow_class.run,
                sample_case(),
                id=workflow_id,
                task_queue=runtime.spec.task_queue,
                result_type=FinalDecision,
            )
            print(f"workflow_id={workflow_id}")
            waiting = await runtime.wait_for_lifecycle_state(workflow_id, "waiting_for_review")
            print(waiting.model_dump_json(indent=2))
            await runtime.submit_lifecycle_review(
                workflow_id,
                ReviewCommand(
                    user_decision="send_email",
                    reviewer="demo",
                    notes="Route directly to email in demo run.",
                ),
            )
            result = await handle.result()
            print(result.model_dump_json(indent=2))
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def run_traced_demo(workflow_id: str | None) -> int:
    _enable_langfuse_observability()
    runtime = await _runtime()
    resolved_id = workflow_id or f"lifecycle-review-traced-{uuid4().hex[:8]}"
    try:
        async with runtime.worker.build_worker():
            review_task = asyncio.create_task(
                _submit_review_when_waiting(
                    runtime,
                    resolved_id,
                    ReviewCommand(
                        user_decision="send_email",
                        reviewer="langfuse-demo",
                        notes="Routed directly to email during traced lifecycle demo run.",
                    ),
                )
            )
            try:
                result = await runtime.execute_workflow(
                    sample_case(),
                    id=resolved_id,
                    result_type=FinalDecision,
                )
                waiting = await review_task
            except BaseException:
                review_task.cancel()
                raise
            else:
                print(f"workflow_id={resolved_id}")
                print(waiting.model_dump_json(indent=2))
                print(result.model_dump_json(indent=2))
                print(
                    "trace_search="
                    "uv run python -m typeflux.observability trace list "
                    f"--workflow-id {resolved_id} --limit 1 --json"
                )
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def _submit_review_when_waiting(
    runtime,
    workflow_id: str,
    command: ReviewCommand,
) -> WorkflowLifecycleStatus:
    # The wait polls untraced; the explicit status query below is the single
    # curated lifecycle-status observation for the traced demo run.
    await runtime.wait_for_lifecycle_state(workflow_id, "waiting_for_review")
    waiting = await runtime.query_lifecycle_status(workflow_id)
    await runtime.submit_lifecycle_review(workflow_id, command)
    return waiting


async def start_workflow(workflow_id: str | None) -> int:
    runtime = await _runtime()
    resolved_id = workflow_id or f"lifecycle-review-{uuid4().hex[:8]}"
    try:
        handle = await runtime.client.start_workflow(
            runtime.workflow_class.run,
            sample_case(),
            id=resolved_id,
            task_queue=runtime.spec.task_queue,
            result_type=FinalDecision,
        )
        print(json.dumps({"workflow_id": resolved_id, "run_id": handle.run_id}, indent=2))
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def status(workflow_id: str) -> int:
    runtime = await _runtime()
    try:
        value = await runtime.query_lifecycle_status(workflow_id)
        print(value.model_dump_json(indent=2))
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def cancel(workflow_id: str, reason: str | None) -> int:
    runtime = await _runtime()
    try:
        await runtime.request_lifecycle_cancel(workflow_id, reason)
        print(json.dumps({"workflow_id": workflow_id, "signal": "typeflux_request_cancel"}))
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def review(
    workflow_id: str,
    *,
    user_decision: str,
    reviewer: str | None,
    notes: str | None,
) -> int:
    runtime = await _runtime()
    try:
        await runtime.submit_lifecycle_review(
            workflow_id,
            ReviewCommand(user_decision=user_decision, reviewer=reviewer, notes=notes),
        )
        print(json.dumps({"workflow_id": workflow_id, "signal": "typeflux_submit_review"}))
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def audit(workflow_id: str, *, run_id: str | None, jsonl: bool, redact: bool = False) -> int:
    runtime = await _runtime()
    try:
        handle = runtime.client.get_workflow_handle(workflow_id, run_id=run_id)
        export = await export_workflow_lifecycle_audit(
            handle,
            data_converter=getattr(runtime.client, "data_converter", None),
            redactor=RegexPIIRedactor.default() if redact else None,
        )
        if jsonl:
            for event in export.events:
                print(event.model_dump_json())
        else:
            print(export.model_dump_json(indent=2))
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def worker() -> int:
    runtime = await _runtime()
    try:
        async with runtime.worker.build_worker():
            print(f"worker started task_queue={runtime.spec.task_queue}")
            await asyncio.Event().wait()
    finally:
        runtime.observability.writer.shutdown()
    return 0


async def _runtime():
    load_env()
    return await build_runtime(load_yaml_spec(YAML_PATH))


def _enable_langfuse_observability() -> None:
    load_env()
    missing = [key for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY") if not os.getenv(key)]
    if missing:
        raise RuntimeError("run-traced requires Langfuse credentials: " + ", ".join(missing))
    os.environ["TYPEFLUX_LIFECYCLE_OBSERVABILITY"] = "langfuse"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lifecycle review YAML demo.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser(
        "run", help="Run worker, start workflow, route review, and print result."
    )

    run_traced = subcommands.add_parser(
        "run-traced",
        help="Run the demo through Typeflux tracing and publish Langfuse metadata.",
    )
    run_traced.add_argument("--workflow-id")

    start = subcommands.add_parser("start", help="Start a workflow without waiting for result.")
    start.add_argument("--workflow-id")

    status_cmd = subcommands.add_parser("status", help="Query lifecycle status.")
    status_cmd.add_argument("workflow_id")

    cancel_cmd = subcommands.add_parser("cancel", help="Request cooperative cancellation.")
    cancel_cmd.add_argument("workflow_id")
    cancel_cmd.add_argument("--reason")

    review_cmd = subcommands.add_parser("review", help="Submit a routed review user decision.")
    review_cmd.add_argument("workflow_id")
    review_cmd.add_argument("user_decision")
    review_cmd.add_argument("--reviewer")
    review_cmd.add_argument("--notes")

    audit_cmd = subcommands.add_parser(
        "audit",
        help="Export durable lifecycle audit events from Temporal history.",
    )
    audit_cmd.add_argument("workflow_id")
    audit_cmd.add_argument("--run-id")
    audit_cmd.add_argument("--jsonl", action="store_true")
    audit_cmd.add_argument(
        "--redact",
        action="store_true",
        help="Redact reviewer identity, cancellation reasons, and failure messages.",
    )

    subcommands.add_parser("worker", help="Run the YAML worker.")

    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(run_demo())
    if args.command == "run-traced":
        return asyncio.run(run_traced_demo(args.workflow_id))
    if args.command == "start":
        return asyncio.run(start_workflow(args.workflow_id))
    if args.command == "status":
        return asyncio.run(status(args.workflow_id))
    if args.command == "cancel":
        return asyncio.run(cancel(args.workflow_id, args.reason))
    if args.command == "review":
        return asyncio.run(
            review(
                args.workflow_id,
                user_decision=args.user_decision,
                reviewer=args.reviewer,
                notes=args.notes,
            )
        )
    if args.command == "audit":
        return asyncio.run(
            audit(args.workflow_id, run_id=args.run_id, jsonl=args.jsonl, redact=args.redact)
        )
    if args.command == "worker":
        return asyncio.run(worker())
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
