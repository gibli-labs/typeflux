"""Direct unit tests for typeflux.project.prompt_status."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_minimal_project
from typeflux.project import load_project_spec, workflow_prompt_status
from typeflux.project import prompt_status as prompt_status_module

_CUSTOM_REGISTRY_BLOCK = """\
            type: custom
            class: prompt_status_unit_project.registry:StubRegistry"""

_LANGSMITH_REGISTRY_BLOCK = """\
            type: langsmith"""

_LANGFUSE_REGISTRY_BLOCK = """\
            type: langfuse"""

# One labeled ref and one version-pinned ref, so status covers both modes.
_ACTIVITIES_BLOCK = """\
            - name: labeled
              input: schemas:InputModel
              output: schemas:OutputModel
              prompt: triage
            - name: pinned
              input: schemas:InputModel
              output: schemas:OutputModel
              prompt:
                name: summarize
                version: 3"""

_STEPS_BLOCK = """\
            - id: labeled
              activity: labeled
            - id: pinned
              activity: pinned"""


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry_block: str,
) -> Path:
    return make_minimal_project(
        tmp_path,
        monkeypatch,
        "prompt_status_unit_project",
        registry_block=registry_block,
        activities_block=_ACTIVITIES_BLOCK,
        steps_block=_STEPS_BLOCK,
        package_modules={
            "registry.py": """
            class StubRegistry:
                pass
            """,
        },
    )


@pytest.mark.parametrize(
    ("registry_block", "registry_type"),
    [
        (_CUSTOM_REGISTRY_BLOCK, "custom"),
        (_LANGSMITH_REGISTRY_BLOCK, "langsmith"),
    ],
)
def test_runtime_resolved_registries_report_honest_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry_block: str,
    registry_type: str,
) -> None:
    manifest = _setup(tmp_path, monkeypatch, registry_block=registry_block)
    project = load_project_spec(manifest)

    status = workflow_prompt_status(project, workflow_id="workflow", environment_id="local")

    assert status.registry_type == registry_type
    by_name = {entry.name: entry for entry in status.prompts}
    # A bare name ref has no label to display; drift is honestly unknowable.
    assert by_name["triage"].mode == "label"
    assert by_name["triage"].selector == "—"
    assert by_name["triage"].status == "unknown"
    assert by_name["triage"].used_by_activities == ("labeled",)
    assert by_name["summarize"].mode == "pinned"
    assert by_name["summarize"].selector == "v3"
    assert by_name["summarize"].status == "unknown"
    # Runtime-resolved registries expose no template text.
    assert all(entry.template is None for entry in status.prompts)


def test_langfuse_default_label_and_observer_none_skips_last_run_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch, registry_block=_LANGFUSE_REGISTRY_BLOCK)
    project = load_project_spec(manifest)

    lookups: list[tuple[str, str, str | None]] = []

    def fake_lookup(name: str, label: str, *, host: str | None = None):
        lookups.append((name, label, host))
        return (4, None)

    def _must_not_query(logical: str) -> dict[str, str]:
        raise AssertionError("observer none has no last-run side to query")

    monkeypatch.setattr(prompt_status_module, "_registry_label_version", fake_lookup)
    monkeypatch.setattr(prompt_status_module, "_last_run_versions", _must_not_query)

    status = workflow_prompt_status(project, workflow_id="workflow", environment_id="local")

    # A ref without a label resolves through the registry default "production".
    assert lookups == [("triage", "production", None)]
    by_name = {entry.name: entry for entry in status.prompts}
    assert by_name["triage"].selector == "@production"
    assert by_name["triage"].registry_version == "4"
    assert by_name["triage"].last_run_version is None
    # One side unknown is reported unknown — never a false in-sync.
    assert by_name["triage"].status == "unknown"
    # Version pins are in sync by construction and need no registry lookup.
    assert by_name["summarize"].status == "in_sync"
    assert by_name["summarize"].registry_version == "3"


def test_registry_label_version_degrades_to_unknown_with_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    langfuse = pytest.importorskip("langfuse")

    def _boom(*args, **kwargs):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(langfuse, "Langfuse", _boom)

    version, detail = prompt_status_module._registry_label_version("triage", "production")

    assert version is None
    assert detail is not None
    assert detail.startswith("registry lookup failed:")
    assert "401 unauthorized" in detail


def test_last_run_versions_degrade_to_empty_when_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    def _boom(*args, **kwargs):
        raise RuntimeError("missing credentials")

    monkeypatch.setattr(LangfuseObservabilityBackend, "from_env", _boom)

    assert prompt_status_module._last_run_versions("PromptStatusDemoWorkflow") == {}


def test_inline_structured_template_renders_as_yaml_not_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _render_template's documented contract (#648): strings pass through; structured (chat)
    # prompt specs render as YAML, never a pydantic model repr on the wire.
    registry_block = """\
            type: inline
            prompts:
              triage: triage {{value}}
              summarize:
                messages:
                  - role: system
                    content: be brief
                  - role: user
                    content: "{{value}}"
"""
    manifest = _setup(tmp_path, monkeypatch, registry_block=registry_block)
    project = load_project_spec(manifest)
    status = workflow_prompt_status(project, workflow_id="workflow", environment_id="local")
    by_name = {entry.name: entry for entry in status.prompts}
    assert by_name["triage"].template == "triage {{value}}"
    structured = by_name["summarize"].template
    assert structured is not None
    assert "role: system" in structured
    assert "be brief" in structured
    # Never a model repr.
    assert "InlinePrompt" not in structured and "messages=[" not in structured
