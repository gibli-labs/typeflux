"""CLI extension kept separate from the static compiler and basic queries."""

from __future__ import annotations

import json
from pathlib import Path

from . import ci
from .compiler import compile_graph
from .explorer import render
from .records import Ledger
from .release import receipt
from .repository import GraphError, Repository
from .verification import RUNNERS, verify


def register(commands):
    sub = commands.add_parser("report")
    sub.add_argument("--base", required=True)
    sub.add_argument("--head")
    sub = commands.add_parser("explore")
    sub.add_argument("--base")
    sub.add_argument("--revision")
    sub.add_argument("--verification", type=Path)
    sub = commands.add_parser("aggregate")
    for name in ("base", "head", "expected-head", "tested-revision"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--run", type=Path, required=True)
    sub.add_argument("--jobs", type=Path, required=True)
    sub.add_argument("--run-id", type=int, required=True)
    sub.add_argument("--attempt", type=int, required=True)
    sub = commands.add_parser("release")
    sub.add_argument("--artifact", action="append", type=Path, required=True)
    sub.add_argument("--environment", default="unassigned")
    sub = commands.add_parser("task")
    sub.add_argument("task_id")
    sub.add_argument("action", choices=("create", "resume", "transition", "effect"))
    sub.add_argument("--intent")
    sub.add_argument("--acceptance", action="append")
    sub.add_argument("--capability", action="append", default=[])
    sub.add_argument("--state")
    sub.add_argument("--key")
    sub.add_argument("--receipt")
    sub = commands.add_parser("verify")
    sub.add_argument("task_id")
    sub.add_argument("verifier", choices=sorted(RUNNERS))


def execute(args):
    root = args.root.resolve()
    if args.command == "report":
        return ci.plan(Repository(root, args.base), Repository(root, args.head))
    if args.command == "aggregate":
        return ci.aggregate(
            Repository(root, args.base),
            Repository(root, args.head),
            json.loads(args.run.read_text()),
            json.loads(args.jobs.read_text()),
            run_id=args.run_id,
            attempt=args.attempt,
            expected_head=args.expected_head,
            tested_revision=args.tested_revision,
        )
    if args.command == "explore":
        repo = Repository(root, args.revision)
        graph = compile_graph(repo)
        report = ci.plan(Repository(root, args.base), repo) if args.base else None
        verification = json.loads(args.verification.read_text()) if args.verification else None
        return render(graph, report, verification)
    if args.command == "release":
        # Working tree must match HEAD; never attest a clean historical view of dirty files.
        return receipt(Repository(root), args.artifact, args.environment)
    if args.command == "verify":
        return verify(root, args.task_id, args.verifier)
    repo = Repository(root)
    ledger = Ledger(root, args.task_id)
    if args.action == "resume":
        return ledger.resume(repo)
    if args.action == "create":
        identifiers = {n["id"] for n in compile_graph(repo)["nodes"] if n["kind"] == "capability"}
        if not set(args.capability) <= identifiers:
            raise GraphError("unknown task capability")
        return ledger.append(
            "create",
            {"intent": args.intent, "acceptance": args.acceptance, "capabilities": args.capability},
            repo,
        )
    if args.action == "transition":
        return ledger.append("transition", {"state": args.state}, repo)
    return ledger.append(
        "effect", {"key": args.key, "state": args.state, "receipt": args.receipt}, repo
    )
