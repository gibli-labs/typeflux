"""Advisory planning and exact-run result aggregation; no check execution from YAML."""

from __future__ import annotations

import itertools
import re

import yaml

from .compiler import compile_for_impact, yaml_data
from .queries import impact
from .repository import GraphError, Repository, digest, git

POLICY = "project-graph/policy.yaml"


def workflow_jobs(repo: Repository) -> set[str]:
    """Bootstrap from the actual base workflow before graph policy was introduced."""
    # Workflow YAML uses the YAML 1.2 `on` key; BaseLoader avoids YAML 1.1's
    # boolean conversion and treats all scalar matrix values as display strings.
    workflow = yaml.load(repo.text(".github/workflows/ci.yml"), Loader=yaml.BaseLoader)
    result = set()
    for key, job in workflow["jobs"].items():
        if key == "graph-aggregate":
            continue
        matrix = job.get("strategy", {}).get("matrix", {})
        if not isinstance(matrix, dict) or "exclude" in matrix:
            raise GraphError("unsupported CI matrix; requires explicit reviewed graph policy")
        if "include" in matrix:
            combinations = matrix["include"]
        else:
            keys = list(matrix)
            combinations = [
                dict(zip(keys, values)) for values in itertools.product(*(matrix[k] for k in keys))
            ]
        for values in combinations or [{}]:
            name = job.get("name", key)
            name = re.sub(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}", lambda m: str(values[m[1]]), name)
            if "${{" in name:
                raise GraphError("unresolved CI job name expression")
            result.add(name)
    return result


def required_jobs(repo: Repository) -> set[str]:
    if POLICY not in repo.files:
        return workflow_jobs(repo)
    policy = yaml_data(repo.text(POLICY))
    if not isinstance(policy, dict) or set(policy) != {"apiVersion", "mode", "requiredJobs"}:
        raise GraphError("invalid graph policy fields")
    if policy["apiVersion"] != "typeflux.dev/graph-policy/v1alpha1" or policy["mode"] != "advisory":
        raise GraphError("only advisory graph policy is supported")
    jobs = policy["requiredJobs"]
    if (
        not isinstance(jobs, list)
        or not jobs
        or not all(isinstance(n, str) and n for n in jobs)
        or len(set(jobs)) != len(jobs)
    ):
        raise GraphError("requiredJobs must be a nonempty unique list of job names")
    return set(jobs)


def plan(base: Repository, head: Repository) -> dict:
    base_graph, head_graph = compile_for_impact(base), compile_for_impact(head)
    report = impact(base_graph, head_graph)
    # Both full mandatory baselines apply. Proposed data can only ADD obligations.
    required = required_jobs(base) | required_jobs(head)
    head_nodes = {n["id"]: n for n in head_graph["nodes"]}
    recommended = sorted(
        {
            name
            for check in report["recommendedChecks"]
            for name in head_nodes.get(check, {}).get("ci", [])
        }
    )
    return {
        **report,
        "requiredJobs": sorted(required),
        "advisorySelectedJobs": recommended,
        "policyBase": base.revision,
        "policyDigest": digest(sorted(required)),
        "selectionEnabled": False,
        "headGraph": head_graph,
    }


def classify(job: dict | None) -> str:
    if job is None:
        return "missing"
    conclusion = job.get("conclusion")
    if conclusion == "success":
        return "passed"
    if conclusion in {"cancelled", "timed_out", "skipped", "neutral"}:
        return str(conclusion)
    failures = [
        step.get("name", "") for step in job.get("steps", []) if step.get("conclusion") == "failure"
    ]
    if failures and all(
        name.startswith(("Set up ", "Install ", "Check out ", "Download ", "Cache "))
        for name in failures
    ):
        return "infrastructure-candidate"
    return "test-or-build-failure" if failures else "unknown-failure"


def aggregate(
    base: Repository,
    head: Repository,
    run: dict,
    pages: list[dict],
    *,
    run_id: int,
    attempt: int,
    expected_head: str,
    tested_revision: str,
) -> dict:
    if (
        run.get("id") != run_id
        or run.get("head_sha") != expected_head
        or run.get("run_attempt") != attempt
    ):
        raise GraphError("CI run identity does not match the requested revision/attempt")
    if head.revision != tested_revision:
        raise GraphError("tested checkout revision does not match the graph")
    if run.get("event") == "pull_request":
        parents = (
            git(head.root, "rev-list", "--parents", "-n", "1", tested_revision)
            .decode()
            .strip()
            .split()[1:]
        )
        if parents != [base.revision, expected_head]:
            raise GraphError("CI merge revision does not bind the expected base and PR head")
    elif tested_revision != expected_head:
        raise GraphError("non-PR run must test the recorded head")
    jobs = [job for page in pages for job in page.get("jobs", [])]
    required = sorted(required_jobs(base) | required_jobs(head))
    results = []
    for name in required:
        matches = [
            job
            for job in jobs
            if job.get("name") == name
            and job.get("run_id") == run_id
            and job.get("run_attempt") == attempt
            and job.get("head_sha") == expected_head
        ]
        job = matches[0] if len(matches) == 1 else None
        passed = bool(
            job and job.get("status") == "completed" and job.get("conclusion") == "success"
        )
        results.append(
            {
                "name": name,
                "passed": passed,
                "classification": classify(job),
                "jobId": job.get("id") if job else None,
            }
        )
    return {
        "kind": "ci-verification",
        "runId": run_id,
        "attempt": attempt,
        "headRevision": expected_head,
        "testedRevision": tested_revision,
        "treeDigest": head.tree_digest,
        "policyBase": base.revision,
        "passed": all(r["passed"] for r in results),
        "checks": results,
        "scope": "CI workflow; external security/DCO checks retain their existing gates",
    }
