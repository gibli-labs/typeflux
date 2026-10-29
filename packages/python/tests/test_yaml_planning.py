from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_yaml_and_project_planning_without_temporal() -> None:
    """Simulate an absent integration even in the full development environment."""
    root = Path(__file__).resolve().parents[3]
    code = """
import importlib.abc
import runpy
import sys

class NoTemporal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "temporalio" or fullname.startswith("temporalio."):
            raise ModuleNotFoundError("No module named 'temporalio'", name="temporalio")

sys.meta_path.insert(0, NoTemporal())
runpy.run_path(sys.argv[1], run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root / "scripts/probe_yaml_planning.py")],
        env={**os.environ, "PYTHONPATH": str(root / "packages/python/src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_temporal_generator_preserves_planned_types_and_validation(tmp_path, monkeypatch) -> None:
    from types import ModuleType

    import pytest
    from scripts.probe_yaml_planning import SPEC, Value

    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec, plan_workflow

    module = ModuleType("planning_fixture.schemas")
    module.Value = Value
    monkeypatch.setitem(sys.modules, module.__name__, module)
    path = tmp_path / "typeflux.yaml"
    path.write_text(SPEC)
    spec = load_yaml_spec(path, load_dotenv=False, env={})
    activities = collect_activities(spec)
    plan = plan_workflow(spec, activities)
    workflow = create_workflow(spec, activities)
    assert workflow.__typeflux_input_type__ is plan.input_type
    assert workflow.__typeflux_output_type__ is plan.output_type

    spec.workflow.steps[0].when.path = "future.text"
    for build in (plan_workflow, create_workflow):
        with pytest.raises(ValueError, match="not available"):
            build(spec, activities)
