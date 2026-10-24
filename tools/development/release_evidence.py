"""Fetch accepted main CI evidence for exactly the source being released."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

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
    if run["conclusion"] != "success":
        raise GraphError("latest main CI run is not successful")
    name = f"accepted-ci-{run['id']}-{run['run_attempt']}"
    artifacts = pages(f"{prefix}/actions/artifacts?name={name}", "artifacts")
    artifacts = [a for a in artifacts if not a["expired"]]
    if len(artifacts) != 1 or artifacts[0]["size_in_bytes"] > MAX_ARTIFACT_BYTES:
        raise GraphError("accepted CI artifact is missing, duplicated, expired or too large")
    artifact = artifacts[0]
    gate = api(f"{prefix}/actions/runs/{artifact['workflow_run']['id']}")
    if (
        gate["path"] != ".github/workflows/ci-acceptance.yml"
        or gate["event"] != "workflow_run"
        or gate["conclusion"] != "success"
    ):
        raise GraphError("release evidence was not produced by successful trusted acceptance")
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
