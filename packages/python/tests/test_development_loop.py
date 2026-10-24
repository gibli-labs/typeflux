from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from scripts.auto_fix_issue import load_auto_fix_policy, protected_path_violations
from tools.development.acceptance import read_receipt, validate_receipts
from tools.development.protection import differences
from tools.development.registry import CI_CHECKS, REQUIRED_CONTEXTS
from tools.development.release_evidence import validate as validate_release
from tools.development.results import journey_evidence, playwright, vitest
from tools.project_graph.ci import required_checks, workflow_jobs
from tools.project_graph.repository import GraphError, Repository

ROOT = Path(__file__).resolve().parents[3]


def graph():
    return {
        "nodes": [
            {
                "id": "check.review",
                "kind": "check",
                "environment": "local",
                "ci": ["browser-python", "browser-typescript"],
                "tests": [{"path": "e2e/review.spec.ts", "name": "review progresses"}],
            }
        ]
    }


def evidence(lane, status="passed"):
    return {
        "checkId": lane,
        "status": "passed",
        "tests": [{"path": "e2e/review.spec.ts", "name": "review progresses", "status": status}],
    }


@pytest.mark.parametrize("status", ["skipped", "failed", "not-run"])
def test_job_success_cannot_substitute_for_each_required_test(status):
    receipts = [evidence("browser-python"), evidence("browser-typescript", status)]
    assert journey_evidence(graph(), receipts)[0]["status"] != "passed"
    assert journey_evidence(graph(), receipts[:1])[0]["status"] == "missing"
    receipts[1] = evidence("browser-typescript")
    assert journey_evidence(graph(), receipts)[0]["status"] == "passed"
    receipts.append(evidence("browser-typescript"))
    assert journey_evidence(graph(), receipts)[0]["status"] == "missing"


def test_parameterized_cases_all_must_pass():
    receipts = [evidence("browser-python"), evidence("browser-typescript")]
    receipts[0]["tests"].append({**receipts[0]["tests"][0], "status": "failed"})
    assert journey_evidence(graph(), receipts)[0]["status"] == "failed"


def test_vitest_preserves_file_identity_and_skips(tmp_path):
    report = {
        "testResults": [
            {
                "name": str(tmp_path / "test.ts"),
                "assertionResults": [
                    {"title": "works", "fullName": "suite works", "status": "passed"},
                    {"title": "later", "fullName": "suite later", "status": "pending"},
                ],
            }
        ]
    }
    results = vitest(report, tmp_path, tmp_path)
    assert [(r["path"], r["name"], r["status"]) for r in results] == [
        ("test.ts", "works", "passed"),
        ("test.ts", "later", "skipped"),
    ]
    report["testResults"][0]["name"] = str(tmp_path.parent / "outside.ts")
    with pytest.raises(ValueError):
        vitest(report, tmp_path, tmp_path)


def test_playwright_does_not_hide_retry_failure(tmp_path):
    report = {
        "config": {"rootDir": str(tmp_path)},
        "suites": [
            {
                "specs": [
                    {
                        "file": "test.spec.ts",
                        "title": "works",
                        "id": "test-id",
                        "tests": [
                            {
                                "projectName": "chromium",
                                "results": [{"status": "failed"}, {"status": "passed"}],
                            }
                        ],
                    }
                ]
            }
        ],
    }
    assert playwright(report, tmp_path, tmp_path)[0]["status"] == "failed"
    report["suites"][0]["specs"][0]["tests"][0]["results"] = [{"status": "passed"}]
    assert playwright(report, tmp_path, tmp_path)[0]["status"] == "passed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("runId", "other"),
        ("attempt", "2"),
        ("sourceRevision", "other"),
        ("treeDigest", "other"),
        ("dirty", True),
        ("status", "failed"),
        ("tests", None),
    ],
)
def test_receipt_must_match_exact_execution(field, value):
    repo = SimpleNamespace(revision="a" * 40, tree_digest="digest")
    receipt = {
        "checkId": "python",
        "runId": "1",
        "attempt": "1",
        "sourceRevision": repo.revision,
        "treeDigest": repo.tree_digest,
        "dirty": False,
        "status": "passed",
        "tests": [],
    }
    validate_receipts([receipt], ["python"], repo, 1, 1)
    with pytest.raises(GraphError, match="failed, stale, or mismatched"):
        validate_receipts([{**receipt, field: value}], ["python"], repo, 1, 1)
    with pytest.raises(GraphError, match="missing or duplicate"):
        validate_receipts([receipt, receipt], ["python"], repo, 1, 1)


def test_artifact_reader_never_extracts_or_executes_content(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../../payload.py", "raise RuntimeError('untrusted')")
        zipped.writestr("check/receipt.json", json.dumps({"kind": "development-verification"}))
    assert read_receipt(archive.getvalue())["kind"] == "development-verification"
    assert not list(tmp_path.iterdir())
    with zipfile.ZipFile(archive, "a") as zipped:
        zipped.writestr("another/receipt.json", "{}")
    with pytest.raises(GraphError, match="exactly one"):
        read_receipt(archive.getvalue())


def test_release_cannot_reuse_pr_merge_or_incomplete_journey_evidence():
    repo = SimpleNamespace(revision="main-sha", tree_digest="digest")
    record = {
        "kind": "ci-verification",
        "passed": True,
        "testedRevision": repo.revision,
        "headRevision": repo.revision,
        "treeDigest": repo.tree_digest,
        "journeyEvidence": [{"status": "passed"}],
    }
    validate_release(repo, record)
    for changed in (
        {"testedRevision": "pr-merge"},
        {"headRevision": "pr-head"},
        {"journeyEvidence": [{"status": "missing"}]},
        {"journeyEvidence": []},
    ):
        with pytest.raises(GraphError, match="exact source revision"):
            validate_release(repo, {**record, **changed})


def test_live_registry_policy_and_workflow_agree():
    repo = Repository(ROOT)
    assert required_checks(repo) == {key: value[0] for key, value in CI_CHECKS.items()}
    assert workflow_jobs(repo) == {value[0] for value in CI_CHECKS.values()}
    checks = yaml.safe_load((ROOT / "project-graph/checks.yaml").read_text())["entities"]
    assert all(set(c["ci"]) <= CI_CHECKS.keys() for c in checks)
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    version = json.loads((ROOT / "clients/console/package.json").read_text())["devDependencies"][
        "@playwright/test"
    ]
    assert f":v{version}-noble@sha256:" in workflow["jobs"]["browser"]["container"]["image"]
    assert workflow["on"]["push"]["branches"] == ["main"]


def test_auto_fix_protects_current_policy_and_dependency_paths():
    policy = load_auto_fix_policy(ROOT)
    paths = [
        "packages/python/pyproject.toml",
        "packages/python/src/typeflux/yaml/spec.py",
        "project-graph/policy.yaml",
        "tools/development/registry.py",
        "tools/project_graph/ci.py",
        ".github/actions/setup-development/action.yml",
        "clients/console/package.json",
        "skills/typeflux-auto-fix-issue/SKILL.md",
    ]
    assert protected_path_violations(paths, frozenset(), policy) == paths
    assert not protected_path_violations(
        ["packages/python/tests/test_controlplane_api.py"], frozenset(), policy
    )


def test_protection_drift_checks_issuer_strictness_and_conversations():
    protection = {
        "required_status_checks": {
            "strict": True,
            "checks": [{"context": name, "app_id": 15368} for name in REQUIRED_CONTEXTS],
        },
        "required_conversation_resolution": {"enabled": True},
    }
    assert not any(differences(protection).values())
    protection["required_status_checks"]["checks"][0]["app_id"] = None
    assert differences(protection)["checks"]


def test_acceptance_resolves_pr_when_run_metadata_has_no_pull_requests(monkeypatch):
    from tools.development import acceptance

    pr = {
        "number": 12,
        "state": "open",
        "head": {"sha": "head"},
        "base": {"ref": "main", "sha": "base", "repo": {"full_name": "owner/repo"}},
    }
    monkeypatch.setattr(acceptance, "api", lambda path: [pr])
    run = {"head_sha": "head", "pull_requests": []}
    assert acceptance.pull_request_for_run("owner/repo", run) == pr
    pr["head"]["sha"] = "new-head"
    with pytest.raises(GraphError, match="current pull request"):
        acceptance.pull_request_for_run("owner/repo", run)
    pr["head"]["sha"] = "head"
    pr["state"] = "closed"
    with pytest.raises(GraphError, match="current pull request"):
        acceptance.pull_request_for_run("owner/repo", run)


def test_delayed_run_cannot_overwrite_newer_acceptance(monkeypatch):
    from tools.development import acceptance

    run = {"id": 5, "head_sha": "head", "event": "pull_request", "run_attempt": 2}
    monkeypatch.setattr(acceptance, "api", lambda path: {"workflow_runs": [{"id": 5}]})
    assert acceptance.is_latest("owner/repo", run, 2)
    assert not acceptance.is_latest("owner/repo", run, 1)
    monkeypatch.setattr(acceptance, "api", lambda path: {"workflow_runs": [{"id": 6}, {"id": 5}]})
    assert not acceptance.is_latest("owner/repo", run, 2)


def test_conversation_protection_changes_only_the_requested_graphql_field(monkeypatch):
    from tools.development import protection

    calls = []

    def api(path, payload):
        calls.append((path, payload))
        if len(calls) == 1:
            return {
                "data": {
                    "repository": {
                        "ref": {"branchProtectionRule": {"id": "rule-id", "pattern": "main"}}
                    }
                }
            }
        return {
            "data": {
                "updateBranchProtectionRule": {
                    "branchProtectionRule": {"requiresConversationResolution": True}
                }
            }
        }

    monkeypatch.setattr(protection, "api", api)
    protection.require_conversation_resolution("owner/repo")
    assert [path for path, _ in calls] == ["graphql", "graphql"]
    assert calls[0][1]["variables"] == {"owner": "owner", "name": "repo"}
    assert calls[1][1]["variables"]["input"] == {
        "branchProtectionRuleId": "rule-id",
        "requiresConversationResolution": True,
    }


@pytest.mark.parametrize("rule", [None, {"id": "shared-rule", "pattern": "*"}])
def test_conversation_protection_refuses_missing_or_shared_rules(monkeypatch, rule):
    from tools.development import protection

    calls = []

    def api(path, payload):
        calls.append(payload)
        return {"data": {"repository": {"ref": {"branchProtectionRule": rule}}}}

    monkeypatch.setattr(protection, "api", api)
    with pytest.raises(GraphError, match="scoped to main"):
        protection.require_conversation_resolution("owner/repo")
    assert len(calls) == 1
