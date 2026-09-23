"""LIVE proof (#618 slice 4): the Python CP operates a TS-edition execution.

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233``
- the TS workspace built (``pnpm -r build``)

The test writes a TS-dialect project, spawns a real TS worker via
``packages/typescript/temporal-yaml/scripts/live-binding-harness.mjs`` (plan-as-argument starts, identity memo
written by the TS edition), and then drives the **Python control plane's
HTTP surface** — a ts-runtime registry project — through the epic's
acceptance: status of / review / cancel a TS execution, plus the 409
binding-mismatch fail-closed path.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from typeflux.controlplane import create_app_from_registry

pytestmark = pytest.mark.live

LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"
TS_YAML_PACKAGE = Path(__file__).resolve().parents[3] / "packages" / "typescript" / "temporal-yaml"
HARNESS = TS_YAML_PACKAGE / "scripts" / "live-binding-harness.mjs"

WORKFLOW_YAML = """\
project: ts_live_demo
name: ts_live_demo_yaml
task_queue: ts-live-binding
runtime:
  temporal:
    address: localhost:7233
  registry:
    type: inline
    prompts:
      assess: assess {{value}}
  provider:
    type: openai
  observability:
    type: none
activities:
  definitions:
    - name: assess
      input: schemas:Item
      output: schemas:Item
      prompt: assess
workflow:
  name: TsLiveDemoWorkflow
  input: schemas:Item
  output: schemas:Item
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
      activity: assess
    - id: finalize
      activity: assess
"""


def _write_ts_project(tmp_path: Path) -> Path:
    (tmp_path / "typeflux.project.yaml").write_text(
        "version: '1'\n"
        "name: ts-live-demo\n"
        "workflows:\n"
        "  - id: demo\n    path: workflow.yaml\n"
        "environments:\n"
        "  local: environments/local.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "environments").mkdir()
    (tmp_path / "environments" / "local.yaml").write_text(
        "version: '1'\nname: local\n", encoding="utf-8"
    )
    (tmp_path / "workflow.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    registry = tmp_path / "typeflux.projects.yaml"
    registry.write_text(
        "version: '1'\n"
        "projects:\n"
        "  - id: ts\n    manifest: typeflux.project.yaml\n    runtime: typescript\n",
        encoding="utf-8",
    )
    return registry


def _await_status(client: TestClient, execution_id: str, state: str, *, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(
            "/api/v1/projects/ts/workflows/demo/status",
            params={"environment_id": "local", "execution_id": execution_id},
        )
        assert response.status_code == 200, response.json()
        last = response.json()
        if last["status"]["state"] == state:
            return last
        time.sleep(0.5)
    raise AssertionError(f"execution {execution_id} never reached {state!r}; last: {last}")


@pytest.mark.skipif(
    not LIVE, reason="TYPEFLUX_LIVE_TEMPORAL=1 required (live Temporal + TS worker)"
)
def test_python_cp_operates_a_ts_execution_live(tmp_path: Path) -> None:
    registry = _write_ts_project(tmp_path)
    review_id = f"ts-live-review-{int(time.time())}"
    cancel_id = f"ts-live-cancel-{int(time.time())}"

    harness = subprocess.Popen(
        ["node", str(HARNESS), str(tmp_path), review_id, cancel_id],
        cwd=TS_YAML_PACKAGE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert harness.stdout is not None
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            line = harness.stdout.readline()
            if not line:
                assert harness.poll() is None, "harness exited before ready"
                continue
            event = json.loads(line)
            if event.get("event") == "ready":
                ready = True
                break
        assert ready, "TS harness never became ready"

        client = TestClient(create_app_from_registry(registry))

        # 1. STATUS: the TS execution reaches its review checkpoint and the
        # Python CP reads it via the shared query — memo-verified.
        waiting = _await_status(client, review_id, "waiting_for_review")
        assert waiting["valid_user_decisions"] == {"approve": "finalize", "reject": "finalize"}

        # 2. REVIEW: approve routes past the checkpoint to completion.
        review = client.post(
            "/api/v1/projects/ts/workflows/demo/review",
            json={
                "environment_id": "local",
                "execution_id": review_id,
                "command": {"user_decision": "approve", "reviewer": "live-proof"},
            },
        )
        assert review.status_code in (200, 204), review.text
        _await_status(client, review_id, "completed")

        # 3. CANCEL: the second execution honors the graceful cancel signal.
        _await_status(client, cancel_id, "waiting_for_review")
        cancel = client.post(
            "/api/v1/projects/ts/workflows/demo/cancel",
            json={
                "environment_id": "local",
                "execution_id": cancel_id,
                "reason": "live-proof cancellation",
            },
        )
        assert cancel.status_code in (200, 204), cancel.text
        cancelled = _await_status(client, cancel_id, "cancelled")
        assert cancelled["status"]["cancellation_reason"] == "live-proof cancellation"

        # 4. FAIL CLOSED: a foreign execution (wrong type) is refused 409.
        import asyncio

        from temporalio.client import Client as TemporalClient

        async def start_foreign() -> str:
            temporal = await TemporalClient.connect("localhost:7233")
            foreign_id = f"ts-live-foreign-{int(time.time())}"
            await temporal.start_workflow(
                "SomeOtherWorkflowType",
                id=foreign_id,
                task_queue="nobody-polls-this",
            )
            return foreign_id

        foreign_id = asyncio.run(start_foreign())
        mismatch = client.get(
            "/api/v1/projects/ts/workflows/demo/status",
            params={"environment_id": "local", "execution_id": foreign_id},
        )
        assert mismatch.status_code == 409, mismatch.text
        assert mismatch.json()["error"] == "LifecycleBindingError"
    finally:
        try:
            os.killpg(harness.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            harness.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(harness.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
