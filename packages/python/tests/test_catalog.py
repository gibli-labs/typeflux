from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from typeflux.project import __main__ as project_cli
from typeflux.project import load_project_spec, resolve_activity_catalog


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(sys.modules):
        if name == "catalog_project" or name.startswith("catalog_project."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    package = tmp_path / "catalog_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class ClaimInput(BaseModel):
            value: str


        class Assessment(BaseModel):
            value: str


        class Decision(BaseModel):
            value: str
        """,
    )
    _write(
        package / "activities.py",
        """
        from temporalio import activity

        from catalog_project.schemas import Assessment, Decision


        @activity.defn(name="decide")
        async def decide(value: Assessment) -> Decision:
            return Decision(value=value.value)
        """,
    )
    _write(
        tmp_path / "workflow.yaml",
        """
        project: catalog_project
        name: catalog_demo
        task_queue: catalog-demo-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              assess: assess {{value}}
          provider:
            type: fake
          observability:
            type: none
        activities:
          modules: [activities]
          definitions:
            - name: assess
              input: schemas:ClaimInput
              output: schemas:Assessment
              prompt: assess
              validation_retries: 2
              start_to_close_timeout_seconds: 120
        workflow:
          name: CatalogDemoWorkflow
          input: schemas:ClaimInput
          output: schemas:Decision
          steps:
            - id: assess
              activity: assess
            - id: decide
              activity: decide
        """,
    )
    manifest = tmp_path / "typeflux.project.yaml"
    _write(
        manifest,
        """
        version: "1"
        name: catalog-demo
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: environments/local.yaml
        """,
    )
    _write(
        tmp_path / "environments" / "local.yaml",
        """
        version: "1"
        name: local
        """,
    )
    return manifest


def test_catalog_distinguishes_ai_and_temporal_activities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    catalog = resolve_activity_catalog(project, workflow_id="workflow", environment_id="local")

    assert catalog.project == "catalog-demo"
    assert [entry.name for entry in catalog.activities] == ["assess", "decide"]
    assess, decide = catalog.activities

    assert assess.kind == "ai"
    assert assess.prompt_ref == {"name": "assess", "version": None, "label": None}
    assert assess.validation_retries == 2
    assert assess.start_to_close_timeout_seconds == 120
    assert assess.definition_source["kind"] == "yaml"

    assert decide.kind == "temporal"
    assert decide.prompt_ref is None
    assert decide.validation_retries is None
    assert decide.provider_params == {}
    assert decide.definition_source["kind"] == "python"
    assert decide.definition_source["module"] == "catalog_project.activities"


def test_catalog_exposes_schemas_and_compatibility_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    catalog = resolve_activity_catalog(project, workflow_id="workflow", environment_id="local")
    assess, decide = catalog.activities

    assert assess.input_schema.name == "ClaimInput"
    assert len(assess.input_schema.hash) == 64
    assert assess.input_schema.json_schema["properties"] == {
        "value": {"title": "Value", "type": "string"}
    }
    assert assess.output_schema.name == "Assessment"

    # assess outputs Assessment; decide consumes Assessment.
    assert assess.compatible_next == ("decide",)
    assert decide.compatible_next == ()
    assert assess.used_by_steps == ("assess",)
    assert decide.used_by_steps == ("decide",)


def test_catalog_payload_is_secret_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    catalog = resolve_activity_catalog(project, workflow_id="workflow", environment_id="local")
    payload = json.dumps(catalog.to_dict())

    # No prompt text and no rendered prompt content in the catalog.
    assert "assess {{value}}" not in payload


def test_catalog_cli_prints_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _setup(tmp_path, monkeypatch)

    exit_code = project_cli.main(
        [
            "catalog",
            str(manifest),
            "--workflow",
            "workflow",
            "--environment",
            "local",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["workflow_id"] == "workflow"
    assert {entry["kind"] for entry in payload["activities"]} == {"ai", "temporal"}
