from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from typeflux.execution.preflight import preflight_ai_activities
from typeflux.project.bundle import resolve_workflow_bundle
from typeflux.project.deployment import (
    build_project_deployment_plan,
    render_project_deployment_plan,
)
from typeflux.project.drain import workflow_drain_status
from typeflux.project.environment import (
    ProjectEnvironmentError,
    load_project_environment,
    project_environment_context,
    resolve_project_workflow,
    resolve_subworkflows_for,
)
from typeflux.project.loader import (
    load_project_spec,
    validate_project,
)
from typeflux.project.policy_enforcement import (
    ProjectPolicyEnforcementError,
    build_project_policy_runtime_guard,
)
from typeflux.project.spec import ProjectValidationReport
from typeflux.project.validation import validate_project_bundle
from typeflux.yaml.imports import (
    collect_activities,
    import_object,
    import_type_ref,
    validate_extension_imports,
)
from typeflux.yaml.runtime import _build_registry, build_runtime

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m typeflux.project")
    subcommands = parser.add_subparsers(dest="command", required=True)

    list_parser = subcommands.add_parser("list")
    list_parser.add_argument("project", help="Path to typeflux.project.yaml")
    list_parser.add_argument("--json", action="store_true", help="Print JSON instead of the table")

    environments_parser = subcommands.add_parser("environments")
    environments_parser.add_argument("project", help="Path to typeflux.project.yaml")
    environments_parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of the table"
    )

    resolve_parser = subcommands.add_parser("resolve")
    resolve_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(resolve_parser)
    resolve_parser.add_argument("--json", action="store_true", help="Print JSON")

    drain_parser = subcommands.add_parser("drain-status")
    drain_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )
    drain_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(drain_parser)

    catalog_parser = subcommands.add_parser("catalog")
    catalog_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )
    catalog_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(catalog_parser)

    migrate_parser = subcommands.add_parser(
        "migrate",
        help="Terminate a running execution and resubmit it against the current version (#204).",
    )
    migrate_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(migrate_parser)
    _add_policy_args(migrate_parser)
    migrate_parser.add_argument(
        "--execution-id",
        required=True,
        help="Temporal workflow id of the execution to migrate (the new run reuses it).",
    )
    migrate_parser.add_argument(
        "--run-id",
        help=(
            "Temporal run id of the specific run to migrate; omitted targets the "
            "execution's current run (the cancel/review addressing convention)."
        ),
    )
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview (#791): run every preflight — binding, same-version, target-queue "
            "pollers, gates, input decode — and report what a real migrate would terminate "
            "and resubmit, without mutating anything."
        ),
    )
    migrate_parser.add_argument(
        "--abandon-gates",
        action="store_true",
        help=(
            "Acknowledge dropping a pending review gate. Without it, an execution parked at a "
            "gate is refused so the human decision is never silently lost."
        ),
    )
    migrate_parser.add_argument(
        "--reason",
        help="Operator note appended to the canonical termination reason (persists in history).",
    )
    migrate_parser.add_argument(
        "--expect-policy-hash",
        help="Expected composed project policy hash; migrate fails on drift before any Temporal call.",
    )
    migrate_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )

    status_parser = subcommands.add_parser(
        "status",
        help="Query an execution's lifecycle status plus the valid review decisions (#802).",
    )
    status_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(status_parser)
    _add_policy_args(status_parser)
    status_parser.add_argument(
        "--execution-id", required=True, help="Temporal workflow id of the execution."
    )
    status_parser.add_argument(
        "--run-id",
        help="Temporal run id of the specific run; omitted targets the current run.",
    )
    status_parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Record this status check as a first-class, auditable lifecycle operation in the "
            "trace. Default polling stays untraced (the CP route's exact semantics)."
        ),
    )
    status_parser.add_argument(
        "--expect-policy-hash",
        help="Expected composed project policy hash; status fails on drift before any Temporal call.",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )

    review_parser = subcommands.add_parser(
        "review",
        help="Submit a review decision to an execution parked at a gate (#802).",
    )
    review_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(review_parser)
    _add_policy_args(review_parser)
    review_parser.add_argument(
        "--execution-id", required=True, help="Temporal workflow id of the execution."
    )
    review_parser.add_argument(
        "--run-id",
        help="Temporal run id of the specific run; omitted targets the current run.",
    )
    review_parser.add_argument(
        "--decision", required=True, help="The review decision (must be valid for the gate)."
    )
    review_parser.add_argument(
        "--gate",
        help=(
            "Which gate to decide (#55 slice 4). Omitted with exactly one gate waiting "
            "decides that gate; omitted with several waiting is refused, never guessed."
        ),
    )
    review_parser.add_argument(
        "--reviewer",
        help=(
            "Reviewer identity recorded on the review signal. Sent as the Temporal signal "
            "payload and persisted in workflow history — send only data appropriate to "
            "retain there (#325)."
        ),
    )
    review_parser.add_argument(
        "--notes", help="Reviewer notes; persisted in workflow history like --reviewer (#325)."
    )
    review_parser.add_argument(
        "--expect-policy-hash",
        help="Expected composed project policy hash; review fails on drift before any Temporal call.",
    )
    review_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )

    cancel_parser = subcommands.add_parser(
        "cancel",
        help="Request cancellation of a running execution (#802).",
    )
    cancel_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(cancel_parser)
    _add_policy_args(cancel_parser)
    cancel_parser.add_argument(
        "--execution-id", required=True, help="Temporal workflow id of the execution."
    )
    cancel_parser.add_argument(
        "--run-id",
        help="Temporal run id of the specific run; omitted targets the current run.",
    )
    cancel_parser.add_argument(
        "--reason",
        help=(
            "Cancellation reason. Sent as the Temporal cancel signal payload, persisted in "
            "workflow history, and readable via inspect — avoid sensitive free text (#325)."
        ),
    )
    cancel_parser.add_argument(
        "--expect-policy-hash",
        help="Expected composed project policy hash; cancel fails on drift before any Temporal call.",
    )
    cancel_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )

    bundle_parser = subcommands.add_parser("bundle")
    bundle_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )
    bundle_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(bundle_parser)
    _add_policy_args(bundle_parser)
    bundle_parser.add_argument(
        "--deployment-image",
        help=(
            "Optional worker image; when provided, the bundle includes a "
            "deployment plan preview for the selected workflow."
        ),
    )

    run_parser = subcommands.add_parser("run")
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; run is a worker process with log output (#814)",
    )
    run_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(run_parser)
    _add_policy_args(run_parser)
    run_parser.add_argument(
        "--preflight",
        action="store_true",
        help="Resolve and validate prompts, then exit.",
    )
    run_parser.add_argument(
        "--expect-policy-hash",
        help=(
            "Expected composed project policy hash. If provided, project run fails before "
            "preflight or worker polling when the selected runtime policy hash differs."
        ),
    )

    submit_parser = subcommands.add_parser("submit")
    submit_parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for cross-command consistency; this command always prints JSON (#814)",
    )
    submit_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(submit_parser)
    _add_policy_args(submit_parser)
    submit_parser.add_argument(
        "--input",
        required=True,
        help="Path to workflow input JSON, or '-' to read JSON from stdin.",
    )
    submit_parser.add_argument("--workflow-id", required=True, help="Temporal workflow ID to use.")
    submit_parser.add_argument(
        "--subject",
        action="append",
        default=[],
        dest="subject_ids",
        help=(
            "Subject id to associate with the run for the erasure index; may be "
            "provided more than once. Overrides the spec `subjects:` extraction."
        ),
    )
    submit_parser.add_argument(
        "--task-queue",
        help="Override the resolved task queue for this workflow start.",
    )
    submit_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="Search/trace tag to attach. May be provided more than once.",
    )
    submit_parser.add_argument(
        "--metadata-json",
        help="JSON object merged into the root workflow trace metadata.",
    )
    submit_parser.add_argument(
        "--expect-policy-hash",
        help=(
            "Expected composed project policy hash. If provided, project submit fails "
            "before starting the workflow when the selected runtime policy hash differs."
        ),
    )

    validate_parser = subcommands.add_parser("validate")
    validate_parser.add_argument("project", help="Path to typeflux.project.yaml")
    validate_parser.add_argument(
        "--environment",
        help="Project environment ID to use for resolved workflow validation.",
    )
    validate_parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        dest="workflows",
        help="Project workflow ID to validate. May be provided more than once.",
    )
    validate_parser.add_argument(
        "--policy",
        action="append",
        default=[],
        dest="policies",
        help="Project policy ID to apply. May be provided more than once.",
    )
    validate_parser.add_argument("--json", action="store_true", help="Print JSON")

    deploy_parser = subcommands.add_parser("deploy")
    deploy_parser.add_argument("project", help="Path to typeflux.project.yaml")
    deploy_parser.add_argument(
        "--environment",
        help=(
            "Project environment ID to use for deployment generation. Required "
            "unless --apply is given (the plan carries its own environment)."
        ),
    )
    deploy_parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        dest="workflows",
        help="Project workflow ID to deploy. May be provided more than once.",
    )
    deploy_parser.add_argument(
        "--policy",
        action="append",
        default=[],
        dest="policies",
        help="Project policy ID to apply. May be provided more than once.",
    )
    deploy_parser.add_argument(
        "--target",
        choices=("kubernetes",),
        default="kubernetes",
        help="Deployment artifact target.",
    )
    deploy_parser.add_argument(
        "--image",
        help=(
            "Digest-pinned worker image. Required unless --apply is given (the "
            "plan carries its own pinned image)."
        ),
    )
    deploy_parser.add_argument(
        "--allow-mutable-image",
        action="store_true",
        help="Allow mutable image tags for local/dev generation.",
    )
    deploy_parser.add_argument(
        "--allow-placeholder-image",
        action="store_true",
        help=(
            "Allow the well-known all-zeros placeholder image digest "
            "(sha256:000...000). It is format-valid but resolves to no published "
            "image; without this flag it is rejected at plan-write and promote time "
            "(it would only fail at pod scheduling). Use for placeholder scaffolding."
        ),
    )
    deploy_parser.add_argument(
        "--allow-shared-task-queue",
        action="store_true",
        help="Allow separate generated workers to share a Temporal task queue.",
    )
    deploy_parser.add_argument(
        "--project-path-in-image",
        help="Absolute path to the project manifest inside the worker image.",
    )
    deploy_parser.add_argument(
        "--project-manifest-path",
        help=(
            "Value to RECORD as the project manifest path in render annotations and "
            "deployment-plan.json. Default: the manifest's path RELATIVE to its own "
            "directory, so committed artifacts are machine-independent (#757 item 1). "
            "Pass an explicit (repo-relative) value here, or --absolute-manifest-path "
            "to record the operator's resolved absolute path. Provenance only — never "
            "part of the plan hash or drift detection."
        ),
    )
    deploy_parser.add_argument(
        "--absolute-manifest-path",
        action="store_true",
        help=(
            "Record the operator's resolved ABSOLUTE manifest path (the pre-#757 "
            "behavior) instead of the machine-independent relative default."
        ),
    )
    deploy_parser.add_argument(
        "--config-env",
        action="append",
        default=[],
        dest="config_env",
        help=(
            "Environment variable name to treat as a non-secret ConfigMap value. "
            "May be provided more than once. Overrides secret-name heuristics for "
            "the named variable; use only for values that are not secrets."
        ),
    )
    deploy_parser.add_argument(
        "--base-env-file",
        help=(
            "Hermetic interpolation base for ${VAR} spec references (#760/#798): the "
            "file's KEY=VALUE pairs REPLACE the process environment for spec "
            "interpolation, so plan bytes are machine-independent given the same file."
        ),
    )
    deploy_parser.add_argument(
        "--hermetic",
        action="store_true",
        help=(
            "Interpolate against an EMPTY base (spec-internal ${VAR:-default} defaults "
            "only) — the no-file form of --base-env-file. Mutually exclusive with it."
        ),
    )
    deploy_parser.add_argument(
        "--plan-out",
        help=(
            "Directory under the project to write the deployment-plan file "
            "(default: deployments/). Plan is an immutable content-hashed "
            "YAML that approval (GitHub PR review) merges to main."
        ),
    )
    deploy_parser.add_argument(
        "--require-merged-plan",
        action="store_true",
        help=(
            "With --apply: refuse unless the plan file's exact bytes exist at its path on "
            "the remote default branch (#790) — the git-native proof that the reviewed, "
            "PR-merged artifact is what is being promoted. Also enabled by "
            "TYPEFLUX_REQUIRE_MERGED_PLAN=1 (the CI-recommended posture)."
        ),
    )
    deploy_parser.add_argument(
        "--apply",
        help=(
            "Apply (promote) the named plan file: verify it against the "
            "currently-resolved bundle and emit artifacts only if identity "
            "(spec digest, policy hash) still matches. Fails closed on drift. "
            "(#816: renamed from --plan — write a plan with --plan-out, apply it with --apply.)"
        ),
    )
    deploy_parser.add_argument(
        "--output",
        help="Directory for rendered deployment artifacts.",
    )
    deploy_parser.add_argument("--json", action="store_true", help="Print JSON plan")

    erase_parser = subcommands.add_parser(
        "erase",
        help=(
            "Erase subject(s) across the surfaces Typeflux controls (#715): Temporal "
            "crypto-shred + execution deletion, Langfuse traces, and the cross-run cache. "
            "Dry-run by default; emits the ErasureReceipt audit record."
        ),
    )
    erase_parser.add_argument("project", help="Path to typeflux.project.yaml")
    _add_workflow_environment_args(erase_parser)
    erase_parser.add_argument(
        "--subject",
        action="append",
        default=[],
        dest="subjects",
        required=True,
        help="Subject id to erase. May be provided more than once.",
    )
    erase_parser.add_argument(
        "--surface",
        action="append",
        default=[],
        dest="surfaces",
        help=(
            "Surface(s) to erase: temporal, langfuse, cache (comma-separated and/or "
            "repeatable). Default: all three."
        ),
    )
    erase_parser.add_argument(
        "--since",
        help="ISO-8601 lower bound for the Langfuse trace scan window.",
    )
    erase_parser.add_argument(
        "--until",
        help="ISO-8601 upper bound for the Langfuse trace scan window.",
    )
    erase_mode = erase_parser.add_mutually_exclusive_group()
    erase_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what WOULD be erased without mutating anything (the default).",
    )
    erase_mode.add_argument(
        "--execute",
        action="store_true",
        help="Actually erase. Without it, erase is a dry run.",
    )
    erase_parser.add_argument(
        "--acknowledge-irreversible",
        action="store_true",
        help=(
            "Acknowledge that --execute PERMANENTLY destroys subject key records "
            "(crypto-shred) and deletes workflow histories (DeleteWorkflowExecution). "
            "Required to execute the temporal surface."
        ),
    )
    erase_parser.add_argument(
        "--actor",
        help=(
            "Principal recorded on the ErasureReceipt. Default: the local OS user "
            "(the receipt is a compliance artifact and must name who ran it)."
        ),
    )
    erase_parser.add_argument(
        "--keystore-class",
        help=(
            "Import path (module:attr) of the deployment's shared SubjectKeystore "
            "backend; instantiated with no arguments (it reads its own config). "
            "Without it the crypto-shred surface is reported skipped — the "
            "process-local in-memory reference keystore holds no records minted "
            "elsewhere."
        ),
    )
    erase_parser.add_argument(
        "--cache-store-class",
        help=(
            "Import path (module:attr) of the deployment's CacheStore; instantiated "
            "with no arguments. Without it the cache surface is reported skipped."
        ),
    )
    erase_parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Execution enumeration limit per subject (truncation is reported).",
    )
    erase_parser.add_argument("--json", action="store_true", help="Print the receipt as JSON")

    admit_parser = subcommands.add_parser("admit")
    admit_parser.add_argument("project", help="Path to typeflux.project.yaml")
    admit_parser.add_argument("file", help="Path to the spec file to admit (YAML).")
    admit_parser.add_argument(
        "--environment",
        required=True,
        help="Project environment ID the spec is admitted under.",
    )
    admit_parser.add_argument(
        "--workflow",
        help=(
            "Project workflow SLOT id whose bound policies govern the submission "
            "(selects the composed admission policy)."
        ),
    )
    admit_parser.add_argument(
        "--policy",
        action="append",
        default=[],
        dest="policies",
        help="Explicit project policy ID to apply. May be provided more than once.",
    )
    admit_parser.add_argument(
        "--origin",
        choices=("operator", "external"),
        default="operator",
        help=(
            "Provenance of the spec. 'external' additionally forbids any "
            "module-import-gated capability structurally (arbitrary code execution)."
        ),
    )
    admit_parser.add_argument("--json", action="store_true", help="Print JSON report")

    args = parser.parse_args(argv)
    try:
        project = load_project_spec(args.project)
    except Exception as exc:  # noqa: BLE001 - CLI should report invalid project manifests.
        manifest_path = str(Path(args.project).expanduser().resolve())
        # #814: every command that accepts --json reports THIS failure as JSON too — a
        # machine consumer must be able to parse the invalid-manifest case, not just success.
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "project_name": "",
                        "manifest_path": manifest_path,
                        "ok": False,
                        "issues": [
                            {
                                "code": "invalid_project_manifest",
                                "message": str(exc),
                                "path": manifest_path,
                            }
                        ],
                        "workflows": [],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"Project manifest is invalid: {exc}")
        return EXIT_VALIDATION

    report = validate_project(project)

    if args.command == "list":
        _print_workflow_table(report, json_output=args.json)
        return 0 if report.ok else EXIT_VALIDATION
    if args.command == "environments":
        return _print_environment_table(project, json_output=args.json)
    if args.command == "resolve":
        if not report.ok:
            return _print_project_validation_error(report, json_output=args.json)
        try:
            resolved = resolve_project_workflow(
                project,
                workflow_id=args.workflow,
                environment_id=args.environment,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should report profile errors.
            return _print_project_command_error(exc, json_output=args.json)
        if args.json:
            print(json.dumps(resolved.summary().model_dump(mode="json"), indent=2, sort_keys=True))
        else:
            _print_resolved_workflow(resolved.summary())
        return 0
    if args.command == "drain-status":
        try:
            status = asyncio.run(
                workflow_drain_status(
                    project,
                    workflow_id=args.workflow,
                    environment_id=args.environment,
                )
            )
        except Exception as exc:  # noqa: BLE001 - CLI should report profile errors.
            return _print_project_command_error(exc, json_output=True)
        print(json.dumps(status.to_dict(), indent=2, sort_keys=True))
        return 0 if status.drained else 1
    if args.command == "migrate":
        if not report.ok:
            return _print_project_validation_error(report, json_output=args.json)
        return asyncio.run(
            _migrate_project_workflow(
                project,
                workflow_id=args.workflow,
                environment_id=args.environment,
                execution_id=args.execution_id,
                run_id=args.run_id,
                abandon_gates=args.abandon_gates,
                reason=args.reason,
                policy_ids=tuple(args.policies),
                expected_policy_hash_arg=args.expect_policy_hash,
                dry_run=args.dry_run,
            )
        )
    if args.command in ("status", "review", "cancel"):
        if not report.ok:
            return _print_project_validation_error(report, json_output=args.json)
        return asyncio.run(
            _lifecycle_project_operation(
                project,
                command=args.command,
                workflow_id=args.workflow,
                environment_id=args.environment,
                execution_id=args.execution_id,
                run_id=args.run_id,
                trace=getattr(args, "trace", False),
                decision=getattr(args, "decision", None),
                gate=getattr(args, "gate", None),
                reviewer=getattr(args, "reviewer", None),
                notes=getattr(args, "notes", None),
                reason=getattr(args, "reason", None),
                policy_ids=tuple(args.policies),
                expected_policy_hash_arg=args.expect_policy_hash,
            )
        )
    if args.command == "catalog":
        from typeflux.project.catalog import resolve_activity_catalog

        try:
            catalog = resolve_activity_catalog(
                project,
                workflow_id=args.workflow,
                environment_id=args.environment,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should report profile errors.
            return _print_project_command_error(exc, json_output=True)
        print(json.dumps(catalog.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.command == "bundle":
        try:
            bundle = resolve_workflow_bundle(
                project,
                workflow_id=args.workflow,
                environment_id=args.environment,
                policy_ids=tuple(args.policies),
                deployment_image=args.deployment_image,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should report profile errors.
            return _print_project_command_error(exc, json_output=True)
        print(json.dumps(bundle.to_dict(), indent=2, sort_keys=True))
        return 0 if bundle.validation.ok else EXIT_VALIDATION
    if args.command == "run":
        if not report.ok:
            return _print_project_validation_error(report, json_output=args.json)
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        return asyncio.run(
            _run_project_worker(
                project,
                workflow_id=args.workflow,
                environment_id=args.environment,
                preflight=args.preflight,
                policy_ids=tuple(args.policies),
                expected_policy_hash_arg=args.expect_policy_hash,
            )
        )
    if args.command == "submit":
        if not report.ok:
            return _print_project_validation_error(report, json_output=args.json)
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        return asyncio.run(
            _submit_project_workflow(
                project,
                workflow_id=args.workflow,
                environment_id=args.environment,
                input_path=args.input,
                workflow_id_override=args.workflow_id,
                task_queue=args.task_queue,
                tags=tuple(args.tags),
                metadata_json=args.metadata_json,
                subject_ids=tuple(args.subject_ids),
                policy_ids=tuple(args.policies),
                expected_policy_hash_arg=args.expect_policy_hash,
            )
        )
    if args.command == "validate":
        report = validate_project_bundle(
            project,
            environment_id=args.environment,
            workflow_ids=tuple(args.workflows),
            policy_ids=tuple(args.policies),
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            _print_validation_report(report, environment_id=args.environment)
        return 0 if report.ok else EXIT_VALIDATION
    if args.command == "deploy":
        from pathlib import Path as _PlanPath

        # When applying an approved plan (`--apply`), the plan file — not the
        # CLI flags — is authoritative for what gets emitted. We verify the
        # plan against the current resolution, then drive the artifact build
        # from the plan's own identity (environment, workflow, image, policies)
        # so verification can never pass while rendering targets something else.
        if not args.apply:
            missing = [
                flag
                for flag, value in (("--environment", args.environment), ("--image", args.image))
                if not value
            ]
            if missing:
                print(
                    f"deploy requires {' and '.join(missing)} unless --apply is given",
                    file=sys.stderr,
                )
                return 2
        deploy_environment = args.environment
        deploy_workflow_ids = tuple(args.workflows)
        deploy_policy_ids = tuple(args.policies)
        deploy_image = args.image
        if args.apply:
            from typeflux.project.deployments import (
                load_deployment_plan,
                verify_deployment_plan,
            )

            # A relative --apply resolves against the manifest directory (where
            # deployments/ lives), so the console's copyable promote command
            # works from any working directory, not only the project root.
            plan_path = _PlanPath(args.apply)
            if not plan_path.is_absolute():
                plan_path = project.manifest_path.parent / plan_path
            # The #790 merged-plan gate runs FIRST: an unmerged plan refuses before any
            # resolution work (exit 3 — a governance verdict under the #818 contract).
            if args.require_merged_plan or os.getenv("TYPEFLUX_REQUIRE_MERGED_PLAN") == "1":
                from typeflux.project.deployments import (
                    verify_plan_merged_to_default_branch,
                )

                merged_gap = verify_plan_merged_to_default_branch(
                    plan_path, project_root=project.manifest_path.parent
                )
                if merged_gap is not None:
                    print(merged_gap, file=sys.stderr)
                    return EXIT_VALIDATION
            try:
                plan_file = load_deployment_plan(project, plan_path)
                verification = verify_deployment_plan(
                    project,
                    plan_file,
                    allow_placeholder_image=args.allow_placeholder_image,
                    # #798: promote verifies under the SAME hermetic base the plan was
                    # authored with — recomputing spec_digest from the promote machine's
                    # shell would reject a valid plan on any env-divergent machine.
                    base_env=_deploy_base_env(args),
                )
            except Exception as exc:  # noqa: BLE001 - CLI should report plan errors.
                return _print_project_command_error(exc, json_output=args.json)
            if not verification.ok:
                from typeflux.project.deployments import (
                    PLACEHOLDER_IMAGE_MISMATCH_PATH,
                )

                lines = ["deployment plan drifted from current resolution:"]
                for mismatch in verification.mismatches:
                    # Synthetic explanation entries (#757 item 5) carry the offending
                    # literal in plan_value and the human explanation in current_value —
                    # render them as an explanation, not a value diff.
                    if mismatch.path == PLACEHOLDER_IMAGE_MISMATCH_PATH:
                        lines.append(
                            f"  {mismatch.path}: {mismatch.plan_value}: {mismatch.current_value}"
                        )
                        continue
                    lines.append(
                        f"  {mismatch.path}: plan={mismatch.plan_value!r} "
                        f"current={mismatch.current_value!r}"
                    )
                print("\n".join(lines), file=sys.stderr)
                return EXIT_VALIDATION
            deploy_environment = plan_file.identity.environment_id
            deploy_workflow_ids = (plan_file.identity.workflow_id,)
            deploy_policy_ids = plan_file.policy.selected_policy_ids
            deploy_image = plan_file.deployment.image
        # `report` is the structural project preflight (validate_project) the
        # deploy command always gates on; it is project-wide, so it applies to
        # the plan's target too — no env-dependent re-validation needed.
        if not report.ok:
            return _print_project_validation_error(report, json_output=args.json)
        # PORTABLE ARTIFACTS (#757 item 1): the value recorded in `project_manifest_path` (render
        # annotations + deployment-plan.json). Default = the manifest RELATIVE to its own directory
        # (machine- and cwd-independent); --project-manifest-path records an explicit value verbatim;
        # --absolute-manifest-path restores the resolved absolute path. Provenance only — not hashed.
        resolved_manifest = project.manifest_path
        if args.project_manifest_path is not None:
            recorded_manifest_path: str = args.project_manifest_path
        elif args.absolute_manifest_path:
            recorded_manifest_path = str(resolved_manifest)
        else:
            recorded_manifest_path = resolved_manifest.name
        try:
            plan = build_project_deployment_plan(
                project,
                environment_id=deploy_environment,
                workflow_ids=deploy_workflow_ids,
                policy_ids=deploy_policy_ids,
                image=deploy_image,
                allow_mutable_image=args.allow_mutable_image,
                allow_placeholder_image=args.allow_placeholder_image,
                allow_shared_task_queue=args.allow_shared_task_queue,
                project_path_in_image=args.project_path_in_image,
                project_manifest_path=recorded_manifest_path,
                config_env_names=tuple(args.config_env),
                target=args.target,
                base_env=_deploy_base_env(args),
            )
            render_result = (
                render_project_deployment_plan(plan, args.output) if args.output else None
            )
            plan_file_path = None
            if args.plan_out:
                from typeflux.project.deployments import write_deployment_plan

                out_dir = (project.manifest_path.parent / args.plan_out).resolve()
                # Plan-out is per-workflow; the loop is intentional — a deploy
                # call that names multiple workflows writes one plan each.
                for worker in plan.workers:
                    plan_file_path, _ = write_deployment_plan(
                        project,
                        workflow_id=worker.workflow_id,
                        environment_id=deploy_environment,
                        image=deploy_image,
                        policy_ids=deploy_policy_ids,
                        allow_mutable_image=args.allow_mutable_image,
                        allow_placeholder_image=args.allow_placeholder_image,
                        out_dir=out_dir,
                        # #798: the SAME hermetic base drives the plan file's spec digest —
                        # the committed identity, not only the in-memory plan.
                        base_env=_deploy_base_env(args),
                    )
        except Exception as exc:  # noqa: BLE001 - CLI should report deployment plan errors.
            return _print_project_command_error(exc, json_output=args.json)
        if args.json:
            print(json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True))
        elif render_result is not None:
            _print_deployment_render_result(render_result)
        else:
            _print_deployment_plan(plan)
        return 0
    if args.command == "erase":
        if not report.ok:
            return _print_project_validation_error(report, json_output=args.json)
        try:
            surfaces = _parse_erase_surfaces(args.surfaces)
            since = _parse_erase_timestamp("--since", args.since)
            until = _parse_erase_timestamp("--until", args.until)
            if args.limit <= 0:
                raise ValueError(f"--limit must be a positive integer (got {args.limit})")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        dry_run = not args.execute
        if not dry_run and "temporal" in surfaces and not args.acknowledge_irreversible:
            # The typed confirmation gate (the --abandon-gates acknowledgment pattern):
            # executing the temporal surface destroys key records and deletes histories
            # PERMANENTLY, so the loss must be named, not implied by --execute alone.
            print(
                "erase --execute on the temporal surface is IRREVERSIBLE (key records "
                "are destroyed and workflow histories deleted permanently). Re-run "
                "with --acknowledge-irreversible to confirm, or drop the temporal "
                "surface (--surface langfuse,cache).",
                file=sys.stderr,
            )
            return 2
        return asyncio.run(
            _erase_project_subjects(
                project,
                workflow_id=args.workflow,
                environment_id=args.environment,
                subject_ids=tuple(args.subjects),
                surfaces=surfaces,
                dry_run=dry_run,
                actor=args.actor,
                since=since,
                until=until,
                keystore_class=args.keystore_class,
                cache_store_class=args.cache_store_class,
                execution_limit=args.limit,
                json_output=args.json,
            )
        )
    if args.command == "admit":
        from typeflux.project.admission import admit_spec

        try:
            spec_text = Path(args.file).expanduser().read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - CLI should report unreadable spec files.
            return _print_project_command_error(exc, json_output=args.json)
        try:
            admission = admit_spec(
                spec_text,
                project,
                environment_id=args.environment,
                origin=args.origin,
                workflow_id=args.workflow,
                policy_ids=tuple(args.policies),
            )
        except Exception as exc:  # noqa: BLE001 - CLI should report admission setup errors.
            return _print_project_command_error(exc, json_output=args.json)
        if args.json:
            print(json.dumps(admission.to_dict(), indent=2, sort_keys=True))
        else:
            _print_admission_report(admission)
        return 0 if admission.admitted else EXIT_VALIDATION
    return 1


def _add_workflow_environment_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workflow", required=True, help="Project workflow ID to use.")
    parser.add_argument("--environment", required=True, help="Project environment ID to use.")


def _add_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--policy",
        action="append",
        default=[],
        dest="policies",
        help="Project policy ID to apply. May be provided more than once.",
    )


class _PolicyHashUsageError(Exception):
    """A malformed or self-conflicting --expect-policy-hash INPUT (#818): a caller
    mistake, exit 2 — distinct from an actual hash-drift verdict, which stays a
    ProjectPolicyEnforcementError and exits 3."""


def _expected_policy_hash(cli_value: str | None) -> str | None:
    cli_hash = _normalize_policy_hash(cli_value)
    env_hash = _normalize_policy_hash(os.getenv("TYPEFLUX_EXPECTED_POLICY_HASH"))
    if cli_hash is not None and env_hash is not None and cli_hash != env_hash:
        raise _PolicyHashUsageError(
            "conflicting expected project policy hashes from --expect-policy-hash "
            "and TYPEFLUX_EXPECTED_POLICY_HASH"
        )
    return cli_hash or env_hash


def _normalize_policy_hash(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise _PolicyHashUsageError(
            "expected project policy hash must be a 64-character sha256 hex digest"
        )
    return normalized


def _print_workflow_table(report: ProjectValidationReport, *, json_output: bool = False) -> None:
    rows = [
        {
            "id": workflow.id,
            "path": workflow.path,
            "yaml_project": workflow.yaml_project or "",
            "yaml_name": workflow.yaml_name or "",
            "workflow_name": workflow.workflow_name or "",
            "task_queue": workflow.task_queue or "",
        }
        for workflow in report.workflows
    ]
    if json_output:
        # #814: every subcommand supports --json. The same rows the table shows, plus the
        # validation verdict the table's trailing hint carries.
        payload = {"workflows": rows, "ok": report.ok, "issue_count": len(report.issues)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    headers = ("id", "path", "yaml_project", "yaml_name", "workflow_name", "task_queue")
    widths = {
        header: max(len(header), *(len(row[header]) for row in rows)) if rows else len(header)
        for header in headers
    }
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print("  ".join(row[header].ljust(widths[header]) for header in headers))
    if not report.ok:
        print()
        print(f"{len(report.issues)} issue(s) found. Run validate for details.")


def _print_environment_table(project, *, json_output: bool = False) -> int:
    rows = []
    failed = False
    for environment_id, raw_path in project.environments.items():
        try:
            environment = load_project_environment(project, environment_id)
        except Exception as exc:  # noqa: BLE001 - show all environment status rows.
            failed = True
            rows.append(
                {
                    "id": environment_id,
                    "path": str(_resolve_project_cli_path(project, raw_path)),
                    "name": "",
                    "status": f"invalid: {exc}",
                }
            )
            continue
        rows.append(
            {
                "id": environment_id,
                "path": str(environment.profile_path),
                "name": environment.name,
                "status": "ok",
            }
        )
    if json_output:
        # #814: every subcommand supports --json. The same rows the table shows.
        print(json.dumps({"environments": rows}, indent=2, sort_keys=True))
        return 1 if failed else 0
    headers = ("id", "path", "name", "status")
    widths = {
        header: max(len(header), *(len(row[header]) for row in rows)) if rows else len(header)
        for header in headers
    }
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print("  ".join(row[header].ljust(widths[header]) for header in headers))
    return 1 if failed else 0


def _resolve_project_cli_path(project, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project.project_dir / path).resolve()


def _print_resolved_workflow(summary) -> None:
    print(f"Project: {summary.project_name}")
    print(f"Workflow: {summary.workflow_id} ({summary.workflow_name})")
    print(f"Environment: {summary.environment_id} ({summary.environment_name})")
    print(f"Task queue: {summary.task_queue}")
    print(f"Temporal: {summary.temporal['address']} / {summary.temporal['namespace']}")
    print(f"Provider: {summary.provider['type']}")
    print(f"Observability: {summary.observability['type']}")
    print(f"Workflow YAML: {summary.workflow_path}")


def _print_deployment_plan(plan) -> None:
    print(f"Project: {plan.project_name}")
    print(f"Environment: {plan.environment_id} ({plan.environment_name})")
    print(f"Target: {plan.target}")
    print(f"Image: {plan.image}")
    print(f"Project path in image: {plan.project_path_in_image}")
    print("Workers:")
    for worker in plan.workers:
        print(
            f"- {worker.workflow_id}: task_queue={worker.task_queue} "
            f"policy_hash={worker.policy.policy_hash}"
        )


def _print_deployment_render_result(result) -> None:
    print(f"Wrote deployment artifacts to {result.output_dir}:")
    for item in result.files:
        print(f"- {item.kind}: {item.path} sha256={item.sha256}")


def _print_admission_report(report) -> None:
    verdict = "ADMITTED" if report.admitted else "REJECTED"
    print(f"Admission: {verdict}")
    print(f"Origin: {report.spec_origin}")
    print(f"Environment: {report.environment_id}")
    if report.workflow_id is not None:
        print(f"Workflow slot: {report.workflow_id}")
    if report.policy_hash is not None:
        print(f"Policy hash: {report.policy_hash}")
    print("Checks:")
    for check in report.checks:
        suffix = f": {check.message}" if check.message else ""
        print(f"- [{check.status}] {check.code}{suffix}")


def _print_validation_report(
    report: ProjectValidationReport,
    *,
    environment_id: str | None,
) -> None:
    if report.ok:
        if environment_id is None:
            print(f"Project {report.project_name!r} is valid.")
            return
        print(f"Project {report.project_name!r} is valid for environment {environment_id!r}.")
        policy_applied = sum(
            1
            for resolved in report.resolved_workflows
            if any(
                check.code == "policy_selection" and check.status == "passed"
                for check in resolved.checks
            )
        )
        if policy_applied:
            print(
                f"Resolved workflow checks passed: {len(report.resolved_workflows)}. "
                f"Policy checks applied: {policy_applied}."
            )
        else:
            print(
                f"Resolved workflow checks passed: {len(report.resolved_workflows)}. "
                "No project policies matched."
            )
        return

    print(f"Project {report.project_name!r} is invalid:")
    for issue in report.issues:
        location = f" ({issue.path})" if issue.path else ""
        print(f"- {issue.code}: {issue.message}{location}")
    if environment_id is not None and report.resolved_workflows:
        print()
        print("Resolved workflow checks:")
        for resolved in report.resolved_workflows:
            status = "ok" if resolved.ok else "failed"
            print(f"- {resolved.environment_id}/{resolved.workflow_id}: {status}")
            for check in resolved.checks:
                if check.status == "failed":
                    message = f": {check.message}" if check.message else ""
                    print(f"  - {check.code}: {check.status}{message}")


async def _run_project_worker(
    project,
    *,
    workflow_id: str,
    environment_id: str,
    preflight: bool,
    policy_ids: Sequence[str] = (),
    expected_policy_hash_arg: str | None = None,
) -> int:
    try:
        expected_policy_hash = _expected_policy_hash(expected_policy_hash_arg)
        resolved = resolve_project_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
        )
        with project_environment_context(resolved.application):
            policy_guard = build_project_policy_runtime_guard(
                project=project,
                resolved=resolved,
                policy_ids=policy_ids,
                enforcement_mode="project_run",
            )
            _verify_expected_policy_hash(
                expected_policy_hash,
                policy_hash=(None if policy_guard is None else policy_guard.policy.policy_hash),
            )
            if preflight:
                validate_extension_imports(resolved.spec)
                report = preflight_ai_activities(
                    activities=tuple(collect_activities(resolved.spec).values()),
                    registry=_build_registry(resolved.spec),
                    provider=_preflight_provider_capability_source(resolved.spec),
                    provider_name=resolved.spec.runtime.provider.type,
                    provider_default_params=resolved.spec.runtime.provider.provider_params(),
                )
                print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
                return 0 if report.ok else 1
            runtime = await _build_runtime_with_policy_guard(
                resolved.spec, policy_guard, project=project, resolved=resolved
            )
            try:
                logger.info(
                    "starting Typeflux project worker: project=%s environment=%s workflow=%s "
                    "task_queue=%s",
                    project.name,
                    environment_id,
                    workflow_id,
                    resolved.spec.task_queue,
                )
                await runtime.worker.run()
            finally:
                logger.info(
                    "stopping Typeflux project worker: project=%s environment=%s workflow=%s "
                    "task_queue=%s",
                    project.name,
                    environment_id,
                    workflow_id,
                    resolved.spec.task_queue,
                )
                runtime.observability.writer.shutdown()
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI should report profile/runtime errors.
        print(f"Project workflow run failed: {exc}", file=sys.stderr)
        # #818: malformed/conflicting --expect-policy-hash input is a usage error (2);
        # an actual hash-drift/enforcement refusal is a verdict (3); anything else is 1.
        if isinstance(exc, _PolicyHashUsageError):
            return 2
        return EXIT_VALIDATION if isinstance(exc, ProjectPolicyEnforcementError) else 1


def _verify_expected_policy_hash(
    expected_policy_hash: str | None,
    *,
    policy_hash: str | None,
) -> None:
    if expected_policy_hash is None:
        return
    if policy_hash is None:
        raise ProjectPolicyEnforcementError(
            "expected project policy hash was provided, but no project policy was selected"
        )
    if policy_hash != expected_policy_hash:
        raise ProjectPolicyEnforcementError(
            "selected project policy hash does not match expected deployment policy hash "
            f"(expected={expected_policy_hash}, actual={policy_hash})"
        )


async def _submit_project_workflow(
    project,
    *,
    workflow_id: str,
    environment_id: str,
    input_path: str,
    workflow_id_override: str,
    task_queue: str | None,
    tags: Sequence[str],
    metadata_json: str | None,
    subject_ids: Sequence[str] = (),
    policy_ids: Sequence[str] = (),
    expected_policy_hash_arg: str | None = None,
) -> int:
    try:
        expected_policy_hash = _expected_policy_hash(expected_policy_hash_arg)
        resolved = resolve_project_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
        )
        input_model = _workflow_input_model(resolved.spec.project, resolved.spec.workflow.input)
        input_value = input_model.model_validate(_read_input_json(input_path))
        metadata = _parse_metadata_json(metadata_json)
        with project_environment_context(resolved.application):
            policy_guard = build_project_policy_runtime_guard(
                project=project,
                resolved=resolved,
                policy_ids=policy_ids,
                enforcement_mode="project_submit",
            )
            _verify_expected_policy_hash(
                expected_policy_hash,
                policy_hash=(None if policy_guard is None else policy_guard.policy.policy_hash),
            )
            runtime = await _build_runtime_with_policy_guard(
                resolved.spec, policy_guard, project=project, resolved=resolved
            )
            try:
                logger.info(
                    "submitting Typeflux project workflow: project=%s environment=%s "
                    "workflow=%s workflow_id=%s task_queue=%s",
                    project.name,
                    environment_id,
                    workflow_id,
                    workflow_id_override,
                    task_queue or resolved.spec.task_queue,
                )
                result = await runtime.execute_workflow(
                    input_value,
                    id=workflow_id_override,
                    task_queue=task_queue,
                    tags=tuple(tags),
                    metadata=metadata,
                    # #805: identical semantics to yaml/submit.py — an explicit --subject
                    # override wins over the spec `subjects:` extraction.
                    subject_ids=tuple(subject_ids) if subject_ids else None,
                )
                print(json.dumps(_json_payload(result), indent=2, sort_keys=True))
            finally:
                runtime.observability.writer.shutdown()
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should report profile/runtime errors.
        print(f"Project workflow submit failed: {exc}", file=sys.stderr)
        # #818: malformed/conflicting --expect-policy-hash input is a usage error (2);
        # an actual hash-drift/enforcement refusal is a verdict (3); anything else is 1.
        if isinstance(exc, _PolicyHashUsageError):
            return 2
        return EXIT_VALIDATION if isinstance(exc, ProjectPolicyEnforcementError) else 1


async def _migrate_project_workflow(
    project,
    *,
    workflow_id: str,
    environment_id: str,
    execution_id: str,
    run_id: str | None,
    abandon_gates: bool,
    reason: str | None,
    dry_run: bool = False,
    policy_ids: Sequence[str] = (),
    expected_policy_hash_arg: str | None = None,
) -> int:
    """Terminate a running execution and resubmit it against the current version (#204).

    Builds the operations runtime for the CURRENT resolved version (which runs
    frozen-version enforcement at build), then drives the driver's migrate:
    same-version / no-serving-workers / open-gate refusals surface as a non-zero
    exit with the reason on stderr, exactly like the control-plane route's 422s.
    """
    from typeflux.project.operations import WorkflowOperations

    try:
        expected_policy_hash = _expected_policy_hash(expected_policy_hash_arg)
        operations = await WorkflowOperations.for_project_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=tuple(policy_ids),
            expected_policy_hash=expected_policy_hash,
        )
        try:
            result = await operations.migrate(
                execution_id,
                run_id=run_id,
                abandon_gates=abandon_gates,
                reason=reason,
                dry_run=dry_run,
            )
        finally:
            operations.shutdown()
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports refusals/runtime errors, exit 1.
        print(f"Project workflow migrate failed: {exc}", file=sys.stderr)
        # #818: malformed/conflicting --expect-policy-hash input is a usage error (2);
        # an actual hash-drift/enforcement refusal is a verdict (3); anything else is 1.
        if isinstance(exc, _PolicyHashUsageError):
            return 2
        return EXIT_VALIDATION if isinstance(exc, ProjectPolicyEnforcementError) else 1


def _deploy_base_env(args) -> dict[str, str] | None:
    """The #798 CLI face of the #760 hermetic base_env seam: --base-env-file loads the
    file's pairs as the ONLY interpolation base; --hermetic is the empty-base form. None
    (neither flag) keeps process-environment interpolation — the pre-#798 behavior.

    The file parses with ``interpolate=False`` (codex): dotenv's own ``${...}`` expansion
    reads ``os.environ`` for names the file does not define, which would silently
    re-introduce the operator's shell into a base documented as the only source. A
    ``${...}`` in the file stays literal — visible garbage over invisible leakage.
    """
    if getattr(args, "base_env_file", None) and getattr(args, "hermetic", False):
        # #818: a flag-combination mistake is a usage error (exit 2), never operational.
        print("--base-env-file and --hermetic are mutually exclusive", file=sys.stderr)
        raise SystemExit(2)
    if getattr(args, "hermetic", False):
        return {}
    if getattr(args, "base_env_file", None):
        from dotenv import dotenv_values

        # Fail CLOSED on a missing/unreadable file (Bugbot): dotenv_values returns an
        # empty mapping for a typo'd path, which would silently behave like --hermetic
        # and author/verify plans against the wrong base.
        base_path = Path(args.base_env_file)
        if not base_path.is_file():
            print(f"--base-env-file not found: {base_path}", file=sys.stderr)
            raise SystemExit(2)
        return {
            key: value
            for key, value in dotenv_values(base_path, interpolate=False).items()
            if value is not None
        }
    return None


async def _lifecycle_project_operation(
    project,
    *,
    command: str,
    workflow_id: str,
    environment_id: str,
    execution_id: str,
    run_id: str | None,
    trace: bool = False,
    decision: str | None = None,
    gate: str | None = None,
    reviewer: str | None = None,
    notes: str | None = None,
    reason: str | None = None,
    policy_ids: Sequence[str] = (),
    expected_policy_hash_arg: str | None = None,
) -> int:
    """status/review/cancel over the SAME pinned operations runtime the CP routes bind
    (#802): version-pinned enforcement runs at build, refusals surface as non-zero exits
    with the reason on stderr — a CLI-only operator gets the exact gate semantics."""
    from typeflux.core.contracts import ReviewCommand
    from typeflux.project.operations import WorkflowOperations

    try:
        expected_policy_hash = _expected_policy_hash(expected_policy_hash_arg)
        operations = await WorkflowOperations.for_project_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=tuple(policy_ids),
            expected_policy_hash=expected_policy_hash,
        )
        try:
            if command == "status":
                status = await operations.status(execution_id, run_id=run_id, trace=trace)
                print(json.dumps(status.model_dump(mode="json"), indent=2, sort_keys=True))
            elif command == "review":
                await operations.submit_review(
                    execution_id,
                    ReviewCommand(
                        user_decision=cast(str, decision),
                        reviewer=reviewer,
                        notes=notes,
                        gate=gate,
                    ),
                    run_id=run_id,
                )
                print(
                    json.dumps(
                        {"execution_id": execution_id, "review": "submitted"},
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                await operations.request_cancel(execution_id, reason, run_id=run_id)
                print(
                    json.dumps(
                        {"cancel": "requested", "execution_id": execution_id},
                        indent=2,
                        sort_keys=True,
                    )
                )
        finally:
            operations.shutdown()
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports refusals/runtime errors, exit 1.
        print(f"Project workflow {command} failed: {exc}", file=sys.stderr)
        if isinstance(exc, _PolicyHashUsageError):
            return 2
        return EXIT_VALIDATION if isinstance(exc, ProjectPolicyEnforcementError) else 1


def _parse_erase_surfaces(raw: Sequence[str]) -> tuple[str, ...]:
    """Normalize repeatable/comma-separated ``--surface`` values; default = all three."""

    from typeflux.project.erase import ERASURE_SURFACES

    if not raw:
        return ERASURE_SURFACES
    names = [name.strip() for chunk in raw for name in chunk.split(",") if name.strip()]
    if not names:
        raise ValueError(
            f"--surface named no surfaces; valid surfaces are {', '.join(ERASURE_SURFACES)}"
        )
    unknown = sorted(set(names) - set(ERASURE_SURFACES))
    if unknown:
        raise ValueError(
            f"unknown surface(s) {', '.join(unknown)}; valid surfaces are "
            f"{', '.join(ERASURE_SURFACES)}"
        )
    return tuple(name for name in ERASURE_SURFACES if name in set(names))


def _parse_erase_timestamp(flag: str, raw: str | None) -> Any:
    if raw is None:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{flag} must be an ISO-8601 timestamp (got {raw!r}): {exc}") from exc


def _instantiate_erase_backend(flag: str, class_path: str) -> Any:
    """Import and zero-arg-instantiate a CLI-injected backend (the ``backend_class``
    convention: the object reads its own configuration from the environment)."""

    backend_cls = import_object(class_path)
    if not callable(backend_cls):
        raise TypeError(f"{flag} must reference a callable (module:Name), got {class_path!r}")
    return backend_cls()


def _codec_free_spec(spec: Any) -> Any:
    """The resolved spec with ``runtime.temporal.payload_codec`` stripped (#715 item 4).

    The erase client never encodes or decodes workflow payloads (visibility reads +
    ``DeleteWorkflowExecution`` only), so it needs no codec — and MUST not require
    one: a ``subject_scope`` spec without an injected keystore fails closed at codec
    construction, which would block plain history deletion behind key material the
    operation never uses. Everything else (address, namespace, TLS, api_key) is
    preserved so the connection honors the manifest exactly like the worker's.
    """

    if spec.runtime.temporal.payload_codec is None:
        return spec
    temporal_spec = spec.runtime.temporal.model_copy(update={"payload_codec": None})
    runtime_spec = spec.runtime.model_copy(update={"temporal": temporal_spec})
    return spec.model_copy(update={"runtime": runtime_spec})


async def _erase_project_subjects(
    project,
    *,
    workflow_id: str,
    environment_id: str,
    subject_ids: tuple[str, ...],
    surfaces: tuple[str, ...],
    dry_run: bool,
    actor: str | None,
    since: Any,
    until: Any,
    keystore_class: str | None,
    cache_store_class: str | None,
    execution_limit: int,
    json_output: bool,
) -> int:
    """Wire the project's resolved environment into the ``erase_subject`` seam (#715).

    The workflow/environment selection supplies the Temporal profile (address,
    namespace, TLS, payload codec) and the observability backend, exactly like
    drain/migrate. The keystore and cache store are CODE-injected via import paths —
    the reference in-memory backends are process-local, so a surface without an
    injected backend is honestly reported skipped by the seam, never silently minted.
    """
    import getpass

    from typeflux.project.environment import async_project_environment_context
    from typeflux.project.erase import erase_subject
    from typeflux.yaml.runtime import _connect_client
    from typeflux.yaml.subject_keystore import SubjectKeystore

    try:
        resolved_actor = actor if actor is not None else getpass.getuser()
        resolved = await asyncio.to_thread(
            resolve_project_workflow,
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
        )
        async with async_project_environment_context(resolved.application):
            # Backends are constructed INSIDE the environment overlay (#715 fix
            # round, item 1): a backend that reads its DSN/credentials from env in
            # __init__ must see the environment's variables, not the ambient shell
            # env — on --execute an ambient-configured backend could shred/delete
            # against the WRONG backing store.
            keystore: Any | None = None
            if keystore_class is not None:
                keystore = _instantiate_erase_backend("--keystore-class", keystore_class)
                if not isinstance(keystore, SubjectKeystore):
                    raise TypeError(
                        f"--keystore-class {keystore_class!r} did not yield a SubjectKeystore "
                        "(it must expose data_key(subject_id, *, create) and "
                        "destroy_subject_key(subject_id))"
                    )
            cache_store: Any | None = None
            if cache_store_class is not None:
                cache_store = _instantiate_erase_backend("--cache-store-class", cache_store_class)
            client = None
            if "temporal" in surfaces:
                # Erase never encodes or decodes workflow payloads — it only reads
                # visibility (list/describe) and issues DeleteWorkflowExecution — so
                # the client connects CODEC-FREE (#715 fix round, item 4). This keeps
                # history deletion available when the operator lacks codec key
                # material, and keeps a subject_scope spec without --keystore-class
                # from tripping the codec seam's fail-closed keystore requirement:
                # the crypto-shred surface is driven by the seam through the
                # injected keystore directly, never through the payload codec.
                client = await _connect_client(_codec_free_spec(resolved.spec), plugin=None)
            trace_reader = None
            if "langfuse" in surfaces and resolved.spec.runtime.observability.type == "langfuse":
                # Spec-declared credentials (#793) count exactly like env ones — the
                # workflow's OWN spec is in hand here, so a workflow whose Langfuse
                # credentials come only from runtime.observability.langfuse.* must not
                # be skipped as unconfigured. Resolution mirrors the worker build
                # (spec wins field-by-field, env fallback); an unconfigured backend is
                # still reported skipped-with-reason by the seam.
                from typeflux.observability.langfuse import (
                    LangfuseObservabilityBackend,
                )
                from typeflux.yaml.runtime import (
                    resolved_observability_credentials,
                )

                credentials = resolved_observability_credentials(resolved.spec)
                # Per-FIELD spec+env combination (codex): a mixed pair (spec public_key,
                # env secret_key) is configured — the same fallback from_env applies.
                if LangfuseObservabilityBackend.is_configured(
                    public_key=credentials.langfuse_public_key,
                    secret_key=credentials.langfuse_secret_key,
                ):
                    trace_reader = LangfuseObservabilityBackend.from_env(
                        public_key=credentials.langfuse_public_key,
                        secret_key=credentials.langfuse_secret_key,
                        host=credentials.langfuse_host,
                    ).reader
            receipt = await erase_subject(
                subject_ids,
                actor=resolved_actor,
                dry_run=dry_run,
                surfaces=surfaces,
                temporal_client=client,
                subject_keystore=keystore,
                trace_reader=trace_reader,
                cache_store=cache_store,
                require_targeted_cache=resolved.spec.runtime.cache_erasure == "targeted",
                since=since,
                until=until,
                execution_limit=execution_limit,
            )
    except Exception as exc:  # noqa: BLE001 - CLI reports wiring/seam errors, exit 1.
        print(f"Project subject erase failed: {exc}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    else:
        _print_erasure_receipt(receipt)
    # A FAILED surface must be unmistakable: non-zero exit whenever any surface failed
    # (dry-run included — a failed dry run is not a reviewable plan).
    return 1 if receipt.failed else 0


def _print_erasure_receipt(receipt) -> None:
    mode = "DRY RUN (no mutation)" if receipt.dry_run else "EXECUTED"
    print(f"Erasure {mode}")
    print(f"Subjects: {', '.join(receipt.subject_ids)}")
    print(f"Actor: {receipt.actor}")
    print(f"At: {receipt.executed_at}")
    print()
    keystore = receipt.temporal.keystore
    executions = receipt.temporal.executions
    print(f"temporal [{receipt.temporal.status}]")
    if receipt.temporal.skip_reason is not None:
        print(f"  skipped: {receipt.temporal.skip_reason}")
    else:
        if keystore.skip_reason is not None:
            print(f"  keystore [skipped]: {keystore.skip_reason}")
        else:
            print(
                f"  keystore [{keystore.status}]: "
                f"shreddable={keystore.shreddable_key_records} "
                f"shredded={keystore.shredded_key_records}"
            )
            for entry in keystore.entries:
                print(
                    f"    - {entry.subject_id}: state_before={entry.state_before} "
                    f"would_shred={entry.would_shred} shredded={entry.shredded}"
                )
            for failure in keystore.failures:
                print(f"    ! {failure.subject_id}: {failure.error}")
        if executions.skip_reason is not None:
            print(f"  executions [skipped]: {executions.skip_reason}")
        else:
            print(f"  executions [{executions.status}]:")
            for report in executions.reports:
                print(
                    f"    - {report.subject_id}: matched={report.executions_matched} "
                    f"deletable={len(report.deletable)} deleted={report.deleted_count} "
                    f"conflicted={len(report.conflicted)} running={len(report.still_running)} "
                    f"failed={len(report.failures)}"
                )
            for failure in executions.failures:
                print(f"    ! {failure.subject_id}: {failure.error}")
    print(f"langfuse [{receipt.langfuse.status}]")
    if receipt.langfuse.skip_reason is not None:
        print(f"  skipped: {receipt.langfuse.skip_reason}")
    else:
        for trace_report in receipt.langfuse.reports:
            print(
                f"  - {trace_report.subject_id}: deletable={len(trace_report.trace_ids)} "
                f"deleted={trace_report.deleted_count} "
                f"conflicted={len(trace_report.conflicted)} "
                f"failed={len(trace_report.failures)}"
            )
        for failure in receipt.langfuse.failures:
            print(f"  ! {failure.subject_id}: {failure.error}")
    print(f"cache [{receipt.cache.status}]")
    if receipt.cache.skip_reason is not None:
        print(f"  skipped: {receipt.cache.skip_reason}")
    else:
        for cache_report in receipt.cache.reports:
            supported = "" if cache_report.supported else " (NOT SUPPORTED: full flush required)"
            print(
                f"  - {cache_report.subject_id}: found={cache_report.keys_found} "
                f"deleted={cache_report.keys_deleted}{supported}"
            )
        for failure in receipt.cache.failures:
            print(f"  ! {failure.subject_id}: {failure.error}")
    print()
    print("Unreachable surfaces (document-only):")
    for note in receipt.unreachable:
        print(f"  - {note.surface}: {note.note}")
    if receipt.warnings:
        print()
        print("Warnings:")
        for warning in receipt.warnings:
            print(f"  - {warning}")
    print()
    print(
        "The receipt is the proof of erasure — persist it (use --json) OUTSIDE the erased surfaces."
    )


def _workflow_input_model(project: str, ref: str) -> type[BaseModel]:
    value = import_type_ref(project, ref)
    if not issubclass(value, BaseModel):
        raise TypeError(f"workflow input must resolve to a Pydantic BaseModel: {ref}")
    return value


async def _build_runtime_with_policy_guard(spec, policy_guard, *, project=None, resolved=None):
    # Sub-workflows (#55 §3.4): resolve the parent's references so the worker co-registers the
    # transitive child classes + activities. Resolution holds the project env lock, so run it in
    # a worker thread (never on the event loop, #590). Absent project context ⇒ V1 build.
    subworkflows = None
    if project is not None and resolved is not None:
        subworkflows = await asyncio.to_thread(resolve_subworkflows_for, project, resolved)
    kwargs: dict[str, Any] = {}
    if policy_guard is not None:
        kwargs["policy_guard"] = policy_guard
    if subworkflows is not None:
        kwargs["subworkflow_records"] = subworkflows.records
        kwargs["child_workflow_classes"] = subworkflows.workflow_classes
        kwargs["child_activities"] = subworkflows.activities
        kwargs["child_registry_specs"] = tuple(subworkflows.child_specs.items())
    return await build_runtime(spec, **kwargs)


def _preflight_provider_capability_source(spec: Any) -> object:
    provider_class = spec.runtime.provider.provider_class
    if provider_class is None:
        return spec.runtime.provider.type
    return import_object(provider_class)


def _read_input_json(path: str) -> Any:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def _parse_metadata_json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    metadata = json.loads(value)
    if not isinstance(metadata, Mapping):
        raise TypeError("--metadata-json must decode to a JSON object")
    return dict(metadata)


def _json_payload(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


#: Exit-code contract (#818), shared with the TS CLI: 0 = success; 1 = operational
#: failure (unreachable backends, profile/runtime errors) or a negative STATUS verdict
#: (drain-status not drained, erase receipt failed); 2 = usage error (argparse);
#: 3 = validation/admission/drift verdict (project validation, bundle validation,
#: plan drift, admission REJECTED, expected-policy-hash drift). CI can distinguish
#: "fix your flags" (2) from "your project is invalid / drifted" (3) from "the
#: operation itself failed" (1).
EXIT_VALIDATION = 3


def _print_project_command_error(exc: Exception, *, json_output: bool) -> int:
    if json_output:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        prefix = (
            "Project environment error" if isinstance(exc, ProjectEnvironmentError) else "Error"
        )
        print(f"{prefix}: {exc}", file=sys.stderr)
    if isinstance(exc, ProjectPolicyEnforcementError):
        return EXIT_VALIDATION
    return 1


def _print_project_validation_error(
    report: ProjectValidationReport,
    *,
    json_output: bool,
) -> int:
    if json_output:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return EXIT_VALIDATION
    print(f"Project {report.project_name!r} is invalid:", file=sys.stderr)
    for issue in report.issues:
        location = f" ({issue.path})" if issue.path else ""
        print(f"- {issue.code}: {issue.message}{location}", file=sys.stderr)
    return EXIT_VALIDATION


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
