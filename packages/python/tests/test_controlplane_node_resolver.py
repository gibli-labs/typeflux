"""The REAL Node subprocess resolver behind the Python control plane (#642).

Node-gated integration (skipped when node or the built TS dist is absent —
the CI python job ships neither; they run locally and in the gated live lane):
the Python CP, configured with a ``SubprocessResolver`` spawning the compiled
``resolver-stdio.js``, serves the conformance TS project's resolution routes
that answer 501 ``UnsupportedRuntime`` without it — one CP, both editions.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RESOLVER_ENTRY = (
    REPO / "packages" / "typescript" / "temporal-controlplane" / "dist" / "resolver-stdio.js"
)
TS_PROJECT_DIR = REPO / "contracts" / "controlplane" / "conformance" / "project" / "typescript"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not RESOLVER_ENTRY.is_file(),
    reason="needs node + the built TS dist (cd packages/typescript && pnpm -r build)",
)


def _node_resolver():
    from typeflux.controlplane.resolver import SubprocessResolver

    return SubprocessResolver(
        ["node", str(RESOLVER_ENTRY), "--conformance-schemas"], runtime="typescript"
    )


@pytest.fixture()
def registry_file(tmp_path: Path) -> Path:
    registry = tmp_path / "typeflux.projects.yaml"
    registry.write_text(
        "version: '1'\n"
        "default: ts\n"
        "projects:\n"
        f"  - id: ts\n    manifest: {TS_PROJECT_DIR / 'typeflux.project.yaml'}\n"
        "    runtime: typescript\n",
        encoding="utf-8",
    )
    return registry


def test_the_node_resolver_resolves_the_conformance_ts_project_directly() -> None:
    resolver = _node_resolver()
    try:
        manifest = str(TS_PROJECT_DIR / "typeflux.project.yaml")
        resolved = resolver.resolve_plan(manifest, workflow_id="workflow", environment_id="local")
        assert resolved.workflow_name == "ConformanceDemoWorkflow"
        assert resolved.task_queue == "conformance-demo-queue"
        assert resolved.plan  # the raw plan, opaque but present
        bundle = resolver.resolve_bundle(manifest, workflow_id="workflow", environment_id="local")
        # The TS edition's DTO validates into the Python model — cross-edition parity.
        assert bundle.workflow.workflow_type == "typefluxYamlWorkflow"
        assert bundle.workflow.spec_digest == resolved.spec_digest
    finally:
        resolver.close()


def test_the_python_cp_serves_ts_resolution_routes_through_the_node_resolver(
    registry_file: Path,
) -> None:
    from fastapi.testclient import TestClient

    from typeflux.controlplane import create_app_from_registry
    from typeflux.controlplane.resolver import InProcessPythonResolver

    resolver = _node_resolver()
    try:
        app = create_app_from_registry(
            registry_file, resolvers=[InProcessPythonResolver(), resolver]
        )
        client = TestClient(app)

        # Capabilities flip: the ts project is resolvable (can_start/can_resolve).
        capabilities = client.get("/api/v1/projects/ts/meta").json()["capabilities"]
        assert capabilities["can_resolve"] is True
        assert capabilities["can_start"] is True

        # The routes that 501 without a resolver now serve real DTOs.
        bundle = client.get(
            "/api/v1/projects/ts/workflows/workflow/bundle",
            params={"environment_id": "local"},
        )
        assert bundle.status_code == 200
        assert bundle.json()["workflow"]["workflow_type"] == "typefluxYamlWorkflow"

        catalog = client.get(
            "/api/v1/projects/ts/workflows/workflow/catalog",
            params={"environment_id": "local"},
        )
        assert catalog.status_code == 200
        assert {activity["name"] for activity in catalog.json()["activities"]} == {
            "assess_item",
            "summarize",
            "decide",
        }

        # Decomposed visibility (#671): connections come from the REAL node
        # resolver's bundle runtime summary — no Temporal, no python loader.
        connections = client.get(
            "/api/v1/projects/ts/workflows/workflow/connections",
            params={"environment_id": "local"},
        )
        assert connections.status_code == 200, connections.text
        body = connections.json()
        assert body["registry"] == {"type": "inline", "reachable": True}
        assert body["observability"]["type"] == "none"
        assert body["observability"]["execution_manifest"] is True
        assert body["observability"]["redaction_enabled"] is True

        # Deployments mirror the TS edition's stub: empty list, per-id 404.
        assert client.get("/api/v1/projects/ts/deployments").json() == []
        unknown_plan = client.get("/api/v1/projects/ts/deployments/x")
        assert unknown_plan.status_code == 404
        assert unknown_plan.json()["message"] == "unknown deployment plan: x"

        # A structured resolver failure forwards VERBATIM (status + ApiError).
        missing = client.get(
            "/api/v1/projects/ts/workflows/nope/bundle",
            params={"environment_id": "local"},
        )
        assert missing.status_code == 404
        assert missing.json() == {
            "error": "NotFound",
            "message": "unknown project workflow: nope",
        }
    finally:
        resolver.close()
