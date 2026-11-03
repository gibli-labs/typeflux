"""Shared computational fixtures on local SDKs and real isolated Temporal workers."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from typeflux.project import load_project_spec
from typeflux.project.environment import resolve_project_workflow, resolve_subworkflows_for
from typeflux.testing import FakeProvider
from typeflux.yaml import build_local_runtime, build_runtime
from typeflux.yaml import runtime as services

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("TYPEFLUX_LIVE_TEMPORAL") != "1", reason="isolated Temporal lane required"
    ),
]
FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[3] / "contracts/local-runtime/golden/execution-cases.json"
    ).read_text()
)


class NumericProvider(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.calls = []
        self.active = self.peak = 0

    async def async_structured_call(self, *, messages, output_schema, **kwargs):
        n = int(messages[-1].content)
        self.calls.append(n)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            # Different latencies expose result-order mistakes without pinning start order.
            await asyncio.sleep((10 - min(n, 9)) * 0.005)
            return output_schema(n=n + 1)
        finally:
            self.active -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda case: case["name"])
async def test_shared_local_and_temporal_execution_fixtures(case, tmp_path, monkeypatch):
    package = tmp_path / "local_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(
        "from pydantic import BaseModel\n"
        "class Item(BaseModel):\n    n: int\n"
        "class Batch(BaseModel):\n    items: list[Item]\n"
        "class Branches(BaseModel):\n    left: Item\n    right: Item | None\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        results = []
        for engine in ("local", "temporal"):
            directory = tmp_path / engine
            directory.mkdir()
            specifications = {"root": case["workflow"], **case["children"]}
            for workflow_id, workflow in specifications.items():
                raw = copy.deepcopy(FIXTURES["base"])
                raw["name"] = workflow_id
                raw["workflow"] = {"name": workflow_id, **workflow}
                # Only placement changes; computational steps, contracts and prompts do not.
                raw["runtime"]["engine"] = engine
                if engine == "temporal":
                    raw["task_queue"] = f"local-parity-{uuid4().hex}"
                (directory / f"{workflow_id}.yaml").write_text(yaml.safe_dump(raw))
            manifest = directory / "typeflux.project.yaml"
            manifest.write_text(
                yaml.safe_dump(
                    {
                        "name": "parity",
                        "workflows": [{"id": key, "path": f"{key}.yaml"} for key in specifications],
                        "environments": {"offline": "environment.yaml"},
                    }
                )
            )
            (directory / "environment.yaml").write_text("name: offline\n")
            project = load_project_spec(manifest)
            resolved = resolve_project_workflow(
                project, workflow_id="root", environment_id="offline"
            )
            provider = NumericProvider()
            monkeypatch.setattr(services, "_build_provider", lambda *args, **kwargs: provider)
            if engine == "local":
                runtime = build_local_runtime(
                    resolved.spec,
                    workflow_id="root",
                    resolve_workflow=lambda key: (
                        resolve_project_workflow(
                            project, workflow_id=key, environment_id="offline"
                        ).spec
                    ),
                )
                try:
                    result = await runtime.execute_workflow(case["input"])
                finally:
                    runtime.shutdown()
            else:
                children = resolve_subworkflows_for(project, resolved)
                runtime = await build_runtime(
                    resolved.spec,
                    subworkflow_records=children.records,
                    child_workflow_classes=children.workflow_classes,
                    child_activities=children.activities,
                    child_registry_specs=tuple(children.child_specs.items()),
                )
                async with runtime.worker.build_worker():
                    result = await runtime.execute_workflow(
                        getattr(runtime.workflow_class, "__typeflux_input_type__").model_validate(
                            case["input"]
                        ),
                        id=f"local-parity-{uuid4().hex}",
                    )
            output = result.model_dump() if hasattr(result, "model_dump") else result
            assert output == case["expected"], engine
            results.append(output)
            calls = provider.calls
            expected_calls = case["calls"]
            if case.get("call_order") == "unordered":
                calls, expected_calls = sorted(calls), sorted(expected_calls)
            assert calls == expected_calls, engine
            assert provider.active == 0
            assert provider.peak <= 2, engine
        assert results[0] == results[1]
    finally:
        for name in tuple(sys.modules):
            if name == "local_fixture" or name.startswith("local_fixture."):
                sys.modules.pop(name, None)
