"""Execute the worked YAML saga example on the lane-owned real Temporal server."""

import asyncio
import json
import os
import sys

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("TYPEFLUX_LIVE_TEMPORAL") != "1", reason="isolated Temporal lane required"
    ),
]


@pytest.mark.asyncio
async def test_documented_compensation_observes_complete_partial_and_worker_replacement():
    environment = dict(os.environ)
    environment.update(
        TEMPORAL_ADDRESS="localhost:7233",
        TEMPORAL_NAMESPACE="default",
        TEMPORAL_TLS="false",
        TYPEFLUX_ENV_FILE=os.devnull,
        TYPEFLUX_SAGA_OBSERVABILITY="none",
    )
    environment.pop("TEMPORAL_API_KEY", None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "examples.compensation_saga.adoption_demo",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=120)
        assert process.returncode == 0, err.decode()
        observations = json.loads(out)
        assert len(observations) == 3
        for observation, scenario in zip(
            observations, ("complete", "partial", "replacement"), strict=True
        ):
            actions = [
                ["book_room", 1],
                ["charge_card", 1],
                ["fulfill_order", 1],
                ["refund_card", 1],
            ]
            if scenario == "replacement":
                actions.append(["refund_card", 2])
            actions.append(["cancel_room", 1])
            assert observation == {
                "scenario": scenario,
                "sameRun": True,
                "originalFailure": True,
                "compensationStatus": "partial" if scenario == "partial" else "complete",
                "actions": actions,
                "forwardFakeWrites": {"book_room": 1, "charge_card": 1},
                "retryRecordedBeforeStop": scenario == "replacement",
            }
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
