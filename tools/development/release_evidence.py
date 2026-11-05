"""Fetch accepted main CI evidence for exactly the source being released."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlencode

from tools.project_graph.repository import GraphError, Repository

from .acceptance import MAX_ARTIFACT_BYTES, api, pages


def validate(repo, record):
    if (
        record.get("kind") != "ci-verification"
        or record.get("passed") is not True
        or record.get("testedRevision") != repo.revision
        or record.get("headRevision") != repo.revision
        or record.get("treeDigest") != repo.tree_digest
        or not record.get("journeyEvidence")
        or not all(c.get("status") == "passed" for c in record["journeyEvidence"])
    ):
        raise GraphError(
            "release requires passing main CI and journey evidence on the exact source revision"
        )


def fetch(repo: Repository, repository: str):
    prefix = f"repos/{repository}"
    runs = api(
        f"{prefix}/actions/workflows/ci.yml/runs?head_sha={repo.revision}&event=push&per_page=100"
    )["workflow_runs"]
    runs = [r for r in runs if r["head_branch"] == "main"]
    if not runs:
        raise GraphError("no main CI run for this source revision")
    run = max(runs, key=lambda r: r["id"])
    if (
        run["conclusion"] != "success"
        or run["status"] != "completed"
        or run["path"] != ".github/workflows/ci.yml"
        or run["event"] != "push"
        or run["head_sha"] != repo.revision
    ):
        raise GraphError("latest main CI run is not successful")
    title = f"CI acceptance {run['id']} attempt {run['run_attempt']}"
    query = urlencode({"branch": "main", "created": f">={run['created_at']}"})
    evaluations = pages(
        f"{prefix}/actions/workflows/ci-acceptance.yml/runs?{query}", "workflow_runs"
    )
    evaluations = [
        r for r in evaluations if r["display_title"] == title and r["head_branch"] == "main"
    ]
    if not evaluations:
        raise GraphError("no trusted acceptance evaluation for the latest CI run")
    # Select the evaluation before looking for artifacts. Missing/failed new
    # evaluations must never fall back to an older passing receipt.
    latest = max(evaluations, key=lambda r: r["id"])
    gate = api(f"{prefix}/actions/runs/{latest['id']}")
    if (
        gate["path"] != ".github/workflows/ci-acceptance.yml"
        or gate["event"] not in {"workflow_run", "workflow_dispatch"}
        or gate["head_branch"] != "main"
        or gate["display_title"] != title
        or gate["status"] != "completed"
        or gate["conclusion"] != "success"
        or gate["run_attempt"] != 1
    ):
        raise GraphError("release evidence was not produced by successful trusted acceptance")
    # head_branch alone cannot distinguish a branch from a same-named tag.
    # Bind evaluator source to the explicit trusted branch's commit history.
    main_sha = api(f"{prefix}/git/ref/heads/main")["object"]["sha"]
    comparison = api(f"{prefix}/compare/{gate['head_sha']}...{main_sha}")
    if comparison["status"] not in {"identical", "ahead"}:
        raise GraphError("trusted acceptance source is not in main branch history")
    name = f"accepted-ci-{run['id']}-{run['run_attempt']}-{gate['id']}-{gate['run_attempt']}"
    artifacts = pages(f"{prefix}/actions/runs/{gate['id']}/artifacts", "artifacts")
    artifacts = [
        a
        for a in artifacts
        if a["name"] == name and not a["expired"] and a["workflow_run"]["id"] == gate["id"]
    ]
    if len(artifacts) != 1 or artifacts[0]["size_in_bytes"] > MAX_ARTIFACT_BYTES:
        raise GraphError("accepted CI artifact is missing, duplicated, expired or too large")
    artifact = artifacts[0]
    archive = subprocess.check_output(
        ["gh", "api", f"{prefix}/actions/artifacts/{artifact['id']}/zip"]
    )
    if len(archive) > MAX_ARTIFACT_BYTES:
        raise GraphError("accepted evidence archive is too large")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        matches = [e for e in zipped.infolist() if e.filename == "ci-receipt.json"]
        if len(matches) != 1 or matches[0].file_size > MAX_ARTIFACT_BYTES:
            raise GraphError("invalid accepted receipt archive")
        record = json.loads(zipped.read(matches[0]))
    if record.get("runId") != run["id"] or record.get("attempt") != run["run_attempt"]:
        raise GraphError("accepted receipt does not identify the latest CI run")
    validate(repo, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = fetch(Repository(Path.cwd()), os.environ["GITHUB_REPOSITORY"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
