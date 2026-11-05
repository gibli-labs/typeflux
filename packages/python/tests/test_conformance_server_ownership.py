"""Real-process ownership checks for the shared stdlib conformance runner."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tools.development import __main__ as development
from tools.project_graph.repository import Repository

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "contracts/controlplane/conformance/runner.py"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="runner uses POSIX process groups")
SERVER = """
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
root = Path(sys.argv[1])
if sys.argv[2] == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        with (root / 'requests').open('a') as log: log.write(self.path + '\\n')
        if self.path == '/case' and sys.argv[2] == 'hang':
            (root / 'inflight').touch()
            while not (root / 'release').exists(): time.sleep(.01)
        body = b'{"ok":false}' if sys.argv[2] == 'failure' else b'{"ok":true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server = HTTPServer((sys.argv[4] if len(sys.argv) > 4 else '127.0.0.1', int(sys.argv[3])), Handler)
staged = root / 'server.tmp'
staged.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp(), 'port': server.server_port}))
staged.replace(root / 'server.json')
server.serve_forever(.01)
"""


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("owned fixture missed its bounded lifecycle state")
        time.sleep(0.01)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def port_closed(port):
    # Connect failures can mean a full backlog, not a closed listener.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen()
        except OSError:
            return False
    return True


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_owned(pid, pgid=None):
    pgid = pid if pgid is None else pgid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@pytest.fixture
def fixture(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "_suite.json").write_text(
        json.dumps({"normalize": {"mask_keys": []}, "cases": ["case.json"]})
    )
    (fixtures / "case.json").write_text(
        json.dumps(
            {
                "name": "owned server case",
                "request": {"method": "GET", "path": "/case"},
                "response": {"status": 200, "body": {"ok": True}},
            }
        )
    )
    (tmp_path / "server.py").write_text(SERVER)
    return tmp_path, [sys.executable, str(SCRIPT), "--fixtures", str(fixtures)]


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_conformance_managed_mode_rejects_decoy_without_requests_or_spawn(fixture, host):
    root, command = fixture
    decoy = subprocess.Popen(
        [sys.executable, str(root / "server.py"), str(root), "ready", "0", host],
        start_new_session=True,
    )
    try:
        until(lambda: (root / "server.json").exists())
        info = json.loads((root / "server.json").read_text())
        marker = root / "spawned"
        startup = shlex.join(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(60)",
            ]
        )
        result = subprocess.run(
            [*command, "--server-cmd", startup, "--port", str(info["port"])],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert not marker.exists()
        assert not (root / "requests").exists()
        assert decoy.poll() is None
        # A reusable loopback bind can coexist with a wildcard listener on BSD;
        # establish that this exact live decoy still accepts TCP connections.
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1):
            pass
    finally:
        stop_owned(decoy.pid)
        decoy.wait(timeout=3)


def test_conformance_explicit_external_mode_remains_available(fixture):
    root, command = fixture
    external = subprocess.Popen(
        [sys.executable, str(root / "server.py"), str(root), "ready", "0"],
        start_new_session=True,
    )
    try:
        until(lambda: (root / "server.json").exists())
        info = json.loads((root / "server.json").read_text())
        result = subprocess.run(
            [*command, "--base-url", f"http://127.0.0.1:{info['port']}"],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode()
        assert (root / "requests").read_text().splitlines().count("/case") == 1
        assert external.poll() is None
    finally:
        stop_owned(external.pid)
        external.wait(timeout=3)


@pytest.mark.parametrize("requested", [signal.SIGINT, signal.SIGTERM])
def test_conformance_signal_cleans_detached_server(fixture, requested):
    root, command = fixture
    port = free_port()
    startup = shlex.join([sys.executable, str(root / "server.py"), str(root), "hang", "{port}"])
    controller = subprocess.Popen(
        [*command, "--server-cmd", startup, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        until(lambda: (root / "inflight").exists())
        controller.send_signal(requested)
        assert controller.wait(timeout=8) != 0
        info = json.loads((root / "server.json").read_text())
        until(lambda: port_closed(port) and not alive(info["pid"]), timeout=2)
    finally:
        if (root / "server.json").exists():
            info = json.loads((root / "server.json").read_text())
            stop_owned(info["pid"], info["pgid"])
        stop_owned(controller.pid)
        controller.wait(timeout=3)


def load_runner():
    spec = importlib.util.spec_from_file_location("ownership_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def controlled_entry(
    root, *, window=None, requested=signal.SIGTERM, short_grace=False, cleanup_error=False
):
    entry = root / "entry.py"
    entry.write_text(
        "import importlib.util, os, signal, sys, time\n"
        "from pathlib import Path\n"
        f"spec=importlib.util.spec_from_file_location('runner', {str(SCRIPT)!r})\n"
        "runner=importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)\n"
        + ("runner.SHUTDOWN_GRACE_SECONDS=.15\n" if short_grace else "")
        + (
            "original=runner._spawn\n"
            "def spawn(command):\n"
            " child=original(command)\n"
            " deadline=time.monotonic()+5\n"
            f" while not Path({str(root / 'server.json')!r}).exists():\n"
            "  if time.monotonic()>=deadline: raise RuntimeError('fixture startup missed deadline')\n"
            "  time.sleep(.01)\n"
            f" os.kill(os.getpid(), {int(requested)})\n"
            " return child\n"
            "runner._spawn=spawn\n"
            if window == "startup"
            else ""
        )
        + (
            "original_teardown=runner._teardown\n"
            "def teardown(child):\n"
            + (f" os.kill(os.getpid(), {int(requested)})\n" if window == "teardown" else "")
            + " original_teardown(child)\n"
            + (" raise RuntimeError('private cleanup exception')\n" if cleanup_error else "")
            + "runner._teardown=teardown\n"
            if window == "teardown" or cleanup_error
            else ""
        )
        + (
            "original_exception=sys.exception\n"
            "fired=False\n"
            "def current_exception():\n"
            " global fired\n"
            " if not fired and sys._getframe(1).f_code.co_name == '_main':\n"
            "  fired=True\n"
            f"  os.kill(os.getpid(), {int(requested)})\n"
            " return original_exception()\n"
            "sys.exception=current_exception\n"
            if window == "cleanup-entry"
            else ""
        )
        + "raise SystemExit(runner.main())\n"
    )
    return entry


def clean_info(root):
    if (root / "server.json").exists():
        info = json.loads((root / "server.json").read_text())
        stop_owned(info["pid"], info["pgid"])


@pytest.mark.parametrize(("mode", "expected"), [("ready", 0), ("failure", 1)])
def test_conformance_managed_success_and_failure_reap_owned_group(fixture, mode, expected):
    root, command = fixture
    port = free_port()
    startup = shlex.join([sys.executable, str(root / "server.py"), str(root), mode, "{port}"])
    try:
        result = subprocess.run(
            [*command, "--server-cmd", startup, "--port", str(port)],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == expected, result.stderr.decode()
        info = json.loads((root / "server.json").read_text())
        until(lambda: port_closed(port) and not alive(info["pid"]))
        assert (root / "requests").read_text().splitlines().count("/case") == 1
    finally:
        clean_info(root)


@pytest.mark.parametrize("window", ["startup", "teardown"])
@pytest.mark.parametrize("requested", [signal.SIGINT, signal.SIGTERM])
def test_conformance_signals_in_ownership_windows_cannot_strand_server(fixture, window, requested):
    root, command = fixture
    entry = controlled_entry(root, window=window, requested=requested)
    port = free_port()
    startup = shlex.join([sys.executable, str(root / "server.py"), str(root), "ready", "{port}"])
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(entry),
                *command[2:],
                "--server-cmd",
                startup,
                "--port",
                str(port),
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 128 + requested, result.stderr.decode()
        info = json.loads((root / "server.json").read_text())
        until(lambda: port_closed(port) and not alive(info["pid"]))
        if window == "startup":
            assert not (root / "requests").exists()
    finally:
        clean_info(root)


def test_conformance_forces_term_resistant_descendant_after_launcher_exit(fixture):
    root, command = fixture
    entry = controlled_entry(root, short_grace=True)
    launcher = root / "launcher.py"
    launcher.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,{str(root / 'server.py')!r},{str(root)!r},'ignore',sys.argv[1]])\n"
        "while True: time.sleep(.1)\n"
    )
    port = free_port()
    startup = shlex.join([sys.executable, str(launcher), "{port}"])
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(entry),
                *command[2:],
                "--server-cmd",
                startup,
                "--port",
                str(port),
            ],
            capture_output=True,
            timeout=8,
        )
        assert result.returncode == 0, result.stderr.decode()
        info = json.loads((root / "server.json").read_text())
        until(lambda: port_closed(port) and not alive(info["pid"]))
    finally:
        clean_info(root)


def test_conformance_cleanup_error_does_not_replace_original_signal(fixture):
    root, command = fixture
    entry = controlled_entry(root, window="startup", requested=signal.SIGINT, cleanup_error=True)
    port = free_port()
    startup = shlex.join([sys.executable, str(root / "server.py"), str(root), "ready", "{port}"])
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(entry),
                *command[2:],
                "--server-cmd",
                startup,
                "--port",
                str(port),
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 130
        assert b"cleanup incomplete" in result.stderr
        assert b"private cleanup exception" not in result.stderr
        info = json.loads((root / "server.json").read_text())
        until(lambda: port_closed(port) and not alive(info["pid"]))
    finally:
        clean_info(root)


def test_conformance_restores_callers_signal_handlers(fixture):
    root, command = fixture
    runner = load_runner()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        assert (
            runner.main(
                ["--fixtures", str(root / "fixtures"), "--server-cmd", "unused", "--port", "0"]
            )
            == 2
        )
    finally:
        assert {sig: signal.getsignal(sig) for sig in previous} == previous


def test_development_interrupt_retains_failed_receipt_and_reaps_conformance_child(
    fixture, monkeypatch
):
    root, command = fixture
    port = free_port()
    startup = shlex.join([sys.executable, str(root / "server.py"), str(root), "hang", "{port}"])
    owned = None
    launch = subprocess.Popen
    source = Repository(ROOT)

    def process(*args, **kwargs):
        nonlocal owned
        owned = launch(*args, **kwargs)
        original_wait = owned.wait

        def wait(timeout=None):
            if timeout == 600:
                until(lambda: (root / "inflight").exists())
                raise KeyboardInterrupt
            return original_wait(timeout=timeout)

        owned.wait = wait
        return owned

    def execute(self, check):
        assert check == "conformance-python"
        self.command(*command, "--server-cmd", startup, "--port", str(port))

    monkeypatch.setattr(development, "Repository", lambda path: source)
    monkeypatch.setattr(development.Runner, "execute", execute)
    monkeypatch.setattr(development.subprocess, "Popen", process)
    try:
        with pytest.raises(KeyboardInterrupt):
            development.run(root, "conformance-python")
        receipt = json.loads(
            (root / ".project-graph/evidence/conformance-python/receipt.json").read_text()
        )
        assert receipt["status"] == "failed" and receipt["error"] == "verification interrupted"
        assert (
            receipt["treeDigest"] == source.tree_digest
            and receipt["sourceRevision"] == source.revision
        )
        assert len(receipt["commands"]) == 1
        info = json.loads((root / "server.json").read_text())
        until(lambda: port_closed(port) and not alive(info["pid"]))
        assert owned.poll() is not None
    finally:
        clean_info(root)
        if owned is not None:
            stop_owned(owned.pid)
            owned.wait(timeout=3)


def test_conformance_checks_every_profile_port_before_any_spawn(fixture, monkeypatch, capsys):
    root, command = fixture
    suite_path = root / "fixtures/_suite.json"
    suite = json.loads(suite_path.read_text())
    suite["profiles"] = {"open": {}, "proxy": {}}
    suite["cases"].append("proxy.json")
    suite_path.write_text(json.dumps(suite))
    case = json.loads((root / "fixtures/case.json").read_text())
    case["profile"] = "proxy"
    (root / "fixtures/proxy.json").write_text(json.dumps(case))
    runner = load_runner()

    def unexpected(command):
        pytest.fail("occupied later profile port must fail before the first spawn")

    monkeypatch.setattr(runner, "_spawn", unexpected)
    first = second = None
    try:
        for _ in range(10):
            first = socket.socket()
            second = socket.socket()
            first.bind(("127.0.0.1", 0))
            port = first.getsockname()[1]
            try:
                second.bind(("127.0.0.1", port + 1))
                second.listen()
                break
            except (OSError, OverflowError):
                first.close()
                second.close()
        else:
            pytest.fail("no adjacent test-owned ports available")
        first.close()
        assert (
            runner.main(
                [*command[2:], "--server-cmd", "unused {profile} {port}", "--port", str(port)]
            )
            == 2
        )
        assert "fixture port unavailable" in capsys.readouterr().err
        assert not port_closed(port + 1)
        assert not (root / "requests").exists()
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()


def test_conformance_group_permission_uncertainty_cannot_count_as_exit(monkeypatch):
    runner = load_runner()

    signals = []

    def denied(pgid, signum):
        signals.append(signum)
        if signum == 0:
            raise PermissionError("unavailable group state")

    monkeypatch.setattr(runner.os, "killpg", denied)
    monkeypatch.setattr(runner, "SHUTDOWN_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(runner, "FORCED_REAP_SECONDS", 0.01)
    assert runner._group_alive(12345) is None
    process = SimpleNamespace(pid=12345, poll=lambda: 0, wait=lambda timeout: 0)
    with pytest.raises(RuntimeError, match="group exit could not be verified"):
        runner._teardown(process)
    assert signal.SIGTERM in signals and signal.SIGKILL in signals


def test_conformance_signal_at_cleanup_entry_still_reaps_owned_server(fixture):
    root, command = fixture
    entry = controlled_entry(root, window="cleanup-entry", requested=signal.SIGTERM)
    port = free_port()
    startup = shlex.join([sys.executable, str(root / "server.py"), str(root), "ready", "{port}"])
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(entry),
                *command[2:],
                "--server-cmd",
                startup,
                "--port",
                str(port),
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 143, result.stderr.decode()
        info = json.loads((root / "server.json").read_text())
        until(lambda: port_closed(port) and not alive(info["pid"]), timeout=2)
    finally:
        clean_info(root)


def test_conformance_uncertain_tcp_probe_refuses_startup(fixture, monkeypatch, capsys):
    root, command = fixture
    runner = load_runner()
    probes = []

    def unavailable(address, timeout):
        probes.append((address, timeout))
        raise TimeoutError("controlled full/uncertain listener backlog")

    def unexpected(command):
        pytest.fail("uncertain occupied-port probe must fail before spawn")

    monkeypatch.setattr(runner.socket, "create_connection", unavailable)
    monkeypatch.setattr(runner, "_spawn", unexpected)
    assert runner.main([*command[2:], "--server-cmd", "unused", "--port", "12345"]) == 2
    assert probes == [(("127.0.0.1", 12345), 0.25)]
    assert "fixture port unavailable" in capsys.readouterr().err
    assert not (root / "requests").exists()
