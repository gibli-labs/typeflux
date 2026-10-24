"""Inspect/reconcile the required-check contract without replacing other branch protections."""

from __future__ import annotations

import argparse
import json
import os
import subprocess

from tools.project_graph.repository import GraphError

from .acceptance import api
from .registry import REQUIRED_CONTEXTS


def differences(protection):
    checks = protection.get("required_status_checks") or {}
    actual = {c["context"]: c.get("app_id") for c in checks.get("checks", [])}
    expected = dict.fromkeys(REQUIRED_CONTEXTS, 15368)  # GitHub Actions, verified on this repo.
    return {
        "checks": actual != expected,
        "strict": checks.get("strict") is not True,
        "conversations": protection.get("required_conversation_resolution", {}).get("enabled")
        is not True,
    }


def require_conversation_resolution(repository):
    # GitHub exposes this field through GraphQL, not a standalone REST endpoint.
    owner, name = repository.split("/", 1)
    result = api(
        "graphql",
        {
            "query": """query($owner:String!, $name:String!) {
              repository(owner:$owner, name:$name) {
                ref(qualifiedName:"refs/heads/main") {
                  branchProtectionRule { id pattern }
                }
              }
            }""",
            "variables": {"owner": owner, "name": name},
        },
    )
    if result.get("errors"):
        raise GraphError(f"cannot inspect branch protection: {result['errors']}")
    repo = result.get("data", {}).get("repository") or {}
    rule = (repo.get("ref") or {}).get("branchProtectionRule") or {}
    if rule.get("pattern") != "main" or not rule.get("id"):
        raise GraphError("conversation reconciliation requires a protection rule scoped to main")
    result = api(
        "graphql",
        {
            "query": """mutation($input:UpdateBranchProtectionRuleInput!) {
              updateBranchProtectionRule(input:$input) {
                branchProtectionRule { requiresConversationResolution }
              }
            }""",
            "variables": {
                "input": {
                    "branchProtectionRuleId": rule["id"],
                    "requiresConversationResolution": True,
                }
            },
        },
    )
    if result.get("errors"):
        raise GraphError(f"cannot update branch protection: {result['errors']}")
    updated = (result.get("data") or {}).get("updateBranchProtectionRule") or {}
    if (updated.get("branchProtectionRule") or {}).get(
        "requiresConversationResolution"
    ) is not True:
        raise GraphError("conversation-resolution update was not confirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY", "gibli-labs/typeflux")
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    endpoint = f"repos/{args.repository}/branches/main/protection"
    current = api(endpoint)
    drift = differences(current)
    if args.apply:
        # Do not strand the migration PR behind a workflow that does not exist on main yet.
        checks = pages_for_commit(args.repository)
        if not any(
            c["name"] == "CI acceptance"
            and c["conclusion"] == "success"
            and c["app"]["id"] == 15368
            for c in checks
        ):
            raise GraphError("first land and verify CI acceptance on main, then apply protection")
        payload = {
            "strict": True,
            "checks": [{"context": n, "app_id": 15368} for n in REQUIRED_CONTEXTS],
        }
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                endpoint + "/required_status_checks",
                "--input",
                "-",
            ],
            input=json.dumps(payload),
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if drift["conversations"]:
            require_conversation_resolution(args.repository)
        drift = differences(api(endpoint))
    print(json.dumps({"requiredContexts": REQUIRED_CONTEXTS, "drift": drift}, indent=2))
    return int(any(drift.values()))


def pages_for_commit(repository):
    from .acceptance import pages

    return pages(f"repos/{repository}/commits/main/check-runs", "check_runs")


if __name__ == "__main__":
    raise SystemExit(main())
