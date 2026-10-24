"""Trusted CI gate: GitHub metadata and bounded JSON only; never execute PR source."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from tools.project_graph.ci import aggregate
from tools.project_graph.compiler import compile_graph
from tools.project_graph.explorer import render
from tools.project_graph.repository import GraphError, Repository, git

from .results import journey_evidence

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


def api(path, payload=None):
    command = ["gh", "api", path]
    if payload is not None:
        command.extend(["--method", "POST", "--input", "-"])
    result = subprocess.run(
        command,
        input=json.dumps(payload).encode() if payload else None,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def pages(path, key):
    result = []
    for page in range(1, 101):
        data = api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")[key]
        result.extend(data)
        if len(data) < 100:
            return result
    raise GraphError("API pagination limit reached")


def read_receipt(archive: bytes) -> dict:
    if len(archive) > MAX_ARTIFACT_BYTES:
        raise GraphError("evidence artifact is too large")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = [
            e
            for e in zipped.infolist()
            if e.filename.endswith("/receipt.json") or e.filename == "receipt.json"
        ]
        if len(entries) != 1 or entries[0].file_size > MAX_ARTIFACT_BYTES:
            raise GraphError("artifact must contain exactly one bounded receipt")
        # Do not extract files, follow paths, import modules, or invoke artifact commands.
        data = json.loads(zipped.read(entries[0]))
    if not isinstance(data, dict) or data.get("kind") != "development-verification":
        raise GraphError("invalid development evidence")
    return data


def validate_receipts(receipts, required, repo, run_id, attempt):
    for identifier in required:
        matches = [r for r in receipts if r.get("checkId") == identifier]
        if len(matches) != 1:
            raise GraphError(f"missing or duplicate evidence for {identifier}")
        record = matches[0]
        if (
            record.get("runId") != str(run_id)
            or record.get("attempt") != str(attempt)
            or record.get("sourceRevision") != repo.revision
            or record.get("treeDigest") != repo.tree_digest
            or record.get("dirty") is not False
            or record.get("status") != "passed"
            or not isinstance(record.get("tests"), list)
        ):
            raise GraphError(f"failed, stale, or mismatched evidence for {identifier}")


def pull_request_for_run(repository, run):
    # workflow_run.pull_requests can be empty even for an ordinary PR run.
    # GitHub's commit association is resolved independently and checked against
    # the current PR head and main base, never inferred from the branch name.
    candidates = api(f"repos/{repository}/commits/{run['head_sha']}/pulls?per_page=100")
    matches = [
        pr
        for pr in candidates
        if pr["state"] == "open"
        and pr["head"]["sha"] == run["head_sha"]
        and pr["base"]["ref"] == "main"
        and pr["base"]["repo"]["full_name"] == repository
    ]
    if len(matches) != 1:
        raise GraphError("CI run must identify one current pull request to main")
    return matches[0]


def evaluate(repository: str, run_id: int, attempt: int, output: Path):
    prefix = f"repos/{repository}"
    run = api(f"{prefix}/actions/runs/{run_id}")
    if (
        run.get("run_attempt") != attempt
        or run.get("path") != ".github/workflows/ci.yml"
        or run.get("repository", {}).get("full_name") != repository
        or run.get("event") not in {"pull_request", "push", "workflow_dispatch"}
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise GraphError("unexpected, stale or unsuccessful CI run")
    head = run["head_sha"]
    required = json.loads(Path(__file__).with_name("checks.json").read_text())
    artifacts = pages(f"{prefix}/actions/runs/{run_id}/artifacts", "artifacts")
    receipts = []
    for identifier in required:
        matches = [
            a
            for a in artifacts
            if a["name"] == f"evidence-{identifier}-{attempt}" and not a["expired"]
        ]
        if len(matches) != 1 or matches[0]["size_in_bytes"] > MAX_ARTIFACT_BYTES:
            raise GraphError(f"missing, duplicate or oversized artifact: {identifier}")
        archive = subprocess.check_output(
            ["gh", "api", f"{prefix}/actions/artifacts/{matches[0]['id']}/zip"]
        )
        receipt = read_receipt(archive)
        if receipt.get("checkId") != identifier:
            raise GraphError("artifact name and check identity disagree")
        receipts.append(receipt)
    revisions = {r.get("sourceRevision") for r in receipts}
    if len(revisions) != 1 or not re.fullmatch(r"[0-9a-f]{40}", next(iter(revisions)) or ""):
        raise GraphError("checks did not test one exact revision")
    tested = next(iter(revisions))
    # Fetch Git objects into an empty repository. No candidate files are checked out.
    with tempfile.TemporaryDirectory(prefix="typeflux-ci-source-") as directory:
        root = Path(directory)
        git(root, "init", "-q")
        git(root, "fetch", "--no-tags", f"https://github.com/{repository}.git", tested)
        parents = git(root, "rev-list", "--parents", "-n", "1", tested).decode().split()[1:]
        base_sha = tested
        if run["event"] == "pull_request":
            pr = pull_request_for_run(repository, run)
            if pr["head"]["sha"] != head or parents != [pr["base"]["sha"], head]:
                raise GraphError("CI did not test the current PR head and base")
            base_sha = parents[0]
        elif tested != head or run["head_branch"] != "main":
            raise GraphError("release/main evidence must test a main revision")
        repo, base = Repository(root, tested), Repository(root, base_sha)
        validate_receipts(receipts, required, repo, run_id, attempt)
        jobs = pages(f"{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        result = aggregate(
            base,
            repo,
            run,
            [{"jobs": jobs}],
            run_id=run_id,
            attempt=attempt,
            expected_head=head,
            tested_revision=tested,
        )
        # Trusted registry obligations also apply if candidate policy deletes IDs.
        passed_names = {
            j["name"]
            for j in jobs
            if j.get("status") == "completed" and j.get("conclusion") == "success"
        }
        if not {c["name"] for c in required.values()} <= passed_names:
            raise GraphError("trusted required jobs did not all pass")
        graph = compile_graph(repo)
        result["journeyEvidence"] = journey_evidence(graph, receipts)
        result["passed"] = result["passed"] and all(
            c["status"] == "passed" for c in result["journeyEvidence"]
        )
        # Retain base journey obligations; removing a declaration cannot erase its own tests.
        base_evidence = journey_evidence(compile_graph(base), receipts)
        if "tools/development/checks.json" in base.files:
            result["baseJourneyEvidence"] = base_evidence
            result["passed"] = result["passed"] and all(
                c["status"] == "passed" for c in base_evidence
            )
        result["authority"] = (
            "trusted default-branch workflow; GitHub run metadata plus candidate test reports"
        )
        result["evidenceLimits"] = (
            "Test output is candidate-produced data; this is not independent execution or production observation."
        )
        output.mkdir(parents=True, exist_ok=True)
        (output / "ci-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
        (output / "explorer.html").write_text(render(graph, verification=result))
        if not result["passed"]:
            raise GraphError("required jobs or journey tests are not all passing")
        return result


def is_latest(repository, run, attempt):
    """Do not let a delayed earlier run overwrite the result of a newer run."""
    latest = api(
        f"repos/{repository}/actions/workflows/ci.yml/runs?head_sha={run['head_sha']}&event={run['event']}&per_page=100"
    )["workflow_runs"]
    return (
        bool(latest) and max(r["id"] for r in latest) == run["id"] and run["run_attempt"] == attempt
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--output", type=Path, default=Path(".project-graph/accepted"))
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise GraphError("invalid repository")
    run = api(f"repos/{repository}/actions/runs/{args.run_id}")
    if not is_latest(repository, run, args.attempt):
        print("Superseded CI run; no acceptance result published")
        return 0
    # Publish a failure for missing evidence too; never leave an old success as the latest result.
    result, error = None, None
    try:
        result = evaluate(repository, args.run_id, args.attempt, args.output)
    except (
        GraphError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as exc:
        error = str(exc)
    if args.publish and is_latest(
        repository, api(f"repos/{repository}/actions/runs/{args.run_id}"), args.attempt
    ):
        api(
            f"repos/{repository}/check-runs",
            {
                "name": "CI acceptance",
                "head_sha": run["head_sha"],
                "status": "completed",
                "conclusion": "failure" if error else "success",
                "external_id": f"{args.run_id}:{args.attempt}",
                "details_url": run["html_url"],
                "output": {
                    "title": "CI evidence rejected" if error else "CI evidence accepted",
                    "summary": error
                    or "All required jobs and declared journey tests passed on the exact tested revision.",
                },
            },
        )
    print(error or json.dumps({"passed": result["passed"], "runId": args.run_id}))
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
