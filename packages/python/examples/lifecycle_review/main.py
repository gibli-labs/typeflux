from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from temporalio.client import WorkflowExecutionStatus

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
    runtime = await _runtime(task_queue=f"lifecycle-review-demo-{uuid4().hex}")
    workflow_id = f"lifecycle-review-{uuid4().hex[:8]}"
    handle = None
    primary_failed = False
    try:
        async with runtime.worker.build_worker():
            handle = await runtime.start_workflow(
                sample_case(),
                id=workflow_id,
                result_type=FinalDecision,
                run_timeout=timedelta(seconds=45),
                rpc_timeout=timedelta(seconds=5),
            )
            before = await asyncio.wait_for(handle.describe(), timeout=3)
            run_id = before.run_id
            waiting = await asyncio.wait_for(
                runtime.wait_for_lifecycle_state(
                    workflow_id,
                    "waiting_for_review",
                    run_id=run_id,
                    timeout_seconds=10,
                    poll_interval_seconds=0.1,
                ),
                timeout=12,
            )
            assert waiting.waiting_checkpoint == "package_for_review"
            assert waiting.completed_units == 2
            # This SDK demo is a trusted caller; operator authorization belongs
            # to the existing control-plane permission boundary, not this script.
            await asyncio.wait_for(
                runtime.submit_lifecycle_review(
                    workflow_id,
                    ReviewCommand(user_decision="send_email"),
                    run_id=run_id,
                ),
                timeout=5,
            )
            result = await asyncio.wait_for(handle.result(), timeout=15)
            assert result.approved is True and result.decision == "approved"
            completed = await asyncio.wait_for(
                runtime.query_lifecycle_status(workflow_id, run_id=run_id), timeout=5
            )
            assert completed.state == "completed"
            assert completed.review_route_target == "send_email"
            assert completed.completed_units == completed.total_units == 3
            ran = [event.step_id for event in completed.events if event.event == "step_started"]
            assert ran == ["assess_case", "package_for_review", "send_email"]
            after = await asyncio.wait_for(handle.describe(), timeout=3)
            assert after.run_id == run_id and after.status == WorkflowExecutionStatus.COMPLETED
            print(
                json.dumps(
                    {
                        "scenario": "review-adoption",
                        "sameRun": True,
                        "waitingCheckpoint": waiting.waiting_checkpoint,
                        "reviewRoute": completed.review_route_target,
                        "state": completed.state,
                        "stepsStarted": ran,
                        "result": result.model_dump(),
                    }
                )
            )
    except BaseException:
        primary_failed = True
        raise
    finally:
        try:
            if (
                handle is not None
                and (await asyncio.wait_for(handle.describe(), timeout=3)).status
                == WorkflowExecutionStatus.RUNNING
            ):
                await asyncio.wait_for(
                    handle.terminate("owned lifecycle review demo cleanup"), timeout=3
                )
        except Exception:
            if not primary_failed:
                raise
            print("Additional lifecycle review demo cleanup failed", file=sys.stderr)
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


async def _runtime(*, task_queue: str | None = None):
    load_env()
    spec = load_yaml_spec(YAML_PATH)
    if task_queue is not None:
        spec = spec.model_copy(update={"task_queue": task_queue})
    return await build_runtime(spec)


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
