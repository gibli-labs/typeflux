"""Exercise the installed Python planning surface without Temporal or network access."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel

from typeflux.execution import execute_ai_activity
from typeflux.project import load_project_spec, resolve_project_workflow
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider
from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec, plan_workflow
from typeflux.yaml.planning import evaluate_when_gate, when_gate_from_spec


class Value(BaseModel):
    text: str


SPEC = """project: planning_fixture
name: demo
task_queue: existing-temporal-default
runtime:
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
    sys.modules[module.__name__] = module
    with tempfile.TemporaryDirectory(prefix="typeflux-planning-probe-") as directory:
        root = Path(directory)
        yaml_path = root / "typeflux.yaml"
        yaml_path.write_text(SPEC)
        spec = load_yaml_spec(yaml_path, load_dotenv=False, env={})
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
            load_project_spec(project_path), workflow_id="demo", environment_id="local", base_env={}
        )
        assert plan_workflow(resolved.spec, collect_activities(resolved.spec)).steps == plan.steps

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
    print("Python YAML and project planning passed without Temporal")


if __name__ == "__main__":
    main()
