"""LIVE proof (#642): one control plane serves both editions END TO END.

The Python CP, holding the Node subprocess resolver, STARTS a TS-edition
execution (plan-as-argument, identity memo), drives its review gate to
completion, and honors a policy selection — the operations that fail closed
without the resolver. The TS side runs only a WORKER (the harness's
``--serve-only`` mode); every start/status/review flows through the Python
control plane's HTTP surface.

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233``
- the TS workspace built (``pnpm -r build``)
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

from tests.test_live_ts_binding import WORKFLOW_YAML, _await_status
from typeflux.controlplane import create_app_from_registry

pytestmark = pytest.mark.live

LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"
REPO = Path(__file__).resolve().parents[3]
TS_YAML_PACKAGE = REPO / "packages" / "typescript" / "temporal-yaml"
HARNESS = TS_YAML_PACKAGE / "scripts" / "live-binding-harness.mjs"
RESOLVER_ENTRY = (
    REPO / "packages" / "typescript" / "temporal-controlplane" / "dist" / "resolver-stdio.js"
)

POLICY_YAML = """\
version: '1'
name: base
providers:
  allowed:
    openai:
      models:
        - gpt-4o-mini
"""


def _write_ts_project(tmp_path: Path) -> Path:
    (tmp_path / "typeflux.project.yaml").write_text(
        "version: '1'\n"
        "name: ts-live-demo\n"
        "workflows:\n"
        "  - id: demo\n    path: workflow.yaml\n"
        "environments:\n"
        "  local: environments/local.yaml\n"
        "policies:\n"
        "  base: policies/base.yaml\n"
        "validation:\n"
        "  targets:\n"
        "    local:\n"
        "      workflows: [demo]\n"
        "      environment: local\n"
        "      policies: [base]\n",
        encoding="utf-8",
    )
    (tmp_path / "environments").mkdir()
    (tmp_path / "environments" / "local.yaml").write_text(
        "version: '1'\nname: local\n", encoding="utf-8"
    )
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "base.yaml").write_text(POLICY_YAML, encoding="utf-8")
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
    not LIVE, reason="TYPEFLUX_LIVE_TEMPORAL=1 required (live Temporal + built TS workspace)"
)
def test_python_cp_starts_a_ts_execution_through_the_node_resolver(tmp_path: Path) -> None:
    from typeflux.controlplane.resolver import InProcessPythonResolver, SubprocessResolver

    registry = _write_ts_project(tmp_path)

    # The TS side is ONLY a worker: nothing is started by the TS edition.
    harness = subprocess.Popen(
        ["node", str(HARNESS), str(tmp_path), "--serve-only"],
        cwd=TS_YAML_PACKAGE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    # `--schemas` is the deployment contract (#620): the operator supplies the
    # project's activity IO schemas; bundle/catalog resolution needs them.
    schemas_module = TS_YAML_PACKAGE / "scripts" / "live-binding-schemas.mjs"
    resolver = SubprocessResolver(
        ["node", str(RESOLVER_ENTRY), "--schemas", str(schemas_module)],
        runtime="typescript",
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
            if json.loads(line).get("event") == "ready":
                ready = True
                break
        assert ready, "TS worker harness never became ready"

        app = create_app_from_registry(registry, resolvers=[InProcessPythonResolver(), resolver])
        client = TestClient(app)

        # Capabilities flip: with the resolver, the TS project is startable.
        capabilities = client.get("/api/v1/projects/ts/meta").json()["capabilities"]
        assert capabilities["can_start"] is True and capabilities["can_resolve"] is True

        # 1. START through the PYTHON control plane: the resolver supplies the
        # plan; the receipt carries the ts-plan-argument identity.
        review_id = f"py-start-ts-{int(time.time())}"
        start = client.post(
            "/api/v1/projects/ts/workflows/demo/start",
            json={
                "environment_id": "local",
                "execution_id": review_id,
                "input": {"value": "started-by-python"},
            },
        )
        assert start.status_code == 200, start.text
        receipt = start.json()
        assert receipt["workflow_type"] == "typefluxYamlWorkflow"
        assert receipt["workflow_name"] == "TsLiveDemoWorkflow"
        assert receipt["spec_digest"]

        # 1b. INPUT VALIDATION (#673): a payload violating the workflow's input
        # JSON Schema answers 422 BEFORE any Temporal call, with the parity prefix.
        bad_input = client.post(
            "/api/v1/projects/ts/workflows/demo/start",
            json={
                "environment_id": "local",
                "execution_id": f"{review_id}-bad-input",
                "input": {"value": 42},
            },
        )
        assert bad_input.status_code == 422, bad_input.text
        assert bad_input.json()["message"].startswith("invalid workflow input for Item:")

        # 2. The TS worker executes it: review gate reached, approve, complete.
        _await_status(client, review_id, "waiting_for_review")
        review = client.post(
            "/api/v1/projects/ts/workflows/demo/review",
            json={
                "environment_id": "local",
                "execution_id": review_id,
                "command": {"user_decision": "approve", "reviewer": "py-cp-live"},
            },
        )
        assert review.status_code in (200, 204), review.text
        _await_status(client, review_id, "completed")

        # 3. POLICY through the resolver: the composed hash pins, a wrong hash
        # refuses BEFORE any start, and the matching hash starts.
        bundle = client.get(
            "/api/v1/projects/ts/workflows/demo/bundle",
            params={"environment_id": "local", "policy_ids": ["base"]},
        )
        assert bundle.status_code == 200, bundle.text
        policy_hash = bundle.json()["policy"]["policy_hash"]

        refused = client.post(
            "/api/v1/projects/ts/workflows/demo/start",
            json={
                "environment_id": "local",
                "execution_id": f"{review_id}-refused",
                "input": {"value": "x"},
                "policy_ids": ["base"],
                "expected_policy_hash": "deadbeef",
            },
        )
        assert refused.status_code == 422, refused.text
        assert "does not match expected deployment policy hash" in refused.json()["message"]

        policied_id = f"py-start-ts-policied-{int(time.time())}"
        policied = client.post(
            "/api/v1/projects/ts/workflows/demo/start",
            json={
                "environment_id": "local",
                "execution_id": policied_id,
                "input": {"value": "policied"},
                "policy_ids": ["base"],
                "expected_policy_hash": policy_hash,
            },
        )
        assert policied.status_code == 200, policied.text
        _await_status(client, policied_id, "waiting_for_review")

        # 4. DECOMPOSED VISIBILITY (#671) against the REAL cluster: the listing
        # finds the executions this test started via their identity MEMO over
        # the generic type — both current (they run the pinned digest).
        listing = client.get(
            "/api/v1/projects/ts/workflows/demo/executions",
            params={"environment_id": "local"},
        )
        assert listing.status_code == 200, listing.text
        listed = listing.json()
        assert listed["logical_workflow"] == "TsLiveDemoWorkflow"
        assert listed["current_workflow_type"] == "typefluxYamlWorkflow"
        by_id = {record["execution_id"]: record for record in listed["executions"]}
        assert review_id in by_id and policied_id in by_id
        assert by_id[review_id]["current_version"] is True
        assert by_id[review_id]["workflow_type"] == "typefluxYamlWorkflow"
        assert by_id[review_id]["status"] == "COMPLETED"
        assert by_id[policied_id]["status"] == "RUNNING"

        # 5. VISIBILITY COMPLETION (#686): versions (the drain view) groups the
        # RUNNING executions by memo version identity — the completed run does
        # not count; the policied one still waits for review on the CURRENT
        # digest, so the current key carries at least one running execution.
        versions = client.get(
            "/api/v1/projects/ts/workflows/demo/versions",
            params={"environment_id": "local"},
        )
        assert versions.status_code == 200, versions.text
        drain = versions.json()
        assert drain["logical_workflow"] == "TsLiveDemoWorkflow"
        current_key = f"TsLiveDemoWorkflow.{receipt['spec_digest'][:12]}"
        assert drain["current_workflow_type"] == current_key
        assert drain["running"].get(current_key, 0) >= 1
        assert drain["total_running"] >= 1
        assert "ExecutionStatus = 'Running'" in drain["query"]

        # workers: the harness's TS worker POLLS ts-live-binding — the
        # describe reports real pollers through the driver's own connection.
        workers = client.get(
            "/api/v1/projects/ts/workflows/demo/workers",
            params={"environment_id": "local"},
        )
        assert workers.status_code == 200, workers.text
        assert workers.json()["task_queue"] == "ts-live-binding"
        assert workers.json()["reachable"] is True
        assert workers.json()["workers_polling"] >= 1

        # ...and a queue nobody polls reports zero, reachable (same cluster).
        idle = client.get(
            "/api/v1/projects/ts/workflows/demo/workers",
            params={"environment_id": "local", "task_queue": "nobody-polls-this-queue"},
        )
        assert idle.status_code == 200, idle.text
        assert idle.json() == {
            "task_queue": "nobody-polls-this-queue",
            "reachable": True,
            "workers_polling": 0,
        }

        # correlation: this project's observer is `none` — the trivial
        # reachable shape from the resolver bundle's runtime summary.
        correlation = client.get(
            "/api/v1/projects/ts/workflows/demo/correlation",
            params={"environment_id": "local", "execution_id": review_id},
        )
        assert correlation.status_code == 200, correlation.text
        assert correlation.json() == {
            "execution_id": review_id,
            "observer": "none",
            "reachable": True,
        }

        # connections decompose from the resolver's bundle; deployments answer
        # the TS edition's stub.
        connections = client.get(
            "/api/v1/projects/ts/workflows/demo/connections",
            params={"environment_id": "local"},
        )
        assert connections.status_code == 200, connections.text
        assert connections.json()["registry"] == {"type": "inline", "reachable": True}
        assert connections.json()["observability"]["type"] == "none"
        assert client.get("/api/v1/projects/ts/deployments").json() == []
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
