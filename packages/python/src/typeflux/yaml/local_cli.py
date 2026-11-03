"""Foreground local YAML execution: JSON on stdout, diagnostics on stderr."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any

from typeflux.project.cli_support import (
    PolicyHashUsageError,
    expected_policy_hash,
    read_input_json,
    verify_expected_policy_hash,
)
from typeflux.project.environment import project_environment_context, resolve_project_workflow
from typeflux.project.loader import load_project_spec
from typeflux.project.policy_enforcement import (
    ProjectPolicyEnforcementError,
    build_project_policy_runtime_guard,
)
from typeflux.yaml.loader import load_yaml_spec
from typeflux.yaml.local import LocalYamlRuntime, build_local_runtime
from typeflux.yaml.spec import TypefluxYamlSpec


@contextmanager
def _runtime(args: argparse.Namespace) -> Iterator[LocalYamlRuntime]:
    # Preserve the operator's pin before application files overlay the environment.
    expected = expected_policy_hash(args.expect_policy_hash)
    if args.project:
        project = load_project_spec(args.project)
        resolved = resolve_project_workflow(
            project,
            workflow_id=args.workflow,
            environment_id=args.environment,
            base_env=dict(os.environ),
        )
        with project_environment_context(resolved.application):
            expected = expected_policy_hash(expected)
            guard = build_project_policy_runtime_guard(
                project=project,
                resolved=resolved,
                policy_ids=tuple(args.policy),
                enforcement_mode="project_local",
            )
            verify_expected_policy_hash(
                expected,
                policy_hash=None if guard is None else guard.policy.policy_hash,
            )
            declared = {workflow.id for workflow in project.workflows}

            def child_spec(workflow_id: str) -> TypefluxYamlSpec | None:
                if workflow_id not in declared:
                    return None
                return resolve_project_workflow(
                    project,
                    workflow_id=workflow_id,
                    environment_id=resolved.environment_id,
                    base_env=resolved.base_env,
                ).spec

            runtime = build_local_runtime(
                resolved.spec,
                resolve_workflow=child_spec,
                workflow_id=resolved.workflow_id,
                policy_guard=guard,
            )
            try:
                yield runtime
            finally:
                runtime.shutdown()
    else:
        spec = load_yaml_spec(args.spec)
        if expected_policy_hash(expected) is not None:
            raise ProjectPolicyEnforcementError(
                "an expected policy hash requires --project, --workflow and --environment"
            )
        runtime = build_local_runtime(spec)
        try:
            yield runtime
        finally:
            runtime.shutdown()


async def _execute(args: argparse.Namespace, value: dict[str, Any]) -> tuple[int, str | None]:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous_term = signal.getsignal(signal.SIGTERM)
    interrupted = False

    def terminate() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            assert task is not None
            task.cancel()

    if os.name == "posix":
        loop.add_signal_handler(signal.SIGTERM, terminate)
    phase = "configuration"
    try:
        with _runtime(args) as runtime:
            phase = "input"
            parsed = runtime.input_type.model_validate(value)
            phase = "execution"
            # Deliver cancellation received during synchronous admission before starting work.
            await asyncio.sleep(0)
            result = await runtime.execute_workflow(parsed)
            payload = result.model_dump_json()
        return 0, payload
    except asyncio.CancelledError:
        if not interrupted:
            raise
        print("Local workflow interrupted by SIGTERM", file=sys.stderr)
        return 143, None
    except PolicyHashUsageError as exc:
        print(f"Local workflow usage error: {exc}", file=sys.stderr)
        return 2, None
    except Exception as exc:  # noqa: BLE001 - the CLI reports a stable exit category.
        code = 2 if phase == "input" else 3 if phase == "configuration" else 1
        if isinstance(exc, OSError):
            code = 1
        print(f"Local workflow {phase} failed: {exc}", file=sys.stderr)
        return code, None
    finally:
        if os.name == "posix":
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous_term)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="typeflux-local",
        description="Execute a local YAML workflow in this process.",
    )
    parser.add_argument("spec", nargs="?", help="Path to workflow YAML")
    parser.add_argument("--project", help="Path to typeflux.project.yaml")
    parser.add_argument("--workflow", help="Project workflow ID")
    parser.add_argument("--environment", help="Project environment ID")
    parser.add_argument(
        "--policy", action="append", default=[], help="Project policy ID; repeatable"
    )
    parser.add_argument("--expect-policy-hash", help="Expected composed project policy SHA-256")
    parser.add_argument("--input", required=True, help="Input JSON file, or '-' for stdin")
    args = parser.parse_args(argv)
    if bool(args.spec) == bool(args.project):
        parser.error("provide either a YAML spec or --project")
    if args.project and (not args.workflow or not args.environment):
        parser.error("--project requires --workflow and --environment")
    if not args.project and (args.workflow or args.environment or args.policy):
        parser.error("--workflow, --environment and --policy require --project")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    original_path = list(sys.path)
    try:
        # Application modules are trusted code, as on the SDK/worker extension boundary.
        sys.path.insert(0, str(Path(args.project or args.spec).expanduser().resolve().parent))
        with redirect_stdout(sys.stderr):
            try:
                value = read_input_json(args.input)
                if not isinstance(value, dict):
                    raise ValueError("--input must decode to a JSON object")
            except (OSError, ValueError) as exc:
                print(f"Local workflow input failed: {exc}", file=sys.stderr)
                return 2
            code, payload = asyncio.run(_execute(args, value))
        if payload is not None:
            print(payload)
        return code
    except KeyboardInterrupt:
        print("Local workflow interrupted by SIGINT", file=sys.stderr)
        return 130
    finally:
        sys.path[:] = original_path


if __name__ == "__main__":
    raise SystemExit(main())
