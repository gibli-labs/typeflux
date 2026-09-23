"""Golden-lock tests for the frozen cross-SDK contract shapes (#390).

Freezes the serialized shapes of the prompt-ref, the activity- and
workflow-execution manifests, and the trace record. Goldens are generated from
the Python baseline (the locked conformance source) and live under
``contracts/``; the TypeScript SDK (#387) must reproduce them and the
conformance runner (#392) consumes them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from typeflux.core.contracts import (
    AIActivity,
    ChatMessage,
    PromptRef,
    ProviderParams,
    ResolvedPrompt,
)
from typeflux.manifests import (
    build_activity_execution_manifest,
    build_activity_manifest,
    build_activity_rollup_entry,
    build_workflow_execution_manifest,
)
from typeflux.manifests.hashing import schema_hash
from typeflux.manifests.provenance import CodeProvenance
from typeflux.observability.inspect import (
    ObservationRecord,
    TraceRecord,
    TraceRetrievalInfo,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


class _Input(BaseModel):
    text: str


class _Output(BaseModel):
    label: str
    score: float | None = None


# Pin __module__ so the schema identity captured in the manifest goldens
# (schema_identity reads model.__module__) is stable no matter how this module
# is imported — pytest uses the basename "test_contracts_shapes" while a direct
# import uses "tests.test_contracts_shapes". The schema *hash* is over
# model_json_schema() and is already module-independent.
_Input.__module__ = "typeflux_temporal_contracts_golden"
_Output.__module__ = "typeflux_temporal_contracts_golden"


# Fixed code provenance so the workflow-manifest golden is deterministic. Real
# provenance (git SHA, package version) is environment data, not part of the
# frozen contract; the goldens pin placeholder values.
_PROVENANCE = CodeProvenance(
    available=True,
    source="golden",
    git_ref="refs/heads/main",
    git_sha="0" * 40,
    dirty=False,
    deployment_id="golden-deployment",
    environment="golden",
    package_version="0.0.0-golden",
)


def _norm(value: object) -> object:
    """JSON round-trip so tuples/sets normalize to their on-the-wire form."""
    return json.loads(json.dumps(value))


def build_goldens() -> dict[str, object]:
    """Construct every frozen contract shape from the Python baseline.

    Returns ``{relative_golden_path: json-normalized payload}``. Shared by the
    golden-lock tests and the regeneration step so the two never drift.
    """

    # prompt_type is a resolve-time hint, not serialized (to_dict emits only
    # name/version/label), so it does not appear in the goldens.
    prompt_ref = PromptRef("support/classify", label="production")
    resolved = ResolvedPrompt(
        ref=prompt_ref,
        messages=(ChatMessage(role="user", content="Classify: {{ text }}"),),
        resolved_version="commit-abc123",
        model="fake-model",
    )
    activity = AIActivity(
        name="classify_ticket",
        input_type=_Input,
        output_type=_Output,
        prompt_ref=prompt_ref,
    )

    activity_manifest = build_activity_manifest(activity, resolved)
    execution = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=activity_manifest,
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify: hello"),),
        validation_attempt=0,
    )
    workflow = build_workflow_execution_manifest(
        workflow_name="SupportTriageWorkflow",
        workflow_id="wf-golden",
        task_queue="support-ai",
        activities=[
            build_activity_rollup_entry(
                activity=activity,
                resolved_prompt=resolved,
                provider_params=ProviderParams(model="fake-model", max_tokens=4096),
            )
        ],
        code_provenance=_PROVENANCE,
        sdk_version="0.0.0-golden",
    )

    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    trace = TraceRecord(
        trace_id="trace-golden",
        name="SupportTriageWorkflow",
        timestamp=start,
        metadata={"workflow_name": "SupportTriageWorkflow", "environment": "golden"},
        observations=(
            ObservationRecord(
                observation_id="obs-1",
                name="classify_ticket",
                type="GENERATION",
                level="DEFAULT",
                start_time=start,
                end_time=end,
                metadata={"activity_name": "classify_ticket"},
            ),
        ),
        retrieval=TraceRetrievalInfo(
            backend="langfuse",
            complete=True,
            pages_read=1,
            observations_read=1,
            page_size=50,
            max_pages=10,
        ),
    )

    return {
        "prompt-ref/golden/prompt_ref.json": _norm(prompt_ref.to_dict()),
        "manifest/golden/activity_execution.json": _norm(execution.to_dict()),
        "manifest/golden/workflow_execution.json": _norm(workflow.to_dict()),
        "trace/golden/trace_record.json": _norm(trace.model_dump(mode="json", exclude_none=True)),
    }


_GOLDENS = build_goldens()


def test_contract_version_is_pinned() -> None:
    assert (CONTRACTS / "CONTRACT_VERSION").read_text(encoding="utf-8").strip() == "1"


def test_execution_manifests_carry_manifest_version() -> None:
    assert _GOLDENS["manifest/golden/activity_execution.json"]["manifest_version"] == "1"  # type: ignore[index]
    assert _GOLDENS["manifest/golden/workflow_execution.json"]["manifest_version"] == "1"  # type: ignore[index]


@pytest.mark.parametrize("rel", sorted(_GOLDENS))
def test_shape_matches_golden(rel: str) -> None:
    expected = json.loads((CONTRACTS / rel).read_text(encoding="utf-8"))
    assert _GOLDENS[rel] == expected


def test_manifest_schema_fixtures_match_models_and_leaf_hashes() -> None:
    # The committed raw-schema fixtures (the `schema_hash` INPUT) match the
    # models; the TS SDK hashes them to reproduce the manifest goldens' leaf
    # `schema.hash` values.
    input_fixture = json.loads(
        (CONTRACTS / "manifest/golden/input_schema.json").read_text(encoding="utf-8")
    )
    output_fixture = json.loads(
        (CONTRACTS / "manifest/golden/output_schema.json").read_text(encoding="utf-8")
    )
    assert _Input.model_json_schema() == input_fixture
    assert _Output.model_json_schema() == output_fixture

    activity = json.loads(
        (CONTRACTS / "manifest/golden/activity_execution.json").read_text(encoding="utf-8")
    )
    assert schema_hash(_Input) == activity["input_schema"]["hash"]
    assert schema_hash(_Output) == activity["output_schema"]["hash"]
