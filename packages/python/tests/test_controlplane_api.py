from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from typeflux.controlplane import __main__ as controlplane_cli
from typeflux.controlplane import check_conformance, create_app, render_openapi_spec
from typeflux.controlplane.auth import (
    Permission,
    ProxyHeaderAuthorizer,
    TokenAuthorizer,
    TokenGrant,
)
from typeflux.core.contracts import ReviewCommand, WorkflowLifecycleStatus
from typeflux.core.errors import LifecycleBindingError, TypefluxError
from typeflux.project import (
    ProjectPolicyEnforcementError,
    WorkflowDrainStatus,
    WorkflowMigrateResult,
    WorkflowOperationStatus,
    WorkflowStartReceipt,
    load_project_spec,
    resolve_activity_catalog,
    resolve_workflow_bundle,
    validate_project_bundle,
)

_SECRET_SENTINEL = "sk-test-controlplane-leak-sentinel"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(sys.modules):
        if name == "controlplane_project" or name.startswith("controlplane_project."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SECRET_SENTINEL)
    package = tmp_path / "controlplane_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class ClaimItem(BaseModel):
            value: str


        class ClaimInput(BaseModel):
            claims: list[ClaimItem]


        class ItemAssessment(BaseModel):
            value: str


        class AssessmentBatch(BaseModel):
            reviews: list[ItemAssessment]


        class Decision(BaseModel):
            value: str
        """,
    )
    _write(
        package / "activities.py",
        """
        from temporalio import activity

        from controlplane_project.schemas import AssessmentBatch, Decision


        @activity.defn(name="decide")
        async def decide(value: AssessmentBatch) -> Decision:
            return Decision(value=str(len(value.reviews)))
        """,
    )
    _write(
        tmp_path / "workflow.yaml",
        """
        project: controlplane_project
        name: controlplane_demo
        task_queue: controlplane-demo-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              assess: assess {{value}}
              summarize: summarize {{reviews}}
          provider:
            type: anthropic
            model: claude-sonnet-4-6
            api_key:
              value_from:
                env: ANTHROPIC_API_KEY
                required: false
          observability:
            type: none
        activities:
          modules: [activities]
          definitions:
            - name: assess_item
              input: schemas:ClaimItem
              output: schemas:ItemAssessment
              prompt: assess
            - name: summarize
              input: schemas:AssessmentBatch
              output: schemas:AssessmentBatch
              prompt: summarize
        workflow:
          name: ControlPlaneDemoWorkflow
          input: schemas:ClaimInput
          output: schemas:Decision
          lifecycle:
            enabled: true
            review:
              after_step: assess_items
              invalid_user_decision: warn
              user_decisions:
                approve:
                  route: summarize
                fast_track:
                  route: decide
          steps:
            - id: assess_items
              map:
                activity: assess_item
                over: input.claims
                concurrency: 2
                collect:
                  output: schemas:AssessmentBatch
                  field: reviews
            - id: summarize
              activity: summarize
            - id: decide
              activity: decide
        """,
    )
    manifest = tmp_path / "typeflux.project.yaml"
    _write(
        manifest,
        """
        version: "1"
        name: controlplane-demo
        workflows:
          - id: workflow
            path: workflow.yaml
          - id: broken
            path: workflow.yaml
            profiles:
              provider: missing-profile
        environments:
          local: environments/local.yaml
        """,
    )
    _write(
        tmp_path / "environments" / "local.yaml",
        """
        version: "1"
        name: local
        """,
    )
    return manifest


def test_meta_reports_contract_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/meta")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "1"
    assert payload["bundle_version"] == "1"
    assert payload["catalog_version"] == "1"
    assert payload["project"] == "controlplane-demo"
    # Open authorizer: no trusted proxy identity to surface (#577).
    assert payload["caller_identity"] is None


_VALID_START_BODY = {
    "environment_id": "local",
    "execution_id": "exec-1",
    "input": {},
}


def test_meta_capabilities_open_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    capabilities = client.get("/api/v1/meta").json()["capabilities"]

    assert capabilities == {
        "can_start": True,
        "can_review": True,
        "can_cancel": True,
        "can_refresh_project": True,
        "can_resolve": True,
        "enforcement_events": True,
        # A single local manifest records no git source, so there is no github repo
        # provenance to serve — the capability is False even though the project resolves.
        "github_provenance": False,
    }


def _token_app(manifest: Path) -> TestClient:
    authorizer = TokenAuthorizer(
        {
            "read-tok": TokenGrant(name="reader", permissions=frozenset({Permission.INSPECT})),
            "op-tok": TokenGrant(name="operator", permissions=frozenset(Permission)),
        }
    )
    return TestClient(create_app(manifest, authorizer=authorizer))


def test_token_auth_denies_reads_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _token_app(_setup(tmp_path, monkeypatch))

    response = client.get("/api/v1/meta")

    assert response.status_code == 403
    assert response.json()["error"] == "Forbidden"


def test_token_auth_fails_closed_on_duplicate_authorization_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Through the REAL ASGI stack (TestClient → Starlette Headers), not the unit-level
    # multidict double in test_controlplane_auth.py — so a future middleware/wrapper that
    # flattened headers back to first-wins would fail here, mirroring the TS edition's
    # socket-level duplicate-wire-line test (#738 follow-up).
    client = _token_app(_setup(tmp_path, monkeypatch))

    duplicated = client.get(
        "/api/v1/meta",
        headers=[("Authorization", "Bearer read-tok"), ("Authorization", "Bearer op-tok")],
    )
    assert duplicated.status_code == 403
    assert duplicated.json()["error"] == "Forbidden"

    smuggled = client.get(
        "/api/v1/meta",
        headers=[("Authorization", "Bearer read-tok"), ("Authorization", "Bearer junk")],
    )
    assert smuggled.status_code == 403


def test_token_auth_read_only_token_inspects_but_cannot_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _token_app(_setup(tmp_path, monkeypatch))
    headers = {"Authorization": "Bearer read-tok"}

    meta = client.get("/api/v1/meta", headers=headers)
    assert meta.status_code == 200
    # Token mode: the actor id is the grant NAME ("reader"), which is config, not an
    # identity — it is never echoed as caller_identity (#577).
    assert meta.json()["caller_identity"] is None
    assert meta.json()["capabilities"] == {
        "can_start": False,
        "can_review": False,
        "can_cancel": False,
        "can_refresh_project": False,
        "can_resolve": True,
        "enforcement_events": True,
        "github_provenance": False,
    }

    # The auth dependency denies before the operation runs.
    start = client.post("/api/v1/workflows/workflow/start", json=_VALID_START_BODY, headers=headers)
    assert start.status_code == 403
    assert "start" in start.json()["message"]

    refresh = client.post("/api/v1/projects/default/refresh", headers=headers)
    assert refresh.status_code == 403


def test_token_auth_operator_token_passes_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _token_app(_setup(tmp_path, monkeypatch))
    headers = {"Authorization": "Bearer op-tok"}

    assert client.get("/api/v1/meta", headers=headers).json()["capabilities"]["can_start"] is True

    # Authorization passes (operator has start); the unknown workflow then 404s,
    # proving the request got past the auth gate without touching Temporal.
    start = client.post(
        "/api/v1/workflows/does-not-exist/start", json=_VALID_START_BODY, headers=headers
    )
    assert start.status_code == 404


def test_proxy_header_authorizer_grants_from_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(
        create_app(_setup(tmp_path, monkeypatch), authorizer=ProxyHeaderAuthorizer())
    )

    denied = client.post("/api/v1/workflows/workflow/start", json=_VALID_START_BODY)
    assert denied.status_code == 403  # no permissions header → no grants

    allowed = client.post(
        "/api/v1/workflows/does-not-exist/start",
        json=_VALID_START_BODY,
        headers={"X-Typeflux-Actor": "alice", "X-Typeflux-Permissions": "inspect,start"},
    )
    assert allowed.status_code == 404  # granted start → past the gate


def test_meta_caller_identity_only_from_trusted_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under --trust-proxy-auth the actor header is a vouched-for principal — echo it (#577).
    client = TestClient(
        create_app(_setup(tmp_path, monkeypatch), authorizer=ProxyHeaderAuthorizer())
    )

    present = client.get(
        "/api/v1/meta",
        headers={"X-Typeflux-Actor": "alice", "X-Typeflux-Permissions": "inspect"},
    )
    assert present.status_code == 200
    assert present.json()["caller_identity"] == "alice"

    # An EMPTY actor header is no identity, not the literal "" — maps to null (the
    # truthiness porting trap the TS edition must match). The permissions header still
    # grants inspect so the read is authorized and we observe the identity, not a 403.
    empty = client.get(
        "/api/v1/meta",
        headers={"X-Typeflux-Actor": "", "X-Typeflux-Permissions": "inspect"},
    )
    assert empty.status_code == 200
    assert empty.json()["caller_identity"] is None

    # A DUPLICATED actor header (a proxy that appends rather than replaces let a
    # client-smuggled copy through) is fail-closed to null — never "first wins",
    # which would echo the attacker's value (#577 hardening).
    duplicated = client.get(
        "/api/v1/meta",
        headers=[
            ("X-Typeflux-Actor", "mallory"),
            ("X-Typeflux-Actor", "alice"),
            ("X-Typeflux-Permissions", "inspect"),
        ],
    )
    assert duplicated.status_code == 200
    assert duplicated.json()["caller_identity"] is None

    # Whitespace-only is not an identity either — trimmed, then empty → null.
    blank = client.get(
        "/api/v1/meta",
        headers={"X-Typeflux-Actor": "   ", "X-Typeflux-Permissions": "inspect"},
    )
    assert blank.status_code == 200
    assert blank.json()["caller_identity"] is None

    # A duplicated PERMISSIONS header under proxy auth reads as absent: no grants at
    # all, so even the read is a 403 (fail closed beats both first-wins and union).
    escalated = client.get(
        "/api/v1/meta",
        headers=[
            ("X-Typeflux-Actor", "alice"),
            ("X-Typeflux-Permissions", "inspect"),
            ("X-Typeflux-Permissions", "*"),
        ],
    )
    assert escalated.status_code == 403


def test_discovery_lists_workflows_and_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    workflows = client.get("/api/v1/workflows")
    environments = client.get("/api/v1/environments")

    assert workflows.status_code == 200
    assert [entry["id"] for entry in workflows.json()["workflows"]] == ["workflow", "broken"]
    assert workflows.json()["workflows"][1]["profiles"] == {"provider": "missing-profile"}
    assert environments.status_code == 200
    assert environments.json()["environments"] == [
        {"id": "local", "path": "environments/local.yaml"}
    ]


def test_validate_response_matches_library_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))
    project = load_project_spec(manifest)

    plain = client.get("/api/v1/validate")
    scoped = client.get(
        "/api/v1/validate",
        params={"environment_id": "local", "workflow_id": ["workflow"]},
    )

    assert plain.status_code == 200
    assert plain.json() == validate_project_bundle(project).model_dump(
        mode="json", exclude_none=True
    )
    assert scoped.status_code == 200
    assert scoped.json() == validate_project_bundle(
        project, environment_id="local", workflow_ids=("workflow",)
    ).model_dump(mode="json", exclude_none=True)


def test_bundle_response_matches_library_payload_and_carries_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))
    project = load_project_spec(manifest)

    response = client.get("/api/v1/workflows/workflow/bundle", params={"environment_id": "local"})

    assert response.status_code == 200
    expected = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    payload = response.json()
    assert payload == expected
    topology = payload["topology"]
    assert [node["kind"] for node in topology["nodes"]] == ["map", "activity", "activity"]
    assert {edge["kind"] for edge in topology["edges"]} == {"sequential", "review"}


def test_catalog_response_matches_library_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))
    project = load_project_spec(manifest)

    response = client.get("/api/v1/workflows/workflow/catalog", params={"environment_id": "local"})

    assert response.status_code == 200
    assert (
        response.json()
        == resolve_activity_catalog(
            project, workflow_id="workflow", environment_id="local"
        ).to_dict()
    )


def test_unknown_ids_return_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    missing_workflow = client.get(
        "/api/v1/workflows/missing/bundle", params={"environment_id": "local"}
    )
    missing_environment = client.get(
        "/api/v1/workflows/workflow/catalog", params={"environment_id": "missing"}
    )

    assert missing_workflow.status_code == 404
    assert missing_workflow.json() == {
        "error": "NotFound",
        "message": "unknown project workflow: missing",
    }
    assert missing_environment.status_code == 404
    assert missing_environment.json() == {
        "error": "NotFound",
        "message": "unknown project environment: missing",
    }


def test_config_errors_map_to_unprocessable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/workflows/broken/bundle", params={"environment_id": "local"})

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "ProjectProfileError"
    assert "missing-profile" in payload["message"]


def test_missing_required_parameter_uses_error_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/workflows/workflow/bundle")

    assert response.status_code == 422
    assert response.json()["error"] == "RequestValidationError"


def test_runtime_typeflux_errors_map_to_server_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    app = create_app(manifest)

    @app.get("/api/v1/_boom")
    def boom() -> dict:
        raise TypefluxError("runtime failure")

    response = TestClient(app).get("/api/v1/_boom")

    assert response.status_code == 500
    assert response.json() == {"error": "TypefluxError", "message": "runtime failure"}


def test_validate_reports_composition_ceiling_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CP admission surface (/validate → validate_project_bundle) carries the new
    # composition ceiling check (#298 gate: TS/py CP operate guard). Binding a policy
    # whose max_steps the demo workflow (3 flattened steps) exceeds fails the workflow.
    manifest = _setup(tmp_path, monkeypatch)
    (tmp_path / "policies").mkdir()
    _write(
        tmp_path / "policies" / "tight.yaml",
        """
        version: "1"
        name: tight
        composition:
          max_steps: 2
        """,
    )
    _write(
        manifest,
        """
        version: "1"
        name: controlplane-demo
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: environments/local.yaml
        policies:
          tight: policies/tight.yaml
        validation:
          targets:
            local:
              environment: local
              workflows: [workflow]
              policies: [tight]
        """,
    )
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/validate", params={"environment_id": "local"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    resolved = next(w for w in payload["resolved_workflows"] if w["workflow_id"] == "workflow")
    composition = next(c for c in resolved["checks"] if c["code"] == "policy_composition_ceilings")
    assert composition["status"] == "failed"
    assert "flattened step count 3 exceeds composition ceiling 2" in composition["message"]


def test_secret_values_never_appear_in_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    bundle = client.get("/api/v1/workflows/workflow/bundle", params={"environment_id": "local"})
    catalog = client.get("/api/v1/workflows/workflow/catalog", params={"environment_id": "local"})
    validate = client.get("/api/v1/validate", params={"environment_id": "local"})

    for response in (bundle, catalog, validate):
        assert response.status_code == 200
        assert _SECRET_SENTINEL not in response.text
    # The secret *reference* (env var name) stays inspectable.
    assert "ANTHROPIC_API_KEY" in bundle.text


def test_emitted_schema_conforms_to_contract() -> None:
    # The normative contract lives under contracts/controlplane/ (#616); the
    # server conforms to it. A divergence fails with the structured
    # path-by-path report, never a raw string diff.
    report = check_conformance()
    assert report.conformant, report.render()


def test_project_scoped_operations_declare_project_path_param() -> None:
    # #354: routes mounted under /projects/{project} read the project from a
    # ContextVar, not a signature param, so FastAPI omitted the `project` path
    # parameter — a typed client then can't supply it. Every operation whose path
    # templates {project} must declare it as a path parameter.
    spec = json.loads(render_openapi_spec())
    http_methods = {"get", "put", "post", "delete", "patch"}
    missing = [
        f"{method.upper()} {path}"
        for path, item in spec["paths"].items()
        if "{project}" in path
        for method, op in item.items()
        if method in http_methods
        and not any(
            p.get("name") == "project" and p.get("in") == "path" for p in op.get("parameters", [])
        )
    ]
    assert missing == [], f"operations missing the project path param: {missing}"
    # And the unprefixed (default-project) twins must NOT carry a project param.
    unprefixed = spec["paths"]["/api/v1/workflows/{workflow_id}/status"]["get"]
    assert not any(p.get("name") == "project" for p in unprefixed.get("parameters", []))


def test_project_param_injection_is_idempotent() -> None:
    # #354: the injector mutates the cached app.openapi_schema in place; FastAPI
    # returns that same dict on every .openapi() call (Swagger UI hits it per page
    # load). The skip guard must keep exactly one project param across calls.
    app = create_app("typeflux.project.yaml")
    first = app.openapi()
    second = app.openapi()
    assert first is second  # cached, same object
    op = second["paths"]["/api/v1/projects/{project}/workflows/{workflow_id}/status"]["get"]
    project_params = [
        p for p in op["parameters"] if p.get("name") == "project" and p.get("in") == "path"
    ]
    assert len(project_params) == 1


def test_cli_exports_openapi_spec(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_path = tmp_path / "openapi.json"

    exit_code = controlplane_cli.main(["openapi", "--out", str(out_path)])
    stdout_code = controlplane_cli.main(["openapi"])

    assert exit_code == 0
    assert out_path.read_text(encoding="utf-8") == render_openapi_spec()
    assert stdout_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["info"]["title"] == "Typeflux Control Plane API"
    assert payload["info"]["version"] == "1"


def test_cli_serve_requires_exactly_one_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # serve takes either a manifest or --registry, not both and not neither.
    # The selection is validated before uvicorn is imported, so this passes
    # even where the optional server dependency is absent (e.g. CI base env).
    with pytest.raises(SystemExit):
        controlplane_cli.main(["serve"])
    with pytest.raises(SystemExit):
        controlplane_cli.main(["serve", "manifest.yaml", "--registry", "reg.yaml"])


def test_cli_serve_with_registry_builds_registry_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        f"projects:\n  - id: solo\n    manifest: {manifest}\n", encoding="utf-8"
    )
    # Inject a stand-in uvicorn so the test runs without the optional server
    # dependency installed (the CLI imports uvicorn only to run the app).
    served: dict[str, object] = {}
    fake_uvicorn = ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **k: served.update(app=app, kwargs=k)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    exit_code = controlplane_cli.main(["serve", "--registry", str(registry_file), "--port", "9001"])

    assert exit_code == 0
    # The served app exposes the registry surface.
    client = TestClient(served["app"])  # type: ignore[arg-type]
    assert [p["id"] for p in client.get("/api/v1/projects").json()] == ["solo"]
    assert served["kwargs"]["port"] == 9001


class _FakeInput(BaseModel):
    value: str


class _FakeOperations:
    """Stands in for WorkflowOperations at the API seam.

    The domain behavior (policy fail-closed, traced/untraced queries,
    metadata hygiene) is tested at the operations layer; these tests cover
    the API's routing, serialization, coercion, caching, and error mapping.
    """

    calls: ClassVar[list[dict[str, Any]]] = []
    recorded: ClassVar[list[tuple[str, dict[str, Any]]]] = []
    instances: ClassVar[list[_FakeOperations]] = []
    raise_on_build: ClassVar[Exception | None] = None
    raise_on_status: ClassVar[Exception | None] = None

    def __init__(self) -> None:
        class _Workflow:
            __typeflux_spec_digest__ = "fake-pinned-digest"

            @staticmethod
            def run(input_value):  # annotations assigned below
                raise NotImplementedError

        _Workflow.run.__annotations__ = {"input_value": _FakeInput, "return": _FakeInput}
        self.runtime = SimpleNamespace(workflow_class=_Workflow)
        self.shutdown_called = False
        type(self).instances.append(self)

    @classmethod
    async def for_project_workflow(
        cls,
        project,
        *,
        workflow_id,
        environment_id,
        policy_ids=(),
        expected_policy_hash=None,
        **_kwargs,
    ):
        if cls.raise_on_build is not None:
            raise cls.raise_on_build
        cls.calls.append(
            {
                "workflow_id": workflow_id,
                "environment_id": environment_id,
                "policy_ids": policy_ids,
                "expected_policy_hash": expected_policy_hash,
            }
        )
        return cls()

    async def start(self, input_value, *, workflow_id, task_queue=None, **start_kwargs):
        type(self).recorded.append(
            (
                "start",
                {
                    "input_value": input_value,
                    "workflow_id": workflow_id,
                    "task_queue": task_queue,
                },
            )
        )
        return WorkflowStartReceipt(
            workflow_id=workflow_id,
            run_id="run-1",
            workflow_name="ControlPlaneDemoWorkflow",
            workflow_type="ControlPlaneDemoWorkflow.abc123def456",
            spec_digest="a" * 64,
            task_queue=task_queue or "controlplane-demo-queue",
            trace_query_hint={"workflow_id": workflow_id, "limit": 1},
        )

    async def status(self, execution_id, *, run_id=None, trace=False):
        type(self).recorded.append(
            ("status", {"execution_id": execution_id, "run_id": run_id, "trace": trace})
        )
        if type(self).raise_on_status is not None:
            raise type(self).raise_on_status
        return WorkflowOperationStatus(
            workflow_id=execution_id,
            run_id=run_id,
            status=WorkflowLifecycleStatus(state="waiting_review", current_step="assess_items"),
            valid_user_decisions={"approve": "summarize", "fast_track": "decide"},
        )

    async def submit_review(self, execution_id, command, *, run_id=None):
        type(self).recorded.append(
            ("review", {"execution_id": execution_id, "command": command, "run_id": run_id})
        )

    async def request_cancel(self, execution_id, reason=None, *, run_id=None):
        type(self).recorded.append(
            ("cancel", {"execution_id": execution_id, "reason": reason, "run_id": run_id})
        )

    async def migrate(
        self, execution_id, *, run_id=None, abandon_gates=False, reason=None, dry_run=False
    ):
        type(self).recorded.append(
            (
                "migrate",
                {
                    "execution_id": execution_id,
                    "run_id": run_id,
                    "abandon_gates": abandon_gates,
                    "reason": reason,
                },
            )
        )
        return WorkflowMigrateResult(
            execution_id=execution_id,
            old_run_id=run_id or "old-run",
            new_run_id="new-run",
            old_version_key="ControlPlaneDemoWorkflow.oldddddddddd",
            new_version_key="ControlPlaneDemoWorkflow.abc123def456",
            abandoned_gate_ids=("g1",) if abandon_gates else (),
        )

    def shutdown(self) -> None:
        self.shutdown_called = True


def _ops_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    manifest = _setup(tmp_path, monkeypatch)
    _FakeOperations.calls = []
    _FakeOperations.recorded = []
    _FakeOperations.instances = []
    _FakeOperations.raise_on_build = None
    _FakeOperations.raise_on_status = None
    monkeypatch.setattr("typeflux.controlplane.api.WorkflowOperations", _FakeOperations)
    return TestClient(create_app(manifest))


def _ops_token_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    manifest = _setup(tmp_path, monkeypatch)
    _FakeOperations.calls = []
    _FakeOperations.recorded = []
    _FakeOperations.instances = []
    _FakeOperations.raise_on_build = None
    _FakeOperations.raise_on_status = None
    monkeypatch.setattr("typeflux.controlplane.api.WorkflowOperations", _FakeOperations)
    authorizer = TokenAuthorizer(
        {
            "read-tok": TokenGrant(name="reader", permissions=frozenset({Permission.INSPECT})),
            "op-tok": TokenGrant(name="operator", permissions=frozenset(Permission)),
        }
    )
    return TestClient(create_app(manifest, authorizer=authorizer))


def test_temporal_tier_timeout_frees_the_env_lock_and_reads_stay_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#581: one Temporal-tier call against a dead cluster must not wedge the API.

    The operations build holds the process-wide env-resolution lock across its
    Temporal await; with the tier bounded, the timeout cancellation unwinds the
    context and frees the lock, so status answers 503 within the bound and a
    concurrent bundle read (which needs the same lock) completes promptly
    instead of starving behind the connect attempt.

    Pure-asyncio on purpose: an in-process ASGI transport and one event loop,
    no threads or portals — every await is bounded, so a regression fails an
    assertion instead of hanging the suite (the CI job timeout ate an earlier
    thread-based version of this test on Python 3.11).
    """
    import asyncio
    import time

    import httpx

    from typeflux.project import resolve_project_workflow
    from typeflux.project.environment import async_project_environment_context

    class _HangingBuildOperations:
        @classmethod
        async def for_project_workflow(
            cls,
            project,
            *,
            workflow_id,
            environment_id,
            policy_ids=(),
            expected_policy_hash=None,
            **_kwargs,
        ):
            resolved = resolve_project_workflow(
                project, workflow_id=workflow_id, environment_id=environment_id
            )
            # Stand-in for Client.connect against an unreachable Temporal:
            # an await that outlives any reasonable bound, held (as the real
            # build holds it) inside the env-resolution lock.
            async with async_project_environment_context(resolved.application):
                await asyncio.sleep(30)
            raise AssertionError("the bounded call must be cancelled before this")

    manifest = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("typeflux.controlplane.api.WorkflowOperations", _HangingBuildOperations)
    app = create_app(manifest, temporal_tier_timeout_seconds=0.3)

    async def main() -> None:
        # ASGI lifespan is not driven by ASGITransport; the app under test
        # needs no startup hooks for these routes.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started = time.monotonic()
            status_task = asyncio.create_task(
                client.get(
                    "/api/v1/workflows/workflow/status",
                    params={"environment_id": "local", "execution_id": "exec-581"},
                )
            )
            await asyncio.sleep(0.05)  # let the build take the env lock

            meta = await asyncio.wait_for(client.get("/api/v1/meta"), timeout=5)
            assert meta.status_code == 200

            # The bundle read contends on the env lock the hanging build
            # holds; the timeout cancellation frees it inside the bound.
            bundle = await asyncio.wait_for(
                client.get(
                    "/api/v1/workflows/workflow/bundle",
                    params={"environment_id": "local"},
                ),
                timeout=5,
            )
            assert bundle.status_code == 200

            status = await asyncio.wait_for(status_task, timeout=5)
            elapsed = time.monotonic() - started
            assert status.status_code == 503
            body = status.json()
            assert body["error"] == "TemporalUnavailable"
            assert "did not complete within" in body["message"]
            assert elapsed < 5

    asyncio.run(main())


def test_pin_build_prelude_never_runs_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#585: the pin build's sync prelude must run off the event loop.

    The prelude (module imports, observability/provider client construction)
    is ~1s of blocking work; on the loop it stalls every concurrent request
    for its duration. A frozen loop cannot observe its own freeze (the #590
    lesson), so timing assertions from inside the loop cannot bite — the
    invariant is asserted directly instead: the prelude executes on the env
    lock holder's dedicated worker thread, never on the thread running the
    event loop and never on the shared default executor (whose threads can
    all be blocked acquiring the env lock, so queueing the lock holder's
    prelude there would deadlock the API).
    """
    import asyncio
    import threading

    import httpx

    import typeflux.yaml.runtime as yaml_runtime

    real_prepare = yaml_runtime.prepare_runtime_build
    prelude_threads: list[tuple[bool, str]] = []

    def recording_prepare(spec, *, policy_guard=None, **subworkflow_kwargs):
        thread = threading.current_thread()
        prelude_threads.append((thread is threading.main_thread(), thread.name))
        return real_prepare(spec, policy_guard=policy_guard, **subworkflow_kwargs)

    # Patch both bindings: the operations module's threaded call site (the
    # fix) and the runtime module's global, which build_runtime's inline
    # prepare uses — the pre-fix on-loop path this test exists to catch.
    monkeypatch.setattr("typeflux.project.binding.prepare_runtime_build", recording_prepare)
    monkeypatch.setattr("typeflux.yaml.runtime.prepare_runtime_build", recording_prepare)

    manifest = _setup(tmp_path, monkeypatch)
    app = create_app(manifest, temporal_tier_timeout_seconds=5.0)

    async def main() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # The pin proceeds to the (unreachable) Temporal connect and
            # answers within the tier bound — the prelude ran either way.
            status = await asyncio.wait_for(
                client.get(
                    "/api/v1/workflows/workflow/status",
                    params={"environment_id": "local", "execution_id": "exec-585"},
                ),
                timeout=15,
            )
            assert status.status_code in (500, 503)

    # asyncio.run drives the loop on this (the main) thread, so main-thread
    # execution of the prelude is exactly "ran on the event loop".
    asyncio.run(main())
    assert prelude_threads, "the build prelude never ran"
    [(on_main_thread, thread_name)] = prelude_threads
    assert not on_main_thread, "build prelude ran on the event loop (#585)"
    assert thread_name.startswith("typeflux-env-lock-holder"), (
        f"build prelude ran on shared-executor thread {thread_name!r}; while the env "
        "lock is held it must use the dedicated holder worker or it can deadlock "
        "behind default-executor threads blocked acquiring that same lock"
    )


def test_concurrent_executions_resolution_never_blocks_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#590, at the real code path: concurrent workflow_executions calls.

    Pre-fix, task A holds the env lock across its (stubbed) Temporal await
    while task B's resolve_project_workflow blocks the event loop on the sync
    env-lock acquire — the loop freezes so hard that no in-loop timer (not
    even wait_for) can observe it, and the process-wide lock stays poisoned.
    The probe therefore runs in a SUBPROCESS: a deadlock becomes a
    TimeoutExpired (loud failure) and can never wedge or poison this suite.
    """
    import subprocess

    manifest = _setup(tmp_path, monkeypatch)
    probe = dedent(
        f"""
        import asyncio

        import typeflux.yaml.runtime as yaml_runtime
        from typeflux.project import load_project_spec, workflow_executions


        async def _stub_connect(spec, *, plugin=None):
            await asyncio.sleep(0.2)  # long enough for the others to contend
            raise RuntimeError("Failed client connect: test stand-in")


        yaml_runtime._connect_client = _stub_connect
        project = load_project_spec({str(manifest)!r})


        async def main():
            # The deadlock needs its ordering: the first task must already
            # hold the env lock inside its Temporal await when the others
            # start their (pre-fix loop-blocking) resolution.
            first = asyncio.create_task(
                workflow_executions(project, workflow_id="workflow", environment_id="local")
            )
            await asyncio.sleep(0.1)
            rest = [
                asyncio.create_task(
                    workflow_executions(project, workflow_id="workflow", environment_id="local")
                )
                for _ in range(2)
            ]
            results = await asyncio.gather(first, *rest, return_exceptions=True)
            assert len(results) == 3
            for result in results:
                assert isinstance(result, RuntimeError), result
                assert "client connect" in str(result), result


        asyncio.run(main())
        print("drained")
        """
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=tmp_path,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("concurrent Temporal-tier requests deadlocked the event loop (#590)")
    assert completed.returncode == 0, completed.stderr
    assert "drained" in completed.stdout


def test_concurrent_temporal_tier_requests_never_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#590: concurrent Temporal-tier requests must all answer within bounds.

    The deadlock shape: request A holds the env lock across its Temporal
    await while request B's resolve_project_workflow blocks the event loop on
    the SYNC env-lock acquire — A can then never complete, permanently. With
    resolution moved off-loop, N concurrent requests drain serially and every
    one answers.
    """
    import asyncio

    import httpx

    from typeflux.project import resolve_project_workflow
    from typeflux.project.environment import async_project_environment_context

    class _SlowBuildOperations:
        @classmethod
        async def for_project_workflow(
            cls,
            project,
            *,
            workflow_id,
            environment_id,
            policy_ids=(),
            expected_policy_hash=None,
            **_kwargs,
        ):
            resolved = await asyncio.to_thread(
                resolve_project_workflow,
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
            )
            async with async_project_environment_context(resolved.application):
                await asyncio.sleep(0.15)  # a short Temporal-shaped await
            raise RuntimeError("Failed client connect: test stand-in")

    manifest = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("typeflux.controlplane.api.WorkflowOperations", _SlowBuildOperations)
    app = create_app(manifest, temporal_tier_timeout_seconds=5.0)

    async def main() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            responses = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.get(
                            "/api/v1/workflows/workflow/status",
                            params={
                                "environment_id": "local",
                                "execution_id": f"exec-{index}",
                            },
                        )
                        for index in range(4)
                    ),
                ),
                timeout=20,
            )
            for response in responses:
                assert response.status_code == 503, response.text

    asyncio.run(main())


def test_temporal_rpc_outage_on_pinned_client_maps_to_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After a client is pinned, an outage surfaces as a gRPC RPCError
    # (UNAVAILABLE), not a connect RuntimeError — same 503 contract (#581).
    from temporalio.service import RPCError, RPCStatusCode

    client = _ops_client(tmp_path, monkeypatch)
    _FakeOperations.raise_on_status = RPCError(
        "operator intervention required", RPCStatusCode.UNAVAILABLE, b""
    )

    response = client.get(
        "/api/v1/workflows/workflow/status",
        params={"environment_id": "local", "execution_id": "exec-581"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "TemporalUnavailable"
    assert "UNAVAILABLE" in body["message"]


def test_temporal_tier_timeout_reads_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS", "2.5")
    app = create_app(manifest)
    assert app.state.temporal_tier_timeout == 2.5
    explicit = create_app(manifest, temporal_tier_timeout_seconds=7.0)
    assert explicit.state.temporal_tier_timeout == 7.0


def test_start_coerces_input_and_returns_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/v1/workflows/workflow/start",
        json={
            "environment_id": "local",
            "execution_id": "case-1",
            "input": {"value": "claim"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workflow_id"] == "case-1"
    assert payload["workflow_type"] == "ControlPlaneDemoWorkflow.abc123def456"
    assert payload["trace_query_hint"] == {"workflow_id": "case-1", "limit": 1}
    kind, recorded = _FakeOperations.recorded[0]
    assert kind == "start"
    assert isinstance(recorded["input_value"], _FakeInput)
    assert recorded["input_value"].value == "claim"


def test_start_rejects_invalid_input_with_error_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/v1/workflows/workflow/start",
        json={
            "environment_id": "local",
            "execution_id": "case-1",
            "input": {"wrong_field": 1},
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "InvalidRequest"
    assert "invalid workflow input for _FakeInput" in payload["message"]
    assert _FakeOperations.recorded == []


def test_status_forwards_trace_flag_and_reuses_cached_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)
    params = {"environment_id": "local", "execution_id": "case-1"}

    first = client.get("/api/v1/workflows/workflow/status", params=params)
    second = client.get("/api/v1/workflows/workflow/status", params={**params, "trace": "true"})

    assert first.status_code == 200
    assert first.json()["valid_user_decisions"] == {
        "approve": "summarize",
        "fast_track": "decide",
    }
    assert first.json()["recommended_poll_interval_seconds"] == 1.0
    assert second.status_code == 200
    # One runtime construction across both requests; trace forwarded per call.
    assert len(_FakeOperations.calls) == 1
    assert [entry[1]["trace"] for entry in _FakeOperations.recorded] == [False, True]


def test_status_surfaces_pinned_runtime_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)

    response = client.get(
        "/api/v1/workflows/workflow/status",
        params={"environment_id": "local", "execution_id": "case-1"},
    )

    assert response.status_code == 200
    pin = response.json()["runtime_pin"]
    # Operator can see which pinned version (and when) mutating ops are bound to
    # (#324). Identity only — no freshness verdict (spec_digest covers the graph,
    # not runtime config; the operator uses repin to refresh).
    assert pin["spec_digest"] == "fake-pinned-digest"
    assert pin["pinned_at"]  # ISO timestamp present
    assert set(pin) == {"spec_digest", "pinned_at"}


def test_repin_drops_pinned_runtime_so_it_reresolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)
    params = {"environment_id": "local", "execution_id": "case-1"}

    # Pin the runtime via a status call, then repin to drop it.
    client.get("/api/v1/workflows/workflow/status", params=params)
    assert len(_FakeOperations.calls) == 1
    pinned_instance = _FakeOperations.instances[-1]

    repin = client.post("/api/v1/workflows/workflow/repin", json={"environment_id": "local"})

    assert repin.status_code == 200
    assert repin.json() == {"repinned": True, "dropped": 1}
    assert pinned_instance.shutdown_called is True

    # The next status call re-pins (a second runtime construction).
    client.get("/api/v1/workflows/workflow/status", params=params)
    assert len(_FakeOperations.calls) == 2


def test_repin_with_nothing_pinned_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)

    repin = client.post("/api/v1/workflows/workflow/repin", json={"environment_id": "local"})

    assert repin.status_code == 200
    assert repin.json() == {"repinned": False, "dropped": 0}


def test_repin_requires_project_refresh_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_token_client(tmp_path, monkeypatch)

    denied = client.post(
        "/api/v1/workflows/workflow/repin",
        json={"environment_id": "local"},
        headers={"Authorization": "Bearer read-tok"},
    )
    assert denied.status_code == 403

    allowed = client.post(
        "/api/v1/workflows/workflow/repin",
        json={"environment_id": "local"},
        headers={"Authorization": "Bearer op-tok"},
    )
    assert allowed.status_code == 200


def test_traced_status_requires_operate_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_token_client(tmp_path, monkeypatch)
    params = {"environment_id": "local", "execution_id": "case-1"}
    read_headers = {"Authorization": "Bearer read-tok"}

    # Read-only token may poll untraced status.
    poll = client.get("/api/v1/workflows/workflow/status", params=params, headers=read_headers)
    assert poll.status_code == 200

    # trace=true records an auditable lifecycle op — refused loud for inspect-only,
    # never silently downgraded to trace=false (#321).
    traced = client.get(
        "/api/v1/workflows/workflow/status",
        params={**params, "trace": "true"},
        headers=read_headers,
    )
    assert traced.status_code == 403
    assert "operate-class" in traced.json()["message"]
    # The refused traced call never reached the operation.
    assert [entry[1]["trace"] for entry in _FakeOperations.recorded] == [False]

    # An operate-class token may take the audited traced status.
    op_traced = client.get(
        "/api/v1/workflows/workflow/status",
        params={**params, "trace": "true"},
        headers={"Authorization": "Bearer op-tok"},
    )
    assert op_traced.status_code == 200
    assert _FakeOperations.recorded[-1][1]["trace"] is True


def test_lifecycle_binding_error_maps_to_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)
    _FakeOperations.raise_on_status = LifecycleBindingError(
        "lifecycle operation refused: execution 'case-1' has workflow type 'Other.x'"
    )

    response = client.get(
        "/api/v1/workflows/workflow/status",
        params={"environment_id": "local", "execution_id": "case-1"},
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"] == "LifecycleBindingError"
    assert "refused" in payload["message"]


def test_distinct_policy_selection_builds_separate_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)
    params = {"environment_id": "local", "execution_id": "case-1"}

    client.get("/api/v1/workflows/workflow/status", params=params)
    client.get("/api/v1/workflows/workflow/status", params={**params, "policy_id": ["strict"]})

    assert len(_FakeOperations.calls) == 2
    assert _FakeOperations.calls[1]["policy_ids"] == ("strict",)


def test_review_and_cancel_return_no_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)

    review = client.post(
        "/api/v1/workflows/workflow/review",
        json={
            "environment_id": "local",
            "execution_id": "case-1",
            "command": {"user_decision": "approve", "reviewer": "ops@example.com"},
        },
    )
    cancel = client.post(
        "/api/v1/workflows/workflow/cancel",
        json={
            "environment_id": "local",
            "execution_id": "case-1",
            "reason": "duplicate submission",
        },
    )

    assert review.status_code == 204
    assert cancel.status_code == 204
    kinds = dict(_FakeOperations.recorded)
    command = kinds["review"]["command"]
    assert isinstance(command, ReviewCommand)
    assert command.user_decision == "approve"
    assert command.reviewer == "ops@example.com"
    assert kinds["cancel"]["reason"] == "duplicate submission"


def test_migrate_route_addresses_like_cancel_with_optional_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)

    # With an explicit run_id in the body (the cancel/review addressing shape).
    response = client.post(
        "/api/v1/workflows/workflow/migrate",
        json={
            "environment_id": "local",
            "execution_id": "case-1",
            "run_id": "run-42",
            "abandon_gates": True,
            "reason": "budget cut",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_id"] == "case-1"
    assert body["new_run_id"] == "new-run"
    assert body["new_version_key"] == "ControlPlaneDemoWorkflow.abc123def456"
    assert body["abandoned_gate_ids"] == ["g1"]
    kinds = dict(_FakeOperations.recorded)
    assert kinds["migrate"] == {
        "execution_id": "case-1",
        "run_id": "run-42",
        "abandon_gates": True,
        "reason": "budget cut",
    }

    # OMITTED run_id targets the current run (dispatched as None), like cancel.
    _FakeOperations.recorded = []
    current = client.post(
        "/api/v1/workflows/workflow/migrate",
        json={"environment_id": "local", "execution_id": "case-1"},
    )
    assert current.status_code == 200
    kinds = dict(_FakeOperations.recorded)
    assert kinds["migrate"] == {
        "execution_id": "case-1",
        "run_id": None,
        "abandon_gates": False,
        "reason": None,
    }


def test_migrate_requires_both_start_and_cancel_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    _FakeOperations.calls = []
    _FakeOperations.recorded = []
    _FakeOperations.instances = []
    _FakeOperations.raise_on_build = None
    _FakeOperations.raise_on_status = None
    monkeypatch.setattr("typeflux.controlplane.api.WorkflowOperations", _FakeOperations)
    authorizer = TokenAuthorizer(
        {
            "start-tok": TokenGrant(
                name="starter", permissions=frozenset({Permission.INSPECT, Permission.START})
            ),
            "cancel-tok": TokenGrant(
                name="canceler", permissions=frozenset({Permission.INSPECT, Permission.CANCEL})
            ),
            "op-tok": TokenGrant(name="operator", permissions=frozenset(Permission)),
        }
    )
    client = TestClient(create_app(manifest, authorizer=authorizer))
    path = "/api/v1/workflows/workflow/migrate"
    body = {"environment_id": "local", "execution_id": "case-1"}

    # START without CANCEL → 403 naming the missing permission.
    start_only = client.post(path, json=body, headers={"authorization": "Bearer start-tok"})
    assert start_only.status_code == 403
    assert "cancel" in start_only.json()["message"]
    # CANCEL without START → 403.
    cancel_only = client.post(path, json=body, headers={"authorization": "Bearer cancel-tok"})
    assert cancel_only.status_code == 403
    assert "start" in cancel_only.json()["message"]
    # Both grants → the route dispatches (200 through the fake).
    both = client.post(path, json=body, headers={"authorization": "Bearer op-tok"})
    assert both.status_code == 200
    assert _FakeOperations.recorded[-1][0] == "migrate"


def test_policy_hash_mismatch_maps_to_unprocessable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)
    _FakeOperations.raise_on_build = ProjectPolicyEnforcementError(
        "selected project policy hash does not match expected deployment policy hash"
    )

    response = client.post(
        "/api/v1/workflows/workflow/start",
        json={
            "environment_id": "local",
            "execution_id": "case-1",
            "input": {"value": "claim"},
            "expected_policy_hash": "0" * 64,
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "ProjectPolicyEnforcementError"
    assert "does not match expected" in payload["message"]


def test_versions_returns_drain_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    drain = WorkflowDrainStatus(
        logical_workflow="ControlPlaneDemoWorkflow",
        current_workflow_type="ControlPlaneDemoWorkflow.abc123def456",
        query=(
            "WorkflowType STARTS_WITH 'ControlPlaneDemoWorkflow.' AND ExecutionStatus = 'Running'"
        ),
        running={
            "ControlPlaneDemoWorkflow.abc123def456": 2,
            "ControlPlaneDemoWorkflow.old456old456": 1,
        },
        total_running=3,
        drained=False,
    )

    async def fake_drain_status(project, *, workflow_id, environment_id):
        assert workflow_id == "workflow"
        assert environment_id == "local"
        return drain

    monkeypatch.setattr("typeflux.controlplane.api.workflow_drain_status", fake_drain_status)
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/workflows/workflow/versions", params={"environment_id": "local"})
    missing = client.get("/api/v1/workflows/missing/versions", params={"environment_id": "local"})

    assert response.status_code == 200
    assert response.json() == drain.to_dict()
    assert missing.status_code == 404


def test_lifespan_shuts_down_cached_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ops_client(tmp_path, monkeypatch)

    with client:
        client.get(
            "/api/v1/workflows/workflow/status",
            params={"environment_id": "local", "execution_id": "case-1"},
        )

    assert len(_FakeOperations.instances) == 1
    assert _FakeOperations.instances[0].shutdown_called is True


def test_cors_is_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    headers = {"Origin": "http://localhost:5173"}

    default = TestClient(create_app(manifest)).get("/api/v1/meta", headers=headers)
    enabled = TestClient(create_app(manifest, cors_origins=("http://localhost:5173",))).get(
        "/api/v1/meta", headers=headers
    )

    assert default.status_code == 200
    assert "access-control-allow-origin" not in default.headers
    assert enabled.status_code == 200
    assert enabled.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_definition_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    environment = client.get("/api/v1/environments/local")
    assert environment.status_code == 200
    assert environment.json()["name"] == "local"
    assert "variables" not in environment.json()

    policies = client.get("/api/v1/policies")
    assert policies.status_code == 200
    assert policies.json() == []

    profiles = client.get("/api/v1/profiles")
    assert profiles.status_code == 200
    assert profiles.json() == []

    missing_policy = client.get("/api/v1/policies/nope")
    missing_profile = client.get("/api/v1/profiles/provider/nope")
    assert missing_policy.status_code == 404
    assert missing_profile.status_code == 404
    assert missing_policy.json()["error"] == "NotFound"


def test_deployments_endpoint_lists_plans_with_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys as _sys

    from tests.test_deployment import _setup_plan_project
    from typeflux.project import write_deployment_plan

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    project = _setup_plan_project(tmp_path)
    _, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "d" * 64,
        policy_ids=("base",),
    )
    client = TestClient(create_app(project.manifest_path))

    listing = client.get("/api/v1/deployments")
    detail = client.get(f"/api/v1/deployments/{plan.plan_id}")
    missing = client.get("/api/v1/deployments/nope")

    assert listing.status_code == 200
    assert [entry["plan_id"] for entry in listing.json()] == [plan.plan_id]
    assert listing.json()[0]["verification"]["ok"] is True
    assert detail.status_code == 200
    assert detail.json()["plan"]["plan_hash"] == plan.plan_hash
    # The promote command is server-built (manifest stays server-side) and
    # needs only the plan path — the plan is authoritative.
    assert listing.json()[0]["promote_command"].endswith(f"--apply deployments/{plan.plan_id}.yaml")
    # The manifest-relative plan path — the console builds source links from it (#610).
    assert listing.json()[0]["path"] == f"deployments/{plan.plan_id}.yaml"
    # Injection defense in depth: the copyable command is shlex-quoted (a plain tmp path
    # passes through unquoted; see the spaced-path test for the quoting itself).
    assert missing.status_code == 404


def test_deployments_endpoint_degrades_per_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad plan file never hides the valid plans (review items 2/6, mirrored in TS).

    A malformed/tampered file surfaces as an ERROR ENTRY (parse mismatch, placeholder plan);
    a STALE plan whose workflow was removed records a failed verification (resolution
    mismatch) — the listing stays 200 either way.
    """
    import sys as _sys

    from tests.test_deployment import _setup_plan_project
    from typeflux.project import write_deployment_plan

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    project = _setup_plan_project(tmp_path)
    _, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "e" * 64,
        policy_ids=("base",),
    )
    (tmp_path / "deployments" / "garbage.yaml").write_text("not: [a, valid", encoding="utf-8")

    listing = TestClient(create_app(project.manifest_path)).get("/api/v1/deployments")
    assert listing.status_code == 200
    entries = {entry["plan_id"]: entry for entry in listing.json()}
    assert set(entries) == {"garbage", plan.plan_id}
    garbage = entries["garbage"]
    assert garbage["verification"]["ok"] is False
    assert garbage["verification"]["mismatches"][0]["path"] == "parse"
    assert garbage["path"] == "deployments/garbage.yaml"
    assert entries[plan.plan_id]["verification"]["ok"] is True
    # The error entry is addressable by id too (the console can drill into it).
    detail = TestClient(create_app(project.manifest_path)).get("/api/v1/deployments/garbage")
    assert detail.status_code == 200
    assert detail.json()["verification"]["mismatches"][0]["path"] == "parse"

    # STALE plan: rename the workflow in the manifest — the plan's workflow no longer exists.
    (tmp_path / "deployments" / "garbage.yaml").unlink()
    manifest = tmp_path / "typeflux.project.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        .replace("- id: workflow", "- id: renamed")
        .replace("workflows: [workflow]", "workflows: [renamed]"),
        encoding="utf-8",
    )
    stale = TestClient(create_app(manifest)).get("/api/v1/deployments")
    assert stale.status_code == 200
    assert [entry["plan_id"] for entry in stale.json()] == [plan.plan_id]
    verification = stale.json()[0]["verification"]
    assert verification["ok"] is False
    assert verification["mismatches"][0]["path"] == "resolution"


def test_deployments_promote_command_is_shell_quoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest path with a space arrives single-quoted in the copyable promote command
    (review item 5 — injection defense in depth on top of the loader's id-charset gate)."""
    import sys as _sys

    from tests.test_deployment import _setup_plan_project
    from typeflux.project import write_deployment_plan

    base = tmp_path / "has space"
    base.mkdir()
    monkeypatch.syspath_prepend(str(base))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    project = _setup_plan_project(base)
    _, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "e" * 64,
        policy_ids=("base",),
    )
    listing = TestClient(create_app(project.manifest_path)).get("/api/v1/deployments")
    assert listing.status_code == 200
    command = listing.json()[0]["promote_command"]
    assert f"'{project.manifest_path}'" in command
    assert command.endswith(f"--apply deployments/{plan.plan_id}.yaml")


# ---------------------------------------------------------------------------
# Enforcement-events feed (#723)
# ---------------------------------------------------------------------------


def _moderation_block_trace(trace_id: str, *, when: str) -> Any:
    from datetime import datetime

    from typeflux.manifests import build_workflow_execution_manifest
    from typeflux.observability.inspect import ObservationRecord, TraceRecord

    workflow = build_workflow_execution_manifest(
        workflow_name="ControlPlaneDemoWorkflow",
        workflow_id=f"{trace_id}-exec",
        task_queue="controlplane-demo-queue",
        activities=[],
    ).to_dict()
    workflow["code_provenance"]["environment"] = "local"
    return TraceRecord(
        trace_id=trace_id,
        timestamp=datetime.fromisoformat(when),
        metadata={"typeflux": {"execution_manifest": workflow}},
        observations=(
            ObservationRecord(
                observation_id=f"{trace_id}-act",
                name="classify",
                start_time=datetime.fromisoformat(when),
                metadata={
                    "typeflux_moderation": {"decision": "block", "categories": ["hate"]},
                },
            ),
        ),
    )


def _reader(status: str, traces: tuple = ()):  # noqa: ANN001, ANN202 - test seam
    from typeflux.project import EnforcementReadResult

    def reader(**_kwargs: Any) -> EnforcementReadResult:
        return EnforcementReadResult(status, traces)

    return reader


def test_enforcement_events_default_reader_reports_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The canonical demo project has observer `none`, so the default reader
    # degrades to `not_configured` — loudly, with a 200 and the marker, never a
    # silent empty list masquerading as "no violations".
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/enforcement-events", params={"environment_id": "local"})

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] == {"langfuse": "not_configured"}
    assert "since" in body and "until" in body


def test_enforcement_events_blocked_moderation_from_injected_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    trace = _moderation_block_trace("trace-1", when="2026-07-10T12:00:00+00:00")
    client = TestClient(create_app(manifest, enforcement_reader=_reader("ok", (trace,))))

    response = client.get(
        "/api/v1/enforcement-events",
        params={"environment_id": "local", "since": "2026-07-01T00:00:00+00:00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] == {"langfuse": "ok"}
    runtime = [event for event in body["events"] if event["source"] == "runtime"]
    assert len(runtime) == 1
    assert runtime[0]["verdict"] == "blocked"
    assert runtime[0]["rule"] == "moderation.on_violation.block"
    assert runtime[0]["execution_id"] == "trace-1-exec"
    assert runtime[0]["evidence"]["trace_id"] == "trace-1"


def test_enforcement_events_since_normalizes_offset_to_utc_in_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An aware-but-non-UTC `since` (+05:00) must be normalized to UTC, not echoed
    # back verbatim (the old _as_utc only handled naive input).
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest, enforcement_reader=_reader("not_configured")))

    response = client.get(
        "/api/v1/enforcement-events",
        params={"environment_id": "local", "since": "2026-07-01T05:00:00+05:00"},
    )

    assert response.status_code == 200
    # 05:00 at +05:00 is 00:00Z — the echo carries the UTC-normalized value.
    assert response.json()["since"] == "2026-07-01T00:00:00+00:00"


def test_enforcement_events_unreachable_degrades_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest, enforcement_reader=_reader("unreachable")))

    response = client.get("/api/v1/enforcement-events", params={"environment_id": "local"})

    assert response.status_code == 200
    assert response.json()["partial"] == {"langfuse": "unreachable"}


def test_enforcement_events_verdict_filter_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    trace = _moderation_block_trace("trace-1", when="2026-07-10T12:00:00+00:00")
    client = TestClient(create_app(manifest, enforcement_reader=_reader("ok", (trace,))))

    # verdict=rejected excludes the blocked moderation event.
    filtered = client.get(
        "/api/v1/enforcement-events",
        params={"environment_id": "local", "verdict": "rejected", "since": "2026-07-01T00:00:00Z"},
    )
    assert filtered.status_code == 200
    assert all(event["verdict"] == "rejected" for event in filtered.json()["events"])

    # An unknown verdict is a bad request (error taxonomy #617); `warned` has no
    # producer this slice and is no longer an accepted filter value.
    bad = client.get(
        "/api/v1/enforcement-events", params={"environment_id": "local", "verdict": "nope"}
    )
    assert bad.status_code == 422
    assert bad.json()["error"] == "InvalidRequest"
    assert "blocked, rejected" in bad.json()["message"]
    assert "warned" not in bad.json()["message"]

    warned = client.get(
        "/api/v1/enforcement-events", params={"environment_id": "local", "verdict": "warned"}
    )
    assert warned.status_code == 422


def test_enforcement_events_pagination_and_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    traces = tuple(
        _moderation_block_trace(f"trace-{i}", when=f"2026-07-1{i}T12:00:00+00:00") for i in range(3)
    )
    client = TestClient(create_app(manifest, enforcement_reader=_reader("ok", traces)))

    first = client.get(
        "/api/v1/enforcement-events",
        params={"environment_id": "local", "since": "2026-07-01T00:00:00Z", "limit": 1},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["events"]) == 1
    cursor = first_body["next_cursor"]
    assert cursor

    second = client.get(
        "/api/v1/enforcement-events",
        params={
            "environment_id": "local",
            "since": "2026-07-01T00:00:00Z",
            "limit": 1,
            "cursor": cursor,
        },
    )
    assert second.status_code == 200
    assert len(second.json()["events"]) == 1
    # Distinct pages.
    assert first_body["events"][0]["execution_id"] != second.json()["events"][0]["execution_id"]

    # A malformed cursor is a bad request, not a 500.
    malformed = client.get(
        "/api/v1/enforcement-events", params={"environment_id": "local", "cursor": "@@@"}
    )
    assert malformed.status_code == 422

    # A cursor minted under one filter set is rejected when a filter changes
    # (bound to the filter fingerprint) — never a silent skip/dupe.
    reused = client.get(
        "/api/v1/enforcement-events",
        params={
            "environment_id": "local",
            "since": "2026-07-01T00:00:00Z",
            "limit": 1,
            "verdict": "blocked",  # a filter the first page did not carry
            "cursor": cursor,
        },
    )
    assert reused.status_code == 422


def test_enforcement_events_unknown_workflow_filter_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    response = client.get(
        "/api/v1/enforcement-events",
        params={"environment_id": "local", "workflow_id": "does-not-exist"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"


def test_enforcement_events_requires_environment_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unscoped call (no environment_id) can never resolve a workflow, so the feed
    # would answer an empty/not_configured body indistinguishable from a healthy quiet
    # project — require the scope explicitly (422), rather than answer silently-empty.
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/enforcement-events")

    assert response.status_code == 422
    assert response.json()["error"] == "InvalidRequest"
    assert "environment_id" in response.json()["message"]


# ---------------------------------------------------------------------------
# GitHub-provenance surface (#727)
# ---------------------------------------------------------------------------


def _github_reader(result: Any):  # noqa: ANN001, ANN202 - test seam
    captured: dict[str, Any] = {}

    def reader(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return result

    reader.captured = captured  # type: ignore[attr-defined]
    return reader


def _inject_github_source(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str,
    ref: str = "main",
    sha: str | None = None,
) -> None:
    # The single-manifest app records no git source (entry.repo is None), so the surface
    # would report not_configured. Inject a recorded github source WITHOUT touching
    # resolution/cloning by overriding just the two registry provenance accessors the
    # endpoint reads — resolve() still returns the local manifest, so no clone is attempted.
    from typeflux.controlplane.git_source import ProjectRepoSource

    registry = app.state.registry
    monkeypatch.setattr(
        registry, "repo_source", lambda _pid: ProjectRepoSource(url=url, ref=ref), raising=False
    )
    monkeypatch.setattr(registry, "repo_head_sha", lambda _pid: sha, raising=False)


def test_github_provenance_local_reports_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A local single-manifest mount records no git source, so there is nothing to compare:
    # not_configured, no head, no network call (the default reader is never even reached).
    manifest = _setup(tmp_path, monkeypatch)

    def _never(**_kwargs: Any) -> Any:
        raise AssertionError("the github reader must not run without a recorded git source")

    client = TestClient(create_app(manifest, github_reader=_never))

    response = client.get("/api/v1/github-provenance")

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] == {"github": "not_configured"}
    assert "head" not in body  # null head is excluded by response_model_exclude_none
    assert body["plans"] == []


def test_github_provenance_capability_reflects_github_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    app = create_app(manifest)
    client = TestClient(app)

    # Without a github source: capability false (resolvable but no repo provenance).
    assert client.get("/api/v1/meta").json()["capabilities"]["github_provenance"] is False

    # With a recorded github source: capability true (resolvable AND repo present).
    _inject_github_source(app, monkeypatch, url="https://github.com/acme/flows")
    assert client.get("/api/v1/meta").json()["capabilities"]["github_provenance"] is True

    # A non-github (GHE) source does NOT light the capability (host gate).
    _inject_github_source(app, monkeypatch, url="https://ghe.example/acme/flows")
    assert client.get("/api/v1/meta").json()["capabilities"]["github_provenance"] is False


def test_github_provenance_injected_reader_surfaces_head_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import GithubReadResult

    manifest = _setup(tmp_path, monkeypatch)
    reader = _github_reader(
        GithubReadResult("ok", head_sha="remote-sha", commits_behind=2, plan_prs={})
    )
    app = create_app(manifest, github_reader=reader)
    _inject_github_source(
        app, monkeypatch, url="https://github.com/acme/flows", ref="main", sha="served-sha"
    )
    client = TestClient(app)

    response = client.get("/api/v1/github-provenance")

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] == {"github": "ok"}
    assert body["head"] == {
        "branch": "main",
        "sha": "remote-sha",
        "ahead_of_served": True,
        "commits_behind": 2,
    }
    # The reader received the served side (repo/branch/sha) the endpoint derived.
    assert reader.captured["branch"] == "main"  # type: ignore[attr-defined]
    assert reader.captured["served_sha"] == "served-sha"  # type: ignore[attr-defined]
    assert reader.captured["repo"].owner == "acme"  # type: ignore[attr-defined]


def test_github_provenance_reader_degradation_keeps_null_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import GithubReadResult

    manifest = _setup(tmp_path, monkeypatch)
    app = create_app(manifest, github_reader=_github_reader(GithubReadResult("rate_limited")))
    _inject_github_source(app, monkeypatch, url="https://github.com/acme/flows", sha="served-sha")
    client = TestClient(app)

    response = client.get("/api/v1/github-provenance")

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] == {"github": "rate_limited"}
    assert "head" not in body  # the remote HEAD was not read, so head stays null
