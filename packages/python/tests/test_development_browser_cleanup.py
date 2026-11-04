from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock

import pytest
from tools.development import __main__ as development
from tools.project_graph.repository import Repository

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="runner uses POSIX process groups")

SERVER = """
import json, os, socket, sys, time
from pathlib import Path
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    server.listen()
    Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'port': server.getsockname()[1]}))
    while True:
        time.sleep(.05)
"""

CONTROLLER = """
import json, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
children = []
try:
    for name in ('api', 'vite'):
        children.append(subprocess.Popen([sys.executable, str(root / 'server.py'), str(root / (name + '.json'))], start_new_session=True))
    while not all((root / (name + '.json')).exists() for name in ('api', 'vite')):
        time.sleep(.01)
    (root / 'ready').touch()
    while not (root / 'release').exists():
        time.sleep(.01)
    if sys.argv[2] == 'failure':
        raise SystemExit(3)
except KeyboardInterrupt:
    raise SystemExit(130)
finally:
    for child in children:
        child.terminate()
        child.wait(timeout=3)
    # Controlled native-report fixture, not a claim to execute browser assertions.
    status = {'success': 'passed', 'failure': 'failed'}.get(sys.argv[2], 'interrupted')
    report = {'config': {'rootDir': str(root)}, 'suites': [{'specs': [{'file': 'fixture.spec.ts', 'title': 'owned fixtures', 'id': 'fixture', 'tests': [{'projectName': 'chromium', 'results': [{'status': status}]}]}]}]}
    Path(sys.argv[3]).write_text(json.dumps(report))
"""


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("fixture did not reach its bounded lifecycle state")
        time.sleep(0.01)


def port_free(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt", "timeout"])
def test_browser_lifecycle_cleans_detached_owned_servers_and_retains_evidence(
    tmp_path, monkeypatch, outcome
):
    (tmp_path / "clients/console").mkdir(parents=True)
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    controller = tmp_path / "controller.py"
    controller.write_text(CONTROLLER)
    launch = subprocess.Popen
    unrelated = launch([sys.executable, str(server), str(tmp_path / "unrelated.json")])
    process = None
    try:
        until(lambda: (tmp_path / "unrelated.json").exists())
        unrelated_info = json.loads((tmp_path / "unrelated.json").read_text())

        def browser_process(argv, **kwargs):
            nonlocal process
            if argv[0] != "node":
                return launch(argv, **kwargs)
            assert argv == ("node", "node_modules/@playwright/test/cli.js", "test")
            assert kwargs["start_new_session"] is True
            process = launch(
                [
                    sys.executable,
                    str(controller),
                    str(tmp_path),
                    outcome,
                    kwargs["env"]["PLAYWRIGHT_JSON_OUTPUT_FILE"],
                ],
                **kwargs,
            )
            real_wait = process.wait

            def wait(timeout=None):
                if timeout == 600:
                    until(lambda: (tmp_path / "ready").exists())
                    if outcome == "interrupt":
                        raise KeyboardInterrupt
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(argv, timeout)
                    (tmp_path / "release").touch()
                return real_wait(timeout=timeout)

            process.wait = wait
            return process

        source = Repository(ROOT)
        monkeypatch.setattr(development.subprocess, "Popen", browser_process)
        monkeypatch.setattr(development, "Repository", lambda root: source)
        if outcome == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                development.run(tmp_path, "browser-python")
        else:
            development.run(tmp_path, "browser-python")
        evidence = tmp_path / ".project-graph/evidence/browser-python"
        receipt = json.loads((evidence / "receipt.json").read_text())
        assert receipt["status"] == ("passed" if outcome == "success" else "failed")
        assert receipt["tests"][0]["name"] == "owned fixtures"
        assert receipt["tests"][0]["status"] == ("passed" if outcome == "success" else "failed")
        assert receipt["sourceRevision"] == source.revision
        assert receipt["treeDigest"] == source.tree_digest
        assert (evidence / "playwright.json").exists()
        if outcome == "interrupt":
            assert receipt["error"] == "verification interrupted"
            assert process.returncode == 130
        elif outcome == "timeout":
            assert "600 seconds" in receipt["error"]
        elif outcome == "failure":
            assert "command failed (3)" in receipt["error"]
        for name in ("api", "vite"):
            info = json.loads((tmp_path / (name + ".json")).read_text())
            assert port_free(info["port"])
        assert unrelated.poll() is None
        assert not port_free(unrelated_info["port"])
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        # Test-owned detached children only; never search or kill by port.
        for name in ("api", "vite"):
            path = tmp_path / (name + ".json")
            if path.exists():
                info = json.loads(path.read_text())
                if not port_free(info["port"]):
                    with suppress(ProcessLookupError):
                        os.kill(info["pid"], signal.SIGKILL)
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_browser_graceful_cleanup_has_bounded_forced_fallback(monkeypatch):
    process = Mock(pid=123)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("playwright", 10), 0]
    kill = Mock()
    monkeypatch.setattr(development.os, "killpg", kill)
    development.stop(process, graceful_interrupt=True)
    process.send_signal.assert_called_once_with(signal.SIGINT)
    assert process.wait.call_args_list[0].kwargs == {"timeout": 10}
    kill.assert_called_once_with(123, signal.SIGKILL)


def test_other_commands_keep_existing_termination_policy(monkeypatch):
    process = Mock(pid=123)
    process.poll.return_value = None
    kill = Mock()
    monkeypatch.setattr(development.os, "killpg", kill)
    development.stop(process)
    process.send_signal.assert_not_called()
    kill.assert_called_once_with(123, signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=10)
