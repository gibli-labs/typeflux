from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from typeflux.execution.preflight import preflight_ai_activities
from typeflux.yaml.imports import (
    collect_activities,
    import_object,
    validate_extension_imports,
)
from typeflux.yaml.loader import load_yaml_spec
from typeflux.yaml.runtime import _build_registry, build_runtime
from typeflux.yaml.spec import TypefluxYamlSpec

logger = logging.getLogger(__name__)


async def _amain(path: str, *, preflight: bool = False) -> int:
    _reject_expected_policy_hash_env()
    spec = load_yaml_spec(path)
    if preflight:
        validate_extension_imports(spec)
        report = preflight_ai_activities(
            activities=tuple(collect_activities(spec).values()),
            registry=_build_registry(spec),
            provider=_preflight_provider_capability_source(spec),
            provider_name=spec.runtime.provider.type,
            provider_default_params=spec.runtime.provider.provider_params(),
        )
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0 if report.ok else 1
    runtime = await build_runtime(spec)
    workflow_name = getattr(getattr(spec, "workflow", None), "name", "<unknown>")
    task_queue = getattr(spec, "task_queue", "<unknown>")
    try:
        logger.info(
            "starting Typeflux YAML worker: spec=%s workflow=%s task_queue=%s",
            path,
            workflow_name,
            task_queue,
        )
        await runtime.worker.run()
    finally:
        logger.info("stopping Typeflux YAML worker: spec=%s task_queue=%s", path, task_queue)
        runtime.observability.writer.shutdown()
    return 0


def _reject_expected_policy_hash_env() -> None:
    # yaml.run is the local-dev single-spec worker path and has no project
    # policy machinery; a deployment that expects a policy hash must run the
    # worker through the project entrypoint, which composes and enforces it.
    if os.getenv("TYPEFLUX_EXPECTED_POLICY_HASH"):
        raise SystemExit(
            "TYPEFLUX_EXPECTED_POLICY_HASH is set, but 'python -m typeflux.yaml.run' "
            "does not enforce project policy. Run the worker through "
            "'python -m typeflux.project run ... --expect-policy-hash' instead."
        )


def _preflight_provider_capability_source(spec: TypefluxYamlSpec) -> object:
    provider_class = spec.runtime.provider.provider_class
    if provider_class is None:
        return spec.runtime.provider.type
    return import_object(provider_class)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m typeflux.yaml.run",
        description="Run a Typeflux Temporal worker from YAML.",
    )
    parser.add_argument("spec", help="Path to typeflux.yaml")
    parser.add_argument(
        "--preflight", action="store_true", help="Resolve and validate prompts, then exit."
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_amain(args.spec, preflight=args.preflight))


if __name__ == "__main__":
    raise SystemExit(main())
