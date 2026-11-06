"""Exercise unknown-outcome and budget guards without any paid API requests."""

import json
import subprocess
from pathlib import Path

import pytest
from tools.development.study86.billing import UnknownPaidOutcome, invoke, total_cost
from tools.project_graph.records import Ledger
from tools.project_graph.repository import Repository


@pytest.fixture
def study(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Study fixture"),
        ("config", "user.email", "study@example.invalid"),
        ("config", "core.hooksPath", "/dev/null"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "source.txt").write_text("fixed study source")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    ledger = Ledger(root, "issue-86")
    ledger.append(
        "create",
        {"intent": "Bounded study", "acceptance": ["account every cost"]},
        Repository(root),
    )
    return {
        "root": root,
        "directory": tmp_path / "responses",
        "request_id": "trial",
        "prompt": "fixed packet",
        "system_prompt": "fixed rules",
        "protocol_sha256": "a" * 64,
    }


def response(cost=0.15, **fields):
    return {
        "total_cost_usd": cost,
        "modelUsage": {
            "claude-fable-5": {
                "costUSD": cost - 0.05,
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
            "auxiliary-model": {
                "costUSD": 0.05,
                "inputTokens": 2,
                "outputTokens": 1,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
        },
        "result": "patch",
        "is_error": False,
        "studyCliExitCode": 0,
        **fields,
    }


def cli(result, calls):
    def execute(command, **options):
        calls.append((command, options))
        options["stdout"].write(json.dumps(result))
        return subprocess.CompletedProcess(command, 0)

    return execute


def test_auxiliary_costs_and_repeated_read_are_accounted_once(study):
    calls = []
    invoke(**study, execute=cli(response(), calls))
    invoke(**study, execute=cli(response(), calls))
    assert len(calls) == 1
    assert total_cost(Ledger(study["root"], "issue-86")) == pytest.approx(0.15)
    command, options = calls[0]
    assert command[command.index("--tools") + 1] == ""
    assert options["env"]["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert options["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192"
    assert list(Path(options["cwd"]).iterdir()) == []


def test_response_then_interrupt_reconciles_without_resubmission(study):
    calls = []

    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        invoke(**study, execute=cli(response(), calls), interrupt_after_response=interrupt)
    assert total_cost(Ledger(study["root"], "issue-86")) == 0
    invoke(**study, execute=cli(response(), calls))
    assert len(calls) == 1
    assert total_cost(Ledger(study["root"], "issue-86")) == pytest.approx(0.15)


def test_unknown_result_blocks_same_and_other_requests(study):
    calls = []

    def unknown(command, **options):
        calls.append(command)
        options["stdout"].write("")
        raise subprocess.TimeoutExpired(command, 240)

    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=unknown)
    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=unknown)
    with pytest.raises(UnknownPaidOutcome, match="another paid outcome"):
        invoke(**{**study, "request_id": "other"}, execute=unknown)
    assert len(calls) == 1


def test_changed_prompt_or_response_is_not_reused(study):
    calls = []
    invoke(**study, execute=cli(response(), calls))
    with pytest.raises(UnknownPaidOutcome, match="identity differs"):
        invoke(**{**study, "prompt": "changed packet"}, execute=cli(response(), calls))
    (study["directory"] / "trial.json").write_text(json.dumps(response(0.2)))
    with pytest.raises(UnknownPaidOutcome, match="bytes changed"):
        invoke(**study, execute=cli(response(), calls))
    assert len(calls) == 1


def test_reported_api_error_is_billed_once_without_retry(study):
    calls = []
    result = response(is_error=True)
    assert invoke(**study, execute=cli(result, calls))["is_error"]
    invoke(**study, execute=cli(result, calls))
    assert len(calls) == 1
    assert total_cost(Ledger(study["root"], "issue-86")) == pytest.approx(0.15)


def test_stop_threshold_keeps_headroom_and_counts_failed_calls(study):
    calls = []
    invoke(**study, execute=cli(response(39.8, is_error=True), calls))
    with pytest.raises(RuntimeError, match="stop threshold"):
        invoke(**{**study, "request_id": "other"}, execute=cli(response(), calls))
    assert len(calls) == 1


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1, True])
def test_invalid_cost_never_becomes_a_completed_effect(study, cost):
    calls = []
    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=cli(response(cost), calls))
    assert total_cost(Ledger(study["root"], "issue-86")) == 0
    assert len(calls) == 1


def test_auxiliary_cost_mismatch_remains_unknown(study):
    calls = []
    result = response()
    result["modelUsage"]["auxiliary-model"]["costUSD"] = 1
    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=cli(result, calls))
    assert total_cost(Ledger(study["root"], "issue-86")) == 0


@pytest.mark.parametrize("body", ["not-json", "[]", "null"])
def test_malformed_response_stays_unknown_and_cannot_be_reissued(study, body):
    calls = []

    def malformed(command, **options):
        calls.append(command)
        options["stdout"].write(body)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=malformed)
    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=malformed)
    assert len(calls) == 1
    assert total_cost(Ledger(study["root"], "issue-86")) == 0


@pytest.mark.parametrize("usage", [[], {}, {"claude-fable-5": None}])
def test_invalid_usage_cannot_hide_unknown_spending(study, usage):
    calls = []
    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=cli(response(modelUsage=usage), calls))
    with pytest.raises(UnknownPaidOutcome):
        invoke(**study, execute=cli(response(), calls))
    assert len(calls) == 1
    assert total_cost(Ledger(study["root"], "issue-86")) == 0


def test_cli_executor_preserves_input_output_and_nonzero_exit(tmp_path):
    import os
    import sys

    from tools.development.study86.billing import execute_cli

    with (tmp_path / "stdout").open("w") as out, (tmp_path / "stderr").open("w") as err:
        result = execute_cli(
            [sys.executable, "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
            input="controlled input",
            text=True,
            stdout=out,
            stderr=err,
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=10,
            check=False,
        )
    assert result.returncode == 7
    assert (tmp_path / "stdout").read_text() == "controlled input\n"


def test_cli_timeout_reaps_only_its_owned_client(tmp_path, monkeypatch):
    import os
    import sys

    from tools.development.study86 import billing

    original = subprocess.Popen
    created = []

    def process(*args, **kwargs):
        child = original(*args, **kwargs)
        created.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", process)
    with (tmp_path / "stdout").open("w") as out, (tmp_path / "stderr").open("w") as err:
        with pytest.raises(subprocess.TimeoutExpired):
            billing.execute_cli(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                input="",
                text=True,
                stdout=out,
                stderr=err,
                cwd=tmp_path,
                env=os.environ.copy(),
                timeout=0.2,
                check=False,
            )
    assert len(created) == 1
    assert created[0].poll() is not None


def test_concurrent_controller_cannot_start_another_request(study):
    import fcntl

    calls = []
    path = study["root"] / ".project-graph/tasks/study86-model.lock"
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(UnknownPaidOutcome, match="in flight"):
            invoke(**study, execute=cli(response(), calls))
    assert calls == []
    invoke(**study, execute=cli(response(), calls))
    assert len(calls) == 1


def test_nonzero_client_exit_stays_failed_across_saved_response_replay(study):
    calls = []

    def failed(command, **options):
        cli(response(), calls)(command, **options)
        return subprocess.CompletedProcess(command, 7)

    assert invoke(**study, execute=failed)["studyCliExitCode"] == 7
    assert invoke(**study, execute=failed)["studyCliExitCode"] == 7
    assert len(calls) == 1
    assert total_cost(Ledger(study["root"], "issue-86")) == pytest.approx(0.15)


def test_usage_canary_accounts_known_cost_before_stopping(study):
    calls = []
    malformed = response()
    del malformed["modelUsage"]["claude-fable-5"]["inputTokens"]
    with pytest.raises(RuntimeError, match="usage schema"):
        invoke(**study, execute=cli(malformed, calls))
    assert total_cost(Ledger(study["root"], "issue-86")) == pytest.approx(0.15)
    with pytest.raises(RuntimeError, match="usage schema"):
        invoke(**study, execute=cli(malformed, calls))
    assert len(calls) == 1
