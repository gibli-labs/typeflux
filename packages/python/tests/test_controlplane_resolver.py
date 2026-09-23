"""The Resolver seam (#619 slice 3): contract-shape pinning + equivalence."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from typeflux.controlplane.resolver import InProcessPythonResolver, Resolver

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "resolver" / "resolver.v1.json").read_text(
        encoding="utf-8"
    )
)
CONFORMANCE_PROJECT = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "controlplane"
    / "conformance"
    / "project"
    / "python"
)


def test_resolver_operations_match_the_contract_surface() -> None:
    # Operation names and parameter names are pinned against the interface
    # document — the same doc-enforcement pattern as the binding suite.
    for operation, spec in CONTRACT["operations"].items():
        method = getattr(Resolver, operation)
        parameters = inspect.signature(method).parameters
        expected = {"self", "manifest_path"} | {
            name for name in spec["params"] if name != "manifest_path"
        }
        assert set(parameters) == expected, operation


@pytest.fixture()
def fixture_project(monkeypatch: pytest.MonkeyPatch) -> str:
    import sys

    for name in tuple(sys.modules):
        if name == "conformance_project" or name.startswith("conformance_project."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(CONFORMANCE_PROJECT))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(CONFORMANCE_PROJECT / "missing.env"))
    return str(CONFORMANCE_PROJECT / "typeflux.project.yaml")


def test_in_process_resolver_equivalent_to_direct_functions(fixture_project: str) -> None:
    from typeflux.project import (
        load_project_spec,
        resolve_workflow_bundle,
        validate_project_bundle,
    )

    resolver = InProcessPythonResolver()
    direct_project = load_project_spec(fixture_project)

    via_seam = resolver.resolve_bundle(
        fixture_project, workflow_id="workflow", environment_id="local"
    )
    direct = resolve_workflow_bundle(direct_project, workflow_id="workflow", environment_id="local")
    assert via_seam.model_dump(exclude_none=True) == direct.model_dump(exclude_none=True)

    seam_report = resolver.validate_project(fixture_project)
    direct_report = validate_project_bundle(direct_project)
    assert seam_report.model_dump(exclude_none=True) == direct_report.model_dump(exclude_none=True)

    catalog = resolver.resolve_catalog(
        fixture_project, workflow_id="workflow", environment_id="local"
    )
    assert catalog.workflow_id == "workflow"
    prompt = resolver.prompt_status(fixture_project, workflow_id="workflow", environment_id="local")
    assert prompt.workflow_id == "workflow"


def test_in_process_resolver_declares_its_runtime() -> None:
    assert InProcessPythonResolver.runtime == "python"
    assert isinstance(InProcessPythonResolver(), Resolver)


def test_injected_resolver_runtime_drives_the_gate(tmp_path, monkeypatch) -> None:
    # An app serving with a typescript resolver honestly resolves ts projects
    # and fails closed on python ones — the gate follows the resolver (#619).
    from fastapi.testclient import TestClient

    from tests.test_controlplane_api import _setup
    from typeflux.controlplane import create_app_from_registry

    manifest = _setup(tmp_path, monkeypatch)
    ts_dir = tmp_path / "ts-project"
    ts_dir.mkdir()
    (ts_dir / "typeflux.project.yaml").write_text(
        "version: '1'\nname: ts-stub\nworkflows:\n  - id: flow\n    path: workflow.yaml\n",
        encoding="utf-8",
    )
    (ts_dir / "workflow.yaml").write_text("placeholder: true\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "version: '1'\n"
        "default: py\n"
        "projects:\n"
        f"  - id: py\n    manifest: {manifest.name}\n"
        "  - id: ts\n    manifest: ts-project/typeflux.project.yaml\n    runtime: typescript\n",
        encoding="utf-8",
    )

    class _FakeTsResolver:
        runtime = "typescript"

        def resolve_bundle(self, manifest_path, **kwargs):  # pragma: no cover - not hit
            raise AssertionError("not exercised")

        resolve_catalog = validate_project = prompt_status = resolve_bundle

    client = TestClient(create_app_from_registry(registry_file, resolvers=[_FakeTsResolver()]))

    assert client.get("/api/v1/projects/ts/meta").json()["capabilities"]["can_resolve"] is True
    assert client.get("/api/v1/projects/py/meta").json()["capabilities"]["can_resolve"] is False
    py_bundle = client.get(
        "/api/v1/projects/py/workflows/workflow/bundle", params={"environment_id": "local"}
    )
    assert py_bundle.status_code == 501
    assert py_bundle.json()["error"] == "UnsupportedRuntime"
    projects = {item["id"]: item["resolvable"] for item in client.get("/api/v1/projects").json()}
    assert projects == {"py": False, "ts": True}


def test_lifecycle_operability_follows_the_resolver(tmp_path, monkeypatch) -> None:
    # The python-versioned-type driver imports modules, so lifecycle routes
    # for python projects stay 501 under a resolver that cannot resolve
    # python (#618 slice 4 review catch); ts projects stay operable
    # (plan-less driver) and their capabilities say so.
    from fastapi.testclient import TestClient

    from tests.test_controlplane_api import _setup
    from typeflux.controlplane import create_app_from_registry

    manifest = _setup(tmp_path, monkeypatch)
    ts_dir = tmp_path / "ts-project"
    ts_dir.mkdir()
    (ts_dir / "typeflux.project.yaml").write_text(
        "version: '1'\nname: ts-stub\nworkflows:\n  - id: flow\n    path: workflow.yaml\n",
        encoding="utf-8",
    )
    (ts_dir / "workflow.yaml").write_text("placeholder: true\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "version: '1'\n"
        "default: py\n"
        "projects:\n"
        f"  - id: py\n    manifest: {manifest.name}\n"
        "  - id: ts\n    manifest: ts-project/typeflux.project.yaml\n    runtime: typescript\n",
        encoding="utf-8",
    )

    class _FakeTsResolver:
        runtime = "typescript"

        def resolve_bundle(self, manifest_path, **kwargs):  # pragma: no cover
            raise AssertionError("not exercised")

        resolve_catalog = validate_project = prompt_status = resolve_bundle

    client = TestClient(create_app_from_registry(registry_file, resolvers=[_FakeTsResolver()]))

    status = client.get(
        "/api/v1/projects/py/workflows/workflow/status",
        params={"environment_id": "local", "execution_id": "x"},
    )
    assert status.status_code == 501
    assert status.json()["error"] == "UnsupportedRuntime"

    py_meta = client.get("/api/v1/projects/py/meta").json()["capabilities"]
    assert py_meta["can_review"] is False and py_meta["can_cancel"] is False
    ts_meta = client.get("/api/v1/projects/ts/meta").json()["capabilities"]
    assert ts_meta["can_review"] is True and ts_meta["can_cancel"] is True


def test_visibility_completion_routes_serve_ts_projects(tmp_path, monkeypatch) -> None:
    # #686 completes the #671 decomposition: versions/workers/correlation no
    # longer 501 for ts projects. No cluster in unit tests, so the versions
    # route's bounded Temporal-tier 503 IS the proof the gate lifted and the
    # DRIVER's memo-scan connection was attempted (not the python type-name
    # path); workers degrades IN-BAND (Python posture — reachable false, 200);
    # correlation is observer-driven and answers the trivial shape for a
    # non-langfuse observer without any Temporal/observer call.
    monkeypatch.setenv("TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS", "2")
    client = _ts_client(
        tmp_path,
        monkeypatch,
        bundle_runtime={
            "temporal": {"address": "localhost:7233"},
            "registry": {"type": "inline", "host": None},
            "observability": {"type": "none"},
        },
    )

    versions = client.get("/api/v1/workflows/workflow/versions", params={"environment_id": "local"})
    assert versions.status_code == 503, versions.text
    assert versions.json()["error"] == "TemporalUnavailable"
    assert "drain view" in versions.json()["message"]

    workers = client.get("/api/v1/workflows/workflow/workers", params={"environment_id": "local"})
    assert workers.status_code == 200, workers.text
    body = workers.json()
    # The queue is the RESOLVED PLAN's (the fake resolver's "q"), proving the
    # driver path — the python loader would have imported project modules.
    assert body["task_queue"] == "q"
    assert body["reachable"] is False
    assert body["workers_polling"] == 0
    assert body["detail"].startswith("could not reach Temporal: ")

    # An explicit override targets that queue (still through the driver).
    overridden = client.get(
        "/api/v1/workflows/workflow/workers",
        params={"environment_id": "local", "task_queue": "panel-queue"},
    )
    assert overridden.status_code == 200
    assert overridden.json()["task_queue"] == "panel-queue"

    correlation = client.get(
        "/api/v1/workflows/workflow/correlation",
        params={"environment_id": "local", "execution_id": "e-1"},
    )
    assert correlation.status_code == 200, correlation.text
    # Python's exclude_none shape: trace/warning OMIT for the trivial answer.
    assert correlation.json() == {"execution_id": "e-1", "observer": "none", "reachable": True}


def test_ts_correlation_defaults_to_observer_none_without_a_summary(tmp_path, monkeypatch) -> None:
    # A bundle whose runtime summary lacks the observability block reads
    # observer "none" (both editions' specs default an absent block to none) —
    # the trivial reachable shape, never a guessed backend lookup.
    client = _ts_client(tmp_path, monkeypatch, bundle_runtime=None)

    correlation = client.get(
        "/api/v1/workflows/workflow/correlation",
        params={"environment_id": "local", "execution_id": "e-2"},
    )
    assert correlation.status_code == 200, correlation.text
    assert correlation.json() == {"execution_id": "e-2", "observer": "none", "reachable": True}


def _ts_registry_with_workflow(tmp_path, monkeypatch):
    """A ts-runtime registry whose project has a REAL workflow yaml (raw-YAML operable)."""
    from tests.test_controlplane_api import _setup

    manifest = _setup(tmp_path, monkeypatch)
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "version: '1'\n"
        "default: ts\n"
        "projects:\n"
        f"  - id: ts\n    manifest: {manifest.name}\n    runtime: typescript\n",
        encoding="utf-8",
    )
    return registry_file


class _FakeStartTsResolver:
    """A full fake ts resolver: plan + bundle (with an input JSON Schema)."""

    runtime = "typescript"

    def __init__(self, *, input_schema: object = None, bundle_runtime: object = None) -> None:
        self._input_schema = input_schema
        self._bundle_runtime = bundle_runtime

    def resolve_plan(self, manifest_path, *, workflow_id, environment_id):
        from typeflux.controlplane.resolver import ResolvedPlan

        self.resolve_plan_calls = getattr(self, "resolve_plan_calls", 0) + 1
        return ResolvedPlan.model_validate(
            {
                "plan": {"steps": [{"id": "s"}]},
                "task_queue": "q",
                "spec_digest": "d1",
                "workflow_name": "ControlPlaneDemoWorkflow",
                "version_label": None,
                "search_attribute": None,
            }
        )

    def resolve_bundle(
        self, manifest_path, *, workflow_id, environment_id, policy_ids=(), deployment_image=None
    ):
        from types import SimpleNamespace

        if isinstance(self._input_schema, Exception):
            raise self._input_schema
        return SimpleNamespace(
            workflow=SimpleNamespace(input_schema=self._input_schema),
            policy=None,
            runtime=self._bundle_runtime,
        )

    def validate_project(
        self, manifest_path, *, environment_id=None, workflow_ids=(), policy_ids=()
    ):
        from types import SimpleNamespace

        return SimpleNamespace(issues=(), resolved_workflows=(SimpleNamespace(checks=()),))


def _start(client, payload):
    return client.post(
        "/api/v1/workflows/workflow/start",
        json={"environment_id": "local", "execution_id": "e-1", "input": payload},
    )


def test_ts_start_validates_input_against_the_pinned_json_schema(tmp_path, monkeypatch) -> None:
    # #673: a foreign-edition start validates against the bundle's input JSON
    # Schema BEFORE the Temporal tier — 422 with the parity prefix on mismatch.

    from fastapi.testclient import TestClient

    from typeflux.controlplane import create_app_from_registry
    from typeflux.controlplane.resolver import InProcessPythonResolver

    registry_file = _ts_registry_with_workflow(tmp_path, monkeypatch)
    schema = {
        "name": "ClaimInput",
        "hash": "h1",
        "json_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    client = TestClient(
        create_app_from_registry(
            registry_file,
            resolvers=[InProcessPythonResolver(), _FakeStartTsResolver(input_schema=schema)],
        )
    )

    # Invalid input → 422 with the cross-edition parity prefix, pre-dispatch.
    bad = _start(client, {"value": 42})
    assert bad.status_code == 422, bad.text
    assert bad.json()["message"].startswith("invalid workflow input for ClaimInput:")

    missing = _start(client, {})
    assert missing.status_code == 422
    assert "invalid workflow input for ClaimInput" in missing.json()["message"]

    # Valid input passes validation and reaches the TEMPORAL tier (503 — no
    # cluster in unit tests; the bounded connect is the proof the gate opened).
    monkeypatch.setenv("TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS", "2")
    ok = _start(client, {"value": "fine"})
    assert ok.status_code == 503, ok.text
    assert ok.json()["error"] == "TemporalUnavailable"


def test_ts_start_without_a_schema_fails_closed_like_the_ts_edition(tmp_path, monkeypatch) -> None:
    # A schema-less resolver (bundle 422s / carries no json_schema): start must
    # refuse — never dispatch unvalidated silently (the TS edition's posture).
    from fastapi.testclient import TestClient

    from typeflux.controlplane import create_app_from_registry
    from typeflux.controlplane.resolver import InProcessPythonResolver

    registry_file = _ts_registry_with_workflow(tmp_path, monkeypatch)

    # Case 1: the bundle fetch itself fails (e.g. "requires an injected schema").
    failing = _FakeStartTsResolver(
        input_schema=RuntimeError("resolved bundle requires an injected schema")
    )
    client = TestClient(
        create_app_from_registry(registry_file, resolvers=[InProcessPythonResolver(), failing])
    )
    refused = _start(client, {"value": "x"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["message"].startswith("starting requires the workflow's input schema")
    assert "requires an injected schema" in refused.json()["message"]

    # Case 2: the bundle resolves but carries no json_schema dict.
    hollow = _FakeStartTsResolver(input_schema={"name": "X", "hash": "h"})
    client = TestClient(
        create_app_from_registry(registry_file, resolvers=[InProcessPythonResolver(), hollow])
    )
    refused = _start(client, {"value": "x"})
    assert refused.status_code == 422
    assert refused.json()["message"].startswith("starting requires the workflow's input schema")

    # Lifecycle ops stay plan-less-operable: the schema pin failure never
    # blocks driver construction (status 503s at the Temporal tier, not 422).
    monkeypatch.setenv("TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS", "2")
    status = client.get(
        "/api/v1/workflows/workflow/status",
        params={"environment_id": "local", "execution_id": "e-1"},
    )
    assert status.status_code == 503, status.text


def _ts_client(tmp_path, monkeypatch, **resolver_kwargs):
    from fastapi.testclient import TestClient

    from typeflux.controlplane import create_app_from_registry
    from typeflux.controlplane.resolver import InProcessPythonResolver

    registry_file = _ts_registry_with_workflow(tmp_path, monkeypatch)
    resolver = _FakeStartTsResolver(**resolver_kwargs)
    client = TestClient(
        create_app_from_registry(registry_file, resolvers=[InProcessPythonResolver(), resolver])
    )
    client.ts_resolver = resolver  # for per-request-freshness assertions
    return client


def test_ts_executions_dispatch_to_the_binding_driver(tmp_path, monkeypatch) -> None:
    # #671: executions for a ts project no longer 501 — the route pins the
    # ts-plan-argument driver and lists by memo identity. No cluster in unit
    # tests, so the bounded Temporal-tier 503 IS the proof the gate lifted and
    # the driver's connection was attempted (not the python type-name path).
    monkeypatch.setenv("TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS", "2")
    client = _ts_client(tmp_path, monkeypatch)

    response = client.get(
        "/api/v1/workflows/workflow/executions", params={"environment_id": "local"}
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "TemporalUnavailable"
    assert "listing executions" in response.json()["message"]

    # PER-REQUEST FRESH (codex P2): each listing re-resolves the plan — never
    # the mutating-ops pin cache, so YAML edits reflect without a repin.
    resolver = client.ts_resolver
    first_calls = resolver.resolve_plan_calls
    client.get("/api/v1/workflows/workflow/executions", params={"environment_id": "local"})
    assert resolver.resolve_plan_calls == first_calls + 1


def test_ts_connections_come_from_the_resolver_bundle(tmp_path, monkeypatch) -> None:
    # #671: connections for a ts project decompose from resolve_bundle's
    # runtime summary — registry type/host + observability flags pass through
    # (redaction_enabled=False proves passthrough, not a hardcoded default).
    client = _ts_client(
        tmp_path,
        monkeypatch,
        bundle_runtime={
            "temporal": {"address": "localhost:7233"},
            "registry": {"type": "inline", "host": None},
            "observability": {
                "type": "none",
                "execution_manifest": True,
                "redaction_enabled": False,
            },
        },
    )

    response = client.get(
        "/api/v1/workflows/workflow/connections", params={"environment_id": "local"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["registry"] == {"type": "inline", "reachable": True}
    assert body["observability"]["type"] == "none"
    assert body["observability"]["reachable"] is True
    assert body["observability"]["execution_manifest"] is True
    assert body["observability"]["redaction_enabled"] is False


def test_ts_connections_fail_closed_without_a_registry_summary(tmp_path, monkeypatch) -> None:
    # A bundle without the registry summary is a resolver-emission bug: 422,
    # never a probed guess or an AttributeError 500.
    client = _ts_client(tmp_path, monkeypatch, bundle_runtime={"observability": {"type": "none"}})

    response = client.get(
        "/api/v1/workflows/workflow/connections", params={"environment_id": "local"}
    )
    assert response.status_code == 422, response.text
    assert "no runtime registry summary" in response.json()["message"]


def test_ts_deployments_answer_the_ts_editions_stub(tmp_path, monkeypatch) -> None:
    # #671: deployments for a ts project mirror the TS edition's stub — an
    # empty list, and every plan id unknown (handlers.ts parity).
    client = _ts_client(tmp_path, monkeypatch)

    listing = client.get("/api/v1/deployments")
    assert listing.status_code == 200, listing.text
    assert listing.json() == []

    detail = client.get("/api/v1/deployments/any-plan")
    assert detail.status_code == 404, detail.text
    assert detail.json()["message"] == "unknown deployment plan: any-plan"
