"""Offline local SDK invocation; run after installing the base candidate wheel."""

import asyncio
import json
from pathlib import Path

from typeflux.yaml import build_local_runtime, load_yaml_spec


async def main():
    root = Path(__file__).resolve().parent
    runtime = build_local_runtime(load_yaml_spec(root / "workflow.yaml"))
    try:
        result = await runtime.execute_workflow(json.loads((root / "input.json").read_text()))
        print(result.model_dump_json())
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
