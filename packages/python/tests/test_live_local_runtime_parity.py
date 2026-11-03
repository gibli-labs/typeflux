"""Shared computational fixtures on local SDKs and real isolated Temporal workers."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import textwrap
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
                patch = copy.deepcopy(case.get("spec_overrides", {}).get(workflow_id, {}))
                raw["runtime"].update(patch.pop("runtime", {}))
                raw.update(patch)
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

            async def build():
                if engine == "local":
                    return build_local_runtime(
                        resolved.spec,
                        workflow_id="root",
                        resolve_workflow=lambda key: (
                            resolve_project_workflow(
                                project, workflow_id=key, environment_id="offline"
                            ).spec
                        ),
                    )
                children = resolve_subworkflows_for(project, resolved)
                return await build_runtime(
                    resolved.spec,
                    subworkflow_records=children.records,
                    child_workflow_classes=children.workflow_classes,
                    child_activities=children.activities,
                    child_registry_specs=tuple(children.child_specs.items()),
                )

            if "error" in case:
                with pytest.raises(services.RegistryCompositionError, match=case["error"]):
                    await build()
                assert provider.calls == []
                results.append("refused")
                continue
            runtime = await build()
            if engine == "local":
                try:
                    result = await runtime.execute_workflow(case["input"])
                finally:
                    runtime.shutdown()
            else:
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


@pytest.mark.asyncio
async def test_offline_example_temporal_adoption_script():
    root = Path(__file__).resolve().parents[1] / "examples/local_runtime"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(root / "temporal_sdk.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, err.decode()
        assert json.loads(out) == {"text": "local result"}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_temporal_adoption_recovers_same_run_and_enforces_timeout():
    root = Path(__file__).resolve().parents[1] / "examples/local_runtime"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(root / "recovery_demo.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, err.decode()
        assert json.loads(out) == [
            {
                "scenario": "retry",
                "attempts": [1, 2],
                "sameRun": True,
                "retryRecordedBeforeStop": True,
                "status": "COMPLETED",
                "maximumAttempts": 2,
                "startToCloseSeconds": 10,
                "result": {"text": "local result"},
            },
            {
                "scenario": "timeout",
                "attempts": [1],
                "sameRun": True,
                "retryRecordedBeforeStop": False,
                "status": "FAILED",
                "maximumAttempts": 1,
                "startToCloseSeconds": 0.5,
                "result": None,
            },
        ]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_temporal_adoption_waits_for_delayed_failure_report_before_shutdown():
    root = Path(__file__).resolve().parents[1] / "examples/local_runtime"
    # Delay SDK failure encoding after the provider has notified the demo. The
    # previous provider-local barrier shuts down during this controlled window.
    script = textwrap.dedent("""
        import asyncio, runpy, sys
        from pathlib import Path
        from unittest.mock import patch
        from temporalio.converter import DataConverter
        from temporalio.worker import Worker
        from typeflux.providers.errors import ProviderTransientError
        root = Path(sys.argv[1])
        sys.path.insert(0, str(root))
        from local_example.recovery_provider import RecoveryProvider
        demo = runpy.run_path(str(root / "recovery_demo.py"))
        async def verify():
            failed, encoded = asyncio.Event(), asyncio.Event()
            delays = 0
            call = RecoveryProvider.async_structured_call
            encode = DataConverter.encode_failure
            shutdown = Worker.shutdown
            async def observed_call(self, **kwargs):
                try:
                    return await call(self, **kwargs)
                except ProviderTransientError:
                    failed.set()
                    raise
            async def delayed_encode(self, error, failure):
                nonlocal delays
                observed = isinstance(error, ProviderTransientError)
                if observed:
                    delays += 1
                    await asyncio.sleep(0.25)
                result = await encode(self, error, failure)
                if observed:
                    encoded.set()
                return result
            async def checked_shutdown(self):
                premature = failed.is_set() and not encoded.is_set()
                try:
                    await shutdown(self)
                finally:
                    assert not premature, "worker shutdown preceded failure report"
            with patch.object(RecoveryProvider, "async_structured_call", observed_call), \
                 patch.object(DataConverter, "encode_failure", delayed_encode), \
                 patch.object(Worker, "shutdown", checked_shutdown):
                await demo["main"]()
            assert delays == 1 and encoded.is_set()
        asyncio.run(verify())
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, err.decode()
        observations = json.loads(out)
        assert observations[0]["attempts"] == [1, 2]
        assert observations[0]["sameRun"] is True
        assert observations[0]["retryRecordedBeforeStop"] is True
        assert observations[1]["status"] == "FAILED"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
