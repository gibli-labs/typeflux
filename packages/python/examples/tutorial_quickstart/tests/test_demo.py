"""Tutorial quickstart example tests.

The quickstart is the smallest useful spec the tutorial (`docs/tutorial.md` Section 1)
quotes verbatim: one inline-prompt AI activity, an OpenAI provider from the environment,
tracing off. These tests back the tutorial's "runnable, tested file" claim without any
live provider or Temporal call:

* the spec strict-loads into the real models with the documented topology, and
* the graph builds (`create_workflow`), and
* the shipped inline prompt renders and returns a schema-validated `Triage` when driven by
  a `FakeProvider` (offline end-to-end).
"""

from __future__ import annotations

import pytest

from examples.tutorial_quickstart.schemas import TicketInput, Triage
from typeflux.execution.executor import execute_ai_activity
from typeflux.testing import FakeProvider
from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec
from typeflux.yaml.runtime import _build_registry

YAML_PATH = "examples/tutorial_quickstart/typeflux.yaml"


def test_tutorial_quickstart_yaml_loads_with_documented_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HERMETIC load (Bugbot): the spec interpolates `${TEMPORAL_TASK_QUEUE:-…}` /
    # `${TYPEFLUX_OPENAI_MODEL:-…}`-style defaults, so a developer's shell/.env values
    # would replace the YAML defaults this test asserts. Clear the interpolated names
    # and skip dotenv so the assertions pin the DOCUMENTED defaults, not local state.
    for name in (
        "TEMPORAL_TASK_QUEUE",
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "TEMPORAL_TLS",
        "TYPEFLUX_OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    spec = load_yaml_spec(YAML_PATH, load_dotenv=False)

    # Identity + task queue (the sibling `-typeflux` suffix convention).
    assert spec.name == "tutorial_quickstart"
    assert spec.task_queue == "tutorial-quickstart-typeflux"

    # Runtime: inline prompt registry, OpenAI provider, tracing off — exactly what the
    # tutorial's Section 1 spec documents.
    assert spec.runtime.registry.type == "inline"
    assert spec.runtime.provider.type == "openai"
    assert spec.runtime.provider.model == "gpt-4o-mini"
    assert spec.runtime.observability.type == "none"

    # One typed AI activity, its IO schemas, and a single-step sequential workflow.
    definition = spec.activities.definitions[0]
    assert definition.name == "triage_ticket"
    assert definition.input == "schemas:TicketInput"
    assert definition.output == "schemas:Triage"
    assert definition.validation_retries == 1
    assert spec.workflow.name == "TutorialQuickstartWorkflow"
    assert spec.workflow.input == "schemas:TicketInput"
    assert spec.workflow.output == "schemas:Triage"
    assert [(step.id, step.activity) for step in spec.workflow.steps] == [
        ("triage", "triage_ticket")
    ]


def test_tutorial_quickstart_graph_builds() -> None:
    spec = load_yaml_spec(YAML_PATH)
    activities = collect_activities(spec)

    assert set(activities) == {"triage_ticket"}
    workflow_cls = create_workflow(spec, activities)

    # The generated class name carries the spec digest (identity/topology), so the graph
    # actually assembled from the documented spec.
    assert workflow_cls.__name__.startswith(
        "examples_tutorial_quickstart_tutorial_quickstart_TutorialQuickstartWorkflow_"
    )


def test_tutorial_quickstart_runs_offline_with_fake_provider() -> None:
    """The shipped inline prompt renders and the output is validated against `Triage` —
    proven end to end with a `FakeProvider`, no live model call."""
    spec = load_yaml_spec(YAML_PATH)
    activity = collect_activities(spec)["triage_ticket"]
    registry = _build_registry(spec)
    provider = FakeProvider(
        [
            Triage(
                category="billing",
                urgency="high",
                summary="Customer was double charged and wants a refund.",
            )
        ]
    )

    result = execute_ai_activity(
        activity=activity,
        input_value=TicketInput(
            subject="Double charged for my subscription",
            body="I was billed twice — please refund.",
        ),
        registry=registry,
        provider=provider,
    )

    assert isinstance(result, Triage)
    assert result.category == "billing"
    assert len(provider.calls) == 1
    # The inline prompt template interpolated the ticket fields.
    rendered = provider.calls[0]["messages"]
    rendered_text = " ".join(message.content for message in rendered)
    assert "Double charged for my subscription" in rendered_text
    assert "I was billed twice" in rendered_text
