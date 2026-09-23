from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from typeflux.project import load_project_spec, resolve_workflow_bundle


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, lifecycle: bool = True) -> Path:
    for name in tuple(sys.modules):
        if name == "topology_project" or name.startswith("topology_project."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    package = tmp_path / "topology_project"
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

        from topology_project.schemas import AssessmentBatch, Decision


        @activity.defn(name="decide")
        async def decide(value: AssessmentBatch) -> Decision:
            return Decision(value=str(len(value.reviews)))
        """,
    )
    lifecycle_block = """
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
    """
    _write(
        tmp_path / "workflow.yaml",
        f"""
        project: topology_project
        name: topology_demo
        task_queue: topology-demo-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              assess: assess {{{{value}}}}
              summarize: summarize {{{{reviews}}}}
          provider:
            type: fake
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
          name: TopologyDemoWorkflow
          input: schemas:ClaimInput
          output: schemas:Decision
{lifecycle_block if lifecycle else ""}
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
        name: topology-demo
        workflows:
          - id: workflow
            path: workflow.yaml
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


def test_topology_projects_sequential_map_and_review_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")
    topology = bundle.topology

    assert [(node.id, node.kind, node.activity) for node in topology.nodes] == [
        ("assess_items", "map", "assess_item"),
        ("summarize", "activity", "summarize"),
        ("decide", "activity", "decide"),
    ]
    assert [(edge.source, edge.target, edge.kind, edge.condition) for edge in topology.edges] == [
        ("assess_items", "summarize", "sequential", None),
        ("summarize", "decide", "sequential", None),
        ("assess_items", "summarize", "review", "approve"),
        ("assess_items", "decide", "review", "fast_track"),
    ]


def test_topology_round_trips_through_bundle_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    payload = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    topology = payload["topology"]

    assert [node["id"] for node in topology["nodes"]] == ["assess_items", "summarize", "decide"]
    sequential = [edge for edge in topology["edges"] if edge["kind"] == "sequential"]
    review = [edge for edge in topology["edges"] if edge["kind"] == "review"]
    # Sequential edges carry no condition key at all (exclude_none), review
    # edges carry the routing decision.
    assert all("condition" not in edge for edge in sequential)
    assert [(edge["source"], edge["target"], edge["condition"]) for edge in review] == [
        ("assess_items", "summarize", "approve"),
        ("assess_items", "decide", "fast_track"),
    ]


def test_topology_without_lifecycle_has_only_sequential_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch, lifecycle=False)
    project = load_project_spec(manifest)

    topology = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).topology

    assert {edge.kind for edge in topology.edges} == {"sequential"}
    assert len(topology.edges) == 2


def test_bundle_links_resolve_from_allowlisted_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TEMPORAL_UI_URL", "http://localhost:8233")
    monkeypatch.setenv("LANGFUSE_PROJECT_URL", "https://cloud.langfuse.com/project/p-123")
    project = load_project_spec(manifest)

    payload = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()

    assert payload["links"] == {
        "temporal_ui": "http://localhost:8233",
        "langfuse_project": "https://cloud.langfuse.com/project/p-123",
    }


def test_bundle_links_derive_from_langfuse_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import bundle as bundle_module

    manifest = _setup(tmp_path, monkeypatch)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            """  registry:
    type: inline
    prompts:
      assess: assess {{value}}
      summarize: summarize {{reviews}}""",
            """  registry:
    type: langfuse
    host: https://us.cloud.langfuse.com""",
        ),
        encoding="utf-8",
    )
    project = load_project_spec(manifest)
    monkeypatch.setattr(
        bundle_module,
        "_langfuse_project_url",
        lambda host: f"{host}/project/p-real" if host else None,
    )
    # A stale explicit URL on a different host loses to the registry-derived one.
    monkeypatch.setenv("LANGFUSE_PROJECT_URL", "https://cloud.langfuse.com/project/stale")

    payload = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()

    assert payload["links"]["langfuse_project"] == "https://us.cloud.langfuse.com/project/p-real"


def test_bundle_links_absent_without_config_and_drop_non_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("TEMPORAL_UI_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_PROJECT_URL", raising=False)
    project = load_project_spec(manifest)

    # No env config: the only link is the Temporal UI derived from the
    # fixture's localhost address (inline registry → no langfuse link).
    unset = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    assert unset["links"] == {"temporal_ui": "http://localhost:8233"}

    # Non-http(s) env values are dropped; the derived localhost UI remains.
    monkeypatch.setenv("TEMPORAL_UI_URL", "not-a-url")
    monkeypatch.setenv("LANGFUSE_PROJECT_URL", "ftp://nope")
    invalid = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    assert invalid["links"] == {"temporal_ui": "http://localhost:8233"}

    # A URL embedding credentials is rejected WHOLE (#600) — the bundle is secret-safe,
    # so the token must never publish; the derived localhost UI still fills in.
    monkeypatch.setenv("TEMPORAL_UI_URL", "https://token@temporal-ui.corp.example")
    monkeypatch.setenv("LANGFUSE_PROJECT_URL", "https://user:sk-secret@lf.example/project/p1")
    credentialed = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    assert credentialed["links"] == {"temporal_ui": "http://localhost:8233"}
    import json as _json

    assert "sk-secret" not in _json.dumps(credentialed)


@pytest.mark.asyncio
async def test_workflow_executions_lists_all_statuses_with_version_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from typeflux.project import workflow_executions

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    captured: dict[str, str] = {}

    class _FakeClient:
        def list_workflows(self, query):
            captured["query"] = query

            async def _iter():
                for item in (
                    SimpleNamespace(
                        id="case-2",
                        run_id="r2",
                        workflow_type="TopologyDemoWorkflow.currentcurr",
                        status=SimpleNamespace(name="RUNNING"),
                        start_time=None,
                        close_time=None,
                    ),
                    SimpleNamespace(
                        id="case-1",
                        run_id="r1",
                        workflow_type="TopologyDemoWorkflow.oldversion1",
                        status=SimpleNamespace(name="COMPLETED"),
                        start_time=None,
                        close_time=None,
                    ),
                ):
                    yield item

            return _iter()

    async def fake_connect(spec, plugin=None):
        return _FakeClient()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    listing = await workflow_executions(
        project, workflow_id="workflow", environment_id="local", limit=10
    )

    assert "ExecutionStatus" not in captured["query"]
    assert captured["query"].startswith("WorkflowType STARTS_WITH 'TopologyDemoWorkflow.'")
    assert [record.execution_id for record in listing.executions] == ["case-2", "case-1"]
    assert [record.status for record in listing.executions] == ["RUNNING", "COMPLETED"]
    assert listing.executions[0].current_version is (
        listing.executions[0].workflow_type == listing.current_workflow_type
    )
    assert listing.executions[1].current_version is False


@pytest.mark.asyncio
async def test_workflow_run_correlation_reports_observer_and_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import workflow_run_correlation

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    # The fixture's observability type is none: reported plainly, no trace.
    plain = await workflow_run_correlation(
        project, workflow_id="workflow", environment_id="local", execution_id="case-1"
    )
    assert plain.observer == "none"
    assert plain.reachable is True
    assert plain.trace is None
    assert "trace" not in plain.to_dict() or plain.to_dict()["trace"] is None


def test_bundle_code_provenance_in_and_out_of_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    outside = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    assert "code" not in outside

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/typeflux-demo.git"],
        cwd=tmp_path,
        check=True,
    )

    inside = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    code = inside["code"]
    assert len(code["sha"]) == 40
    assert code["branch"] == "main"
    assert code["dirty"] is False
    assert code["repo_url"] == "https://github.com/acme/typeflux-demo"
    assert code["workflow_path"] == "workflow.yaml"
    assert code["manifest_path"] == "typeflux.project.yaml"

    (tmp_path / "scratch.txt").write_text("x", encoding="utf-8")
    dirty = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    assert dirty["code"]["dirty"] is True


def test_bundle_code_strips_remote_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://user:tok-SECRET@github.com/acme/demo.git"],
        cwd=tmp_path,
        check=True,
    )

    payload = json.dumps(
        resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local").to_dict()
    )

    assert "tok-SECRET" not in payload
    assert '"repo_url": "https://github.com/acme/demo"' in payload


def test_workflow_connections_reports_yaml_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import workflow_connections

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    status = workflow_connections(project, workflow_id="workflow", environment_id="local")

    # Fixture YAML: inline registry, observability none — nothing to reach.
    assert status.registry.type == "inline"
    assert status.registry.reachable is True
    assert status.observability.type == "none"
    assert status.observability.reachable is True
    payload = json.dumps(status.to_dict())
    assert "SECRET" not in payload and "key" not in payload.lower()


def test_workflow_connections_probe_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    from typeflux.project import connections as connections_module

    probed_hosts: list[str | None] = []

    def boom(host: str | None) -> None:
        probed_hosts.append(host)
        raise RuntimeError("auth failed")

    monkeypatch.setattr(connections_module, "_langfuse_probe", boom)
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example")

    probe = connections_module._probe(kind="langfuse", configured_host=None)

    assert probe.reachable is False
    assert probe.host == "https://lf.example"
    assert "auth failed" in (probe.detail or "")

    monkeypatch.setattr(
        connections_module, "_langfuse_probe", lambda host: probed_hosts.append(host)
    )
    ok = connections_module._probe(kind="langfuse", configured_host="https://other.example")
    assert ok.reachable is True
    assert ok.host == "https://other.example"
    # The probe receives the same host the response displays.
    assert probed_hosts == ["https://lf.example", "https://other.example"]


def test_prompt_status_inline_registry_is_in_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import workflow_prompt_status

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    status = workflow_prompt_status(project, workflow_id="workflow", environment_id="local")

    assert status.registry_type == "inline"
    assert {entry.name for entry in status.prompts} == {"assess", "summarize"}
    assert all(entry.status == "in_sync" and entry.mode == "inline" for entry in status.prompts)
    assert status.prompts[0].used_by_activities
    # Inline prompts carry their own YAML template for preview.
    by_name = {entry.name: entry for entry in status.prompts}
    assert by_name["assess"].template == "assess {{value}}"


def test_prompt_status_label_drift_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import prompt_status as module
    from typeflux.project import workflow_prompt_status

    manifest = _setup(tmp_path, monkeypatch)
    # Point the fixture at a langfuse registry with one label-pinned and one
    # version-pinned prompt ref.
    workflow = tmp_path / "workflow.yaml"
    text = workflow.read_text(encoding="utf-8")
    text = text.replace(
        """  registry:
    type: inline
    prompts:
      assess: assess {{value}}
      summarize: summarize {{reviews}}""",
        """  registry:
    type: langfuse
    host: https://lf.example
    label: staging""",
    )
    # The drift fixture needs a traced observer for last-run lookups.
    text = text.replace("observability:\n    type: none", "observability:\n    type: langfuse")
    text = text.replace(
        "prompt: assess",
        "prompt:\n        name: assess\n        label: live",
    )
    text = text.replace(
        "prompt: summarize",
        "prompt:\n        name: summarize\n        version: 7",
    )
    workflow.write_text(text, encoding="utf-8")
    project = load_project_spec(manifest)

    lookups: list[tuple[str, str, str | None]] = []

    def fake_lookup(name, label, *, host=None):
        lookups.append((name, label, host))
        return (4, None)

    monkeypatch.setattr(module, "_last_run_versions", lambda logical: {"assess": "3"})
    monkeypatch.setattr(module, "_registry_label_version", fake_lookup)
    drift = workflow_prompt_status(project, workflow_id="workflow", environment_id="local")
    by_name = {entry.name: entry for entry in drift.prompts}
    assert by_name["assess"].status == "drift"
    # The lookup honors the YAML registry host; the explicit ref label wins
    # over the registry default.
    assert lookups == [("assess", "live", "https://lf.example")]
    assert (by_name["assess"].last_run_version, by_name["assess"].registry_version) == ("3", "4")
    assert by_name["summarize"].mode == "pinned"
    assert by_name["summarize"].status == "in_sync"
    # Registry-managed prompt text never serializes.
    assert all(entry.template is None for entry in drift.prompts)

    # Registry failure degrades the label ref to unknown — never false in-sync.
    monkeypatch.setattr(
        module,
        "_registry_label_version",
        lambda name, label, *, host=None: (None, "registry lookup failed: x"),
    )
    unknown = workflow_prompt_status(project, workflow_id="workflow", environment_id="local")
    assert {e.name: e.status for e in unknown.prompts}["assess"] == "unknown"


def test_project_url_failures_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    from typeflux.project import bundle as bundle_module

    bundle_module._LANGFUSE_PROJECT_URL_CACHE.clear()
    calls = {"n": 0}

    class _FlakyClient:
        class api:  # noqa: N801 - mirrors the SDK surface.
            class projects:  # noqa: N801
                @staticmethod
                def get():
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("transient")
                    from types import SimpleNamespace

                    return SimpleNamespace(data=[SimpleNamespace(id="p-1")])

    import langfuse

    monkeypatch.setattr(langfuse, "Langfuse", lambda host=None: _FlakyClient())

    assert bundle_module._langfuse_project_url("https://lf.example") is None
    # The failure was not cached: the retry succeeds and is then cached.
    assert bundle_module._langfuse_project_url("https://lf.example") == (
        "https://lf.example/project/p-1"
    )
    assert calls["n"] == 2
    assert bundle_module._langfuse_project_url("https://lf.example") == (
        "https://lf.example/project/p-1"
    )
    assert calls["n"] == 2
    bundle_module._LANGFUSE_PROJECT_URL_CACHE.clear()


def test_inline_chat_templates_render_as_yaml() -> None:
    from typeflux.project.prompt_status import _render_template

    rendered = _render_template({"messages": [{"role": "system", "content": "be terse"}]})
    assert "role: system" in rendered
    assert "be terse" in rendered
    assert _render_template("plain {{x}}") == "plain {{x}}"


def test_project_runtime_defaults_sit_beneath_yaml_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import resolve_project_workflow

    manifest = _setup(tmp_path, monkeypatch)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "name: topology-demo",
            """name: topology-demo
defaults:
  runtime:
    provider_retry:
      max_attempts: 5
      initial_backoff_seconds: 2
    provider:
      type: fake""",
        ),
        encoding="utf-8",
    )
    project = load_project_spec(manifest)

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    # Project default fills the unset provider_retry...
    assert resolved.spec.runtime.provider_retry is not None
    assert resolved.spec.runtime.provider_retry.max_attempts == 5
    # ...but the workflow YAML's own provider section wins over the default.
    assert resolved.spec.runtime.provider.type == "fake"


def test_project_runtime_defaults_respect_the_override_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import resolve_project_workflow

    manifest = _setup(tmp_path, monkeypatch)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "name: topology-demo",
            """name: topology-demo
defaults:
  runtime:
    not_a_real_section:
      x: 1""",
        ),
        encoding="utf-8",
    )
    project = load_project_spec(manifest)

    with pytest.raises(Exception, match="not_a_real_section|override"):
        resolve_project_workflow(project, workflow_id="workflow", environment_id="local")


def test_bundle_workflow_input_schema_carries_json_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    payload = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()
    input_schema = payload["workflow"]["input_schema"]

    assert input_schema["name"] == "ClaimInput"
    assert "json_schema" in input_schema
    assert "properties" in input_schema["json_schema"]


def test_runtime_effective_tags_engine_project_and_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    # A project default for one knob; the workflow YAML configures the model.
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "name: topology-demo",
            """name: topology-demo
defaults:
  runtime:
    provider_retry:
      max_attempts: 4""",
        ),
        encoding="utf-8",
    )
    project = load_project_spec(manifest)

    effective = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()["runtime_effective"]
    by_path = {e["path"]: e for e in effective}

    # project default surfaces with its value and source
    assert by_path["provider_retry.max_attempts"] == {
        "path": "provider_retry.max_attempts",
        "value": 4,
        "source": "project_default",
    }
    # an untouched retry knob materializes the engine default
    assert by_path["provider_retry.jitter_ratio"]["source"] == "engine_default"
    assert by_path["provider_retry.jitter_ratio"]["value"] == 0.1


def test_runtime_effective_higher_layer_override_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    # Project default sets max_attempts: 3; the workflow YAML overrides it to 5.
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "name: topology-demo",
            """name: topology-demo
defaults:
  runtime:
    provider_retry:
      max_attempts: 3""",
        ),
        encoding="utf-8",
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "  provider:\n    type: fake",
            "  provider:\n    type: fake\n  provider_retry:\n    max_attempts: 5",
        ),
        encoding="utf-8",
    )
    project = load_project_spec(manifest)

    effective = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()["runtime_effective"]
    entry = next(e for e in effective if e["path"] == "provider_retry.max_attempts")

    # The workflow override wins — value 5, tagged configured, not project_default.
    assert entry == {"path": "provider_retry.max_attempts", "value": 5, "source": "configured"}


def test_temporal_ui_link_derives_from_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project.bundle import _temporal_ui_url

    assert _temporal_ui_url("acme.abc.tmprl.cloud:7233") == "https://cloud.temporal.io"
    assert _temporal_ui_url("localhost:7233") == "http://localhost:8233"
    assert _temporal_ui_url("temporal.internal:7233") is None
    assert _temporal_ui_url(None) is None

    # An explicit TEMPORAL_UI_URL still wins over the derived value.
    manifest = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TEMPORAL_UI_URL", "https://temporal.example.internal")
    project = load_project_spec(manifest)
    links = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()["links"]
    assert links["temporal_ui"] == "https://temporal.example.internal"


@pytest.mark.asyncio
async def test_workflow_task_queue_workers_counts_pollers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from typeflux.project import workflow_task_queue_workers

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    class _Svc:
        async def describe_task_queue(self, request):
            # Two pollers on the queue.
            return SimpleNamespace(pollers=[object(), object()])

    class _Client:
        workflow_service = _Svc()

    async def fake_connect(spec, plugin=None):
        return _Client()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    polled: dict[str, str] = {}

    class _Svc2:
        async def describe_task_queue(self, request):
            polled["queue"] = request.task_queue.name
            return SimpleNamespace(pollers=[object(), object()])

    class _Client2:
        workflow_service = _Svc2()

    async def fake_connect2(spec, plugin=None):
        return _Client2()

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect2)

    status = await workflow_task_queue_workers(
        project, workflow_id="workflow", environment_id="local"
    )
    assert status.task_queue == "topology-demo-queue"
    assert status.reachable is True
    assert status.workers_polling == 2

    # An explicit override is the queue actually polled and reported.
    override = await workflow_task_queue_workers(
        project, workflow_id="workflow", environment_id="local", task_queue="override-queue"
    )
    assert override.task_queue == "override-queue"
    assert polled["queue"] == "override-queue"


@pytest.mark.asyncio
async def test_workflow_task_queue_workers_degrades_when_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import workflow_task_queue_workers

    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    async def fake_connect(spec, plugin=None):
        raise RuntimeError("Timeout expired")

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)

    status = await workflow_task_queue_workers(
        project, workflow_id="workflow", environment_id="local"
    )

    assert status.reachable is False
    assert status.workers_polling == 0
    assert "could not reach Temporal" in (status.detail or "")


def test_bundle_exposes_canonical_observability_trace_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")

    # Exactly the Langfuse trace title the writer records runs under.
    assert bundle.workflow.observability_trace_name == "TypefluxWorkflow:TopologyDemoWorkflow"
