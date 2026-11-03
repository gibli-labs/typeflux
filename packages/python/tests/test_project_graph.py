from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from tools.project_graph.__main__ import main as graph_main
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
        "tools/development/checks.json",
        (ROOT / "tools/development/checks.json").read_text(),
    )
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
            "ci": ["python-3.12"],
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


@pytest.mark.parametrize("lane", ["python", "Tests (Python 3.12)", "unknown-ci"])
def test_validate_rejects_unknown_ci_execution_ids(graph_repo, capsys, lane):
    edit_entities(graph_repo, lambda entities: entities[-1].update(ci=[lane]))
    code = graph_main(["--root", str(graph_repo), "validate"])
    output = capsys.readouterr()
    assert code == 2, output.out
    assert "project-graph/components.yaml" in output.err
    assert "check.review" in output.err
    assert f"unknown CI execution ID {lane!r}" in output.err
    assert "tools/development/checks.json" in output.err
    registry = json.loads((graph_repo / "tools/development/checks.json").read_text())
    assert all(identifier in output.err for identifier in registry)


def test_registered_ci_execution_ids_validate(graph_repo, capsys):
    registry = json.loads((graph_repo / "tools/development/checks.json").read_text())
    edit_entities(graph_repo, lambda entities: entities[-1].update(ci=sorted(registry)))
    assert graph_main(["--root", str(graph_repo), "validate"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


@pytest.mark.parametrize(
    "content", [None, "{", "[]", "{}", '{"python-3.12": {"name": "Tests", "verifier": null}}']
)
def test_missing_or_malformed_ci_registry_fails_validation(graph_repo, content):
    path = graph_repo / "tools/development/checks.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    with pytest.raises(GraphError, match=r"tools/development/checks\.json"):
        compile_graph(Repository(graph_repo))


def test_ci_registry_is_revision_bound_data_without_imports(graph_repo):
    base = run_git(graph_repo, "rev-parse", "HEAD")
    marker = graph_repo / "imported-candidate-code"
    write(
        graph_repo,
        "tools/development/registry.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('candidate registry code must not execute')\n",
    )
    write(
        graph_repo,
        "tools/development/checks.json",
        json.dumps({"candidate-lane": {"name": "Candidate lane", "verifier": "python"}}),
    )
    edit_entities(graph_repo, lambda entities: entities[-1].update(ci=["candidate-lane"]))
    for repo, expected in (
        (Repository(graph_repo), "candidate-lane"),
        (Repository(graph_repo, base), "python-3.12"),
    ):
        graph = compile_graph(repo)
        assert next(node for node in graph["nodes"] if node["id"] == "check.review")["ci"] == [
            expected
        ]
    assert not marker.exists()
    edit_entities(graph_repo, lambda entities: entities[-1].update(ci=["python-3.12"]))
    with pytest.raises(GraphError, match="unknown CI execution ID 'python-3.12'"):
        compile_graph(Repository(graph_repo))


@pytest.mark.parametrize("with_policy", [False, True])
def test_legacy_policy_cannot_bypass_worktree_ci_validation(graph_repo, capsys, with_policy):
    (graph_repo / "tools/development/checks.json").unlink()
    if with_policy:
        write(
            graph_repo,
            "project-graph/policy.yaml",
            yaml.safe_dump(
                {
                    "apiVersion": "typeflux.dev/graph-policy/v1alpha1",
                    "mode": "advisory",
                    "requiredJobs": ["Tests"],
                }
            ),
        )
    edit_entities(graph_repo, lambda entities: entities[-1].update(ci=["Tests"]))
    run_git(graph_repo, "add", ".")
    run_git(graph_repo, "commit", "-m", "pre-registry snapshot")
    assert graph_main(["--root", str(graph_repo), "validate"]) == 2
    assert "tools/development/checks.json" in capsys.readouterr().err
    assert graph_main(["--root", str(graph_repo), "validate", "--revision", "HEAD"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_modern_snapshot_requires_ci_registry(graph_repo):
    (graph_repo / "tools/development/checks.json").unlink()
    write(
        graph_repo,
        "project-graph/policy.yaml",
        yaml.safe_dump(
            {
                "apiVersion": "typeflux.dev/graph-policy/v1",
                "mode": "advisory",
                "requiredChecks": ["python-3.12"],
            }
        ),
    )
    run_git(graph_repo, "add", ".")
    run_git(graph_repo, "commit", "-m", "missing modern registry")
    with pytest.raises(GraphError, match=r"tools/development/checks\.json"):
        compile_graph(Repository(graph_repo, "HEAD"))


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


def test_clean_symlink_fingerprints_match_and_target_changes_are_detected(graph_repo):
    link = graph_repo / "src/link"
    link.symlink_to("worker.py")
    run_git(graph_repo, "add", "src/link")
    run_git(graph_repo, "commit", "-m", "link")
    committed, working = Repository(graph_repo, "HEAD"), Repository(graph_repo)
    assert committed.inputs == working.inputs
    assert committed.tree_digest == working.tree_digest
    assert impact(compile_graph(committed), compile_graph(working))["changedFiles"] == []
    link.unlink()
    link.symlink_to("missing.py")
    assert impact(compile_graph(committed), compile_graph(Repository(graph_repo)))[
        "changedFiles"
    ] == ["src/link"]


def test_uninitialized_gitlink_is_retained_in_worktree_inputs(graph_repo):
    oid = run_git(graph_repo, "rev-parse", "HEAD")
    run_git(graph_repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},vendor")
    run_git(graph_repo, "commit", "-m", "gitlink")
    committed, working = Repository(graph_repo, "HEAD"), Repository(graph_repo)
    assert committed.inputs == working.inputs
    assert committed.tree_digest == working.tree_digest
    assert impact(compile_graph(committed), compile_graph(working))["changedFiles"] == []


def test_initialized_gitlink_detects_changed_commit(graph_repo):
    child = graph_repo / "vendor"
    run_git(graph_repo, "clone", "--no-hardlinks", str(graph_repo), str(child))
    run_git(child, "config", "user.email", "graph@example.invalid")
    run_git(child, "config", "user.name", "Graph Test")
    oid = run_git(child, "rev-parse", "HEAD")
    run_git(graph_repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},vendor")
    run_git(graph_repo, "commit", "-m", "gitlink")
    committed = Repository(graph_repo, "HEAD")
    assert committed.tree_digest == Repository(graph_repo).tree_digest
    write(child, "src/worker.py", "next submodule version")
    run_git(child, "add", "src/worker.py")
    run_git(child, "commit", "-m", "advance child")
    assert impact(compile_graph(committed), compile_graph(Repository(graph_repo)))[
        "changedFiles"
    ] == ["vendor"]
