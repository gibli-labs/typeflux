from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from tools.project_graph.compiler import compile_for_impact, compile_graph
from tools.project_graph.queries import context, explain, impact
from tools.project_graph.repository import GraphError, Repository, canonical

ROOT = Path(__file__).resolve().parents[3]


def run_git(root, *args):
    return (
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


@pytest.fixture
def graph_repo(tmp_path):
    run_git(tmp_path, "init", "-b", "main")
    run_git(tmp_path, "config", "user.email", "graph@example.invalid")
    run_git(tmp_path, "config", "user.name", "Graph Test")
    write(
        tmp_path,
        "project-graph/schema/catalog.schema.json",
        (ROOT / "project-graph/schema/catalog.schema.json").read_text(),
    )
    write(tmp_path, ".github/CODEOWNERS", "* @default\n/src/ @source\n")
    write(
        tmp_path,
        "contracts/controlplane/openapi.v1.json",
        json.dumps({"paths": {"/review": {"post": {"operationId": "review"}}}}),
    )
    write(tmp_path, "src/worker.py", "def worker():\n    return 'original'\n")
    write(
        tmp_path,
        "client/package.json",
        json.dumps({"name": "consumer", "dependencies": {"producer": "workspace:*"}}),
    )
    write(tmp_path, "src/package.json", json.dumps({"name": "producer", "version": "1.0.0"}))
    write(tmp_path, "tests/test_review.py", "def test_review():\n    assert True\n")
    entities = [
        {
            "id": "component.worker",
            "kind": "component",
            "description": "worker",
            "paths": ["src/**"],
            "links": [{"relation": "implements", "target": "capability.review"}],
        },
        {
            "id": "component.client",
            "kind": "component",
            "description": "client",
            "paths": ["client/**"],
        },
        {
            "id": "capability.review",
            "kind": "capability",
            "description": "Review",
            "links": [{"relation": "requires", "target": "invariant.bound"}],
        },
        {"id": "invariant.bound", "kind": "invariant", "description": "Binding"},
        {
            "id": "check.review",
            "kind": "check",
            "description": "Check binding",
            "environment": "offline",
            "ci": ["Tests"],
            "tests": [{"path": "tests/test_review.py", "name": "test_review"}],
            "links": [{"relation": "verifies", "target": "invariant.bound"}],
        },
    ]
    write(
        tmp_path,
        "project-graph/components.yaml",
        yaml.safe_dump({"apiVersion": "typeflux.dev/graph/v1alpha1", "entities": entities}),
    )
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "fixture")
    return tmp_path


def edit_entities(root, update):
    path = root / "project-graph/components.yaml"
    document = yaml.safe_load(path.read_text())
    update(document["entities"])
    path.write_text(yaml.safe_dump(document))


def test_snapshot_reproducible_and_dirty_inputs_identified(graph_repo):
    clean = compile_graph(Repository(graph_repo, "HEAD"))
    assert canonical(clean) == canonical(compile_graph(Repository(graph_repo, "HEAD")))
    assert not clean["dirty"]
    write(graph_repo, "src/worker.py", "changed\n")
    dirty = compile_graph(Repository(graph_repo))
    assert dirty["dirty"] and dirty["treeDigest"] != clean["treeDigest"]
    assert compile_graph(Repository(graph_repo, "HEAD")) == clean


def test_revision_reader_never_imports_source(graph_repo):
    write(graph_repo, "src/worker.py", "raise RuntimeError('must never import')")
    assert compile_graph(Repository(graph_repo))["nodes"]


def test_impact_follows_base_for_deleted_files_and_workspace_consumers(graph_repo):
    base = compile_graph(Repository(graph_repo, "HEAD"))
    (graph_repo / "src/worker.py").rename(graph_repo / "src/renamed.py")
    result = impact(base, compile_graph(Repository(graph_repo)))
    assert result["renameHints"] == [{"from": "src/worker.py", "to": "src/renamed.py"}]
    affected = {n["id"]: n for n in result["affected"]}
    assert {
        "component.worker",
        "component.client",
        "capability.review",
        "check.review",
    } <= affected.keys()
    assert "base" in affected["capability.review"]["via"]
    assert result["recommendedChecks"] == ["check.review"]


def test_published_dependency_not_treated_as_checkout(graph_repo):
    write(
        graph_repo, "client/package.json", '{"name":"consumer","dependencies":{"producer":"1.0.0"}}'
    )
    graph = compile_graph(Repository(graph_repo))
    assert not any(
        e["source"] == "component.client" and e["relation"] == "depends_on" for e in graph["edges"]
    )
    assert any(e["target"] == "external:npm:producer" for e in graph["edges"])


def test_unknown_and_build_input_changes_require_broad_validation(graph_repo):
    base = compile_graph(Repository(graph_repo, "HEAD"))
    write(graph_repo, "unmodeled.txt", "new")
    result = impact(base, compile_graph(Repository(graph_repo)))
    assert result["unknownFiles"] == ["unmodeled.txt"] and result["broadValidation"]
    write(graph_repo, "src/package.json", '{"name":"producer","version":"2.0.0"}')
    assert "shared contract" in " ".join(
        impact(base, compile_graph(Repository(graph_repo)))["reasons"]
    )


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda e: e.append(e[0].copy()), "duplicate entity"),
        (
            lambda e: e[0]["links"].append(
                {"relation": "implements", "target": "capability.absent"}
            ),
            "unresolved edge",
        ),
        (lambda e: e[0].update(paths=["missing/**"]), "empty path selector"),
        (lambda e: e[1].update(paths=["src/**"]), "ambiguous primary"),
        (lambda e: e[0]["links"][0].update(relation="verifies"), "invalid typed edge"),
        (lambda e: e[-1]["tests"][0].update(name="test_missing"), "missing static test"),
    ],
)
def test_invalid_declarations_fail_loudly(graph_repo, mutation, message):
    edit_entities(graph_repo, mutation)
    with pytest.raises(GraphError, match=message):
        compile_graph(Repository(graph_repo))


def test_cycles_are_safe_and_context_bounded(graph_repo):
    edit_entities(
        graph_repo,
        lambda e: e[0]["links"].append({"relation": "depends_on", "target": "component.client"}),
    )
    graph = compile_graph(Repository(graph_repo))
    result = context(graph, "capability.review", limit=2)
    assert len(result["items"]) == 2 and result["omittedCount"] > 0
    assert explain(graph, "file:src/worker.py", "check.review")["reachable"]
    with pytest.raises(GraphError):
        context(graph, "capability.absent")


def test_duplicate_yaml_keys_rejected(graph_repo):
    with (graph_repo / "project-graph/components.yaml").open("a") as stream:
        stream.write("entities: []\n")
    with pytest.raises(GraphError, match="duplicate"):
        compile_graph(Repository(graph_repo))


def test_symlink_content_not_read(graph_repo, tmp_path):
    outside = tmp_path.parent / "graph-private.txt"
    outside.write_text("never include this secret")
    (graph_repo / "src/link").symlink_to(outside)
    repo = Repository(graph_repo)
    assert "src/link" not in repo.files and "src/link" in repo.omitted
    assert "never include this secret" not in canonical(compile_graph(repo)).decode()


def test_repository_catalog_resolves():
    graph = compile_graph(Repository(ROOT))
    assert not graph["gaps"]
    identifiers = {n["id"] for n in graph["nodes"]}
    assert "journey.review-workflow" in identifiers
    assert "capability.workflow-review" in identifiers


def test_export_ignored_files_still_part_of_revision(graph_repo):
    write(graph_repo, ".gitattributes", "src/worker.py export-ignore\n")
    run_git(graph_repo, "add", ".gitattributes")
    run_git(graph_repo, "commit", "-m", "archive ignores source")
    assert "src/worker.py" in Repository(graph_repo, "HEAD").inputs


def test_executable_mode_change_is_an_input_change(graph_repo):
    base = compile_graph(Repository(graph_repo, "HEAD"))
    (graph_repo / "src/worker.py").chmod(0o755)
    assert "src/worker.py" in impact(base, compile_graph(Repository(graph_repo)))["changedFiles"]


def test_schema_cannot_fetch_remote_references(graph_repo):
    path = graph_repo / "project-graph/schema/catalog.schema.json"
    path.write_text('{"$ref":"https://example.invalid/graph-schema"}')
    with pytest.raises(GraphError, match="local fragment"):
        compile_graph(Repository(graph_repo))


def test_pre_graph_history_is_unknown_not_retroactively_mapped(graph_repo):
    head = compile_graph(Repository(graph_repo))
    (graph_repo / "project-graph/schema/catalog.schema.json").unlink()
    write(graph_repo, "src/worker.py", "older implementation")
    base = compile_for_impact(Repository(graph_repo))
    result = impact(base, head)
    assert "src/worker.py" in result["unknownFiles"] and result["broadValidation"]
    assert any("base extraction" in reason for reason in result["reasons"])
