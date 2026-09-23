from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from typeflux.yaml.imports import import_type_ref
from typeflux.yaml.loader import load_yaml_spec
from typeflux.yaml.runtime import build_runtime

logger = logging.getLogger(__name__)


async def _amain(
    spec_path: str,
    *,
    input_path: str,
    workflow_id: str,
    task_queue: str | None = None,
    tags: Sequence[str] = (),
    metadata_json: str | None = None,
    subject_ids: Sequence[str] = (),
) -> int:
    spec = load_yaml_spec(spec_path)
    input_model = _workflow_input_model(spec.project, spec.workflow.input)
    input_value = input_model.model_validate(_read_input_json(input_path))
    metadata = _parse_metadata_json(metadata_json)
    runtime = await build_runtime(spec)
    try:
        logger.info(
            "submitting Typeflux YAML workflow: spec=%s workflow=%s workflow_id=%s task_queue=%s",
            spec_path,
            spec.workflow.name,
            workflow_id,
            task_queue or spec.task_queue,
        )
        result = await runtime.execute_workflow(
            input_value,
            id=workflow_id,
            task_queue=task_queue,
            tags=tuple(tags),
            metadata=metadata,
            # An explicit --subject override wins over the spec `subjects:` block;
            # None (no flag) falls through to declarative extraction (#715).
            subject_ids=tuple(subject_ids) if subject_ids else None,
        )
        print(json.dumps(_json_payload(result), indent=2, sort_keys=True))
    finally:
        runtime.observability.writer.shutdown()
    return 0


def _workflow_input_model(project: str, ref: str) -> type[BaseModel]:
    value = import_type_ref(project, ref)
    if not issubclass(value, BaseModel):
        raise TypeError(f"workflow input must resolve to a Pydantic BaseModel: {ref}")
    return value


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m typeflux.yaml.submit",
        description="Submit a Typeflux YAML workflow.",
    )
    parser.add_argument("spec", help="Path to typeflux.yaml")
    parser.add_argument(
        "--input",
        required=True,
        help="Path to workflow input JSON, or '-' to read JSON from stdin.",
    )
    parser.add_argument("--workflow-id", required=True, help="Temporal workflow ID to use.")
    parser.add_argument(
        "--task-queue",
        help="Override the YAML task queue for this workflow start.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="Search/trace tag to attach. May be provided more than once.",
    )
    parser.add_argument(
        "--metadata-json",
        help="JSON object merged into the root workflow trace metadata.",
    )
    parser.add_argument(
        "--subject",
        action="append",
        default=[],
        dest="subject_ids",
        help=(
            "Subject id to index this execution under (TypefluxSubjectIds). May be "
            "provided more than once. Overrides the spec `subjects:` extraction."
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(
        _amain(
            args.spec,
            input_path=args.input,
            workflow_id=args.workflow_id,
            task_queue=args.task_queue,
            tags=tuple(args.tags),
            metadata_json=args.metadata_json,
            subject_ids=tuple(args.subject_ids),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
