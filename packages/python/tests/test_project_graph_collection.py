"""Command/refusal regressions; actual runner collection is a separate graph check."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from tools.project_graph import collection
from tools.project_graph.repository import GraphError


def fixture(tmp_path, package, runner, *, installed=True):
    cwd = tmp_path / package
    cwd.mkdir(parents=True)
    (cwd / "package.json").write_text('{"name":"fixture"}')
    suffix = ".spec.ts" if runner == "playwright" else ".test.ts"
    path = cwd / ("fixture" + suffix)
    path.write_text("// collection fixture")
    entry = (
        cwd
        / "node_modules"
        / ("@playwright/test/cli.js" if runner == "playwright" else "vitest/vitest.mjs")
    )
    if installed:
        entry.parent.mkdir(parents=True)
        entry.write_text("// installed fixture entrypoint; command is inspected, not executed")
    graph = {
        "nodes": [
            {
                "id": "check.fixture",
                "kind": "check",
                "tests": [{"path": str(path.relative_to(tmp_path)), "name": "fixture identity"}],
            }
        ],
        "treeDigest": "source",
        "digest": "graph",
        "revision": "revision",
        "dirty": False,
    }
    return cwd, path, entry, graph


@pytest.mark.parametrize(
    ("package", "runner"),
    [
        ("clients/console", "vitest"),
        ("packages/typescript/temporal-yaml", "vitest"),
        ("clients/console", "playwright"),
    ],
)
def test_collection_uses_installed_package_cli(tmp_path, monkeypatch, package, runner):
    cwd, path, entry, graph = fixture(tmp_path, package, runner)
    monkeypatch.setattr(
        collection, "Repository", lambda root: SimpleNamespace(tree_digest="source")
    )
    monkeypatch.setenv("FIXTURE_API_KEY", "fixture-not-a-credential")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert command[:2] == ["node", str(entry)]
        assert kwargs["cwd"] == cwd
        assert "FIXTURE_API_KEY" not in kwargs["env"]
        if runner == "vitest":
            assert command[2:4] == ["list", "--no-static-parse"]
            assert command[-2:] == [str(path), "--json"]
            report = [{"file": str(path), "name": "group > fixture identity"}]
        else:
            assert command[2:5] == ["test", "--list", "--reporter=json"]
            report = {
                "config": {"rootDir": str(cwd)},
                "suites": [
                    {
                        "specs": [
                            {"file": path.name, "title": "fixture identity", "id": "fixture-id"}
                        ]
                    }
                ],
            }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(collection.subprocess, "run", run)
    result = collection.collect_tests(tmp_path, graph)
    assert len(commands) == 1
    assert result["collected"][0]["name"] == "fixture identity"
    assert result["meaning"] == "test identities collected; tests have not run"


@pytest.mark.parametrize("runner", ["vitest", "playwright"])
def test_missing_installed_cli_refuses_collection(tmp_path, monkeypatch, runner):
    _, _, _, graph = fixture(tmp_path, "clients/console", runner, installed=False)

    def unexpected(*args, **kwargs):
        raise AssertionError("missing setup must never invoke a package manager or download")

    monkeypatch.setattr(collection.subprocess, "run", unexpected)
    with pytest.raises(GraphError, match="installed.*sync all"):
        collection.collect_tests(tmp_path, graph)


def test_collection_keeps_source_drift_failure(tmp_path, monkeypatch):
    _, path, _, graph = fixture(tmp_path, "clients/console", "vitest")
    monkeypatch.setattr(
        collection, "Repository", lambda root: SimpleNamespace(tree_digest="changed")
    )
    monkeypatch.setattr(
        collection.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([{"file": str(path), "name": "fixture identity"}]),
            stderr="",
        ),
    )
    with pytest.raises(GraphError, match="test collection changed repository inputs"):
        collection.collect_tests(tmp_path, graph)


def test_collection_keeps_missing_identity_failure(tmp_path, monkeypatch):
    _, _, _, graph = fixture(tmp_path, "clients/console", "vitest")
    monkeypatch.setattr(
        collection, "Repository", lambda root: SimpleNamespace(tree_digest="source")
    )
    monkeypatch.setattr(
        collection.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="[]", stderr=""),
    )
    with pytest.raises(GraphError, match="declared tests absent from collection"):
        collection.collect_tests(tmp_path, graph)
