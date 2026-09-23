"""LIVE proof (#55 slice 2): a Python composition workflow end-to-end on a real
Temporal dev server — the Python edition of the slice-1 TS live coverage
(live-yaml-workflow.test.ts).

A `parallel:` block (when-gated branch + ungated branch) with a typed collect
merge and a when-gated early-exit tail runs via ``build_runtime`` against
``localhost:7233``: the full path executes both branches concurrently and the
tail; the gated path drives a None collect field through the strict activity
boundary, early-exits, and records ``step_skipped`` events carrying the rendered
condition (#55 §3.3 provenance).

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233``

Run: TYPEFLUX_LIVE_TEMPORAL=1 uv run --all-extras pytest -m live -k live_yaml_composition
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


def _skipped(status: dict) -> list[tuple[str, str]]:
    return [
        (event["step_id"], event["condition"])
        for event in status["events"]
        if event["event"] == "step_skipped"
    ]


def _started(status: dict) -> list[str]:
    return [event["step_id"] for event in status["events"] if event["event"] == "step_started"]


@pytest.mark.asyncio
async def test_live_composition_full_and_gated_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]

    from replay_demo_project.schemas import InputModel, OutputModel

    from typeflux.yaml import build_runtime, load_yaml_spec

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "composition.yaml"))
    async with runtime.worker.build_worker():
        # FULL path: the gated branch runs — both branches genuinely fan out on
        # the dev server — and the gated tail executes.
        full_id = f"live-composition-full-{uuid4().hex[:8]}"
        result = await runtime.execute_workflow(
            InputModel(value="deep"), id=full_id, result_type=OutputModel
        )
        assert result == OutputModel(value="replay-fixture")
        status = await runtime.client.get_workflow_handle(full_id).query(
            "typeflux_lifecycle_status"
        )
        assert status["state"] == "completed"
        assert _skipped(status) == []
        started = _started(status)
        assert started[0] == "fanout"
        # Per-branch subsequences: each branch's steps precede the post-block
        # step; the cross-branch interleaving is deliberately not pinned (§5.1).
        assert started.index("screen_step") < started.index("merge")
        assert started.index("plain_step") < started.index("merge")
        assert started.index("merge") < started.index("escalate")
        assert status["completed_units"] == status["total_units"] == 4

        # GATED path: the `screen` branch contributes None through the strict
        # activity boundary (FanoutModel.screen is Optional), the tail gate
        # early-exits, and both skips carry the rendered condition.
        gated_id = f"live-composition-gated-{uuid4().hex[:8]}"
        result = await runtime.execute_workflow(
            InputModel(value="start"), id=gated_id, result_type=OutputModel
        )
        assert result == OutputModel(value="replay-fixture")
        status = await runtime.client.get_workflow_handle(gated_id).query(
            "typeflux_lifecycle_status"
        )
        assert status["state"] == "completed"
        assert _skipped(status) == [
            ("screen", 'input.value == "deep"'),
            ("escalate", 'input.value == "deep"'),
        ]
        assert "screen_step" not in _started(status)
        # Skipped units released through the clamp: progress completes at 100%.
        assert status["completed_units"] == status["total_units"]
