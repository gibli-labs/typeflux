"""Execute the documented trusted SDK review caller on the owned Temporal lane."""

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
async def test_documented_review_adoption_observes_bound_gate_route_and_result():
    environment = dict(os.environ)
    environment.update(
        TEMPORAL_ADDRESS="localhost:7233",
        TEMPORAL_NAMESPACE="default",
        TYPEFLUX_LIFECYCLE_OBSERVABILITY="none",
        TEMPORAL_TLS="false",
    )
    # The application example can load dotenv; this check owns its service and
    # never opts into a caller's cloud/observability settings.
    environment["TYPEFLUX_ENV_FILE"] = os.devnull
    environment.pop("TEMPORAL_API_KEY", None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "examples.lifecycle_review.main",
        "run",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=75)
        assert process.returncode == 0, err.decode()
        observation = json.loads(out)
        assert observation == {
            "scenario": "review-adoption",
            "sameRun": True,
            "waitingCheckpoint": "package_for_review",
            "reviewRoute": "send_email",
            "state": "completed",
            "stepsStarted": ["assess_case", "package_for_review", "send_email"],
            "result": {
                "case_id": "CASE-2026-0101",
                "decision": "approved",
                "summary": "Human approval was received and the case can proceed.",
                "approved": True,
            },
        }
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
