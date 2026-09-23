"""Direct unit tests for typeflux.project.definitions."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from typeflux.project import load_project_spec
from typeflux.project.definitions import (
    environment_definition,
    policy_definition,
    policy_definitions,
    profile_definitions,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


def _setup(tmp_path: Path) -> Path:
    _write(tmp_path / "workflow.yaml", "placeholder: true\n")
    _write(
        tmp_path / "env.yaml",
        """
        version: "1"
        name: local
        env_files:
          - path: secrets/.env
            required: false
        variables:
          ZETA_VAR: sensitive-z-value
          ALPHA_VAR: sensitive-a-value
        workflows:
          workflow:
            profiles:
              provider: prod
        """,
    )
    _write(
        tmp_path / "policies" / "base.yaml",
        """
        version: "1"
        name: base
        description: Base guardrails
        providers:
          allowed:
            fake: {}
        """,
    )
    _write(
        tmp_path / "policies" / "strict.yaml",
        """
        version: "1"
        name: strict
        description: Stricter guardrails
        extends: [base]
        observability:
          allowed_backends: [none]
        """,
    )
    _write(
        tmp_path / "policies" / "cyclic.yaml",
        """
        version: "1"
        name: cyclic
        extends: [cyclic]
        """,
    )
    manifest = tmp_path / "typeflux.project.yaml"
    _write(
        manifest,
        """
        version: "1"
        name: definitions-demo
        workflows:
          - id: workflow
            path: workflow.yaml
          - id: other
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          strict: policies/strict.yaml
          base: policies/base.yaml
          cyclic: policies/cyclic.yaml
        validation:
          targets:
            local:
              workflows: [workflow, other]
              environment: local
              policies: [strict]
        """,
    )
    return manifest


def test_environment_definition_serializes_names_only_with_reverse_index(
    tmp_path: Path,
) -> None:
    project = load_project_spec(_setup(tmp_path))

    definition = environment_definition(project, "local")

    assert definition.id == "local"
    assert definition.name == "local"
    # Sorted names only — variable values must never serialize.
    assert definition.variable_names == ("ALPHA_VAR", "ZETA_VAR")
    payload = json.dumps(definition.to_dict())
    assert "sensitive-z-value" not in payload
    assert "sensitive-a-value" not in payload
    assert definition.env_files == ({"path": "secrets/.env", "required": False},)
    assert definition.workflow_profiles == {"workflow": {"provider": "prod"}}
    # used_by joins the environment's own workflow entries with every
    # validation target pinned to this environment.
    assert definition.used_by == ("other", "workflow")


def test_policy_definitions_list_sorted_summaries(tmp_path: Path) -> None:
    project = load_project_spec(_setup(tmp_path))

    summaries = policy_definitions(project)

    assert [entry.id for entry in summaries] == ["base", "cyclic", "strict"]
    by_id = {entry.id: entry for entry in summaries}
    assert by_id["strict"].name == "strict"
    assert by_id["strict"].description == "Stricter guardrails"
    assert by_id["strict"].path == "policies/strict.yaml"
    assert by_id["cyclic"].description is None


def test_policy_definition_carries_rules_hash_and_used_by(tmp_path: Path) -> None:
    project = load_project_spec(_setup(tmp_path))

    definition = policy_definition(project, "strict")

    assert definition.extends == ("base",)
    assert definition.policy_hash is not None
    assert len(definition.policy_hash) == 64
    # Full rules render (policy rules are config), header fields excluded.
    assert definition.rules["observability"]["allowed_backends"] == ["none"]
    for header in ("version", "name", "description", "extends"):
        assert header not in definition.rules
    assert definition.used_by == ("other", "workflow")

    # A policy no validation target selects has an empty reverse index.
    assert policy_definition(project, "base").used_by == ()


def test_policy_definition_degrades_hash_when_composition_fails(tmp_path: Path) -> None:
    project = load_project_spec(_setup(tmp_path))

    # The self-extending policy loads fine but cannot compose; the definition
    # stays inspectable with the hash reported as unknown.
    definition = policy_definition(project, "cyclic")

    assert definition.id == "cyclic"
    assert definition.policy_hash is None
    assert definition.extends == ("cyclic",)


def test_profile_definitions_empty_when_project_declares_none(tmp_path: Path) -> None:
    project = load_project_spec(_setup(tmp_path))

    assert project.profiles is None
    assert profile_definitions(project) == ()


def test_environment_definition_unknown_id_raises(tmp_path: Path) -> None:
    project = load_project_spec(_setup(tmp_path))

    with pytest.raises(Exception, match="staging"):
        environment_definition(project, "staging")
