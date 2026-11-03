"""Foreground execution through real loaders, admission and the activity pipeline."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from typeflux.project import compose_project_policies, load_project_spec
from typeflux.yaml.local_cli import main

MODULE = """
import asyncio
import os
from pathlib import Path
from pydantic import BaseModel
from typeflux.testing import FakeProvider
from typeflux.observability.backend import NoOpTraceWriter, NoOpTraceReader

def record(event):
    path = os.getenv("CLI_EVENT_LOG")
    if path:
        with Path(path).open("a") as stream:
            stream.write(event + "\\n")

class Item(BaseModel):
    n: int

class Writer(NoOpTraceWriter):
    def shutdown(self):
        record("shutdown")

class Backend:
    def __init__(self):
        self.writer, self.reader = Writer(), NoOpTraceReader()

class Provider(FakeProvider):
    def __init__(self):
        record("constructed")
        if os.getenv("CLI_MODE") == "construction_failure":
            raise ValueError("provider configuration refused")
        super().__init__([])

    async def async_structured_call(self, *, messages, output_schema, **kwargs):
        record("called")
        mode = os.getenv("CLI_MODE")
        n = int(messages[-1].content)
        if mode == "block" or (mode == "process_loss" and n == 2):
            record("ready")
            try:
                await asyncio.Event().wait()
            finally:
                record("cancelled")
        if os.getenv("CLI_MODE") == "failure":
            raise RuntimeError("provider execution refused")
        if mode in ("process_loss", "fresh_run"):
            record(f"effect:{n}")
        print("extension diagnostic")
        return output_schema(n=int(messages[-1].content) + int(os.getenv("INCREMENT", "1")))
"""


@pytest.fixture
def application(tmp_path, monkeypatch):
    package = tmp_path / "local_cli_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(dedent(MODULE))
    raw = {
        "project": "local_cli_fixture",
        "name": "Root",
        "runtime": {
            "engine": "local",
            "provider": {"type": "custom", "class": "local_cli_fixture.models:Provider"},
            "registry": {"type": "inline", "prompts": {"echo": "{{n}}"}},
            "observability": {"type": "custom", "class": "local_cli_fixture.models:Backend"},
            "imports": {"allow_provider_class": True, "allow_observability_class": True},
        },
        "activities": {
            "definitions": [
                {"name": "echo", "input": "models:Item", "output": "models:Item", "prompt": "echo"}
            ]
        },
        "workflow": {
            "name": "Root",
            "input": "models:Item",
            "output": "models:Item",
            "steps": [{"id": "echo", "activity": "echo"}],
        },
    }
    (tmp_path / "workflow.yaml").write_text(yaml.safe_dump(raw))
    (tmp_path / "input.json").write_text('{"n":1}')
    monkeypatch.setenv("CLI_EVENT_LOG", str(tmp_path / "events"))
    monkeypatch.delenv("TYPEFLUX_EXPECTED_POLICY_HASH", raising=False)
    monkeypatch.delenv("INCREMENT", raising=False)
    yield tmp_path, raw
    for name in list(sys.modules):
        if name == "local_cli_fixture" or name.startswith("local_cli_fixture."):
            sys.modules.pop(name, None)


def events(root):
    path = root / "events"
    return path.read_text().splitlines() if path.exists() else []


def arguments(root):
    return [str(root / "workflow.yaml"), "--input", str(root / "input.json")]


def project_arguments(root, raw):
    child = json.loads(json.dumps(raw))
    child["name"] = child["workflow"]["name"] = "Child"
    # Local comes from the runtime profile; the environment overrides that profile.
    raw["runtime"].pop("engine")
    child["runtime"].pop("engine")
    raw["workflow"]["steps"] = [{"id": "child", "workflow": "child"}]
    (root / "workflow.yaml").write_text(yaml.safe_dump(raw))
    (root / "child.yaml").write_text(yaml.safe_dump(child))
    (root / "runtime.yaml").write_text(
        "name: foreground\nkind: runtime\nruntime: {engine: temporal}\n"
    )
    (root / "env.yaml").write_text(
        "name: local\nvariables: {INCREMENT: '4'}\noverrides: {runtime: {engine: local}}\n"
    )
    (root / "policy.yaml").write_text("name: base\nproviders: {allowed: {custom: {}}}\n")
    (root / "typeflux.project.yaml").write_text(
        dedent("""
        name: local-cli
        workflows:
          - {id: root, path: workflow.yaml, profiles: {runtime: foreground}}
          - {id: child, path: child.yaml, profiles: {runtime: foreground}}
        profiles: {runtime: {foreground: runtime.yaml}}
        environments: {local: env.yaml}
        policies: {base: policy.yaml}
        validation:
          targets:
            local: {workflows: [root], environment: local, policies: [base]}
    """)
    )
    return [
        "--project",
        str(root / "typeflux.project.yaml"),
        "--workflow",
        "root",
        "--environment",
        "local",
        "--input",
        str(root / "input.json"),
    ]


def test_local_cli_file_and_stdin_keep_json_output_clean(application, capsys, monkeypatch):
    root, _ = application
    original_path = list(sys.path)
    assert main(arguments(root)) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"n": 2}
    assert "extension diagnostic" in out.err
    assert sys.path == original_path
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"n":5}'))
    assert main([str(root / "workflow.yaml"), "--input", "-"]) == 0
    assert json.loads(capsys.readouterr().out) == {"n": 6}
    assert events(root).count("shutdown") == 2


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"n":"wrong"}'])
def test_local_cli_input_errors_are_usage_and_never_execute(application, capsys, payload):
    root, _ = application
    (root / "input.json").write_text(payload)
    assert main(arguments(root)) == 2
    assert capsys.readouterr().out == ""
    assert "called" not in events(root)


def test_local_cli_project_profiles_environment_policy_and_children(
    application, capsys, monkeypatch
):
    root, raw = application
    args = project_arguments(root, raw)
    expected = compose_project_policies(
        load_project_spec(root / "typeflux.project.yaml"), ("base",)
    ).policy_hash
    monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", " " + expected.upper() + " ")
    assert main([*args, "--expect-policy-hash", expected]) == 0
    assert json.loads(capsys.readouterr().out) == {"n": 5}
    assert main([*args, "--policy", "base"]) == 0
    assert json.loads(capsys.readouterr().out) == {"n": 5}
    assert os.getenv("INCREMENT") is None
    assert events(root) == ["constructed", "called", "shutdown"] * 2


@pytest.mark.parametrize(
    "failure",
    [
        "hash",
        "conflict",
        "environment_pin",
        "cleared_pin",
        "malformed",
        "standalone",
        "child_capability",
        "child_policy",
    ],
)
def test_local_cli_refusals_precede_service_construction(application, capsys, monkeypatch, failure):
    root, raw = application
    args = project_arguments(root, raw)
    code = 3
    if failure == "hash":
        args += ["--expect-policy-hash", "0" * 64]
    elif failure == "conflict":
        monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", "1" * 64)
        args += ["--expect-policy-hash", "0" * 64]
        code = 2
    elif failure == "malformed":
        args += ["--expect-policy-hash", "bad"]
        code = 2
    elif failure in {"environment_pin", "cleared_pin"}:
        monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", "0" * 64)
        selected = compose_project_policies(
            load_project_spec(root / "typeflux.project.yaml"), ("base",)
        ).policy_hash
        environment = yaml.safe_load((root / "env.yaml").read_text())
        environment["variables"]["TYPEFLUX_EXPECTED_POLICY_HASH"] = (
            selected if failure == "environment_pin" else ""
        )
        (root / "env.yaml").write_text(yaml.safe_dump(environment))
        if failure == "environment_pin":
            code = 2
    elif failure == "standalone":
        raw["runtime"]["engine"] = "local"
        (root / "workflow.yaml").write_text(yaml.safe_dump(raw))
        args = [*arguments(root), "--expect-policy-hash", "0" * 64]
    else:
        child = yaml.safe_load((root / "child.yaml").read_text())
        raw["workflow"]["steps"][0]["when"] = {"path": "input.n", "lt": 0}
        (root / "workflow.yaml").write_text(yaml.safe_dump(raw))
        if failure == "child_capability":
            child["workflow"]["risk_tier"] = "human_gated"
        else:
            child["runtime"]["provider"] = {"type": "openai"}
        (root / "child.yaml").write_text(yaml.safe_dump(child))
    assert main(args) == code
    captured = capsys.readouterr()
    assert captured.out == ""
    diagnostic = {
        "hash": "does not match",
        "conflict": "conflicting expected",
        "environment_pin": "conflicting expected",
        "cleared_pin": "does not match",
        "malformed": "64-character",
        "standalone": "requires --project",
        "child_capability": "human_gated",
        "child_policy": "openai",
    }[failure]
    assert diagnostic in captured.err
    assert events(root) == []


@pytest.mark.parametrize(
    "mode,code,expected_events",
    [
        ("failure", 1, ["constructed", "called", "shutdown"]),
        ("construction_failure", 3, ["constructed", "shutdown"]),
    ],
)
def test_local_cli_shuts_down_after_execution_and_preparation_failure(
    application, capsys, monkeypatch, mode, code, expected_events
):
    root, _ = application
    monkeypatch.setenv("CLI_MODE", mode)
    assert main(arguments(root)) == code
    captured = capsys.readouterr()
    assert captured.out == ""
    if mode == "failure":
        assert "workflow 'Root' step 'echo' failed: provider execution refused" in captured.err
    assert events(root) == expected_events


GUARDED_RUN = """
import importlib.abc, runpy, socket, sys
class NoTemporal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "temporalio" or fullname.startswith("temporalio."):
            raise ModuleNotFoundError("No module named 'temporalio'", name="temporalio")
sys.meta_path.insert(0, NoTemporal())
def no_network(*args, **kwargs):
    raise AssertionError("local CLI attempted a network connection")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
sys.argv = ["typeflux-local", *sys.argv[1:]]
try:
    runpy.run_module("typeflux.yaml.local_cli", run_name="__main__")
finally:
    assert not any(n == "temporalio" or n.startswith("temporalio.") for n in sys.modules)
"""


@pytest.mark.parametrize("project_mode", [False, True])
def test_local_cli_subprocess_reports_sdk_failure_context_and_shuts_down(application, project_mode):
    root, raw = application
    args = project_arguments(root, raw) if project_mode else arguments(root)
    completed = subprocess.run(
        [sys.executable, "-c", GUARDED_RUN, *args],
        cwd=root,
        env={**os.environ, "CLI_MODE": "failure"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 1, completed.stderr
    assert completed.stdout == ""
    workflow = "Child" if project_mode else "Root"
    assert (
        f"workflow {workflow!r} step 'echo' failed: provider execution refused" in completed.stderr
    )
    assert events(root) == ["constructed", "called", "shutdown"]


def test_local_cli_subprocess_executes_without_temporal_or_network(application):
    root, raw = application
    for project_mode in (False, True):
        args = project_arguments(root, raw) if project_mode else arguments(root)
        result = subprocess.run(
            [sys.executable, "-I", "-c", GUARDED_RUN, *args],
            cwd=root.parent,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"n": 5 if project_mode else 2}
        assert "extension diagnostic" in result.stderr


def test_local_cli_refuses_temporal_engine_before_services(application, capsys):
    root, raw = application
    raw["runtime"]["engine"] = "temporal"
    raw["task_queue"] = "existing-temporal"
    (root / "workflow.yaml").write_text(yaml.safe_dump(raw))
    assert main(arguments(root)) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and "runtime.engine" in captured.err
    assert events(root) == []


def test_local_cli_offline_example_file_and_project_without_services():
    root = Path(__file__).resolve().parents[1] / "examples/local_runtime"
    for args in [
        [str(root / "workflow.yaml")],
        [
            "--project",
            str(root / "typeflux.project.yaml"),
            "--workflow",
            "echo",
            "--environment",
            "offline",
        ],
    ]:
        result = subprocess.run(
            [sys.executable, "-I", "-c", GUARDED_RUN, *args, "--input", "-"],
            input='{"text":"hello"}',
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"text": "local result"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal exit statuses")
@pytest.mark.parametrize("signum,status", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_local_cli_signals_cancel_and_drain_before_shutdown(application, signum, status):
    root, _ = application
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", GUARDED_RUN, *arguments(root)],
        env={**os.environ, "CLI_MODE": "block"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while "ready" not in events(root):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process.send_signal(signum)
        out, err = process.communicate(timeout=15)
        assert process.returncode == status, err
        assert out == ""
        assert events(root) == ["constructed", "called", "ready", "cancelled", "shutdown"]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGKILL process-loss semantics")
def test_local_cli_process_loss_keeps_effects_and_fresh_run_repeats_from_start(application):
    root, raw = application
    raw["workflow"]["steps"] = [
        {"id": name, "activity": "echo"} for name in ("first", "second", "downstream")
    ]
    (root / "workflow.yaml").write_text(yaml.safe_dump(raw))
    command = [sys.executable, "-I", "-c", GUARDED_RUN, *arguments(root)]
    process = subprocess.Popen(
        command,
        env={**os.environ, "CLI_MODE": "process_loss"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while "ready" not in events(root):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # The second provider call proves the first step returned; its effect survives.
        process.kill()
        out, err = process.communicate(timeout=15)
        assert process.returncode == -signal.SIGKILL, err
        assert out == ""
        before_restart = ["constructed", "called", "effect:1", "called", "ready"]
        assert events(root) == before_restart  # No graceful cancellation/shutdown after SIGKILL.
        fresh = subprocess.run(
            command,
            env={**os.environ, "CLI_MODE": "fresh_run"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert fresh.returncode == 0, fresh.stderr
        assert json.loads(fresh.stdout) == {"n": 4}
        assert events(root) == before_restart + [
            "constructed",
            "called",
            "effect:1",
            "called",
            "effect:2",
            "called",
            "effect:3",
            "shutdown",
        ]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
