"""LIVE proof on TEMPORAL CLOUD (#672): the composed profile connection is real.

The workflow YAML deliberately points at a DEAD address (127.0.0.1:1); a
selected ``runtime`` profile overrides the connection to Temporal Cloud
(``TEMPORAL_ADDRESS``/``TEMPORAL_NAMESPACE``/TLS/API key). The Python control
plane (with the Node subprocess resolver) then starts a TS-edition execution,
drives its review gate to completion on a real TS worker polling CLOUD — the
only way any of that can work is the composed profile connection (#672), on
both the driver side (Python CP) and the worker side.

Requires (gated): ``TYPEFLUX_LIVE_TEMPORAL_CLOUD=1`` plus ``TEMPORAL_ADDRESS``
(a ``*.tmprl.cloud`` endpoint), ``TEMPORAL_NAMESPACE``, ``TEMPORAL_API_KEY``
in the environment (e.g. ``set -a; source .env.temporal-cloud``), and the TS
workspace built (``pnpm -r build``).
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

from tests.test_live_ts_binding import _await_status
from typeflux.controlplane import create_app_from_registry

pytestmark = pytest.mark.live

CLOUD = (
    os.environ.get("TYPEFLUX_LIVE_TEMPORAL_CLOUD") == "1"
    and bool(os.environ.get("TEMPORAL_ADDRESS"))
    and bool(os.environ.get("TEMPORAL_NAMESPACE"))
    and bool(os.environ.get("TEMPORAL_API_KEY"))
)
REPO = Path(__file__).resolve().parents[3]
TS_YAML_PACKAGE = REPO / "packages" / "typescript" / "temporal-yaml"
HARNESS = TS_YAML_PACKAGE / "scripts" / "live-binding-harness.mjs"
RESOLVER_ENTRY = (
    REPO / "packages" / "typescript" / "temporal-controlplane" / "dist" / "resolver-stdio.js"
)

# The DEAD base address is the point: any un-composed fallback fails loudly.
WORKFLOW_YAML = """\
project: ts_cloud_demo
name: ts_cloud_demo_yaml
task_queue: ts-cloud-binding
runtime:
  temporal:
    address: 127.0.0.1:1
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
  name: TsCloudDemoWorkflow
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
  steps:
    - id: assess
      activity: assess
    - id: finalize
      activity: assess
"""

CLOUD_PROFILE_YAML = """\
name: cloud
kind: runtime
runtime:
  temporal:
    address: ${TEMPORAL_ADDRESS}
    namespace: ${TEMPORAL_NAMESPACE}
    tls: true
    api_key:
      value_from:
        env: TEMPORAL_API_KEY
"""


def _write_ts_project(tmp_path: Path) -> Path:
    (tmp_path / "typeflux.project.yaml").write_text(
        "version: '1'\n"
        "name: ts-cloud-demo\n"
        "workflows:\n"
        "  - id: demo\n    path: workflow.yaml\n"
        "    profiles:\n      runtime: cloud\n"
        "profiles:\n"
        "  runtime:\n    cloud: profiles/cloud.yaml\n"
        "environments:\n"
        "  local: environments/local.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "environments").mkdir()
    (tmp_path / "environments" / "local.yaml").write_text(
        "version: '1'\nname: local\n", encoding="utf-8"
    )
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "cloud.yaml").write_text(CLOUD_PROFILE_YAML, encoding="utf-8")
    (tmp_path / "workflow.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    registry = tmp_path / "typeflux.projects.yaml"
    registry.write_text(
        "version: '1'\n"
        "projects:\n"
        "  - id: ts\n    manifest: typeflux.project.yaml\n    runtime: typescript\n",
        encoding="utf-8",
    )
    return registry


@pytest.mark.skipif(
    not CLOUD,
    reason="TYPEFLUX_LIVE_TEMPORAL_CLOUD=1 + TEMPORAL_ADDRESS/NAMESPACE/API_KEY required",
)
def test_python_cp_operates_a_ts_execution_on_temporal_cloud(tmp_path: Path) -> None:
    from typeflux.controlplane.resolver import InProcessPythonResolver, SubprocessResolver

    registry = _write_ts_project(tmp_path)

    # The TS worker polls CLOUD (the harness reads TEMPORAL_* env, inherited).
    harness = subprocess.Popen(
        ["node", str(HARNESS), str(tmp_path), "--serve-only"],
        cwd=TS_YAML_PACKAGE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    schemas_module = TS_YAML_PACKAGE / "scripts" / "live-binding-schemas.mjs"
    resolver = SubprocessResolver(
        ["node", str(RESOLVER_ENTRY), "--schemas", str(schemas_module)],
        runtime="typescript",
    )
    try:
        assert harness.stdout is not None
        deadline = time.monotonic() + 90
        ready = False
        while time.monotonic() < deadline:
            line = harness.stdout.readline()
            if not line:
                assert harness.poll() is None, "harness exited before ready"
                continue
            if json.loads(line).get("event") == "ready":
                ready = True
                break
        assert ready, "TS worker harness never became ready against Temporal Cloud"

        app = create_app_from_registry(registry, resolvers=[InProcessPythonResolver(), resolver])
        client = TestClient(app)

        execution_id = f"py-cp-cloud-{int(time.time())}"
        start = client.post(
            "/api/v1/projects/ts/workflows/demo/start",
            json={
                "environment_id": "local",
                "execution_id": execution_id,
                "input": {"value": "started-on-cloud"},
            },
        )
        assert start.status_code == 200, start.text
        assert start.json()["workflow_type"] == "typefluxYamlWorkflow"

        # The review gate is reached and approved TO COMPLETION — on a cluster
        # only reachable through the composed profile connection.
        _await_status(client, execution_id, "waiting_for_review", timeout=60.0)
        review = client.post(
            "/api/v1/projects/ts/workflows/demo/review",
            json={
                "environment_id": "local",
                "execution_id": execution_id,
                "command": {"user_decision": "approve", "reviewer": "cloud-live-proof"},
            },
        )
        assert review.status_code in (200, 204), review.text
        _await_status(client, execution_id, "completed", timeout=60.0)

        # DECOMPOSED VISIBILITY (#671) on CLOUD: the listing reaches Temporal
        # Cloud through the same composed profile connection and finds this
        # execution by its identity memo, running the pinned (current) digest.
        # Cloud's visibility store indexes eventually — poll briefly for the
        # just-closed execution to appear.
        by_id: dict = {}
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            listing = client.get(
                "/api/v1/projects/ts/workflows/demo/executions",
                params={"environment_id": "local"},
            )
            assert listing.status_code == 200, listing.text
            listed = listing.json()
            assert listed["current_workflow_type"] == "typefluxYamlWorkflow"
            by_id = {record["execution_id"]: record for record in listed["executions"]}
            if by_id.get(execution_id, {}).get("status") == "COMPLETED":
                break
            time.sleep(2)
        assert execution_id in by_id, listed
        assert by_id[execution_id]["current_version"] is True
        assert by_id[execution_id]["status"] == "COMPLETED"

        connections = client.get(
            "/api/v1/projects/ts/workflows/demo/connections",
            params={"environment_id": "local"},
        )
        assert connections.status_code == 200, connections.text
        assert connections.json()["registry"]["type"] == "inline"
    finally:
        resolver.close()
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
