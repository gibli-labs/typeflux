from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from examples.lifecycle_review.main import _enable_langfuse_observability, sample_case
from examples.lifecycle_review.schemas import FinalDecision, ReviewPacket, RiskAssessment
from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec


def test_lifecycle_review_yaml_loads() -> None:
    spec = load_yaml_spec("examples/lifecycle_review/typeflux.yaml")

    assert spec.workflow.lifecycle is not None
    assert spec.workflow.lifecycle.enabled is True
    assert spec.workflow.lifecycle.review is not None
    assert spec.workflow.lifecycle.review.after_step == "package_for_review"


def test_lifecycle_review_sample_case_has_risk_notes() -> None:
    case = sample_case()

    assert case.case_id == "CASE-2026-0101"
    assert case.risk_notes


def test_lifecycle_review_readme_documents_stable_lifecycle_snippets() -> None:
    readme = Path("examples/lifecycle_review/README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())

    assert 'handle.query("typeflux_lifecycle_status")' in readme
    assert 'handle.signal("typeflux_request_cancel", "user requested cancel")' in readme
    assert 'handle.signal("typeflux_submit_review", {"user_decision": "send_email"})' in readme
    assert "main review lifecycle-review-demo send_email" in normalized_readme
    assert "main approve" not in normalized_readme
    assert "main reject" not in normalized_readme
    assert '{"decision"' not in readme
    assert "the routed step and every later YAML step run in order" in normalized_readme
    assert "freeform review notes are not included" in normalized_readme
    assert "identity, review notes, or cancellation reason under `typeflux.*`" in normalized_readme


def test_lifecycle_review_traced_mode_enables_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.delenv("TYPEFLUX_LIFECYCLE_OBSERVABILITY", raising=False)

    _enable_langfuse_observability()

    assert os.environ["TYPEFLUX_LIFECYCLE_OBSERVABILITY"] == "langfuse"


def test_lifecycle_review_traced_mode_requires_langfuse_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="run-traced requires Langfuse credentials"):
        _enable_langfuse_observability()


@pytest.mark.asyncio
async def test_lifecycle_review_workflow_reaches_review_and_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_cls = _workflow()

    import temporalio.workflow

    calls: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if name == "assess_case":
            return RiskAssessment(
                case_id=arg.case_id,
                risk_level="high",
                summary="Risk notes require approval.",
                flags=["manual review"],
            )
        if name == "package_for_review":
            return ReviewPacket(
                case_id=arg.case_id,
                recommendation="approve_after_review",
                summary="Ready for review.",
                approval_required=True,
                flags=arg.flags,
            )
        if name in {"prepare_submission", "route_to_department"}:
            return ReviewPacket(
                case_id=arg.case_id,
                recommendation=arg.recommendation,
                summary=f"{arg.summary} {name}.",
                approval_required=arg.approval_required,
                flags=arg.flags,
            )
        return FinalDecision(
            case_id=arg.case_id,
            decision="approved",
            summary="Approved after review.",
            approved=True,
        )

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    instance = workflow_cls()
    task = asyncio.create_task(instance.run(sample_case()))
    await _wait_for_state(instance, "waiting_for_review")
    instance.typeflux_submit_review({"user_decision": "route_department", "reviewer": "demo"})

    result = await task
    status = instance.typeflux_lifecycle_status()

    assert result.approved is True
    assert calls == [
        "assess_case",
        "package_for_review",
        "route_to_department",
        "send_email",
    ]
    assert status.state == "completed"
    assert status.review_user_decision == "route_department"
    assert status.review_route_target == "route_to_department"
    assert status.completed_units == status.total_units == 4


@pytest.mark.asyncio
async def test_lifecycle_review_workflow_invalid_review_waits_for_valid_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_cls = _workflow()

    import temporalio.workflow

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        if name == "assess_case":
            return RiskAssessment(
                case_id=arg.case_id,
                risk_level="high",
                summary="Risk notes require approval.",
                flags=["manual review"],
            )
        if name == "package_for_review":
            return ReviewPacket(
                case_id=arg.case_id,
                recommendation="approve_after_review",
                summary="Ready for review.",
                approval_required=True,
                flags=arg.flags,
            )
        if name in {"prepare_submission", "route_to_department"}:
            return ReviewPacket(
                case_id=arg.case_id,
                recommendation=arg.recommendation,
                summary=arg.summary,
                approval_required=arg.approval_required,
                flags=arg.flags,
            )
        return FinalDecision(
            case_id=arg.case_id,
            decision="approved",
            summary="Approved after review.",
            approved=True,
        )

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    instance = workflow_cls()
    task = asyncio.create_task(instance.run(sample_case()))
    await _wait_for_state(instance, "waiting_for_review")
    instance.typeflux_submit_review({"decision": "rejected", "notes": "missing evidence"})
    await asyncio.sleep(0.02)

    status = instance.typeflux_lifecycle_status()
    assert status.state == "waiting_for_review"
    assert status.review_user_decision is None
    assert "missing evidence" not in status.model_dump_json()

    instance.typeflux_submit_review({"user_decision": "send_email"})
    result = await task
    status = instance.typeflux_lifecycle_status()
    assert result.approved is True
    assert status.state == "completed"
    assert status.review_user_decision == "send_email"


@pytest.mark.asyncio
async def test_lifecycle_review_workflow_cancellation_skips_later_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_cls = _workflow()

    import temporalio.workflow

    instance = workflow_cls()
    calls: list[str] = []

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.append(name)
        instance.typeflux_request_cancel("cancel during first activity")
        return RiskAssessment(
            case_id=arg.case_id,
            risk_level="high",
            summary="Risk notes require approval.",
            flags=["manual review"],
        )

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)

    with pytest.raises(Exception, match="workflow cancellation requested"):
        await instance.run(sample_case())

    status = instance.typeflux_lifecycle_status()
    assert calls == ["assess_case"]
    assert status.state == "cancelled"
    assert status.cancellation_requested is True


def _workflow():
    spec = load_yaml_spec("examples/lifecycle_review/typeflux.yaml")
    return create_workflow(spec, collect_activities(spec))


async def _wait_for_state(instance: Any, state: str) -> None:
    for _ in range(100):
        if instance.typeflux_lifecycle_status().state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"workflow did not reach {state!r}")
