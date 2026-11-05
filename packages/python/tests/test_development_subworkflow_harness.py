"""Required subworkflow proofs must fail setup errors, never find an alternate server."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "controlled_subworkflow_tests", ROOT / "packages/python/tests/test_yaml_subworkflows.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "test_parent_runs_child_plain_and_map_with_ids_and_memo",
        "test_still_running_child_id_collision_fails_the_parent_loudly",
    ],
)
@pytest.mark.parametrize("error_type", [RuntimeError, OSError, TypeError])
async def test_required_subworkflow_setup_error_cannot_skip_or_use_another_server(
    harness, tmp_path, monkeypatch, target, error_type
):
    error = error_type("controlled connection failure")
    connect = AsyncMock(side_effect=error)
    alternate = AsyncMock(side_effect=error)
    monkeypatch.setattr(Client, "connect", connect)
    monkeypatch.setattr(WorkflowEnvironment, "start_time_skipping", alternate)
    monkeypatch.setenv("TYPEFLUX_LIVE_TEMPORAL_ADDRESS", "unowned.invalid:7233")
    with pytest.raises(error_type) as caught:
        await getattr(harness, target)(tmp_path, monkeypatch)
    assert caught.value is error
    connect.assert_awaited_once()
    assert connect.call_args.args == ("127.0.0.1:7233",)
    alternate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "already-exists"])
async def test_required_search_attribute_registration_accepts_only_success_or_already_exists(
    harness, outcome
):
    register = AsyncMock()
    if outcome == "already-exists":
        register.side_effect = RPCError("opaque diagnostic", RPCStatusCode.ALREADY_EXISTS, b"")
    client = SimpleNamespace(
        namespace="default", operator_service=SimpleNamespace(add_search_attributes=register)
    )
    await harness._ensure_workflow_search_attribute_registered(client)
    register.assert_awaited_once()
    request = register.call_args.args[0]
    assert request.namespace == "default" and set(request.search_attributes) == {"TypefluxWorkflow"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        RPCStatusCode.PERMISSION_DENIED,
        RPCStatusCode.UNIMPLEMENTED,
        RPCStatusCode.INVALID_ARGUMENT,
        None,
    ],
)
async def test_required_search_attribute_registration_preserves_real_errors(harness, status):
    # These message substrings used to make even unrelated errors look benign.
    message = "already unsupported permission denied"
    error = RuntimeError(message) if status is None else RPCError(message, status, b"")
    register = AsyncMock(side_effect=error)
    client = SimpleNamespace(
        namespace="default", operator_service=SimpleNamespace(add_search_attributes=register)
    )
    with pytest.raises(type(error)) as caught:
        await harness._ensure_workflow_search_attribute_registered(client)
    assert caught.value is error
    register.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_search_attribute_registration_keeps_existing_deadline(harness, monkeypatch):
    error = TimeoutError("controlled RPC deadline")
    register = AsyncMock()

    async def expire(awaitable, *, timeout):
        awaitable.close()
        assert timeout == 15.0
        raise error

    monkeypatch.setattr(harness.asyncio, "wait_for", expire)
    client = SimpleNamespace(
        namespace="default", operator_service=SimpleNamespace(add_search_attributes=register)
    )
    with pytest.raises(TimeoutError) as caught:
        await harness._ensure_workflow_search_attribute_registered(client)
    assert caught.value is error
