from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_project_graph import graph_repo as graph_repo
from test_project_graph import run_git, write
from tools.project_graph.ci import aggregate, plan, required_jobs, workflow_jobs
from tools.project_graph.compiler import compile_graph
from tools.project_graph.explorer import render
from tools.project_graph.records import Ledger
from tools.project_graph.release import receipt
from tools.project_graph.repository import GraphError, Repository
from tools.project_graph.verification import RUNNERS, verify


def policy(root, jobs):
    write(
        root,
        "project-graph/policy.yaml",
        yaml.safe_dump(
            {
                "apiVersion": "typeflux.dev/graph-policy/v1alpha1",
                "mode": "advisory",
                "requiredJobs": jobs,
            }
        ),
    )


def commit(root):
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "policy fixture")
    return run_git(root, "rev-parse", "HEAD")


def test_policy_removal_cannot_reduce_own_obligations(graph_repo):
    policy(graph_repo, ["Python", "Web"])
    base = commit(graph_repo)
    policy(graph_repo, ["Web", "New check"])
    result = plan(Repository(graph_repo, base), Repository(graph_repo))
    assert result["requiredJobs"] == ["New check", "Python", "Web"]
    assert not result["selectionEnabled"]


def test_bootstrap_expands_base_workflow_matrix(graph_repo):
    write(
        graph_repo,
        ".github/workflows/ci.yml",
        """name: CI
on: [pull_request]
jobs:
  tests:
    name: Tests (Python ${{ matrix.python-version }})
    strategy:
      matrix:
        python-version: ['3.11', '3.12']
  web:
    name: Web
""",
    )
    assert workflow_jobs(Repository(graph_repo)) == {
        "Tests (Python 3.11)",
        "Tests (Python 3.12)",
        "Web",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda jobs: jobs.clear(),
        lambda jobs: jobs[0].update(conclusion="skipped"),
        lambda jobs: jobs[0].update(conclusion="neutral"),
        lambda jobs: jobs[0].update(conclusion="cancelled"),
        lambda jobs: jobs[0].update(conclusion="failure"),
        lambda jobs: jobs[0].update(status="in_progress"),
        lambda jobs: jobs[0].update(run_attempt=2),
        lambda jobs: jobs[0].update(head_sha="wrong"),
        lambda jobs: jobs.append(jobs[0].copy()),
    ],
)
def test_aggregate_refuses_missing_nonpassing_duplicate_or_stale_checks(graph_repo, mutation):
    policy(graph_repo, ["Python"])
    sha = commit(graph_repo)
    repo = Repository(graph_repo, sha)
    run = {"id": 123, "head_sha": sha, "run_attempt": 1, "event": "push"}
    jobs = [
        {
            "id": 456,
            "name": "Python",
            "run_id": 123,
            "run_attempt": 1,
            "head_sha": sha,
            "status": "completed",
            "conclusion": "success",
        }
    ]
    args = dict(run_id=123, attempt=1, expected_head=sha, tested_revision=sha)
    assert aggregate(repo, repo, run, [{"jobs": jobs}], **args)["passed"]
    mutation(jobs)
    assert not aggregate(repo, repo, run, [{"jobs": jobs}], **args)["passed"]


def test_aggregate_binds_pr_merge_to_base_and_head(graph_repo):
    policy(graph_repo, ["Python"])
    base = commit(graph_repo)
    run_git(graph_repo, "switch", "-c", "proposed")
    write(graph_repo, "src/worker.py", "new implementation")
    head = commit(graph_repo)
    run_git(graph_repo, "switch", "main")
    run_git(graph_repo, "merge", "--no-ff", "proposed", "-m", "integration")
    merged = run_git(graph_repo, "rev-parse", "HEAD")
    run = {"id": 1, "head_sha": head, "run_attempt": 1, "event": "pull_request"}
    jobs = [
        {
            "jobs": [
                {
                    "id": 2,
                    "name": "Python",
                    "run_id": 1,
                    "run_attempt": 1,
                    "head_sha": head,
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }
    ]
    assert aggregate(
        Repository(graph_repo, base),
        Repository(graph_repo, merged),
        run,
        jobs,
        run_id=1,
        attempt=1,
        expected_head=head,
        tested_revision=merged,
    )["passed"]
    with pytest.raises(GraphError, match="merge revision"):
        aggregate(
            Repository(graph_repo, base),
            Repository(graph_repo, head),
            run,
            jobs,
            run_id=1,
            attempt=1,
            expected_head=head,
            tested_revision=head,
        )


def test_policy_cannot_become_executable_or_enable_selection(graph_repo):
    policy(graph_repo, ["Python"])
    p = graph_repo / "project-graph/policy.yaml"
    p.write_text(p.read_text() + "command: echo unsafe\n")
    with pytest.raises(GraphError, match="fields"):
        required_jobs(Repository(graph_repo))


def test_task_resume_invalidates_stale_results_and_reconciles_effects(graph_repo):
    write(graph_repo, ".gitignore", ".project-graph/\n")
    ledger = Ledger(graph_repo, "issue-46")
    repo = Repository(graph_repo)
    ledger.append("create", {"intent": "review", "acceptance": ["observe progress"]}, repo)
    ledger.append("transition", {"state": "implementing"}, repo)
    ledger.append(
        "verification",
        {"authority": "local-execution", "result": "passed", "verifier": "graph"},
        repo,
    )
    assert ledger.resume(repo)["verification"][0]["applicable"]
    write(graph_repo, "src/worker.py", "changed after test")
    assert not ledger.resume(Repository(graph_repo))["verification"][0]["applicable"]
    ledger.append("effect", {"key": "create-pr:branch", "state": "pending"}, repo)
    assert not ledger.resume(repo)["canRetryExternalEffects"]
    with pytest.raises(GraphError, match="reconcile"):
        ledger.append("effect", {"key": "create-pr:branch", "state": "pending"}, repo)
    ledger.append(
        "effect",
        {
            "key": "create-pr:branch",
            "state": "confirmed",
            "receipt": "https://github.com/example/repo/pull/1",
        },
        repo,
    )
    assert ledger.resume(repo)["canRetryExternalEffects"]
    with pytest.raises(GraphError):
        ledger.append("transition", {"state": "accepted"}, repo)


def test_task_journal_detects_tampering_and_path_escape(graph_repo):
    ledger = Ledger(graph_repo, "safe")
    ledger.append("create", {"intent": "review", "acceptance": ["test"]}, Repository(graph_repo))
    ledger.path.write_text(ledger.path.read_text().replace('"review"', '"tampered"'))
    with pytest.raises(GraphError, match="integrity"):
        ledger.read()
    with pytest.raises(GraphError):
        Ledger(graph_repo, "../../escape")


def test_release_receipt_binds_artifact_bytes_without_inventing_journey_success(
    graph_repo, tmp_path
):
    repo = Repository(graph_repo)
    artifact = tmp_path.parent / "candidate.whl"
    artifact.write_bytes(b"candidate bytes")
    result = receipt(repo, [artifact])
    assert result["sourceRevision"] == repo.revision
    assert result["artifacts"][0]["sha256"] == hashlib.sha256(b"candidate bytes").hexdigest()
    assert all(check["status"] == "not-recorded" for check in result["journeyEvidence"])
    assert result["rollout"]["status"] == "pending"
    write(graph_repo, "src/worker.py", "dirty")
    with pytest.raises(GraphError, match="clean"):
        receipt(Repository(graph_repo), [artifact])


def test_explorer_embeds_repository_text_as_data(graph_repo):
    graph = compile_graph(Repository(graph_repo))
    graph["nodes"][0]["description"] = '</script><script>alert("unsafe")</script>'
    html = render(graph)
    assert '</script><script>alert("unsafe")' not in html
    assert "\\u003c/script>" in html
    assert ".innerHTML" not in html
    assert "textContent" in html


def test_explorer_refuses_stale_or_unrelated_ci_receipts(graph_repo):
    graph = compile_graph(Repository(graph_repo))
    evidence = {
        "kind": "ci-verification",
        "testedRevision": graph["revision"],
        "treeDigest": graph["treeDigest"],
        "checks": [],
    }
    assert "Imported CI receipt" in render(graph, verification=evidence)
    for field in ("testedRevision", "treeDigest", "kind"):
        with pytest.raises(GraphError, match="exact tested revision"):
            render(graph, verification={**evidence, field: "unrelated"})


def test_verifier_records_failure_and_scrubs_credentials(graph_repo, monkeypatch):
    write(graph_repo, ".gitignore", ".project-graph/\n")
    ledger = Ledger(graph_repo, "verify")
    ledger.append("create", {"intent": "test", "acceptance": ["pass"]}, Repository(graph_repo))
    monkeypatch.setenv("CODEX_API_KEY", "must-not-reach-test")
    monkeypatch.setitem(
        RUNNERS,
        "fixture",
        (
            ".",
            (
                sys.executable,
                "-c",
                "import os,sys; sys.exit(9 if 'CODEX_API_KEY' not in os.environ else 1)",
            ),
        ),
    )
    record = verify(graph_repo, "verify", "fixture")
    assert record["payload"]["result"] == "failed"
    assert record["payload"]["exitCode"] == 9
    assert ledger.resume(Repository(graph_repo))["verification"][0]["applicable"]


def test_verifier_timeout_stops_descendant_processes(graph_repo, monkeypatch, tmp_path):
    write(graph_repo, ".gitignore", ".project-graph/\n")
    ledger = Ledger(graph_repo, "timeout")
    ledger.append("create", {"intent": "test", "acceptance": ["pass"]}, Repository(graph_repo))
    # A descendant inherits stdout; communicate would hang if only the parent died.
    command = (
        sys.executable,
        "-c",
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)",
    )
    monkeypatch.setitem(RUNNERS, "fixture", (".", command))
    record = verify(graph_repo, "timeout", "fixture", timeout=0.5)
    assert record["payload"]["result"] == "timeout"


def test_workflow_aggregate_covers_jobs_and_keeps_credentials_out_of_validator():
    root = Path(__file__).resolve().parents[3]
    repo = Repository(root)
    assert workflow_jobs(repo) == required_jobs(repo)
    workflow = yaml.load(repo.text(".github/workflows/ci.yml"), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    aggregate_job = jobs["graph-aggregate"]
    assert set(aggregate_job["needs"]) == set(jobs) - {"graph-aggregate"}
    assert aggregate_job["permissions"] == {"contents": "read", "actions": "read"}
    validator = next(
        s for s in aggregate_job["steps"] if s.get("name", "").startswith("Verify every")
    )
    assert "GH_TOKEN" not in validator.get("env", {})
    assert 'tools_root="$GITHUB_WORKSPACE/trusted"' in validator["run"]
    assert '--tested-revision "$GITHUB_SHA"' in validator["run"]
    for step in aggregate_job["steps"]:
        if "run" in step:
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)


def test_auto_fix_graph_context_is_bounded_and_scrubs_secrets(monkeypatch, tmp_path):
    import subprocess

    from scripts.auto_fix_issue import graph_planning_context

    calls = []
    monkeypatch.setenv("CODEX_API_KEY", "never-expose")

    def fake(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='{"items":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert "untrusted data" in graph_planning_context(tmp_path, ["capability.workflow-review"])
    assert "CODEX_API_KEY" not in calls[0][1]["env"]
    assert "--max-chars" in calls[0][0]
    with pytest.raises(SystemExit):
        graph_planning_context(tmp_path, ["; echo unsafe"])
