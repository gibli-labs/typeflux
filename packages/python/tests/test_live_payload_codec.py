"""LIVE proof (#188): the AES-256-GCM payload codec end-to-end on a real Temporal dev
server. With the codec ON:

- a workflow runs end-to-end (the worker decrypts activity/workflow IO transparently);
- the Temporal HISTORY payloads are CIPHERTEXT — a plain (non-codec) client sees
  metadata encoding = binary/encrypted and the distinctive plaintext is NOT present;
- a codec-aware read of the start event returns the DECRYPTED input;
- FAIL-CLOSED: a client whose key cannot decrypt the existing history hard-errors,
  never a silent plaintext passthrough.

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233`` (``temporal server start-dev``).

Run: TYPEFLUX_LIVE_TEMPORAL=1 uv run --all-extras pytest -m live -k live_payload_codec
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live

LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"
FIXTURES_DIR = Path(__file__).resolve().parent / "replay_fixtures"
# A 32-byte (AES-256) key as utf-8 text; distinctive so we can prove it never leaks.
CODEC_KEY = "typeflux-live-codec-key-32bytes!"  # noqa: S105 - test key, not a real secret
DISTINCTIVE = "codec-live-plaintext-marker-9f3a"


@pytest.mark.asyncio
async def test_live_payload_codec_history_is_ciphertext(monkeypatch: pytest.MonkeyPatch) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    assert len(CODEC_KEY.encode("utf-8")) == 32
    monkeypatch.setenv("TYPEFLUX_LIVE_CODEC_KEY", CODEC_KEY)
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]

    from replay_demo_project.schemas import InputModel, OutputModel

    from typeflux.project.migrate import read_start_event_input
    from typeflux.yaml import build_runtime, load_yaml_spec

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "plain_codec.yaml"))
    workflow_id = f"live-codec-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        # 1) End-to-end: the codec-carrying worker + client run the workflow to completion.
        result = await runtime.execute_workflow(
            InputModel(value=DISTINCTIVE), id=workflow_id, result_type=OutputModel
        )
        assert result == OutputModel(value="replay-fixture")

        handle = runtime.client.get_workflow_handle(workflow_id)

        # 2) HISTORY IS CIPHERTEXT: a PLAIN (non-codec) client reads the raw payloads —
        #    the start input is marked binary/encrypted and the plaintext is absent.
        from temporalio.client import Client
        from temporalio.contrib.pydantic import pydantic_data_converter

        plain_client = await Client.connect(
            os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
            data_converter=pydantic_data_converter,
        )
        saw_encrypted_input = False
        from temporalio.api.enums.v1 import EventType

        async for event in plain_client.get_workflow_handle(workflow_id).fetch_history_events():
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                payloads = list(event.workflow_execution_started_event_attributes.input.payloads)
                assert payloads, "start event carried no input"
                for payload in payloads:
                    assert payload.metadata.get("encoding") == b"binary/encrypted"
                    assert payload.metadata.get("typeflux-key-id") == b"main"
                    assert DISTINCTIVE.encode("utf-8") not in payload.data
                saw_encrypted_input = True
        assert saw_encrypted_input, "no WorkflowExecutionStarted event found"

        # 3) A CODEC-AWARE read of the start event returns the DECRYPTED input (the raw
        #    converter yields JSON without the result_type, so the value is a plain dict —
        #    the point is the distinctive plaintext round-trips back through decryption).
        decoded_input = await read_start_event_input(handle, runtime.client.data_converter)
        decoded_value = (
            decoded_input.get("value")
            if isinstance(decoded_input, dict)
            else getattr(decoded_input, "value", None)
        )
        assert decoded_value == DISTINCTIVE

        # 4) FAIL-CLOSED: a client whose key can't decrypt the history hard-errors.
        from typeflux.yaml.payload_codec import (
            PayloadCodecError,
            TypefluxAesGcmPayloadCodec,
        )

        wrong = TypefluxAesGcmPayloadCodec(current_kid="main", keys={"main": b"x" * 32})
        wrong_converter = dataclasses.replace(pydantic_data_converter, payload_codec=wrong)
        with pytest.raises((PayloadCodecError, Exception)) as excinfo:
            await read_start_event_input(handle, wrong_converter)
        assert "authentication" in str(excinfo.value) or isinstance(
            excinfo.value, PayloadCodecError
        )
