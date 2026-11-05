"""Record native outcomes and fixed skip classifications without freeform payloads."""

import json
import os
from pathlib import Path

_results = {}
_errors = []

# Exact reviewed reasons only: truncating arbitrary reason text cannot redact it.
_SKIP_REASONS = {
    "needs node + the built TS dist (cd packages/typescript && pnpm -r build)": "node-resolver-prerequisite",
    "isolated Temporal lane required": "isolated-temporal-lane-required",
    "git is not installed": "git-prerequisite",
    "temporalio is not installed": "temporal-sdk-prerequisite",
}


def _skip_diagnostic(report):
    reason = "unclassified"
    if hasattr(report, "wasxfail"):
        reason = "expected-failure"
    elif isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
        text = report.longrepr[2]
        if isinstance(text, str):
            reason = _SKIP_REASONS.get(text.removeprefix("Skipped: "), reason)
    return {
        "phase": report.when if report.when in {"setup", "call", "teardown"} else "unknown",
        "reasonCode": reason,
        "detailsOmitted": reason in {"unclassified", "expected-failure"},
    }


def pytest_collection_finish(session):
    root = Path(os.environ["TYPEFLUX_REPO_ROOT"])
    for item in session.items:
        _results[item.nodeid] = {
            "path": str(Path(item.path).relative_to(root)),
            "name": getattr(item, "originalname", None) or item.name,
            "id": item.nodeid,
            "status": "not-run",
        }


def pytest_collectreport(report):
    if report.failed:
        _errors.append(report.nodeid)


def pytest_runtest_logreport(report):
    item = _results.get(report.nodeid)
    if item is None:
        return
    if report.skipped:
        diagnostics = item.setdefault("skipDiagnostics", [])
        if len(diagnostics) < 3:
            diagnostics.append(_skip_diagnostic(report))
        else:
            item["skipDiagnosticsOmitted"] = item.get("skipDiagnosticsOmitted", 0) + 1
    if report.failed:
        item["status"] = "failed"
    elif item["status"] != "failed":
        if report.skipped:
            item["status"] = "skipped"
        elif report.when == "call":
            item["status"] = "passed"


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["TYPEFLUX_TEST_REPORT"]).write_text(
        json.dumps(
            {
                "tests": list(_results.values()),
                "errors": _errors,
                "exitCode": int(exitstatus),
            }
        )
    )
