"""Direct unit tests for the control-plane API's request/response contracts.

tests/test_controlplane_api.py drives the lifecycle endpoints end to end; this
covers the payload models themselves (round trips, strictness, capability
projection) and the read-route serialization glue (limit clamping,
exclude_none rendering) through the TestClient fake transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import make_minimal_project
from typeflux.controlplane import create_app
from typeflux.controlplane.api import (
    ApiCancelRequest,
    ApiCapabilities,
    ApiError,
    ApiMeta,
    ApiReviewRequest,
    ApiStartRequest,
)
from typeflux.controlplane.auth import Actor, Permission
from typeflux.core.contracts import ReviewCommand
from typeflux.project.connections import (
    ConnectionStatus,
    ObserverStatus,
    WorkflowConnections,
)
from typeflux.project.runs import WorkflowExecutionList, WorkflowExecutionRecord


def test_start_request_round_trips_a_realistic_payload() -> None:
    payload = {
        "environment_id": "local",
        "execution_id": "case-2026-0042",
        "input": {"claims": [{"value": "windshield"}, {"value": "bumper"}]},
        "policy_ids": ["regulated"],
        "expected_policy_hash": "a" * 64,
    }

    request = ApiStartRequest.model_validate(payload)

    # The input payload passes through untouched — coercion against the
    # workflow's input model happens at dispatch, not at request parsing.
    assert request.input == payload["input"]
    assert request.task_queue is None
    assert request.policy_ids == ("regulated",)
    assert ApiStartRequest.model_validate(request.model_dump(mode="json")) == request


def test_review_request_parses_the_review_command() -> None:
    request = ApiReviewRequest.model_validate(
        {
            "environment_id": "local",
            "execution_id": "case-1",
            "command": {"user_decision": "approve", "reviewer": "ops@acme", "notes": "lgtm"},
        }
    )

    assert request.command == ReviewCommand(
        user_decision="approve", reviewer="ops@acme", notes="lgtm"
    )
    assert request.run_id is None
    assert ApiReviewRequest.model_validate(request.model_dump(mode="json")) == request


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ApiStartRequest, {"environment_id": "local", "execution_id": "e", "input": {}}),
        (
            ApiReviewRequest,
            {
                "environment_id": "local",
                "execution_id": "e",
                "command": {"user_decision": "approve"},
            },
        ),
        (ApiCancelRequest, {"environment_id": "local", "execution_id": "e"}),
    ],
)
def test_operation_requests_reject_unknown_keys_and_are_frozen(model, payload) -> None:
    request = model.model_validate(payload)

    with pytest.raises(ValidationError):
        model.model_validate({**payload, "unexpected": "field"})
    with pytest.raises(ValidationError):
        request.environment_id = "other"


def test_capabilities_project_exactly_the_actor_grants() -> None:
    reviewer = ApiCapabilities.for_actor(
        Actor(id="reviewer", permissions=frozenset({Permission.REVIEW}))
    )
    assert reviewer.model_dump() == {
        "can_start": False,
        "can_review": True,
        "can_cancel": False,
        "can_refresh_project": False,
        "can_resolve": True,
        "enforcement_events": True,
        # github_provenance additionally needs a recorded github repo source (default
        # False here), so a resolvable project without one still reports it False.
        "github_provenance": False,
    }

    nobody = ApiCapabilities.for_actor(Actor(id=None, permissions=frozenset()))
    # can_resolve and enforcement_events are server capabilities, not grants (both
    # follow `resolvable`, which defaults True here); an actor with no permissions
    # still sees them true because they are not permission-gated. github_provenance
    # stays False without a recorded github repo source (github_repo_present default).
    assert nobody.model_dump() == {
        "can_start": False,
        "can_review": False,
        "can_cancel": False,
        "can_refresh_project": False,
        "can_resolve": True,
        "enforcement_events": True,
        "github_provenance": False,
    }

    # With a github repo source present (and resolvable), every server/permission flag
    # is true for the all-permissions operator.
    operator = ApiCapabilities.for_actor(
        Actor(id="op", permissions=frozenset(Permission)), github_repo_present=True
    )
    assert all(operator.model_dump().values())
    # Without the repo source, only github_provenance drops (it needs the surface too).
    operator_no_repo = ApiCapabilities.for_actor(Actor(id="op", permissions=frozenset(Permission)))
    assert operator_no_repo.model_dump()["github_provenance"] is False
    assert operator_no_repo.model_dump()["enforcement_events"] is True

    # Starting needs resolution; review/cancel need only a binding driver
    # (#618/#619). An unresolvable-but-operable project (ts-plan-argument)
    # reviews and cancels honestly; registry refresh stays grant-only.
    plan_less = ApiCapabilities.for_actor(
        Actor(id="op", permissions=frozenset(Permission)), resolvable=False, operable=True
    )
    assert plan_less.model_dump() == {
        "can_start": False,
        "can_review": True,
        "can_cancel": True,
        "can_refresh_project": True,
        "can_resolve": False,
        # Enforcement events are resolution-bound (admission verdicts need the resolved
        # report), so an unresolvable project reports False — the endpoint would 501.
        "enforcement_events": False,
        # github_provenance is resolution-bound too, so it is False for an unresolvable
        # project even if a github repo source were present.
        "github_provenance": False,
    }

    grounded = ApiCapabilities.for_actor(
        Actor(id="op", permissions=frozenset(Permission)), resolvable=False, operable=False
    )
    assert grounded.model_dump() == {
        "can_start": False,
        "can_review": False,
        "can_cancel": False,
        "can_refresh_project": True,
        "can_resolve": False,
        "enforcement_events": False,
        "github_provenance": False,
    }


def test_error_and_meta_models_enforce_their_contracts() -> None:
    error = ApiError(error="NotFound", message="unknown project workflow: nope")
    assert ApiError.model_validate(error.model_dump()) == error

    with pytest.raises(ValidationError):
        ApiMeta.model_validate(
            {
                "api_version": "2",  # only version "1" exists
                "bundle_version": "1",
                "catalog_version": "1",
                "project": "demo",
                "manifest_path": "typeflux.project.yaml",
                "capabilities": ApiCapabilities.for_actor(
                    Actor(id=None, permissions=frozenset())
                ).model_dump(),
            }
        )


def test_executions_endpoint_clamps_the_limit_and_serializes_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(tmp_path, monkeypatch, "api_models_project")
    listing = WorkflowExecutionList(
        logical_workflow="ApiModelsDemoWorkflow",
        current_workflow_type="ApiModelsDemoWorkflow.vX",
        executions=(
            WorkflowExecutionRecord(
                execution_id="case-1",
                run_id=None,
                workflow_type="ApiModelsDemoWorkflow.vX",
                current_version=True,
                status="RUNNING",
            ),
        ),
    )
    seen_limits: list[int] = []

    async def fake_executions(project, *, workflow_id, environment_id, limit):
        seen_limits.append(limit)
        return listing

    monkeypatch.setattr("typeflux.controlplane.api.workflow_executions", fake_executions)
    client = TestClient(create_app(manifest))

    response = client.get(
        "/api/v1/workflows/workflow/executions",
        params={"environment_id": "local", "limit": 100000},
    )
    assert response.status_code == 200
    assert response.json() == listing.to_dict()

    client.get(
        "/api/v1/workflows/workflow/executions", params={"environment_id": "local", "limit": 0}
    )
    client.get("/api/v1/workflows/workflow/executions", params={"environment_id": "local"})

    # Requested 100000/0/default → served 100/1/20.
    assert seen_limits == [100, 1, 20]

    unknown = client.get("/api/v1/workflows/missing/executions", params={"environment_id": "local"})
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "NotFound"


def test_connections_endpoint_omits_none_fields_from_the_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(tmp_path, monkeypatch, "api_models_project")
    status = WorkflowConnections(
        workflow_id="workflow",
        environment_id="local",
        registry=ConnectionStatus(type="inline", host=None, reachable=True, detail=None),
        observability=ObserverStatus(
            type="none",
            host=None,
            reachable=True,
            detail=None,
            execution_manifest=False,
            redaction_enabled=False,
        ),
    )

    def fake_connections(project, *, workflow_id, environment_id):
        return status

    monkeypatch.setattr("typeflux.controlplane.api.workflow_connections", fake_connections)
    client = TestClient(create_app(manifest))

    response = client.get(
        "/api/v1/workflows/workflow/connections", params={"environment_id": "local"}
    )

    assert response.status_code == 200
    body = response.json()
    # response_model_exclude_none drops null host/detail entirely.
    assert body["registry"] == {"type": "inline", "reachable": True}
    assert "detail" not in body["observability"]
    assert body["observability"]["execution_manifest"] is False
