from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from tools.development import __main__ as development
from tools.development import host
from tools.project_graph.repository import Repository

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("wall,monotonic", [(301, 301), (301, 2), (-301, 2)])
def test_host_gaps_report_uncertainty_for_suspension_stalls_and_clock_changes(wall, monotonic):
    gaps = host.ClockGaps()
    gaps.sample(0, 0)
    gaps.sample(wall, monotonic)
    result = gaps.result()
    assert result["possibleHostInterruption"] is True
    assert result["samples"] == [{"wallSeconds": wall, "monotonicSeconds": monotonic}]
    assert "do not establish the cause" in result["uncertainty"]
    assert "No observed gap does not exclude suspension" in result["uncertainty"]


def test_host_observations_are_bounded_without_hiding_gap_count():
    gaps = host.ClockGaps()
    gaps.sample(0, 0)
    for value in range(10, 210, 10):
        gaps.sample(value, value)
    result = gaps.result()
    assert result["gapCount"] == 20
    assert len(result["samples"]) == host.MAX_GAPS
    assert result["omittedSamples"] == 20 - host.MAX_GAPS
    gaps = host.ClockGaps()
    for value in range(5):
        gaps.sample(value, value)
    assert gaps.result()["possibleHostInterruption"] is False


def test_host_observer_degrades_when_sampling_thread_is_unavailable(monkeypatch):
    monkeypatch.setattr(host.threading.Thread, "start", Mock(side_effect=RuntimeError("limit")))
    with host.observe_host() as gaps:
        assert gaps.result()["available"] is False
    assert gaps.result()["possibleHostInterruption"] is False


def test_host_observer_stops_on_command_failure():
    with pytest.raises(ValueError), host.observe_host() as gaps:
        raise ValueError("command failure")
    assert gaps.result()["available"] is True
    assert not any(t.name == "typeflux-host-observer" for t in host.threading.enumerate())


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_host_clock_unavailable_does_not_block_command(monkeypatch, error):
    monkeypatch.setattr(host.time, "time", Mock(side_effect=error))
    with host.observe_host() as gaps:
        assert gaps.result()["available"] is False


def test_runner_retains_failed_native_evidence_despite_suspected_host_gap(tmp_path, monkeypatch):
    @contextmanager
    def observed_gap():
        gaps = host.ClockGaps()
        gaps.sample(0, 0)
        gaps.sample(300, 1)
        yield gaps

    monkeypatch.setattr(development, "observe_host", observed_gap)
    report = tmp_path / "native.json"

    def failing_check(runner, check):
        runner.tests.append({"path": "test.py", "name": "fails", "status": "failed"})
        report.write_text('{"failed": true}')
        runner.command(sys.executable, "-c", "raise SystemExit(3)")

    monkeypatch.setattr(development.Runner, "execute", failing_check)
    monkeypatch.setattr(development, "Repository", lambda root: Repository(ROOT))
    result = development.run(tmp_path, "browser-python")
    saved = json.loads(
        (tmp_path / ".project-graph/evidence/browser-python/receipt.json").read_text()
    )
    assert saved == result
    assert result["status"] == "failed"
    assert "command failed (3)" in result["error"]
    assert result["tests"][0]["status"] == "failed"
    assert result["hostDiagnostics"][0]["possibleHostInterruption"] is True
    assert result["sourceRevision"] == Repository(ROOT).revision
    assert result["treeDigest"] == Repository(ROOT).tree_digest
    assert report.read_text() == '{"failed": true}'


def test_runner_host_observations_do_not_change_command_timeout(tmp_path, monkeypatch):
    runner = development.Runner(tmp_path, tmp_path)
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("command", 7)
    process.poll.return_value = 1
    process.__enter__ = Mock(return_value=process)
    process.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(development.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(subprocess.TimeoutExpired):
        runner.command("command", timeout=7)
    process.wait.assert_called_once_with(timeout=7)
    assert runner.host_diagnostics[0]["commandIndex"] == 0


def test_runner_writes_failed_receipt_before_propagating_interrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(development, "Repository", lambda root: Repository(ROOT))
    monkeypatch.setattr(development.Runner, "execute", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        development.run(tmp_path, "browser-python")
    receipt = json.loads(
        (tmp_path / ".project-graph/evidence/browser-python/receipt.json").read_text()
    )
    assert receipt["status"] == "failed"
    assert receipt["error"] == "verification interrupted"


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_keep_awake_is_process_scoped_and_cleans_up_on_exit(monkeypatch, failure):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr(host.subprocess, "Popen", launch)

    def execute():
        with host.keep_awake(True) as record:
            assert record == {"requested": True, "status": "started"}
            if failure:
                raise failure

    if failure:
        with pytest.raises(failure):
            execute()
    else:
        execute()
    assert launch.call_args.args[0] == ["/usr/bin/caffeinate", "-i", "-w", str(host.os.getpid())]
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    process.kill.assert_not_called()


def test_keep_awake_kills_only_its_stuck_helper(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 5), 0]
    monkeypatch.setattr(host.subprocess, "Popen", Mock(return_value=process))
    with host.keep_awake(True):
        pass
    process.kill.assert_called_once()


@pytest.mark.parametrize(
    "system,requested,status",
    [("Linux", True, "unsupported-platform"), ("Darwin", False, "disabled")],
)
def test_keep_awake_is_opt_in_and_graceful_on_unsupported_hosts(
    monkeypatch, system, requested, status
):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(host.platform, "system", lambda: system)
    launch = Mock(side_effect=AssertionError("must not spawn a helper"))
    monkeypatch.setattr(host.subprocess, "Popen", launch)
    with host.keep_awake(requested) as record:
        assert record["status"] == status
    launch.assert_not_called()


def test_keep_awake_missing_tool_does_not_fail_verification(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host.subprocess, "Popen", Mock(side_effect=FileNotFoundError))
    with host.keep_awake(True) as record:
        assert record["status"] == "unavailable"


@pytest.mark.parametrize(
    "key,value", [("CI", "true"), ("CI", "yes"), ("CI", "on"), ("GITHUB_ACTIONS", "true")]
)
def test_keep_awake_refuses_ci_without_starting_process(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    launch = Mock(side_effect=AssertionError("must not spawn in CI"))
    monkeypatch.setattr(host.subprocess, "Popen", launch)
    with pytest.raises(ValueError, match="local option"), host.keep_awake(True):
        pass
    launch.assert_not_called()


def test_cli_keeps_awake_for_selected_checks_and_cleans_up_after_failure(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(host.subprocess, "Popen", Mock(return_value=process))
    run = Mock(side_effect=[{"status": "passed"}, {"status": "failed"}])
    monkeypatch.setattr(development, "run", run)
    assert development.main(["run", "python", "web", "--keep-awake"]) == 1
    assert [call.args[1] for call in run.call_args_list] == ["python", "web"]
    assert all(
        call.kwargs["power"] == {"requested": True, "status": "started"}
        for call in run.call_args_list
    )
    process.terminate.assert_called_once()


def test_cli_rejects_ci_keep_awake_before_executing_checks(monkeypatch):
    monkeypatch.setenv("CI", "yes")
    run = Mock(side_effect=AssertionError("CI power control must fail before execution"))
    monkeypatch.setattr(development, "run", run)
    with pytest.raises(SystemExit) as error:
        development.main(["run", "python", "--keep-awake"])
    assert error.value.code == 2
    run.assert_not_called()
