"""Record collected identities and all test phases without assertion text or inputs."""

import json
import os
from pathlib import Path

_results = {}
_errors = []


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
