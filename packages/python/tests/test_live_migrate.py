"""LIVE proof (#204): long-drain migration (terminate-and-resubmit) on a real
Temporal dev server.

A v1 workflow with a review gate is started and parks at the gate. A v2 of the
same logical workflow (the gate removed → a new graph digest → a new registered
type) is deployed on the SAME task queue. The control-plane ``migrate`` operation
then:

1. refuses while the execution is parked at the gate (the review-state-loss
   guardrail), and
2. with ``abandon_gates``, terminates the old run with the canonical reason and
   resubmits the carried-over input against v2 — the new run running under the v2
   version key, carrying the ``typeflux_migrated_from`` provenance memo, and
   completing.

Deterministic: the fixture provider (``ReplayFixtureProvider``) returns a fixed
value, so no LLM/provider credentials are needed — only a dev server.

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233``

Run: TYPEFLUX_LIVE_TEMPORAL=1 uv run --all-extras pytest -m live -k live_migrate
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

_QUEUE = f"migrate-live-{uuid4().hex[:8]}"

_V1_YAML = f"""\
project: replay_demo_project
name: migrate_live_v1
task_queue: {_QUEUE}
runtime:
  temporal:
    address: ${{TEMPORAL_ADDRESS:-localhost:7233}}
  registry:
    type: inline
    prompts:
      first: first {{{{value}}}}
      second: second {{{{value}}}}
  provider:
    type: custom
    class: replay_demo_project.fakes:ReplayFixtureProvider
  imports:
    allow_provider_class: true
  observability:
    type: none
activities:
  modules:
    - activities
workflow:
  name: MigrateLiveWorkflow
  input: schemas:InputModel
  output: schemas:OutputModel
  lifecycle:
    enabled: true
    review:
      after_step: assess
      invalid_user_decision: warn
      user_decisions:
        approve:
          route: finalize
        reject:
          route: finalize
  steps:
    - id: assess
      activity: first
    - id: finalize
      activity: second
"""

# v2: the SAME logical workflow with the review gate removed — a different graph,
# hence a different spec digest and a different registered versioned type.
_V2_YAML = f"""\
project: replay_demo_project
name: migrate_live_v2
task_queue: {_QUEUE}
runtime:
  temporal:
    address: ${{TEMPORAL_ADDRESS:-localhost:7233}}
  registry:
    type: inline
    prompts:
      first: first {{{{value}}}}
      second: second {{{{value}}}}
  provider:
    type: custom
    class: replay_demo_project.fakes:ReplayFixtureProvider
  imports:
    allow_provider_class: true
  observability:
    type: none
activities:
  modules:
    - activities
workflow:
  name: MigrateLiveWorkflow
  input: schemas:InputModel
  output: schemas:OutputModel
  steps:
    - id: assess
      activity: first
    - id: finalize
      activity: second
"""


@pytest.mark.asyncio
async def test_live_migrate_gate_refusal_then_abandon_and_resubmit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]

    from replay_demo_project.schemas import InputModel, OutputModel

    from typeflux.project.binding import PythonVersionedTypeDriver
    from typeflux.project.migrate import WaitingGateMigrateError
    from typeflux.project.operations import WorkflowOperations
    from typeflux.yaml import build_runtime, load_yaml_spec

    v1_path = tmp_path / "v1.yaml"
    v1_path.write_text(_V1_YAML, encoding="utf-8")
    v2_path = tmp_path / "v2.yaml"
    v2_path.write_text(_V2_YAML, encoding="utf-8")

    v1 = await build_runtime(load_yaml_spec(v1_path))
    v2 = await build_runtime(load_yaml_spec(v2_path))
    v1_type = getattr(v1.workflow_class, "__typeflux_workflow_type__")
    v2_type = getattr(v2.workflow_class, "__typeflux_workflow_type__")
    assert v1_type != v2_type  # the graph changed → distinct versioned types

    exec_id = f"migrate-live-{uuid4().hex[:8]}"
    async with v1.worker.build_worker(), v2.worker.build_worker():
        # Start v1 and let it park at the review gate (assess completed, waiting).
        await v1.start_workflow(InputModel(value="carry-me"), id=exec_id)
        await v1.wait_for_lifecycle_state(exec_id, "waiting_for_review", timeout_seconds=30)
        old_run_id = (await v1.client.get_workflow_handle(exec_id).describe()).run_id
        assert isinstance(old_run_id, str) and old_run_id

        ops = WorkflowOperations(driver=PythonVersionedTypeDriver(runtime=v2))

        # 1) Refused while the gate is open (review-state-loss guardrail).
        with pytest.raises(WaitingGateMigrateError, match="waiting at review gate"):
            await ops.migrate(exec_id, run_id=old_run_id, abandon_gates=False)
        # The old run is still RUNNING — the refusal never terminated it.
        old_desc = await v1.client.get_workflow_handle(exec_id, run_id=old_run_id).describe()
        assert old_desc.status.name == "RUNNING"

        # 2) With abandon_gates: terminate + resubmit against v2.
        result = await ops.migrate(exec_id, run_id=old_run_id, abandon_gates=True)

        assert result.old_run_id == old_run_id
        assert result.old_version_key == v1_type
        assert result.new_version_key == v2_type
        assert result.abandoned_gate_ids  # the open gate was acknowledged/abandoned

        # The OLD run is TERMINATED with the canonical migrate reason.
        old_desc = await v1.client.get_workflow_handle(exec_id, run_id=old_run_id).describe()
        assert old_desc.status.name == "TERMINATED"

        # The NEW run runs under the v2 versioned type and carries the provenance memo.
        new_handle = v2.client.get_workflow_handle(exec_id, run_id=result.new_run_id)
        new_desc = await new_handle.describe()
        assert new_desc.workflow_type == v2_type
        new_memo = await new_desc.memo()
        assert new_memo["typeflux_migrated_from"] == old_run_id
        assert new_memo["typeflux_migrated_from_version"] == v1_type

        # And it completes (v2 has no gate) while the v2 worker is still polling,
        # carrying the original input through to the deterministic result.
        completed = await new_handle.result()
        assert OutputModel.model_validate(completed) == OutputModel(value="replay-fixture")

    v1.observability.writer.shutdown()
    v2.observability.writer.shutdown()
