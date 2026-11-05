"""Trusted-gate regressions; simulated GitHub transport never runs candidate source."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tools.development import acceptance
from tools.project_graph.repository import GraphError


@pytest.fixture
def gate(monkeypatch, tmp_path):
    required = json.loads(acceptance.Path(acceptance.__file__).with_name("checks.json").read_text())
    head = "a" * 40
    run = {
        "id": 17,
        "run_attempt": 2,
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "owner/repo"},
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_sha": head,
        "head_branch": "main",
        "html_url": "https://github.com/owner/repo/actions/runs/17",
    }
    jobs = [
        {
            "id": index,
            "name": check["name"],
            "status": "completed",
            "conclusion": "success",
            "run_id": 17,
            "run_attempt": 2,
            "head_sha": head,
        }
        for index, check in enumerate(required.values(), 1)
    ]
    published = []

    def api(path, payload=None):
        if payload:
            published.append(payload)
            return {}
        if "/workflows/ci.yml/runs?" in path:
            return {"workflow_runs": [{"id": 17}]}
        assert path == "repos/owner/repo/actions/runs/17"
        return run

    def pages(path, key):
        if key == "jobs":
            assert path == "repos/owner/repo/actions/runs/17/attempts/2/jobs"
            return jobs
        assert key == "artifacts"
        return [
            {"id": key, "name": f"evidence-{key}-2", "expired": False, "size_in_bytes": 1}
            for key in required
        ]

    def receipt(archive):
        identifier = archive.decode()
        return {
            "kind": "development-verification",
            "checkId": identifier,
            "runId": "17",
            "attempt": "2",
            "sourceRevision": head,
            "treeDigest": "digest",
            "dirty": False,
            "status": "passed",
            "tests": [],
        }

    # These tests isolate the job guard; existing graph/receipt tests cover
    # revision and journey policy. No GitHub requests or candidate execution.
    monkeypatch.setattr(acceptance, "api", api)
    monkeypatch.setattr(acceptance, "pages", pages)
    monkeypatch.setattr(
        acceptance.subprocess, "check_output", lambda args: args[-1].split("/")[-2].encode()
    )
    monkeypatch.setattr(acceptance, "read_receipt", receipt)
    monkeypatch.setattr(acceptance, "git", lambda *args: head.encode())
    monkeypatch.setattr(
        acceptance,
        "Repository",
        lambda *args: SimpleNamespace(revision=head, tree_digest="digest", files=set()),
    )
    monkeypatch.setattr(acceptance, "aggregate", lambda *args, **kwargs: {"passed": True})
    monkeypatch.setattr(acceptance, "compile_graph", lambda repo: {"nodes": []})
    monkeypatch.setattr(acceptance, "render", lambda *args, **kwargs: "human view")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    return SimpleNamespace(
        jobs=jobs,
        expected=sorted(check["name"] for check in required.values()),
        published=published,
        output=tmp_path / "accepted",
        argv=[
            "--run-id",
            "17",
            "--attempt",
            "2",
            "--output",
            str(tmp_path / "accepted"),
            "--publish",
        ],
    )


def observations(gate):
    return [json.loads(path.read_text()) for path in gate.output.glob("job-observations/*.json")]


@pytest.mark.parametrize("state", ["missing", "failed", "incomplete"])
def test_required_job_rejection_records_observed_metadata(gate, capsys, state):
    job = gate.jobs[0]
    name = job["name"]
    if state == "missing":
        gate.jobs.pop(0)
    elif state == "failed":
        job["conclusion"] = "failure"
    else:
        job["status"] = "in_progress"
        job["conclusion"] = None
    assert acceptance.main(gate.argv) == 1
    (record,) = observations(gate)
    assert record["runId"] == 17 and record["attempt"] == 2
    assert record["headRevision"] == record["testedRevision"] == "a" * 40
    assert record["notPassed"] == [name]
    assert record["missing"] == ([name] if state == "missing" else [])
    assert record["expected"] == gate.expected
    assert record["omittedJobs"] == record["truncatedFields"] == 0
    observed = [item for item in record["observed"] if item["name"] == name]
    assert observed == ([] if state == "missing" else [job])
    assert not (gate.output / "ci-receipt.json").exists()
    (check,) = gate.published
    assert check["conclusion"] == "failure" and check["external_id"] == "17:2"
    summary = check["output"]["summary"]
    details = json.loads(summary.split(": ", 1)[1])
    assert details["notPassed"] == [name]
    assert details["missing"] == record["missing"]
    assert details["observed"] == observed
    assert summary in capsys.readouterr().out


def test_reevaluation_retains_original_job_failure(gate):
    original = gate.jobs.pop(0)
    assert acceptance.main(gate.argv) == 1
    (path,) = gate.output.glob("job-observations/*.json")
    original_bytes = path.read_bytes()
    gate.jobs.insert(0, original)
    assert acceptance.main(gate.argv) == 0
    assert path.read_bytes() == original_bytes
    records = observations(gate)
    assert len(records) == 2
    assert sorted(len(record["notPassed"]) for record in records) == [0, 1]
    assert all(record["runId"] == 17 and record["attempt"] == 2 for record in records)
    assert [check["conclusion"] for check in gate.published] == ["failure", "success"]
    assert json.loads((gate.output / "ci-receipt.json").read_text())["passed"]


def test_job_observations_are_bounded_and_do_not_change_rejection(gate):
    required = gate.jobs[0]
    required["status"] = "x" * 1000
    required["conclusion"] = {"untrusted": "payload"}
    extra = {
        "id": 10**100,
        "name": "untrusted " + "x" * 1000,
        "status": "s" * 1000,
        "conclusion": ["payload"],
        "steps": [{"name": "DO NOT RETAIN FREEFORM STEPS"}],
    }
    gate.jobs[:0] = [extra] * 150
    assert acceptance.main(gate.argv) == 1
    (record,) = observations(gate)
    assert len(record["observed"]) == 100
    assert record["totalJobs"] == 150 + len(gate.expected)
    assert record["omittedJobs"] == 50 + len(gate.expected)
    assert record["truncatedFields"] == 1 + 2 * (100 - len(gate.expected))
    assert len(json.dumps(record)) < 50_000
    assert record["observed"][0]["name"] == required["name"]
    assert record["notPassed"] == [required["name"]]
    assert record["observed"][0]["status"] == "x" * 128
    assert record["observed"][0]["conclusion"] == "<invalid type>"
    assert record["observed"][-1]["id"] == "<out of range>"
    assert "DO NOT RETAIN" not in json.dumps(record)
    assert gate.published[0]["conclusion"] == "failure"


def test_job_diagnostics_do_not_override_other_acceptance_obligations(gate, monkeypatch):
    monkeypatch.setattr(acceptance, "aggregate", lambda *args, **kwargs: {"passed": False})
    with pytest.raises(GraphError, match="required jobs or journey tests"):
        acceptance.evaluate("owner/repo", 17, 2, gate.output)
    (record,) = observations(gate)
    assert record["notPassed"] == []
    assert json.loads((gate.output / "ci-receipt.json").read_text())["passed"] is False


def test_workflow_preserves_diagnostics_across_acceptance_attempts():
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.load(
        (root / ".github/workflows/ci-acceptance.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["accept"]["steps"]
    diagnostic = next(
        step
        for step in steps
        if step.get("with", {}).get("path") == ".project-graph/accepted/job-observations/"
    )
    assert diagnostic["if"] == "always()"
    assert diagnostic["with"].get("overwrite", "false") == "false"
    name = diagnostic["with"]["name"]
    values = {
        "github.event.workflow_run.id || inputs.run_id": "17",
        "github.event.workflow_run.run_attempt || inputs.attempt": "2",
        "github.run_id": "50",
    }

    def expand(attempt):
        expanded = name
        for key, value in {**values, "github.run_attempt": str(attempt)}.items():
            expanded = expanded.replace("${{ " + key + " }}", value)
        assert "${{" not in expanded
        return expanded

    assert expand(1) == "ci-job-observations-17-2-50-1"
    assert expand(2) == "ci-job-observations-17-2-50-2"
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "main"


@pytest.mark.parametrize("name", [None, [], 3])
def test_malformed_job_name_is_retained_without_granting_acceptance(gate, name):
    job = gate.jobs[0]
    missing = job["name"]
    if name is None:
        del job["name"]
    else:
        job["name"] = name
    assert acceptance.main(gate.argv) == 1
    (record,) = observations(gate)
    assert record["missing"] == record["notPassed"] == [missing]
    assert record["observed"][-1]["name"] == (
        None if name is None else "<invalid type>" if isinstance(name, list) else name
    )
    assert gate.published[0]["conclusion"] == "failure"


def test_anomalous_duplicate_jobs_cannot_overflow_published_failure(gate):
    name = gate.jobs[0]["name"]
    duplicate = {
        **gate.jobs[0],
        "status": "\U0001f680" * 128,
        "conclusion": "\U0001f680" * 128,
        "head_sha": "\U0001f680" * 128,
    }
    gate.jobs[:] = [duplicate] * 100 + gate.jobs[1:]
    assert acceptance.main(gate.argv) == 1
    (record,) = observations(gate)
    assert record["notPassed"] == [name]
    assert len(record["observed"]) == 100
    summary = gate.published[0]["output"]["summary"]
    assert len(summary.encode()) <= 16_100
    details = json.loads(summary.split(": ", 1)[1])
    assert details["notPassed"] == [name]
    assert details["summaryOmittedJobs"] > 0
    assert len(details["observed"]) + details["summaryOmittedJobs"] == 100
    assert details["observationOmittedJobs"] == len(gate.expected) - 1
    assert gate.published[0]["conclusion"] == "failure"


def test_malformed_extra_job_does_not_relax_original_fail_closed_gate(gate):
    gate.jobs.append({"id": 99, "status": "completed", "conclusion": "success"})
    assert acceptance.main(gate.argv) == 1
    (record,) = observations(gate)
    assert record["notPassed"] == []
    assert record["observed"][-1]["name"] is None
    assert gate.published[0]["conclusion"] == "failure"
    assert gate.published[0]["output"]["summary"].startswith("invalid job metadata:")
