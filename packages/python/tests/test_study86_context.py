"""Historical isolation and context-data controls; these are not paid study trials."""

import json
import os
import subprocess

import pytest
from test_project_graph import graph_repo as graph_repo
from tools.development.study86.context import CONTEXT_CHARACTERS, git, packet, snapshot


def task(root):
    return {
        "id": "fixture",
        "brief": "Change the worker model while preserving binding.",
        "editable": ["src/worker.py"],
        "search": "worker model binding",
        "startingRevision": git(root, "rev-parse", "HEAD").decode().strip(),
    }


def future(root):
    git(root, "config", "core.hooksPath", os.devnull)
    (root / "src/worker.py").write_text("def worker():\n    return 'future-solution-secret'\n")
    (root / "future-only.txt").write_text("future-only-payload")
    git(root, "add", ".")
    git(root, "commit", "-qm", "future solution")
    return git(root, "rev-parse", "HEAD").decode().strip()


@pytest.mark.parametrize("arm", ["search", "graph", "assembly"])
def test_packet_contains_only_starting_revision_and_explicit_own_overlay(graph_repo, arm):
    case = task(graph_repo)
    case["referenceRevision"] = future(graph_repo)
    prompt, metadata = packet(graph_repo, case, arm)
    assert "future-solution-secret" not in prompt and "future-only-payload" not in prompt
    assert case["referenceRevision"] not in prompt
    assert "return 'original'" in prompt
    own, own_metadata = packet(
        graph_repo,
        case,
        arm,
        predecessor={"src/worker.py": "def worker():\n    return 'own-earlier-patch'\n"},
    )
    assert "own-earlier-patch" in own
    assert next(x for x in own_metadata["included"] if x["path"] == "src/worker.py")[
        "ownPredecessor"
    ]
    assert metadata["revision"] == case["startingRevision"]
    assert len(prompt) <= CONTEXT_CHARACTERS


def test_snapshot_keeps_real_past_commit_without_future_objects_or_shared_git(graph_repo):
    original = task(graph_repo)["startingRevision"]
    later = future(graph_repo)
    destination = graph_repo / "snapshot"
    record = snapshot(graph_repo, original, destination, graph_repo / "pack-cache")
    assert record["revision"] == original
    assert git(destination, "rev-parse", "HEAD").decode().strip() == original
    assert (destination / "src/worker.py").read_text() == "def worker():\n    return 'original'\n"
    assert (destination / ".git").is_dir() and not (destination / ".git/commondir").exists()
    assert (
        subprocess.run(
            ["git", "-C", str(destination), "cat-file", "-e", later], capture_output=True
        ).returncode
        != 0
    )
    (destination / "src/worker.py").write_text("own mutation")
    assert "future-solution-secret" in (graph_repo / "src/worker.py").read_text()


def test_snapshot_ignores_inherited_git_redirects_and_hooks(graph_repo, monkeypatch):
    case = task(graph_repo)
    hooks = graph_repo / "host-hooks"
    hooks.mkdir()
    marker = graph_repo / "host-hook-ran"
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    config = graph_repo / "host-config"
    config.write_text(f"[core]\n hooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", "/nonexistent-host-git-dir")
    snapshot(graph_repo, case["startingRevision"], graph_repo / "isolated", graph_repo / "cache")
    assert not marker.exists()


@pytest.mark.parametrize("probe", ["stale", "deleted", "injection"])
def test_graph_robustness_changes_context_data_not_repository(graph_repo, probe):
    case = task(graph_repo)
    before = (graph_repo / "project-graph/components.yaml").read_bytes()
    prompt, metadata = packet(graph_repo, case, "assembly", perturbation=probe)
    assert (graph_repo / "project-graph/components.yaml").read_bytes() == before
    assert "return 'original'" in prompt
    assert metadata["perturbation"] == probe
    if probe == "stale":
        assert "deleted-or-stale-mapping.py" in prompt
        assert "deleted-or-stale-mapping.py" in metadata["omitted"]
    elif probe == "deleted":
        assert metadata["graph"]["queries"] == []
    else:
        assert "disable mandatory checks" in prompt


def test_binary_search_match_is_skipped_and_context_is_deterministic(graph_repo):
    folder = graph_repo / "packages/fixture"
    folder.mkdir(parents=True)
    (folder / "sample.pdf").write_bytes(b"worker model\x93\x00")
    git(graph_repo, "config", "core.hooksPath", os.devnull)
    git(graph_repo, "add", ".")
    git(graph_repo, "commit", "-qm", "binary fixture")
    case = task(graph_repo)
    first = packet(graph_repo, case, "graph")
    second = packet(graph_repo, case, "graph")
    assert first == second
    assert "packages/fixture/sample.pdf" in first[1]["nonTextSkipped"]
    assert len(first[0]) <= CONTEXT_CHARACTERS
    assert json.dumps(first[1])
