"""Exercise the installed Python planning surface without Temporal or network access."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel

from typeflux import TypefluxError
from typeflux.execution import execute_ai_activity
from typeflux.project import load_project_spec, resolve_project_workflow
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider
from typeflux.yaml import (
    LocalWorkflowError,
    RuntimeCapabilityError,
    build_local_runtime,
    collect_activities,
    create_workflow,
    load_yaml_spec,
    plan_workflow,
    validate_local_capabilities,
)
from typeflux.yaml.planning import evaluate_when_gate, when_gate_from_spec


class Value(BaseModel):
    text: str


class LocalProvider(FakeProvider):
    def __init__(self):
        super().__init__([Value(text="local result")])


class FailingLocalProvider(LocalProvider):
    async def async_structured_call(self, **kwargs):
        raise RuntimeError("offline provider refused")


SPEC = """project: planning_fixture
name: demo
task_queue: existing-temporal-default
runtime:
  engine: temporal
  temporal: {}
  registry: {type: inline, prompts: {echo: 'Echo {{text}}'}}
  provider: {type: fake}
activities:
  definitions:
    - {name: echo, input: 'schemas:Value', output: 'schemas:Value', prompt: echo}
workflow:
  name: Demo
  input: schemas:Value
  output: schemas:Value
  steps:
    - id: echo
      activity: echo
      when: {path: input.text, neq: stop}
"""


def main() -> None:
    module = ModuleType("planning_fixture.schemas")
    module.Value = Value  # type: ignore[attr-defined]
    module.LocalProvider = LocalProvider  # type: ignore[attr-defined]
    module.FailingLocalProvider = FailingLocalProvider  # type: ignore[attr-defined]
    sys.modules[module.__name__] = module
    with tempfile.TemporaryDirectory(prefix="typeflux-planning-probe-") as directory:
        root = Path(directory)
        yaml_path = root / "typeflux.yaml"
        yaml_path.write_text(SPEC)
        spec = load_yaml_spec(yaml_path, load_dotenv=False, env={})
        local_path = root / "local-workflow.yaml"
        local_path.write_text(
            SPEC.replace("task_queue: existing-temporal-default\n", "").replace(
                "  engine: temporal\n", ""
            )
        )
        local = load_yaml_spec(local_path, load_dotenv=False, env={})
        assert validate_local_capabilities(local) == ("demo",)
        local.runtime.provider.type = "custom"
        local.runtime.provider.provider_class = "planning_fixture.schemas:LocalProvider"
        local.runtime.imports.allow_provider_class = True
        runtime = build_local_runtime(local)
        assert asyncio.run(runtime.execute_workflow({"text": "hello"})) == Value(
            text="local result"
        )
        runtime.shutdown()
        local.runtime.provider.provider_class = "planning_fixture.schemas:FailingLocalProvider"
        failing_runtime = build_local_runtime(local)
        try:
            asyncio.run(failing_runtime.execute_workflow({"text": "hello"}))
        except LocalWorkflowError as exc:
            assert isinstance(exc, TypefluxError)
            assert (exc.workflow, exc.step) == ("Demo", "echo")
            assert isinstance(exc.__cause__, RuntimeError)
            assert str(exc.__cause__) == "offline provider refused"
        else:
            raise AssertionError("expected an attributed local execution failure")
        finally:
            failing_runtime.shutdown()
        try:
            create_workflow(local, {})
        except RuntimeCapabilityError as exc:
            assert exc.path == "runtime.engine"
        else:
            raise AssertionError("local selection reached the Temporal generator")
        activities = collect_activities(spec)
        result = execute_ai_activity(
            activity=activities["echo"],
            input_value=Value(text="hello"),
            registry=InlinePromptRegistry({"echo": "Echo {{text}}"}),
            provider=FakeProvider([Value(text="accepted")]),
        )
        assert result == Value(text="accepted")
        plan = plan_workflow(spec, activities)
        assert plan.input_type is Value and plan.output_type is Value
        assert [step.id for step in plan.steps] == ["echo"]
        gate = when_gate_from_spec(plan.steps[0].when)
        assert evaluate_when_gate(gate, {"input": Value(text="go")})
        assert not evaluate_when_gate(gate, {"input": Value(text="stop")})

        (root / "local.yaml").write_text("name: local\nvariables: {}\n")
        project_path = root / "typeflux.project.yaml"
        project_path.write_text(
            "name: demo\nworkflows:\n  - {id: demo, path: typeflux.yaml}\n"
            "environments: {local: local.yaml}\n"
        )
        resolved = resolve_project_workflow(
            load_project_spec(project_path),
            workflow_id="demo",
            environment_id="local",
            base_env={},
        )
        assert plan_workflow(resolved.spec, collect_activities(resolved.spec)).steps == plan.steps
        project_path.write_text(
            project_path.read_text().replace("path: typeflux.yaml", "path: local-workflow.yaml")
        )
        local_resolved = resolve_project_workflow(
            load_project_spec(project_path),
            workflow_id="demo",
            environment_id="local",
            base_env={},
        )
        assert validate_local_capabilities(local_resolved.spec) == ("demo",)

        invalid = spec.model_copy(deep=True)
        invalid.workflow.steps[0].when.path = "future.text"
        try:
            plan_workflow(invalid, activities)
        except ValueError as exc:
            assert "not available" in str(exc)
        else:
            raise AssertionError("planning accepted an unavailable predicate root")

        try:
            create_workflow(spec, activities)
        except RuntimeError as exc:
            assert "typeflux[temporal]" in str(exc)
        else:
            raise AssertionError("Temporal construction should require the integration")
    assert not any(name == "temporalio" or name.startswith("temporalio.") for name in sys.modules)
    print("Python YAML planning and local execution passed without Temporal")


if __name__ == "__main__":
    main()
