from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from tools.development import __main__ as development
from tools.project_graph.repository import GraphError, Repository

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="runner owns POSIX process groups")

SERVER = """
import json, os, signal, socket, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    server.listen()
    path = Path(sys.argv[1])
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid': os.getpid(), 'port': server.getsockname()[1]}))
    temporary.replace(path)
    while True:
        time.sleep(.05)
"""

RUNNER = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.development import __main__ as development
from tools.project_graph.repository import Repository
root = Path(sys.argv[2])
source = Repository(Path(sys.argv[1]))
development.Repository = lambda root: source
original_stop = development.stop
def observed_stop(process, **kwargs):
    wait = process.wait
    def observed_wait(timeout=None):
        if timeout == 10:
            (root / 'stop-started').touch()
        return wait(timeout=timeout)
    process.wait = observed_wait
    return original_stop(process, **kwargs)
def execute(runner, check):
    # Controlled native report fixture, not an executed browser assertion.
    runner.tests.append({'path': 'fixture.py', 'name': 'interrupted fixture', 'status': 'failed'})
    (runner.output / 'native.json').write_text(json.dumps({'failure': 'retained fixture'}))
    runner.command(sys.executable, str(root / 'server.py'), str(root / 'owned.json'))
development.stop = observed_stop
development.Runner.execute = execute
try:
    development.run(root, 'browser-python')
except KeyboardInterrupt:
    raise SystemExit(130)
"""


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("owned fixture did not reach its bounded lifecycle state")
        time.sleep(0.01)


def port_free(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


@contextmanager
def owned_process(tmp_path, name="owned"):
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    path = tmp_path / (name + ".json")
    process = subprocess.Popen([sys.executable, str(server), str(path)], start_new_session=True)
    reap = process.wait
    poll = process.poll
    signal_group = os.killpg
    try:
        until(path.exists)
        yield process, json.loads(path.read_text())
    finally:
        if poll() is None:
            with suppress(ProcessLookupError):
                signal_group(process.pid, signal.SIGKILL)
        reap(timeout=3)


def original_error(kind):
    return {
        "interrupt": KeyboardInterrupt("original interrupt"),
        "timeout": subprocess.TimeoutExpired("original command", 7),
        "failure": GraphError("original failure"),
    }[kind]


def stop_and_reraise(process, error, **kwargs):
    try:
        raise error
    except BaseException:
        development.stop(process, **kwargs)
        raise


@pytest.mark.parametrize("kind", ["interrupt", "timeout", "failure"])
@pytest.mark.parametrize("graceful", [False, True])
def test_cleanup_finishes_after_additional_interrupt_and_preserves_original_error(
    tmp_path, kind, graceful
):
    with owned_process(tmp_path) as (process, info):
        wait = process.wait

        def interrupted_wait(timeout=None):
            if timeout == 10:
                raise KeyboardInterrupt("additional interrupt")
            return wait(timeout=timeout)

        process.wait = interrupted_wait
        error = original_error(kind)
        with pytest.raises(BaseException) as caught:
            stop_and_reraise(process, error, graceful_interrupt=graceful)
        assert caught.value is error
        assert process.poll() is not None
        assert port_free(info["port"])


@pytest.mark.parametrize("kind", ["interrupt", "timeout", "failure"])
@pytest.mark.parametrize("phase", ["initial", "forced"])
def test_cleanup_tolerates_group_exit_race_without_masking_original_error(
    tmp_path, monkeypatch, kind, phase
):
    with owned_process(tmp_path) as (process, info):
        signal_group = os.killpg
        wait = process.wait

        def disappearing_group(pid, delivered):
            assert pid == process.pid
            if phase == "initial" or delivered == signal.SIGKILL:
                signal_group(pid, signal.SIGKILL)
                wait(timeout=3)
                raise ProcessLookupError("owned group disappeared during signal")
            signal_group(pid, delivered)

        def expired_wait(timeout=None):
            if timeout == 10:
                raise subprocess.TimeoutExpired("grace wait", 10)
            return wait(timeout=timeout)

        monkeypatch.setattr(development.os, "killpg", disappearing_group)
        process.wait = expired_wait if phase == "forced" else wait
        error = original_error(kind)
        with pytest.raises(BaseException) as caught:
            stop_and_reraise(process, error)
        assert caught.value is error
        assert process.poll() is not None
        assert port_free(info["port"])


def test_new_interrupt_during_normal_cleanup_is_raised_after_reaping(tmp_path, monkeypatch):
    with owned_process(tmp_path) as (process, info):
        wait = process.wait
        poll = process.poll
        interrupts = 0
        delivered = []
        signal_group = os.killpg

        def record_signal(pid, requested):
            assert pid == process.pid
            delivered.append(requested)
            signal_group(pid, requested)

        monkeypatch.setattr(development.os, "killpg", record_signal)

        def interrupted_wait(timeout=None):
            nonlocal interrupts
            if interrupts < 3:
                interrupts += 1
                raise KeyboardInterrupt
            return wait(timeout=timeout)

        process.wait = interrupted_wait
        process.poll = lambda: None if interrupts < 3 else poll()
        with pytest.raises(KeyboardInterrupt):
            development.stop(process)
        assert interrupts == 3
        assert delivered == [signal.SIGTERM, signal.SIGKILL]
        assert process.poll() is not None
        assert port_free(info["port"])


def test_repeated_real_sigint_retains_failed_receipt_and_stops_only_owned_command(tmp_path):
    with owned_process(tmp_path, "unrelated") as (unrelated, unrelated_info):
        ready = tmp_path / "owned.json"
        with (tmp_path / "terminal.log").open("w") as log:
            runner = subprocess.Popen(
                [sys.executable, "-c", RUNNER, str(ROOT), str(tmp_path)],
                start_new_session=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                until(ready.exists)
                info = json.loads(ready.read_text())
                runner.send_signal(signal.SIGINT)
                until(lambda: (tmp_path / "stop-started").exists())
                # The fixture ignores TERM/INT; only owned-group escalation can stop it.
                runner.send_signal(signal.SIGINT)
                assert runner.wait(timeout=5) == 130
                receipt = json.loads(
                    (tmp_path / ".project-graph/evidence/browser-python/receipt.json").read_text()
                )
                assert receipt["status"] == "failed"
                assert receipt["error"] == "verification interrupted"
                assert receipt["tests"] == [
                    {"path": "fixture.py", "name": "interrupted fixture", "status": "failed"}
                ]
                assert receipt["sourceRevision"] == Repository(ROOT).revision
                assert receipt["treeDigest"] == Repository(ROOT).tree_digest
                native = tmp_path / ".project-graph/evidence/browser-python/native.json"
                assert json.loads(native.read_text()) == {"failure": "retained fixture"}
                assert port_free(info["port"])
                assert unrelated.poll() is None
                assert not port_free(unrelated_info["port"])
            finally:
                if runner.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(runner.pid, signal.SIGKILL)
                runner.wait(timeout=3)
                if ready.exists():
                    info = json.loads(ready.read_text())
                    if not port_free(info["port"]):
                        with suppress(ProcessLookupError):
                            os.killpg(info["pid"], signal.SIGKILL)
