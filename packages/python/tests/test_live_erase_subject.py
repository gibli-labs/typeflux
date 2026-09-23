"""LIVE proof (#715 slice 5): the erase seam end-to-end on a real Temporal dev server.

Drives ``erase_subject`` against a real execution started through the wired
subject-scope stack (the same ``build_runtime`` route as the slice-4 shred proof):

1. a workflow started WITH a subject id completes; a DRY-RUN erase reports the plan —
   the live key record (``would_shred``) and the closed subject-dedicated execution
   (``deletable``) — while provably mutating NOTHING (the key stays live, the
   execution's history stays on the server);
2. an EXECUTE erase destroys the key record AND issues ``DeleteWorkflowExecution``;
   the receipt reports both performed outcomes, the keystore probe reads
   ``destroyed``, and the execution disappears from the server.

``DeleteWorkflowExecution`` is ASYNCHRONOUS server-side, and a COLD dev server's
first-ever delete can take tens of seconds (observed live: a fresh
``temporal server start-dev`` failed a ~20s polling window that a warm server passed
easily). The deletion wait therefore uses bounded exponential backoff with a
generous ceiling and treats EITHER signal as success: describe answering NOT_FOUND,
or the ``TypefluxSubjectIds`` visibility enumeration no longer listing the
execution. Cleanup is failure-path-safe (the slice-4 lesson): every execution this
test starts is best-effort-deleted in ``finally`` so encrypted leftovers on a
shared dev server never break other suites' scans.

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233`` (``temporal server start-dev``)
  with custom search attributes enabled (the slice-1 index registration helper runs).

Run: TYPEFLUX_LIVE_TEMPORAL=1 uv run --all-extras pytest -m live -k live_erase_subject
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live

LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"
FIXTURES_DIR = Path(__file__).resolve().parent / "replay_fixtures"
# A 32-byte (AES-256) shared key as utf-8 text (same convention as the #188 live test).
CODEC_KEY = "typeflux-live-codec-key-32bytes!"  # noqa: S105 - test key, not a real secret

#: Deletion-visibility ceiling. A COLD dev server's first DeleteWorkflowExecution is
#: the worst case (tens of seconds); a warm server clears in well under a second, and
#: the backoff below returns as soon as either success signal lands.
DELETION_WAIT_SECONDS = 60.0


async def _wait_until_deleted(client: Any, workflow_id: str, subject_id: str) -> None:
    """Wait for an issued ``DeleteWorkflowExecution`` to land (async server-side).

    Bounded exponential backoff up to :data:`DELETION_WAIT_SECONDS`. SUCCESS is
    either signal, whichever lands first:

    * ``describe`` raises NOT_FOUND — the history + mutable state are gone;
    * the ``TypefluxSubjectIds`` visibility enumeration no longer lists the
      execution — the visibility record is gone.

    A NOT_FOUND from describe is a SUCCESS path, never an error; any other
    describe/list error is treated as transient and retried until the deadline.
    """

    from temporalio.service import RPCError, RPCStatusCode

    from typeflux.project.runs import list_executions_for_subject

    deadline = time.monotonic() + DELETION_WAIT_SECONDS
    delay = 0.25
    last_state = "describable"
    while True:
        try:
            await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return
            last_state = f"describe transient RPCError: {exc.status!r}"
        else:
            try:
                enumeration = await list_executions_for_subject(client, subject_id)
            except Exception as exc:  # noqa: BLE001 - transient visibility errors retry.
                last_state = f"enumeration transient error: {type(exc).__name__}"
            else:
                if all(ref.execution_id != workflow_id for ref in enumeration.executions):
                    return
                last_state = "still describable and still in the subject enumeration"
        if time.monotonic() >= deadline:
            pytest.fail(
                f"execution {workflow_id!r} not deleted within {DELETION_WAIT_SECONDS:g}s "
                f"after DeleteWorkflowExecution (last state: {last_state})"
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5.0)


async def _cleanup_executions(client: Any, workflow_ids: list[str]) -> None:
    """Best-effort delete of every execution the test started (failure-path safe).

    Runs in ``finally``: encrypted leftovers on a shared dev server break other
    suites' scans (the slice-4 lesson), so each started execution is deleted even
    when an assertion failed mid-test. Errors never mask the test outcome; the
    happy path's own erase makes this a harmless idempotent repeat.
    """

    from temporalio.api.common.v1 import WorkflowExecution as WorkflowExecutionProto
    from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest

    for workflow_id in workflow_ids:
        with contextlib.suppress(Exception):
            await client.workflow_service.delete_workflow_execution(
                DeleteWorkflowExecutionRequest(
                    namespace=client.namespace,
                    workflow_execution=WorkflowExecutionProto(workflow_id=workflow_id),
                )
            )


@pytest.mark.asyncio
async def test_live_erase_subject_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    assert len(CODEC_KEY.encode("utf-8")) == 32
    monkeypatch.setenv("TYPEFLUX_LIVE_CODEC_KEY", CODEC_KEY)
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]

    from replay_demo_project.schemas import InputModel, OutputModel

    from tests.test_live_subject_index import _ensure_subject_attribute_registered
    from typeflux.project.erase import erase_subject
    from typeflux.yaml import build_runtime, load_yaml_spec
    from typeflux.yaml.subject_keystore import (
        InMemorySubjectKeystore,
        subject_key_state,
    )

    keystore = InMemorySubjectKeystore()
    spec = load_yaml_spec(FIXTURES_DIR / "subjects_shred.yaml")
    runtime = await build_runtime(spec, subject_keystore=keystore)
    await _ensure_subject_attribute_registered(runtime.client)

    subject_id = f"subject-{uuid4().hex[:8]}"
    workflow_id = f"live-erase-{uuid4().hex[:8]}"
    created_workflow_ids = [workflow_id]

    try:
        async with runtime.worker.build_worker():
            result = await runtime.execute_workflow(
                InputModel(value=subject_id), id=workflow_id, result_type=OutputModel
            )
            assert result == OutputModel(value="replay-fixture")
        assert subject_key_state(keystore, subject_id) == "live"

        # Visibility is eventually consistent: wait (bounded backoff) until the
        # closed execution shows up in the TypefluxSubjectIds enumeration before
        # asserting the plan.
        async def _dry_run_receipt():  # noqa: ANN202
            return await erase_subject(
                subject_id,
                actor="live-test",
                surfaces=("temporal",),
                temporal_client=runtime.client,
                subject_keystore=keystore,
            )

        receipt = await _dry_run_receipt()
        enumeration_deadline = time.monotonic() + 30.0
        enumeration_delay = 0.25
        while time.monotonic() < enumeration_deadline:
            report = receipt.temporal.executions.reports[0]
            if any(ref.execution_id == workflow_id for ref in report.deletable):
                break
            await asyncio.sleep(enumeration_delay)
            enumeration_delay = min(enumeration_delay * 2, 2.0)
            receipt = await _dry_run_receipt()

        # ---- 1) The dry-run plan is complete and provably mutation-free. -------------
        assert receipt.dry_run is True
        assert receipt.failed is False
        entry = receipt.temporal.keystore.entries[0]
        assert (entry.state_before, entry.would_shred, entry.shredded) == ("live", True, False)
        report = receipt.temporal.executions.reports[0]
        assert any(ref.execution_id == workflow_id for ref in report.deletable)
        assert report.deleted_count == 0
        # Nothing mutated: the key is still live and the execution still describable.
        assert subject_key_state(keystore, subject_id) == "live"
        description = await runtime.client.get_workflow_handle(workflow_id).describe()
        assert description.status is not None

        # ---- 2) EXECUTE: shred + DeleteWorkflowExecution, receipt reports both. ------
        executed = await erase_subject(
            subject_id,
            actor="live-test",
            dry_run=False,
            surfaces=("temporal",),
            temporal_client=runtime.client,
            subject_keystore=keystore,
        )
        assert executed.failed is False
        assert executed.temporal.keystore.shredded_key_records == 1
        assert subject_key_state(keystore, subject_id) == "destroyed"
        executed_report = executed.temporal.executions.reports[0]
        assert any(ref.execution_id == workflow_id for ref in executed_report.deleted)
        assert executed_report.deleted_count >= 1

        # The server-side delete is asynchronous (and slow on a COLD dev server) —
        # bounded backoff until either success signal lands.
        await _wait_until_deleted(runtime.client, workflow_id, subject_id)
    finally:
        await _cleanup_executions(runtime.client, created_workflow_ids)
