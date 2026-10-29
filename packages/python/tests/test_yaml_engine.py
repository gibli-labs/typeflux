from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from typeflux.core.errors import TypefluxError
from typeflux.yaml import (
    RuntimeCapabilityError,
    TypefluxYamlSpec,
    runtime_engine,
    validate_local_capabilities,
)
from typeflux.yaml.engine import require_temporal_engine

FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[3] / "contracts/local-runtime/golden/capability-cases.json"
    ).read_text()
)


def local_spec(name="root", steps=None):
    raw = copy.deepcopy(FIXTURES["base"])
    raw["name"] = raw["workflow"]["name"] = name
    if steps is not None:
        raw["workflow"]["steps"] = steps
    return TypefluxYamlSpec.model_validate(raw)


def test_shared_local_capability_cases():
    for case in FIXTURES["cases"]:
        raw = copy.deepcopy(FIXTURES["base"])
        for patch in case["patches"]:
            current = raw
            for key in patch["path"][:-1]:
                current = current[int(key)] if isinstance(current, list) else current[key]
            current[patch["path"][-1]] = patch["value"]
        spec = TypefluxYamlSpec.model_validate(raw)
        if "errorPath" in case:
            with pytest.raises(RuntimeCapabilityError) as caught:
                validate_local_capabilities(spec)
            assert isinstance(caught.value, TypefluxError)
            assert caught.value.path == case["errorPath"], case["name"]
            assert caught.value.workflow == "Root"
            assert "runtime.engine: temporal" in str(caught.value)
        else:
            assert validate_local_capabilities(spec) == ("root",), case["name"]


def test_local_closure_checks_skipped_nested_maps_and_references():
    root = local_spec(
        steps=[{"id": "nested", "workflow": "child", "when": {"path": "input.text", "eq": "never"}}]
    )
    child = local_spec(
        "child",
        [
            {
                "id": "items",
                "map": {
                    "workflow": "leaf",
                    "over": "input.items",
                    "concurrency": 2,
                    "collect": {"output": "schemas:List", "field": "items"},
                },
            }
        ],
    )
    leaf = local_spec("leaf")
    leaf.workflow.risk_tier = "human_gated"
    lookup = {"child": child, "leaf": leaf}
    with pytest.raises(RuntimeCapabilityError) as caught:
        validate_local_capabilities(root, resolve_workflow=lookup.get)
    assert (caught.value.workflow, caught.value.path) == ("leaf", "workflow.risk_tier")
    leaf.workflow.risk_tier = None
    assert validate_local_capabilities(root, resolve_workflow=lookup.get) == (
        "root",
        "child",
        "leaf",
    )
    for resolver, feature in ((None, "project resolver"), ({}.get, "not declared")):
        with pytest.raises(RuntimeCapabilityError, match=feature):
            validate_local_capabilities(root, resolve_workflow=resolver)
    leaf.project = "different"
    with pytest.raises(RuntimeCapabilityError, match="cross-project"):
        validate_local_capabilities(root, resolve_workflow=lookup.get)
    leaf.project = root.project
    lookup["leaf"] = root
    with pytest.raises(RuntimeCapabilityError, match="cyclic"):
        validate_local_capabilities(root, resolve_workflow=lookup.get)
    child.runtime.engine = "temporal"
    with pytest.raises(RuntimeCapabilityError, match="local graph requires"):
        validate_local_capabilities(root, resolve_workflow=lookup.get)


def test_local_diamond_resolves_each_child_once_and_preserves_errors():
    root = local_spec(steps=[{"id": "one", "workflow": "a"}, {"id": "two", "workflow": "b"}])
    lookup = {name: local_spec(name, [{"id": "shared", "workflow": "c"}]) for name in ("a", "b")}
    lookup["c"] = local_spec("c")
    calls = []

    def resolve(name):
        calls.append(name)
        return lookup[name]

    assert validate_local_capabilities(root, resolve_workflow=resolve) == ("root", "a", "c", "b")
    assert calls == ["a", "c", "b"]
    error = ValueError("invalid child environment")

    def broken(_):
        raise error

    with pytest.raises(ValueError) as caught:
        validate_local_capabilities(root, resolve_workflow=broken)
    assert caught.value is error
    alias_root = local_spec("alias", [{"id": "nested", "workflow": "alias"}])
    assert validate_local_capabilities(
        alias_root, resolve_workflow=lambda _: local_spec("different")
    ) == ("alias", "alias")


def test_engine_defaults_overrides_and_roundtrip(tmp_path):
    from typeflux.yaml import load_yaml_spec

    raw = copy.deepcopy(FIXTURES["base"])
    del raw["runtime"]["engine"]
    raw["task_queue"] = "existing"
    temporal = TypefluxYamlSpec.model_validate(raw)
    assert runtime_engine(temporal) == "temporal"
    assert require_temporal_engine(temporal) == "existing"
    assert "engine" not in temporal.model_dump()["runtime"]
    del raw["task_queue"]
    with pytest.raises(ValueError, match="task_queue"):
        TypefluxYamlSpec.model_validate(raw)
    path = tmp_path / "workflow.yaml"
    path.write_text(json.dumps(raw))
    local = load_yaml_spec(
        path,
        load_dotenv=False,
        runtime_defaults={"engine": "temporal"},
        overrides={"runtime": {"engine": "local"}},
    )
    assert runtime_engine(local) == "local"
    assert validate_local_capabilities(local) == ("root",)
    assert validate_local_capabilities(
        TypefluxYamlSpec.model_validate_json(local.model_dump_json())
    ) == ("root",)
    explicit = local.model_copy(deep=True)
    explicit.runtime.temporal.api_key = "secret-must-stay-excluded"
    dumped = explicit.model_dump(exclude={"runtime": {"temporal": {"api_key"}}})
    assert "api_key" not in dumped["runtime"]["temporal"]
    for engine in ("unknown", None):
        raw["runtime"]["engine"] = engine
        with pytest.raises(ValueError, match="engine"):
            TypefluxYamlSpec.model_validate(raw)


@pytest.mark.asyncio
async def test_temporal_entry_points_refuse_local_before_effects(monkeypatch):
    from typeflux.yaml import runtime, workflow

    def forbidden(*args, **kwargs):
        pytest.fail("Temporal construction was reached")

    monkeypatch.setattr(runtime, "collect_activities", forbidden)
    monkeypatch.setattr(runtime, "_build_provider", forbidden)
    monkeypatch.setattr(runtime, "load_env", forbidden)
    monkeypatch.setattr(workflow, "require_temporal", forbidden)
    spec = local_spec()
    for call in (
        lambda: runtime.prepare_runtime_build(spec),
        lambda: workflow.create_workflow(spec, {}),
    ):
        with pytest.raises(RuntimeCapabilityError, match="Temporal entry point"):
            call()
    with pytest.raises(RuntimeCapabilityError):
        await runtime.build_runtime(spec, prepared=object())
    with pytest.raises(RuntimeCapabilityError):
        await runtime._connect_client(spec, plugin=None)


@pytest.mark.asyncio
async def test_local_worker_query_rejects_even_with_queue_override(monkeypatch):
    from types import SimpleNamespace

    from typeflux.project import workers

    monkeypatch.setattr(
        workers, "resolve_project_workflow", lambda *a, **kw: SimpleNamespace(spec=local_spec())
    )
    with pytest.raises(RuntimeCapabilityError, match="Temporal entry point"):
        await workers.workflow_task_queue_workers(
            object(), workflow_id="root", environment_id="local", task_queue="override"
        )


def test_runtime_profile_selects_engine_before_schema_validation(tmp_path):
    from typeflux.project import load_project_profile, load_project_spec
    from typeflux.yaml import load_yaml_spec

    (tmp_path / "profile.yaml").write_text("name: local\nkind: runtime\nruntime: {engine: local}\n")
    (tmp_path / "typeflux.project.yaml").write_text(
        "name: demo\nworkflows: [{id: root, path: workflow.yaml}]\n"
        "profiles: {runtime: {local: profile.yaml}}\n"
    )
    raw = copy.deepcopy(FIXTURES["base"])
    del raw["runtime"]["engine"]
    path = tmp_path / "workflow.yaml"
    path.write_text(json.dumps(raw))
    project = load_project_spec(tmp_path / "typeflux.project.yaml")
    profile = load_project_profile(project, kind="runtime", profile_id="local")
    local = load_yaml_spec(path, load_dotenv=False, overrides=profile.overrides())
    assert validate_local_capabilities(local) == ("root",)


def test_ts_binding_rejects_local_engine_in_every_configuration_layer(tmp_path, monkeypatch):
    from typeflux.project.binding_ts import TsBindingConfigError, TsBindingTarget

    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("TF_LOCAL_ENGINE_BOUNDARY", raising=False)
    for layer in ("defaults", "workflow", "profile", "environment", "per_workflow"):
        root = tmp_path / layer
        root.mkdir()
        manifest = {
            "name": "demo",
            "workflows": [{"id": "root", "path": "workflow.yaml"}],
            "environments": {"local": "env.yaml"},
        }
        raw = copy.deepcopy(FIXTURES["base"])
        del raw["runtime"]["engine"]
        raw["runtime"]["temporal"] = {"address": "localhost:7233"}
        env = {"name": "local", "variables": {"TF_LOCAL_ENGINE_BOUNDARY": "local"}}
        engine = {"engine": "${TF_LOCAL_ENGINE_BOUNDARY}"}
        if layer == "defaults":
            manifest["defaults"] = {"runtime": engine}
        elif layer == "workflow":
            raw["runtime"].update(engine)
        elif layer == "profile":
            manifest["profiles"] = {"runtime": {"chosen": "profile.yaml"}}
            manifest["workflows"][0]["profiles"] = {"runtime": "chosen"}
            (root / "profile.yaml").write_text(
                json.dumps({"name": "chosen", "kind": "runtime", "runtime": engine})
            )
        elif layer == "environment":
            env["overrides"] = {"runtime": engine}
        else:
            env["workflows"] = {"root": {"overrides": {"runtime": engine}}}
        (root / "project.yaml").write_text(json.dumps(manifest))
        (root / "workflow.yaml").write_text(json.dumps(raw))
        (root / "env.yaml").write_text(json.dumps(env))
        with pytest.raises(TsBindingConfigError, match="runtime.engine.*temporal"):
            TsBindingTarget.from_project_yaml(
                root / "project.yaml", workflow_id="root", environment_id="local"
            )
        # A higher-precedence explicit Temporal choice still works.
        env["workflows"] = {"root": {"overrides": {"runtime": {"engine": "temporal"}}}}
        (root / "env.yaml").write_text(json.dumps(env))
        target = TsBindingTarget.from_project_yaml(
            root / "project.yaml", workflow_id="root", environment_id="local"
        )
        assert target.address == "localhost:7233"
