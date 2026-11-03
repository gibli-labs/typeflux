"""Start a fresh Temporal run of the same computation; requires the Temporal extra/server."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from typeflux.project import load_project_spec
from typeflux.project.environment import resolve_project_workflow
from typeflux.yaml import build_runtime


async def main():
    root = Path(__file__).resolve().parent
    project = load_project_spec(root / "typeflux.project.yaml")
    resolved = resolve_project_workflow(project, workflow_id="echo", environment_id="temporal")
    runtime = await build_runtime(resolved.spec)
    try:
        async with runtime.worker.build_worker():
            result = await runtime.execute_workflow(
                json.loads((root / "input.json").read_text()), id=f"local-example-{uuid4().hex}"
            )
            print(result.model_dump_json())
    finally:
        runtime.observability.writer.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
