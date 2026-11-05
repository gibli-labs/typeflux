"""Temporal startup observations do not alter readiness, ownership or evidence status."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tools.development import __main__ as development
from tools.development import host
from tools.project_graph.repository import GraphError, Repository

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def startup(tmp_path, monkeypatch):
    source = Repository(ROOT)
    state = SimpleNamespace(
        failure=None,
        phase="temporal-preflight",
        active=[],
        sockets=0,
        clock=0,
        sleeps=[],
        source=source,
    )

    @contextmanager
    def observe():
        gaps = host.ClockGaps()
        gaps.sample(0, 0)
        state.active.append(gaps)
        try:
            yield gaps
        finally:
            state.active.pop()

    def gap(phase):
        if state.active and state.phase == phase:
            state.active[-1].sample(300, 1)

    def connect(address):
        assert address == ("127.0.0.1", 7233)
        state.sockets += 1
        if state.sockets == 1:
            if state.failure == "occupied":
                gap("temporal-preflight")
                return 0
            return 1
        gap("temporal-startup")
        if state.failure == "interrupt":
            raise KeyboardInterrupt
        return 1 if state.failure in {"timeout", "early-exit"} else 0

    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.connect_ex.side_effect = connect
    monkeypatch.setattr(development, "socket", SimpleNamespace(socket=lambda: probe))

    def version(args, *, stdout, stderr):
        assert args == [str(tmp_path / ".project-graph/bin/temporal"), "--version"]
        gap("temporal-preflight")
        stderr.write("CLI version diagnostic\n")
        if state.failure in {"version-error", "version-interrupt"}:
            stdout.write("failed CLI stdout\n")
        if state.failure == "version-error":
            raise subprocess.CalledProcessError(9, args)
        if state.failure == "version-interrupt":
            raise KeyboardInterrupt
        stdout.write(
            "temporal version "
            + ("9.9.9" if state.failure == "bad-version" else "1.7.0")
            + " test\n"
        )
        return 0

    process = Mock()
    process.poll.side_effect = lambda: 3 if state.failure == "early-exit" else None

    def launch(args, **kwargs):
        assert args == [
            str(tmp_path / ".project-graph/bin/temporal"),
            "server",
            "start-dev",
            "--ip",
            "127.0.0.1",
            "--port",
            "7233",
            "--headless",
        ]
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["UV_FROZEN"] == "1"
        kwargs["stdout"].write("owned Temporal startup log\n")
        if state.failure == "launch-error":
            gap("temporal-startup")
            raise OSError("controlled launch failure")
        return process

    # Keep Git/source inspection and observer internals real; replace only this
    # runner's Temporal transport. No real server, port or unrelated process used.
    state.version, state.launch, state.stop = (
        Mock(side_effect=version),
        Mock(side_effect=launch),
        Mock(),
    )
    monkeypatch.setattr(
        development,
        "subprocess",
        SimpleNamespace(
            check_call=state.version,
            Popen=state.launch,
            STDOUT=subprocess.STDOUT,
            CalledProcessError=subprocess.CalledProcessError,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    monkeypatch.setattr(development, "stop", state.stop)
    monkeypatch.setattr(development, "observe_host", observe)
    monkeypatch.setattr(development, "Repository", lambda root: source)

    def sleep(seconds):
        assert seconds == 0.25
        state.sleeps.append(seconds)
        state.clock += 45.25

    monkeypatch.setattr(
        development, "time", SimpleNamespace(monotonic=lambda: state.clock, sleep=sleep)
    )

    def native(runner, *args, **kwargs):
        assert not state.active  # Startup observation must end before test commands.
        assert kwargs["env"] == {"TYPEFLUX_LIVE_TEMPORAL": "1"}
        failed = state.failure == "native-test"
        runner.tests.append(
            {"path": "controlled.py", "name": "native", "status": "failed" if failed else "passed"}
        )
        if failed:
            raise GraphError("controlled native failure")

    monkeypatch.setattr(development.Runner, "pytest", native)
    monkeypatch.setattr(development.Runner, "vitest", native)
    state.process = process
    state.output = tmp_path / ".project-graph/evidence/temporal"
    state.run = lambda: development.run(tmp_path, "temporal")
    state.receipt = lambda: json.loads((state.output / "receipt.json").read_text())
    return state


@pytest.mark.parametrize("phase", ["temporal-preflight", "temporal-startup"])
def test_temporal_startup_gaps_are_retained_without_changing_success(startup, phase):
    startup.phase = phase
    record = startup.run()
    assert record == startup.receipt() and record["status"] == "passed"
    assert record["sourceRevision"] == startup.source.revision
    assert record["treeDigest"] == startup.source.tree_digest
    diagnostics = {d["operation"]: d for d in record["hostDiagnostics"]}
    assert set(diagnostics) == {"temporal-preflight", "temporal-startup"}
    assert diagnostics[phase]["possibleHostInterruption"] is True
    assert diagnostics[phase]["samples"] == [{"wallSeconds": 300, "monotonicSeconds": 1}]
    assert "do not establish the cause" in diagnostics[phase]["uncertainty"]
    assert all("commandIndex" not in d for d in diagnostics.values())
    assert all(t["status"] == "passed" for t in record["tests"])
    startup.version.assert_called_once()
    startup.launch.assert_called_once()
    startup.stop.assert_called_once_with(startup.process)
    assert not startup.sleeps
    log = (startup.output / "temporal-version.log").read_text()
    assert "temporal version 1.7.0" in log and "CLI version diagnostic" not in log
    assert (
        startup.output / "temporal-version.stderr.log"
    ).read_text() == "CLI version diagnostic\n"
    assert (startup.output / "temporal.log").read_text() == "owned Temporal startup log\n"


@pytest.mark.parametrize(
    "failure,error",
    [
        ("occupied", "port 7233 is occupied"),
        ("bad-version", "requires CLI 1.7.0"),
        ("version-error", "non-zero exit status 9"),
        ("version-interrupt", "verification interrupted"),
        ("launch-error", "controlled launch failure"),
        ("early-exit", "failed to start"),
        ("timeout", "failed to start"),
        ("interrupt", "verification interrupted"),
        ("native-test", "controlled native failure"),
    ],
)
def test_temporal_startup_diagnostics_preserve_failures_deadline_and_ownership(
    startup, failure, error
):
    startup.failure = failure
    preflight = failure in {"occupied", "bad-version", "version-error", "version-interrupt"}
    startup.phase = "temporal-preflight" if preflight else "temporal-startup"
    if failure in {"interrupt", "version-interrupt"}:
        with pytest.raises(KeyboardInterrupt):
            startup.run()
        record = startup.receipt()
    else:
        record = startup.run()
    assert record["status"] == "failed" and error in record["error"]
    assert (
        record["sourceRevision"] == startup.source.revision
        and record["treeDigest"] == startup.source.tree_digest
    )
    diagnostics = {d["operation"]: d for d in record["hostDiagnostics"]}
    assert diagnostics[startup.phase]["possibleHostInterruption"] is True
    if preflight or failure == "launch-error":
        startup.stop.assert_not_called()
    else:
        startup.stop.assert_called_once_with(startup.process)
    if preflight:
        startup.launch.assert_not_called()
    if failure == "occupied":
        startup.version.assert_not_called()
    if failure == "timeout":
        assert startup.clock == 45.25 and startup.sleeps == [0.25]
    if failure in {"version-error", "version-interrupt"}:
        assert (startup.output / "temporal-version.log").read_text() == "failed CLI stdout\n"
        assert (
            startup.output / "temporal-version.stderr.log"
        ).read_text() == "CLI version diagnostic\n"
    if failure in {"early-exit", "timeout", "interrupt", "native-test"}:
        assert (startup.output / "temporal.log").read_text() == "owned Temporal startup log\n"
    if failure == "native-test":
        assert record["tests"][0]["status"] == "failed"
    else:
        assert record["tests"] == []


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_temporal_startup_unavailable_observer_does_not_block_validation(
    startup, monkeypatch, failure
):
    startup.failure = failure
    monkeypatch.setattr(development, "observe_host", host.observe_host)
    monkeypatch.setattr(
        host.threading.Thread, "start", Mock(side_effect=RuntimeError("unavailable"))
    )
    record = startup.run()
    assert record["status"] == ("failed" if failure else "passed")
    if failure:
        assert "failed to start" in record["error"] and startup.clock == 45.25
    assert len(record["hostDiagnostics"]) == 2
    assert all(
        d["available"] is False and not d["possibleHostInterruption"]
        for d in record["hostDiagnostics"]
    )
    startup.stop.assert_called_once_with(startup.process)


@pytest.mark.parametrize("failure", ["occupied", "bad-version"])
def test_temporal_preflight_failure_cannot_retain_stale_startup_logs(startup, failure):
    startup.output.mkdir(parents=True)
    names = ("temporal-version.log", "temporal-version.stderr.log", "temporal.log")
    for name in names:
        (startup.output / name).write_text("prior invocation output\n")
    startup.failure = failure
    record = startup.run()
    assert record["status"] == "failed"
    assert not (startup.output / "temporal.log").exists()
    if failure == "occupied":
        assert all(not (startup.output / name).exists() for name in names)
        startup.version.assert_not_called()
    else:
        assert "temporal version 9.9.9" in (startup.output / "temporal-version.log").read_text()
        assert (
            startup.output / "temporal-version.stderr.log"
        ).read_text() == "CLI version diagnostic\n"
    startup.launch.assert_not_called()
    startup.stop.assert_not_called()
