"""Release selection across retained trusted evaluations; GitHub transport is simulated."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import yaml
from tools.development import release_evidence as release
from tools.project_graph import ci as graph_ci
from tools.project_graph.repository import GraphError


@pytest.fixture
def retained(monkeypatch):
    repo = SimpleNamespace(revision="a" * 40, tree_digest="digest")
    ci = {
        "id": 17,
        "run_attempt": 2,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": repo.revision,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-11-05T12:00:00Z",
    }
    old = {
        "id": 50,
        "run_attempt": 1,
        "path": ".github/workflows/ci-acceptance.yml",
        "event": "workflow_run",
        "head_branch": "main",
        "display_title": "CI acceptance 17 attempt 2",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "b" * 40,
    }
    latest = {**old, "id": 51, "event": "workflow_dispatch", "conclusion": "success"}
    evaluations = [latest, old]
    record = {
        "kind": "ci-verification",
        "runId": 17,
        "attempt": 2,
        "passed": True,
        "testedRevision": repo.revision,
        "headRevision": repo.revision,
        "treeDigest": repo.tree_digest,
        "journeyEvidence": [{"status": "passed"}],
    }
    artifacts = [
        {
            "id": identifier,
            "name": f"accepted-ci-17-2-{gate}-1",
            "expired": False,
            "size_in_bytes": 1,
            "workflow_run": {"id": gate},
        }
        for identifier, gate in [(90, 50), (80, 51)]
    ]
    downloads = []
    provenance = {"status": "ahead"}
    records = {90: None, 80: record}

    def api(path):
        if path.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main", "object": {"sha": "c" * 40}}
        if "/compare/" in path:
            assert path == f"repos/owner/repo/compare/{latest['head_sha']}...{'c' * 40}"
            return provenance
        if "/workflows/ci.yml/runs?" in path:
            assert f"head_sha={repo.revision}&event=push" in path
            return {"workflow_runs": [ci]}
        return next(g for g in evaluations if path.endswith(f"/runs/{g['id']}"))

    def pages(path, key):
        if key == "workflow_runs":
            base, query = path.split("?", 1)
            assert base == "repos/owner/repo/actions/workflows/ci-acceptance.yml/runs"
            assert parse_qs(query) == {"branch": ["main"], "created": [">=" + ci["created_at"]]}
            return evaluations
        assert key == "artifacts"
        assert path in {f"repos/owner/repo/actions/runs/{g['id']}/artifacts" for g in evaluations}
        return artifacts

    def download(args):
        identifier = int(args[-1].split("/")[-2])
        downloads.append(identifier)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            if records[identifier] is not None:
                zipped.writestr("ci-receipt.json", json.dumps(records[identifier]))
            else:
                zipped.writestr("job-observations/failure.json", "{}")
        return archive.getvalue()

    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release, "pages", pages)
    monkeypatch.setattr(release.subprocess, "check_output", download)
    return SimpleNamespace(
        repo=repo,
        ci=ci,
        old=old,
        latest=latest,
        evaluations=evaluations,
        artifacts=artifacts,
        record=record,
        records=records,
        downloads=downloads,
        provenance=provenance,
    )


@pytest.mark.parametrize("event", ["workflow_run", "workflow_dispatch"])
def test_fresh_evaluation_retains_rejection_and_selects_latest_success(retained, event):
    retained.latest["event"] = event
    originals = copy.deepcopy((retained.artifacts, retained.records))
    assert release.fetch(retained.repo, "owner/repo") == retained.record
    assert retained.downloads == [80]  # IDs need not increase with evaluation order.
    assert (retained.artifacts, retained.records) == originals
    # An older success must not hide a later rejection or missing accepted receipt.
    retained.old["conclusion"] = "success"
    retained.records[90] = retained.record
    retained.records[80] = None
    with pytest.raises(GraphError, match="receipt archive"):
        release.fetch(retained.repo, "owner/repo")
    assert retained.downloads == [80, 80]


@pytest.mark.parametrize(
    "changed",
    [
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"status": "queued", "conclusion": None},
        {"status": "in_progress", "conclusion": None},
        {"event": "push"},
        {"path": ".github/workflows/other.yml"},
        {"run_attempt": 2},
    ],
)
def test_latest_untrusted_or_unsuccessful_evaluation_blocks_older_success(retained, changed):
    retained.old["conclusion"] = "success"
    retained.latest.update(changed)
    with pytest.raises(GraphError, match="trusted acceptance"):
        release.fetch(retained.repo, "owner/repo")
    assert not retained.downloads


def test_non_main_dispatch_is_not_an_evaluation_of_main_evidence(retained):
    retained.latest["head_branch"] = "feature"
    retained.old["conclusion"] = "success"
    retained.records[90] = retained.record
    assert release.fetch(retained.repo, "owner/repo") == retained.record
    assert retained.downloads == [90]


def test_foreign_same_name_artifact_cannot_block_owned_evidence(retained):
    # Lookup is run-scoped, with defensive ownership filtering before uniqueness.
    retained.artifacts.append({**retained.artifacts[1], "id": 999, "workflow_run": {"id": 99}})
    assert release.fetch(retained.repo, "owner/repo") == retained.record
    assert retained.downloads == [80]


@pytest.mark.parametrize("status", ["identical", "diverged", "behind"])
def test_evaluator_source_must_belong_to_explicit_main_branch_history(retained, status):
    # A tag named main can report head_branch=main while using unreviewed source.
    retained.provenance["status"] = status
    retained.latest["head_sha"] = "c" * 40 if status == "identical" else "d" * 40
    if status == "identical":
        assert release.fetch(retained.repo, "owner/repo") == retained.record
        assert retained.downloads == [80]
    else:
        with pytest.raises(GraphError, match="source is not in main branch history"):
            release.fetch(retained.repo, "owner/repo")
        assert not retained.downloads


def test_release_consumes_actual_aggregate_receipt_identity(retained, monkeypatch):
    monkeypatch.setattr(graph_ci, "required_jobs", lambda repo: {"Tests"})
    record = graph_ci.aggregate(
        retained.repo,
        retained.repo,
        retained.ci,
        [
            {
                "jobs": [
                    {
                        "name": "Tests",
                        "run_id": 17,
                        "run_attempt": 2,
                        "head_sha": retained.repo.revision,
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        ],
        run_id=17,
        attempt=2,
        expected_head=retained.repo.revision,
        tested_revision=retained.repo.revision,
    )
    record["journeyEvidence"] = retained.record["journeyEvidence"]
    retained.records[80] = record
    assert release.fetch(retained.repo, "owner/repo") == record
    assert type(record["runId"]) is int and type(record["attempt"]) is int


@pytest.mark.parametrize(
    "state", ["missing", "duplicate", "expired", "oversized", "foreign", "old-attempt"]
)
def test_latest_evaluation_requires_unique_owned_current_artifact(retained, state):
    artifact = retained.artifacts[1]
    if state == "missing":
        retained.artifacts.pop()
    elif state == "duplicate":
        retained.artifacts.append(dict(artifact))
    elif state == "expired":
        artifact["expired"] = True
    elif state == "oversized":
        artifact["size_in_bytes"] = release.MAX_ARTIFACT_BYTES + 1
    elif state == "foreign":
        artifact["workflow_run"] = {"id": 99}
    else:
        artifact["name"] = "accepted-ci-17-2-51-2"
    with pytest.raises(GraphError, match="accepted CI artifact"):
        release.fetch(retained.repo, "owner/repo")
    assert not retained.downloads


@pytest.mark.parametrize(
    "changed",
    [
        {"runId": 16},
        {"attempt": 1},
        {"testedRevision": "pr-merge"},
        {"headRevision": "old-main"},
        {"treeDigest": "other"},
        {"passed": False},
        {"journeyEvidence": [{"status": "missing"}]},
    ],
)
def test_selected_receipt_still_requires_exact_native_run_source_and_journeys(retained, changed):
    retained.record.update(changed)
    with pytest.raises(GraphError):
        release.fetch(retained.repo, "owner/repo")
    assert retained.downloads == [80]


def test_evaluation_title_does_not_cross_native_attempt_or_accept_legacy_fallback(retained):
    for gate in retained.evaluations:
        gate["display_title"] = "CI acceptance 17 attempt 1"
    with pytest.raises(GraphError, match="no trusted acceptance"):
        release.fetch(retained.repo, "owner/repo")
    assert not retained.downloads


@pytest.mark.parametrize(
    "changed",
    [
        {"conclusion": "failure"},
        {"status": "in_progress"},
        {"head_sha": "other"},
        {"path": ".github/workflows/other.yml"},
        {"event": "pull_request"},
    ],
)
def test_native_ci_must_still_be_successful_main_push_for_exact_source(retained, changed):
    retained.ci.update(changed)
    with pytest.raises(GraphError, match="main CI run"):
        release.fetch(retained.repo, "owner/repo")
    assert not retained.downloads


def test_automatic_and_dispatched_evaluations_share_identity_and_retain_distinct_artifacts():
    workflow = yaml.load(
        (Path(__file__).resolve().parents[3] / ".github/workflows/ci-acceptance.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"run_id", "attempt"}
    assert all(v["required"] == "true" and v["type"] == "string" for v in inputs.values())
    job = workflow["jobs"]["accept"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    steps = job["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "main"
    command = next(s for s in steps if s.get("name") == "Accept exact-run evidence")
    values = {
        "github.event.workflow_run.id || inputs.run_id": "17",
        "github.event.workflow_run.run_attempt || inputs.attempt": "2",
        "github.run_id": "51",
        "github.run_attempt": "1",
    }

    def expand(text, **overrides):
        for key, value in {**values, **overrides}.items():
            text = text.replace("${{ " + key + " }}", value)
        assert "${{" not in text
        return text

    assert expand(workflow["run-name"]) == "CI acceptance 17 attempt 2"
    assert expand(job["env"]["RUN_ID"]) == "17"
    assert expand(job["env"]["ATTEMPT"]) == "2"
    assert '--run-id "$RUN_ID" --attempt "$ATTEMPT" --publish' in command["run"]
    upload = next(s for s in steps if s.get("name") == "Upload accepted evidence and human view")
    assert upload["if"] == "always()" and upload["with"]["overwrite"] == "false"
    name = upload["with"]["name"]
    assert expand(name) == "accepted-ci-17-2-51-1"
    assert expand(name, **{"github.run_id": "52"}) == "accepted-ci-17-2-52-1"
    assert expand(name, **{"github.run_attempt": "2"}) == "accepted-ci-17-2-51-2"


@pytest.mark.parametrize(
    "run_id,attempt,valid",
    [
        ("17", "2", True),
        ("017", "2", False),
        ("17", "02", False),
        ("0", "1", False),
        ("17", "-1", False),
        ("", "1", False),
        ("17; exit 0", "2", False),
        ("17", "1\n2", False),
    ],
)
def test_dispatch_identifiers_are_validated_before_execution(run_id, attempt, valid):
    workflow = yaml.load(
        (Path(__file__).resolve().parents[3] / ".github/workflows/ci-acceptance.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    guard = workflow["jobs"]["accept"]["steps"][0]
    result = subprocess.run(
        ["bash", "-c", guard["run"]],
        env={**os.environ, "RUN_ID": run_id, "ATTEMPT": attempt, "GITHUB_RUN_ATTEMPT": "1"},
        capture_output=True,
    )
    assert (result.returncode == 0) is valid


def test_workflow_rerun_is_rejected_before_validator():
    workflow = yaml.load(
        (Path(__file__).resolve().parents[3] / ".github/workflows/ci-acceptance.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    guard = workflow["jobs"]["accept"]["steps"][0]
    result = subprocess.run(
        ["bash", "-c", guard["run"]],
        env={**os.environ, "RUN_ID": "17", "ATTEMPT": "2", "GITHUB_RUN_ATTEMPT": "2"},
        capture_output=True,
    )
    assert result.returncode != 0


@pytest.mark.parametrize("state", ["duplicate", "expanded", "archive-size"])
def test_selected_archive_remains_bounded_and_receipt_unique(retained, monkeypatch, state):
    monkeypatch.setattr(release, "MAX_ARTIFACT_BYTES", 1024)
    if state == "archive-size":
        archive = b"x" * 1025
    else:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("ci-receipt.json", "x" * 1025 if state == "expanded" else "{}")
            if state == "duplicate":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    zipped.writestr("ci-receipt.json", "{}")
        archive = output.getvalue()
    monkeypatch.setattr(release.subprocess, "check_output", lambda args: archive)
    with pytest.raises(GraphError, match="too large|receipt archive"):
        release.fetch(retained.repo, "owner/repo")
