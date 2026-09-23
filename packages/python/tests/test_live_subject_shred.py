"""LIVE proof (#715 slice 4): per-subject crypto-shred end-to-end on a real Temporal
dev server, through the REAL build path (``build_runtime`` with a spec declaring
``runtime.temporal.payload_codec.subject_scope`` + an injected in-memory keystore —
the same ``_connect_client`` route production takes; no hand-built codecs):

1. a workflow started WITH a subject id completes, and its history payloads ON THE
   SERVER (start input, activity results, workflow result — read raw with a
   codec-less plain client) are sealed under the ``tfsubj1:`` subject kid, with the
   subject's plaintext absent from the ciphertext;
2. after ``destroy_subject_key``, decoding those payloads through the subject-scoped
   converter — and reading the result through the codec-aware client — raises the
   DISTINCT ``SubjectKeyShreddedError``, never plaintext; a post-shred START for the
   same subject also fails closed (no key resurrection);
3. a NO-subject workflow on the same wired stack (same codec spec + keystore) carries
   the ordinary shared kid (``main``) and stays readable before AND after the destroy;
4. the visibility-describe fallback: a SECOND client built through the same
   ``_connect_client`` route with a FRESH (empty) binding registry resolves the
   workflow's subjects from the live ``TypefluxSubjectIds`` search attribute and
   ENCODES under the correct subject kid without any registration — the encode-side
   fallback proven live. (Decode never consults the bindings at all — the ``tfsubj1:``
   kid itself carries the subject set — so the second client's decode also succeeding
   is kid-driven, not a fallback proof; the encode assertion is the fallback proof.)

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233`` (``temporal server start-dev``).

Run: TYPEFLUX_LIVE_TEMPORAL=1 uv run --all-extras pytest -m live -k live_subject_shred
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live

LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"
FIXTURES_DIR = Path(__file__).resolve().parent / "replay_fixtures"
# A 32-byte (AES-256) shared key as utf-8 text (same convention as the #188 live test).
CODEC_KEY = "typeflux-live-codec-key-32bytes!"  # noqa: S105 - test key, not a real secret


def _shred_in_chain(exc: BaseException | None) -> bool:
    """True when a SubjectKeyShreddedError appears anywhere in the exception chain."""

    from typeflux.yaml.subject_keystore import SubjectKeyShreddedError

    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, SubjectKeyShreddedError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def _collect_history_payloads(plain_client, workflow_id: str) -> dict[str, list]:
    """Raw payloads per history-event kind, read with a CODEC-LESS client."""

    from temporalio.api.enums.v1 import EventType

    collected: dict[str, list] = {"input": [], "activity_results": [], "result": []}
    async for event in plain_client.get_workflow_handle(workflow_id).fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            collected["input"].extend(
                event.workflow_execution_started_event_attributes.input.payloads
            )
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            collected["activity_results"].extend(
                event.activity_task_completed_event_attributes.result.payloads
            )
        elif event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
            collected["result"].extend(
                event.workflow_execution_completed_event_attributes.result.payloads
            )
    return collected


@pytest.mark.asyncio
async def test_live_subject_shred_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    assert len(CODEC_KEY.encode("utf-8")) == 32
    monkeypatch.setenv("TYPEFLUX_LIVE_CODEC_KEY", CODEC_KEY)
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]

    from replay_demo_project.schemas import InputModel, OutputModel
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.converter import WorkflowSerializationContext

    from tests.test_live_subject_index import _ensure_subject_attribute_registered
    from typeflux.yaml import build_runtime, load_yaml_spec
    from typeflux.yaml.runtime import _connect_client
    from typeflux.yaml.subject_keystore import (
        InMemorySubjectKeystore,
        SubjectKeyShreddedError,
        SubjectScopedPayloadCodec,
        subject_kid,
    )

    keystore = InMemorySubjectKeystore()
    subject_spec = load_yaml_spec(FIXTURES_DIR / "subjects_shred.yaml")
    runtime = await build_runtime(subject_spec, subject_keystore=keystore)
    assert isinstance(runtime.client.data_converter.payload_codec, SubjectScopedPayloadCodec)
    await _ensure_subject_attribute_registered(runtime.client)

    subject_id = f"subject-{uuid4().hex[:8]}"
    workflow_id = f"live-shred-{uuid4().hex[:8]}"
    expected_kid = subject_kid([subject_id]).encode("utf-8")

    # ---- 1) A subject execution completes through the wired stack, and the SERVER
    #         history is sealed under the tfsubj1: subject kid. --------------------------
    async with runtime.worker.build_worker():
        result = await runtime.execute_workflow(
            InputModel(value=subject_id), id=workflow_id, result_type=OutputModel
        )
        assert result == OutputModel(value="replay-fixture")

    plain_client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        data_converter=pydantic_data_converter,
    )
    raw = await _collect_history_payloads(plain_client, workflow_id)
    assert raw["input"], "start event carried no input payloads"
    assert raw["activity_results"], "no activity result payloads found"
    assert raw["result"], "completion event carried no result payloads"
    for kind, payloads in raw.items():
        for payload in payloads:
            assert payload.metadata.get("encoding") == b"binary/encrypted", kind
            # The load-bearing wire assertion: the SUBJECT kid, not the shared "main" —
            # encode ran through the subject seam on the wire (client start input AND
            # worker-encoded activity/workflow results).
            assert payload.metadata.get("typeflux-key-id") == expected_kid, kind
            assert subject_id.encode("utf-8") not in payload.data, kind

    # Pre-shred sanity: the codec-aware converter opens the on-server payloads.
    subject_codec = runtime.client.data_converter.payload_codec
    opened = await subject_codec.decode(raw["input"])
    assert subject_id.encode("utf-8") in opened[0].data

    # ---- 4) Visibility-describe fallback: a SECOND client (same _connect_client route,
    #         same keystore, FRESH empty registry) resolves the subjects from the live
    #         TypefluxSubjectIds attribute and ENCODES under the subject kid with no
    #         registration — the encode-side fallback channel, live. --------------------
    second_client = await _connect_client(subject_spec, plugin=None, subject_keystore=keystore)
    second_codec = second_client.data_converter.payload_codec
    assert isinstance(second_codec, SubjectScopedPayloadCodec)
    assert second_codec is not subject_codec  # a genuinely separate codec + registry
    resolved = await second_codec.bindings.resolve(workflow_id)
    assert resolved == (subject_id,)
    bound = second_client.data_converter.with_context(
        WorkflowSerializationContext(namespace=second_client.namespace, workflow_id=workflow_id)
    )
    from temporalio.api.common.v1 import Payload

    probe = Payload(metadata={"encoding": b"json/plain"}, data=b'{"probe":1}')
    sealed = await bound.payload_codec.encode([probe])
    assert sealed[0].metadata.get("typeflux-key-id") == expected_kid
    # And the second client's decode of the on-server payloads succeeds (kid-driven —
    # see the module docstring; this is keystore sharing, not the fallback).
    assert (await second_codec.decode(raw["result"]))[0].data

    # ---- 3, part A) A NO-subject workflow on the same wired stack (same keystore)
    #         carries the ordinary shared kid and reads back fine. ----------------------
    plain_spec = load_yaml_spec(FIXTURES_DIR / "plain_shred.yaml")
    plain_runtime = await build_runtime(plain_spec, subject_keystore=keystore)
    plain_workflow_id = f"live-shred-plain-{uuid4().hex[:8]}"
    async with plain_runtime.worker.build_worker():
        plain_result = await plain_runtime.execute_workflow(
            InputModel(value="no-subject-marker"), id=plain_workflow_id, result_type=OutputModel
        )
        assert plain_result == OutputModel(value="replay-fixture")
    plain_raw = await _collect_history_payloads(plain_client, plain_workflow_id)
    for kind, payloads in plain_raw.items():
        for payload in payloads:
            assert payload.metadata.get("encoding") == b"binary/encrypted", kind
            assert payload.metadata.get("typeflux-key-id") == b"main", kind

    # ---- 2) THE SHRED: destroy the subject's key record. ------------------------------
    destruction = keystore.destroy_subject_key(subject_id)
    assert destruction.key_existed is True

    # Direct decode of the on-server payloads now raises the DISTINCT shred error.
    with pytest.raises(SubjectKeyShreddedError) as excinfo:
        await subject_codec.decode(raw["input"])
    assert excinfo.value.subject_id == subject_id
    with pytest.raises(SubjectKeyShreddedError):
        await second_codec.decode(raw["result"])

    # Reading the result through the codec-aware client fails with the shred in the
    # chain — and NEVER yields plaintext.
    result_after_shred: object | None = None
    with pytest.raises(Exception) as result_excinfo:  # noqa: PT011 - chain-asserted below
        result_after_shred = await runtime.client.get_workflow_handle(workflow_id).result()
    assert result_after_shred is None
    assert _shred_in_chain(result_excinfo.value), (
        f"expected SubjectKeyShreddedError in the chain, got {result_excinfo.value!r}"
    )

    # A post-shred START for the same subject fails closed at the client-side input
    # encode (no key resurrection) — no worker needed, the start never leaves the client.
    with pytest.raises(Exception) as start_excinfo:  # noqa: PT011 - chain-asserted below
        await runtime.execute_workflow(
            InputModel(value=subject_id),
            id=f"live-shred-after-{uuid4().hex[:8]}",
            result_type=OutputModel,
        )
    assert _shred_in_chain(start_excinfo.value), (
        f"expected SubjectKeyShreddedError in the chain, got {start_excinfo.value!r}"
    )

    # ---- 3, part B) The no-subject workflow is STILL readable after the destroy. ------
    still_readable = await plain_runtime.client.get_workflow_handle(plain_workflow_id).result()
    assert (
        still_readable == {"value": "replay-fixture"}
        or getattr(still_readable, "value", None) == "replay-fixture"
    )
    shared_opened = await plain_runtime.client.data_converter.payload_codec.decode(
        plain_raw["input"]
    )
    assert b"no-subject-marker" in shared_opened[0].data
