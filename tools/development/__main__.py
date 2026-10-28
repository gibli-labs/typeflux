"""Run reviewed commands and write revision-bound evidence; no shell from graph data."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from tools.project_graph.collection import collection_environment
from tools.project_graph.repository import GraphError, Repository

from .registry import CI_CHECKS, DEFAULT
from .results import playwright, vitest


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class Runner:
    def __init__(self, root: Path, output: Path):
        self.root, self.output = root, output
        self.tests = []
        self.commands = []
        self.env = {
            **collection_environment(),
            "PYTHONPATH": str(root),
            "TYPEFLUX_REPO_ROOT": str(root),
            "UV_FROZEN": "1",
            "PATH": str(root / ".project-graph/bin") + os.pathsep + os.environ["PATH"],
        }
        # Live credentials are scrubbed; only the isolated Temporal check opts in.
        self.env.pop("TYPEFLUX_LIVE_TEMPORAL", None)

    def command(self, *args, cwd=".", env=None, timeout=600):
        self.commands.append({"cwd": cwd, "argv": list(args)})
        print(f"[{cwd}] {' '.join(args)}", flush=True)
        with subprocess.Popen(
            args, cwd=self.root / cwd, env={**self.env, **(env or {})}, start_new_session=True
        ) as process:
            try:
                code = process.wait(timeout=timeout)
            except BaseException:
                stop(process)
                raise
        if code:
            raise GraphError(f"command failed ({code}): {' '.join(args)}")

    def pytest(self, *args, label="python", env=None):
        path = self.output / f"{label}.json"
        path.unlink(missing_ok=True)
        try:
            self.command(
                "uv",
                "run",
                "--no-sync",
                "pytest",
                "-q",
                "-p",
                "tools.development.pytest_results",
                *args,
                cwd="packages/python",
                env={"TYPEFLUX_TEST_REPORT": str(path), **(env or {})},
            )
        finally:
            if path.exists():
                self.tests.extend(json.loads(path.read_text())["tests"])

    def vitest(self, cwd, *args, env=None):
        path = self.output / (Path(cwd).name + ".json")
        path.unlink(missing_ok=True)
        try:
            self.command(
                "npm",
                "test",
                "--",
                *args,
                "--reporter=default",
                "--reporter=json",
                f"--outputFile.json={path}",
                cwd=cwd,
                env=env,
            )
        finally:
            if path.exists():
                self.tests.extend(vitest(json.loads(path.read_text()), self.root, self.root / cwd))

    def execute(self, check):
        if check == "quality":
            for command in (
                ("uv", "lock", "--check"),
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "ruff",
                    "check",
                    "--config",
                    "pyproject.toml",
                    ".",
                    "../../scripts",
                    "../../tools",
                ),
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "ruff",
                    "format",
                    "--check",
                    "--config",
                    "pyproject.toml",
                    ".",
                    "../../scripts",
                    "../../tools",
                ),
                ("uv", "run", "--no-sync", "mypy", "src/typeflux"),
                ("uv", "run", "--no-sync", "python", "-m", "typeflux.controlplane", "conformance"),
                ("uv", "run", "--no-sync", "python", "-m", "build"),
            ):
                self.command(*command, cwd="packages/python")
            self.command(sys.executable, "scripts/verify_python_planning_install.py")
        elif check == "python":
            self.pytest("-m", "not live")
        elif check == "typescript":
            self.command("pnpm", "-r", "build")
            self.command("pnpm", "-r", "typecheck")
            self.command("pnpm", "--filter", "@typeflux/temporal-controlplane", "generate:contract")
            self.command(
                "git",
                "diff",
                "--exit-code",
                "--",
                "packages/typescript/temporal-controlplane/src/http/contract.ts",
            )
            for path in sorted((self.root / "packages/typescript").glob("*/package.json")):
                self.vitest(str(path.parent.relative_to(self.root)))
            self.command("node", "scripts/verify-yaml-planning-pack.mjs")
        elif check == "web":
            for command in ("generate", "typecheck", "build"):
                self.command("npm", "run", command, cwd="clients/typescript")
            self.command("git", "diff", "--exit-code", "--", "clients/typescript/src/schema.ts")
            self.command(
                "node",
                "../../scripts/assert_client_contract_lockstep.mjs",
                cwd="clients/typescript",
            )
            self.command("npm", "run", "typecheck", cwd="clients/console")
            self.vitest("clients/console")
            self.command("npm", "run", "build", cwd="clients/console")
        elif check == "mcp":
            self.vitest("clients/mcp")
            self.command("npm", "run", "build", cwd="clients/mcp")
            self.command("npm", "run", "verify:pack", cwd="clients/mcp")
        elif check.startswith("conformance-"):
            edition = "python" if check.endswith("python") else "typescript"
            flags = () if edition == "python" else ("--edition", "ts-cp")
            self.command(
                sys.executable,
                "runner.py",
                "--server-cmd",
                f"./serve-{edition}.sh {{profile}} {{port}}",
                "--port",
                "8411",
                *flags,
                cwd="contracts/controlplane/conformance",
            )
        elif check.startswith("browser-"):
            path = self.output / "playwright.json"
            path.unlink(missing_ok=True)
            try:
                self.command(
                    "npm",
                    "run",
                    "test:e2e",
                    cwd="clients/console",
                    env={
                        "CI": "true",
                        "E2E_SERVER": "python-cp" if check.endswith("python") else "ts-cp",
                        "PLAYWRIGHT_JSON_OUTPUT_FILE": str(path),
                    },
                )
            finally:
                if path.exists():
                    self.tests.extend(
                        playwright(
                            json.loads(path.read_text()), self.root, self.root / "clients/console"
                        )
                    )
        elif check == "temporal":
            self.temporal()
        elif check == "graph":
            for command in ("validate", "collect"):
                self.command(
                    sys.executable,
                    "-m",
                    "tools.project_graph",
                    "--output",
                    str(self.output / f"{command}.json"),
                    command,
                )
        else:
            raise GraphError(f"unknown check: {check}")

    def temporal(self):
        # Never attach tests to an existing developer/production server.
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 7233)) == 0:
                raise GraphError(
                    "port 7233 is occupied; stop that server before isolated validation"
                )
        version = subprocess.check_output(
            [str(self.root / ".project-graph/bin/temporal"), "--version"], text=True
        )
        if not version.startswith("temporal version 1.7.0 "):
            raise GraphError("local Temporal verification requires CLI 1.7.0")
        with (self.output / "temporal.log").open("w") as log:
            process = subprocess.Popen(
                [
                    str(self.root / ".project-graph/bin/temporal"),
                    "server",
                    "start-dev",
                    "--ip",
                    "127.0.0.1",
                    "--port",
                    "7233",
                    "--headless",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=self.env,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 45
                while True:
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", 7233)) == 0:
                            break
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise GraphError(
                            "isolated Temporal server failed to start; inspect temporal.log"
                        )
                    time.sleep(0.25)
                env = {"TYPEFLUX_LIVE_TEMPORAL": "1"}
                self.pytest("tests/test_live_ts_binding.py", label="temporal-python", env=env)
                self.vitest(
                    "packages/typescript/temporal-controlplane",
                    "test/live-operate.test.ts",
                    env=env,
                )
            finally:
                stop(process)


def run(root: Path, check: str, check_id: str | None = None) -> dict:
    repo = Repository(root)
    check_id = check_id or (
        f"python-{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}"
        if check == "python"
        else check
    )
    if check_id not in CI_CHECKS or CI_CHECKS[check_id][1] != check:
        raise GraphError("check ID does not match its fixed verifier")
    output = root / ".project-graph/evidence" / check_id
    output.mkdir(parents=True, exist_ok=True)
    receipt = output / "receipt.json"
    receipt.unlink(missing_ok=True)
    runner = Runner(root, output)
    status, error = "passed", None
    started = time.monotonic()
    try:
        runner.execute(check)
        if (
            check
            in {
                "python",
                "typescript",
                "web",
                "mcp",
                "browser-python",
                "browser-typescript",
                "temporal",
            }
            and not runner.tests
        ):
            raise GraphError("test runner produced no test identities; evidence is incomplete")
        if Repository(root).tree_digest != repo.tree_digest:
            raise GraphError("verification changed source inputs; evidence is invalid")
    except (
        GraphError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        status, error = "failed", str(exc)
    record = {
        "kind": "development-verification",
        "version": 1,
        "checkId": check_id,
        "verifier": check,
        "status": status,
        "error": error,
        "sourceRevision": repo.revision,
        "treeDigest": repo.tree_digest,
        "dirty": repo.dirty,
        "runId": os.environ.get("GITHUB_RUN_ID"),
        "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "commands": runner.commands,
        "tests": runner.tests,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "durationSeconds": round(time.monotonic() - started, 3),
        "authority": "execution record; CI authenticity requires trusted run validation",
    }
    receipt.write_text(json.dumps(record, indent=2) + "\n")
    print(f"{check_id}: {status} ({receipt})", flush=True)
    if error:
        print(error, file=sys.stderr)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    setup = sub.add_parser("sync")
    setup.add_argument(
        "scope",
        choices=("python", "typescript", "web", "mcp", "graph", "browser", "temporal", "all"),
        default="all",
        nargs="?",
    )
    execute = sub.add_parser("run")
    execute.add_argument("checks", nargs="+", choices=(*DEFAULT, "default"))
    execute.add_argument("--check-id")
    args = parser.parse_args(argv)
    if args.command == "sync":
        from .setup import sync

        sync(Path(__file__).resolve().parents[2], args.scope)
        return 0
    if args.command == "list":
        print(json.dumps(CI_CHECKS, indent=2))
        return 0
    selected = list(
        dict.fromkeys(c for v in args.checks for c in (DEFAULT if v == "default" else (v,)))
    )
    if args.check_id and len(selected) != 1:
        parser.error("--check-id requires one verifier")
    root = Path(__file__).resolve().parents[2]
    for check in selected:
        if run(root, check, args.check_id)["status"] != "passed":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
