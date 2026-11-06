"""Study CLI effects and reported cost estimates; never retry an unknown paid outcome."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from tools.development.__main__ import stop
from tools.project_graph.records import Ledger
from tools.project_graph.repository import Repository, canonical

MODEL = "claude-fable-5"
STOP_NEW_CALLS_USD = 40.0  # Reserve $10 below the user's $50 ceiling for overruns.
PER_CALL_ESTIMATE_USD = 2.0  # Allows one bounded completion; shared $40 stop still applies.
PER_CALL_ALLOWANCE_USD = 3.0  # At least the observed 1.45x CLI-budget overrun.
MAX_OUTPUT_TOKENS = 8192


class UnknownPaidOutcome(RuntimeError):
    """A pending request without a verified result must be reconciled, never repeated."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def effects(ledger: Ledger) -> dict:
    return {
        record["payload"]["key"]: record["payload"]
        for record in ledger.read()
        if record["event"] == "effect" and record["payload"]["key"].startswith("study86-")
    }


def total_cost(ledger: Ledger) -> float:
    return math.fsum(
        effect["costUSD"] for effect in effects(ledger).values() if effect["state"] == "confirmed"
    )


def reported_result(path: Path) -> tuple[dict, float]:
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise UnknownPaidOutcome("missing or malformed paid response") from error
    if not isinstance(result, dict):
        raise UnknownPaidOutcome("paid response must be an object")
    cost = result.get("total_cost_usd")
    usage = result.get("modelUsage")
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (float, int))
        or not math.isfinite(cost)
        or cost < 0
        or not isinstance(usage, dict)
        or not usage
        or not all(isinstance(entry, dict) for entry in usage.values())
    ):
        raise UnknownPaidOutcome("missing or invalid reported cost/model usage")
    model_costs = [entry.get("costUSD") for entry in usage.values()]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or value < 0
        for value in model_costs
    ) or not math.isclose(math.fsum(model_costs), cost, rel_tol=1e-6, abs_tol=1e-6):
        raise UnknownPaidOutcome("auxiliary model costs do not reconcile with total")
    # Errors still consume the estimate budget. This is not a provider invoice.
    return result, float(cost)


def execute_cli(command, *, input, text, stdout, stderr, cwd, env, timeout, check):
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        text=text,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        process.communicate(input, timeout=timeout)
    except BaseException:
        # Reuse the maintained repeated-interrupt/owned-group cleanup; no retry.
        stop(process)
        raise
    result = subprocess.CompletedProcess(command, process.returncode)
    if check:
        result.check_returncode()
    return result


def verify_usage_canary(result: dict) -> None:
    """Run after known costs are recorded, before any subsequent invocation."""
    for usage in result["modelUsage"].values():
        for field in [
            "inputTokens",
            "outputTokens",
            "cacheReadInputTokens",
            "cacheCreationInputTokens",
        ]:
            if type(usage.get(field)) is not int or usage[field] < 0:
                raise RuntimeError(
                    "CLI usage schema differs from frozen analysis; cost recorded; stop new calls"
                )


def cli_command(cli: str, system_prompt: str) -> list[str]:
    """One command shared by paid execution and the no-request parser preflight."""
    return [
        cli,
        "-p",
        "--model",
        MODEL,
        "--effort",
        "low",
        "--tools",
        "",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-chrome",
        "--setting-sources",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--output-format",
        "json",
        "--system-prompt",
        system_prompt,
        "--max-budget-usd",
        str(PER_CALL_ESTIMATE_USD),
    ]


def _invoke_locked(
    *,
    root: Path,
    directory: Path,
    request_id: str,
    prompt: str,
    system_prompt: str,
    protocol_sha256: str,
    cli: str = "/opt/homebrew/bin/claude",
    execute: Callable = execute_cli,
    interrupt_after_response: Callable | None = None,
) -> dict:
    """Run at most once, or reconcile an already-written result without another API call.

    Callers verify the frozen protocol/source/environment before this function.
    A known paid error is recorded as billed, not retried or accepted as a task pass.
    Ordinary code-review invocations are intentionally outside this study ledger.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,100}", request_id):
        raise ValueError("invalid study request ID")
    directory.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(root, "issue-86")
    repo = Repository(root)
    ledger.resume(repo)
    key = "study86-" + request_id
    request = {
        "id": request_id,
        "promptSha256": digest(prompt.encode()),
        "systemSha256": digest(system_prompt.encode()),
        "protocolSha256": protocol_sha256,
        "model": MODEL,
        "outputTokens": MAX_OUTPUT_TOKENS,
        "effort": "low",
        "perCallEstimateUSD": PER_CALL_ESTIMATE_USD,
    }
    signature = digest(canonical(request))
    prior = effects(ledger)
    previous = prior.get(key)
    stdout = directory / (request_id + ".json")
    stderr = directory / (request_id + ".stderr")
    exit_path = directory / (request_id + ".exit.json")
    if previous:
        if previous.get("requestSha256") != signature:
            raise UnknownPaidOutcome("existing request identity differs; do not retry")
        if not stdout.is_file() or stdout.is_symlink():
            raise UnknownPaidOutcome("paid request has no reconcilable result")
        result, cost = reported_result(stdout)
        if previous["state"] == "confirmed":
            if previous["receipt"] != digest(stdout.read_bytes()):
                raise UnknownPaidOutcome("confirmed paid result bytes changed")
            if previous.get("clientExitReceipt") != (
                digest(exit_path.read_bytes()) if exit_path.is_file() else None
            ):
                raise UnknownPaidOutcome("confirmed client exit observation changed")
            verify_usage_canary(result)
            return {**result, "studyCliExitCode": previous.get("clientExitCode")}
        # The interrupted-after-response boundary is recoverable without a second call.
    else:
        if any(effect["state"] == "pending" for effect in prior.values()):
            raise UnknownPaidOutcome("another paid outcome is unknown; stop all new calls")
        for item in prior.values():
            if item["state"] == "confirmed":
                verify_usage_canary({"modelUsage": item["models"]})
        allowance = max(
            PER_CALL_ALLOWANCE_USD,
            max((item.get("costUSD", 0) for item in prior.values()), default=0),
        )
        if total_cost(ledger) + allowance > STOP_NEW_CALLS_USD:
            raise RuntimeError("study stop threshold reached; reserve remains below $50")
        if stdout.exists() or stderr.exists() or exit_path.exists():
            raise UnknownPaidOutcome("unrecorded request artifacts exist; reconcile first")
        empty = directory / ("cwd-" + signature[:24])
        if empty.exists():
            raise UnknownPaidOutcome("unrecorded request directory exists; reconcile first")
        empty.mkdir()
        ledger.append(
            "effect",
            {
                "key": key,
                "state": "pending",
                "receipt": signature,
                "requestSha256": signature,
                "request": request,
            },
            repo,
        )
        command = cli_command(cli, system_prompt)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME"}
        }
        environment.update(
            CLAUDE_CODE_MAX_RETRIES="0",
            CLAUDE_CODE_EFFORT_LEVEL="low",
            CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(MAX_OUTPUT_TOKENS),
            CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1",
        )
        # No source, held-out tests, Git objects, hooks, skills, MCP or execution tools
        # are available to the primary model; it receives only the explicit packet.
        try:
            with stdout.open("x") as output, stderr.open("x") as errors:
                completed = execute(
                    command,
                    input=prompt,
                    text=True,
                    stdout=output,
                    stderr=errors,
                    cwd=empty,
                    env=environment,
                    timeout=240,
                    check=False,
                )
            exit_path.write_text(json.dumps({"returncode": completed.returncode}) + "\n")
            if interrupt_after_response:
                interrupt_after_response()
            result, cost = reported_result(stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise UnknownPaidOutcome("paid outcome requires reconciliation; no retry") from error
    exit_code = None
    if exit_path.is_file() and not exit_path.is_symlink():
        observed_exit = json.loads(exit_path.read_text()).get("returncode")
        if type(observed_exit) is int:
            exit_code = observed_exit
    ledger.append(
        "effect",
        {
            "key": key,
            "state": "confirmed",
            "clientExitCode": exit_code,
            "clientExitReceipt": digest(exit_path.read_bytes()) if exit_path.is_file() else None,
            "receipt": digest(stdout.read_bytes()),
            "requestSha256": signature,
            "costUSD": cost,
            "costBasis": "Claude CLI estimate; provider invoice unavailable",
            "models": result["modelUsage"],
            "reportedError": result.get("is_error"),
            "reportedCliTurns": result.get("num_turns"),
            "observedOutputExceedsRequestedPerResponse": any(
                v.get("outputTokens", 0) > MAX_OUTPUT_TOKENS for v in result["modelUsage"].values()
            ),
        },
        repo,
    )
    if total_cost(ledger) > 50:
        raise RuntimeError("reported study cost exceeded authorized ceiling; stop")
    verify_usage_canary(result)
    return {**result, "studyCliExitCode": exit_code}


def invoke(**arguments) -> dict:
    """Serialize the budget check, pending effect and response across processes."""
    lock_path = arguments["root"] / ".project-graph/tasks/study86-model.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UnknownPaidOutcome(
                "another study invocation is in flight; no new call"
            ) from error
        try:
            return _invoke_locked(**arguments)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
