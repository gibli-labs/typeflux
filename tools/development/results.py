"""Normalize runner output and require actual passing declared test identities."""

from pathlib import Path

from tools.project_graph.repository import GraphError, safe_path


def relative(path: str, root: Path, cwd: Path) -> str:
    value = Path(path)
    if not value.is_absolute():
        value = cwd / value
    result = str(value.resolve().relative_to(root.resolve()))
    if not safe_path(result):
        raise GraphError("test result path escapes the repository")
    return result


def vitest(data: dict, root: Path, cwd: Path) -> list[dict]:
    result = []
    for suite in data.get("testResults", []):
        for test in suite.get("assertionResults", []):
            status = test["status"]
            result.append(
                {
                    "path": relative(suite["name"], root, cwd),
                    "name": test["title"],
                    "id": test["fullName"],
                    "status": status if status in {"passed", "failed"} else "skipped",
                }
            )
    return result


def playwright(data: dict, root: Path, cwd: Path) -> list[dict]:
    result = []
    directory = Path(data.get("config", {}).get("rootDir", cwd / "e2e"))

    def visit(suite):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                outcomes = [r.get("status") for r in test.get("results", [])]
                # A retry that eventually passes is still visible as a failure.
                status = (
                    "passed"
                    if outcomes and all(s == "passed" for s in outcomes)
                    else (
                        "skipped"
                        if outcomes and all(s == "skipped" for s in outcomes)
                        else "failed"
                    )
                )
                result.append(
                    {
                        "path": relative(spec["file"], root, directory),
                        "name": spec["title"],
                        "id": f"{spec['id']}:{test.get('projectName', '')}",
                        "status": status,
                    }
                )
        for child in suite.get("suites", []):
            visit(child)

    for suite in data.get("suites", []):
        visit(suite)
    return result


def journey_evidence(graph: dict, receipts: list[dict]) -> list[dict]:
    """Every declared test must pass in every mapped execution lane."""
    checks = []
    for check in graph["nodes"]:
        if check["kind"] != "check":
            continue
        results = []
        for lane in check.get("ci", []):
            candidates = [r for r in receipts if r.get("checkId") == lane]
            receipt = candidates[0] if len(candidates) == 1 else None
            for reference in check["tests"]:
                matches = [
                    t
                    for t in (receipt or {}).get("tests", [])
                    if (t.get("path"), t.get("name")) == (reference["path"], reference["name"])
                ]
                status = "missing"
                if matches:
                    statuses = {t.get("status") for t in matches}
                    status = (
                        "passed"
                        if statuses == {"passed"}
                        else ("failed" if "failed" in statuses else "skipped")
                    )
                if receipt and receipt.get("status") != "passed" and status == "passed":
                    status = "failed"
                results.append({**reference, "checkId": lane, "status": status})
        checks.append(
            {
                "id": check["id"],
                "environment": check["environment"],
                "tests": results,
                "status": "passed"
                if results and all(t["status"] == "passed" for t in results)
                else "failed"
                if any(t["status"] == "failed" for t in results)
                else "missing",
            }
        )
    return checks
