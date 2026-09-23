from __future__ import annotations

import pytest

from examples.support_triage_langfuse import domain
from examples.support_triage_langfuse import main as example_main
from examples.support_triage_langfuse.activities import ALL_ACTIVITIES
from examples.support_triage_langfuse.fakes import FakeProvider
from examples.support_triage_langfuse.main import (
    PROMPT_DIR,
    PROMPTS,
    inline_registry,
    sample_ticket,
)
from examples.support_triage_langfuse.pipeline import PIPELINE_NAME, run
from examples.support_triage_langfuse.schemas import ReviewPacket
from examples.support_triage_langfuse.workflow import SupportTriageLangfuseWorkflow


def test_all_prompt_files_exist() -> None:
    for filename in PROMPTS:
        assert (PROMPT_DIR / filename).is_file()


def test_prompts_contain_expected_mustache_placeholders() -> None:
    expected = {
        "classify.txt": ["{{subject}}", "{{body}}", "{{received_at}}"],
        "route.txt": ["{{category}}", "{{urgency}}", "{{topics}}"],
        "draft.txt": ["{{team}}", "{{sla_hours}}", "{{escalated}}"],
        "package.txt": ["{{subject}}", "{{body}}", "{{tone}}"],
    }
    for filename, placeholders in expected.items():
        body = (PROMPT_DIR / filename).read_text(encoding="utf-8")
        for placeholder in placeholders:
            assert placeholder in body


def test_offline_pipeline_end_to_end_returns_review_packet() -> None:
    result = run(sample_ticket(), registry=inline_registry(), provider=FakeProvider())

    assert isinstance(result, ReviewPacket)
    assert result.approval_required is True


@pytest.mark.asyncio
async def test_live_example_uses_typeflux_workflow_execution_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    langfuse_client = object()

    async def fake_execute_typeflux_workflow(**kwargs):
        captured.update(kwargs)
        return ReviewPacket(verdict="approve", summary="ok")

    monkeypatch.setattr(
        example_main,
        "execute_typeflux_workflow",
        fake_execute_typeflux_workflow,
    )

    result = await example_main._execute_support_triage_workflow(
        object(),
        workflow_id="wf-example",
        task_queue="tq-example",
        langfuse_client=langfuse_client,
    )

    assert result == ReviewPacket(verdict="approve", summary="ok")
    assert captured["workflow"] is SupportTriageLangfuseWorkflow.run
    assert captured["id"] == "wf-example"
    assert captured["task_queue"] == "tq-example"
    assert captured["result_type"] is ReviewPacket
    assert captured["langfuse_client"] is langfuse_client
    assert captured["workflow_name"] == "SupportTriageYamlWorkflow"
    assert captured["activities"] == ALL_ACTIVITIES
    assert set(captured["tags"]) >= {"typeflux", "support-triage", "temporal"}
    assert captured["metadata"] == {
        "typeflux.pipeline": PIPELINE_NAME,
        "typeflux.activities": ",".join(activity.name for activity in ALL_ACTIVITIES),
        "temporal.workflow_id": "wf-example",
        "temporal.task_queue": "tq-example",
    }


def test_example_redact_pii_masks_luhn_valid_cards() -> None:
    redacted, count = domain.redact_pii("card 4242 4242 4242 4242 backup 4111-1111-1111-1111")

    assert redacted == "card [CARD] backup [CARD]"
    assert count == 2


def test_example_redact_pii_does_not_mask_non_luhn_numeric_ids() -> None:
    redacted, count = domain.redact_pii("order O-1234567890123 account 123456789012345")

    assert redacted == "order O-1234567890123 account 123456789012345"
    assert count == 0
