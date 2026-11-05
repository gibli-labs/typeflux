"""Native pytest outcomes retain bounded classification without reason payloads."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from tools.development import __main__ as development
from tools.development.results import journey_evidence
from tools.project_graph.repository import Repository

ROOT = Path(__file__).resolve().parents[3]
NODE_REASON = "needs node + the built TS dist (cd packages/typescript && pnpm -r build)"


def test_native_skip_diagnostics_survive_failed_receipt_without_reason_payloads(
    tmp_path, monkeypatch
):
    source = Repository(ROOT)
    evidence = ROOT / ".project-graph/evidence"
    original_receipts = {p: p.read_bytes() for p in evidence.glob("*/receipt.json")}
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_cases.py").write_text(
        f"""import pytest
@pytest.fixture
def setup_skip():
    pytest.skip({NODE_REASON!r})
@pytest.fixture
def teardown_skip():
    yield
    pytest.skip("secret-token alice@example.com " + "x" * 100000)
def test_setup(setup_skip):
    assert False
def test_call():
    pytest.skip("isolated Temporal lane required")
def test_teardown(teardown_skip):
    pass
def test_failed(teardown_skip):
    assert False
@pytest.mark.xfail(reason="secret-token alice@example.com")
def test_xfail():
    assert False
def test_unknown():
    pytest.skip("Skip: " + {NODE_REASON!r} + " secret-token")
def test_pass():
    pass
"""
    )

    def execute(runner, check):
        assert check == "python"
        # Use the real frozen environment, but keep every controlled output temporary.
        runner.root = ROOT
        runner.env["PYTHONPATH"] = str(ROOT)
        runner.pytest(
            "-c",
            str(tmp_path / "pytest.ini"),
            str(tmp_path / "test_cases.py"),
            label="controlled-skips",
            env={"TYPEFLUX_REPO_ROOT": str(tmp_path), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )

    monkeypatch.setattr(development.Runner, "execute", execute)
    monkeypatch.setattr(development, "Repository", lambda root: Repository(ROOT))
    record = development.run(tmp_path, "python")
    assert {p: p.read_bytes() for p in evidence.glob("*/receipt.json")} == original_receipts
    output = tmp_path / ".project-graph/evidence" / record["checkId"]
    assert json.loads((output / "receipt.json").read_text()) == record
    native = json.loads((output / "controlled-skips.json").read_text())
    assert record["status"] == "failed" and native["exitCode"] == 1
    assert (
        record["sourceRevision"] == source.revision and record["treeDigest"] == source.tree_digest
    )
    assert record["tests"] == native["tests"]
    tests = {t["name"]: t for t in record["tests"]}
    for name, phase, reason in [
        ("test_setup", "setup", "node-resolver-prerequisite"),
        ("test_call", "call", "isolated-temporal-lane-required"),
        ("test_teardown", "teardown", "unclassified"),
        ("test_failed", "teardown", "unclassified"),
        ("test_xfail", "call", "expected-failure"),
        ("test_unknown", "call", "unclassified"),
    ]:
        item = tests[name]
        assert item["status"] == ("failed" if name == "test_failed" else "skipped")
        assert item["skipDiagnostics"] == [
            {
                "phase": phase,
                "reasonCode": reason,
                "detailsOmitted": reason in {"unclassified", "expected-failure"},
            }
        ]
        assert item["path"] == "test_cases.py"
        graph = {
            "nodes": [
                {
                    "id": "check.controlled",
                    "kind": "check",
                    "environment": "test",
                    "ci": [record["checkId"]],
                    "tests": [{"path": item["path"], "name": name}],
                }
            ]
        }
        assert journey_evidence(graph, [{**record, "status": "passed"}])[0]["status"] != "passed"
    assert tests["test_pass"]["status"] == "passed" and "skipDiagnostics" not in tests["test_pass"]
    serialized = json.dumps(record)
    assert "secret-token" not in serialized and "alice@example.com" not in serialized
    assert "x" * 100 not in serialized and NODE_REASON not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["yaml", "composition"])
@pytest.mark.parametrize("error_type", [RuntimeError, OSError, TypeError])
async def test_required_temporal_connection_errors_fail_instead_of_skip(
    tmp_path, monkeypatch, target, error_type
):
    error = error_type("controlled connection failure")
    # The old implementation used this alternate startup path and swallowed it.
    monkeypatch.setattr(WorkflowEnvironment, "start_time_skipping", AsyncMock(side_effect=error))
    connect = AsyncMock(side_effect=error)
    monkeypatch.setattr(Client, "connect", connect)
    if target == "composition":
        from examples.child_workflow_composition.tests.test_composition import _running_worker

        with pytest.raises(error_type) as caught:
            async with _running_worker([]):
                pytest.fail("a failed connection cannot yield a worker")
    else:
        spec = importlib.util.spec_from_file_location(
            "controlled_yaml_tests", ROOT / "packages/python/tests/test_yaml.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with pytest.raises(error_type) as caught:
            await module.test_sandboxed_yaml_runtime_worker_executes_generated_workflow_end_to_end(
                tmp_path, monkeypatch
            )
    assert caught.value is error
    connect.assert_awaited_once()
    assert connect.call_args.args == ("127.0.0.1:7233",)
    WorkflowEnvironment.start_time_skipping.assert_not_awaited()


@pytest.mark.parametrize("failed_first", [False, True])
def test_skip_diagnostics_are_bounded_and_cannot_overwrite_failure(failed_first):
    spec = importlib.util.spec_from_file_location(
        "controlled_pytest_results", ROOT / "tools/development/pytest_results.py"
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    item = {"status": "not-run"}
    plugin._results["controlled"] = item
    failed = SimpleNamespace(nodeid="controlled", failed=True, skipped=False, when="call")
    skipped = SimpleNamespace(
        nodeid="controlled",
        failed=False,
        skipped=True,
        when="secret phase",
        longrepr=("secret path", 1, "secret reason"),
    )
    if failed_first:
        plugin.pytest_runtest_logreport(failed)
    for _ in range(20):
        plugin.pytest_runtest_logreport(skipped)
    if not failed_first:
        plugin.pytest_runtest_logreport(failed)
    assert item["status"] == "failed"
    assert (
        item["skipDiagnostics"]
        == [{"phase": "unknown", "reasonCode": "unclassified", "detailsOmitted": True}] * 3
    )
    assert item["skipDiagnosticsOmitted"] == 17
    assert "secret" not in json.dumps(item)
