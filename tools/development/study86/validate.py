"""Apply scoped text patches and execute held-out checks in an owned offline container."""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import yaml
from tools.development.deployed_review.run import Target, write

from .context import git, git_environment


class RejectedPatch(ValueError):
    pass


def workflow_preserved(before: str, after: str) -> bool:
    """Only add the requested diagnostics upload; existing CI policy stays identical."""
    try:
        original = yaml.load(before, Loader=yaml.BaseLoader)
        candidate = yaml.load(after, Loader=yaml.BaseLoader)
        projected = deepcopy(candidate)
        steps = projected["jobs"]["accept"]["steps"]
        diagnostic = [
            step
            for step in steps
            if step.get("with", {}).get("path") == ".project-graph/accepted/job-observations/"
        ]
        if len(diagnostic) != 1:
            return False
        step = diagnostic[0]
        if set(step) - {"name", "if", "uses", "with"}:
            return False
        runs = [s for s in original["jobs"]["accept"]["steps"] if "run" in s]
        if not runs or steps.index(step) <= steps.index(runs[-1]):
            return False
        pins = {
            s.get("uses")
            for s in original["jobs"]["accept"]["steps"]
            if s.get("uses", "").startswith("actions/upload-artifact@")
        }
        if step.get("uses") not in pins or step.get("if") != "always()":
            return False
        steps.remove(step)
        return projected == original
    except (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return False


def valid_native(report: object, expected: dict) -> bool:
    if not isinstance(report, dict) or report.get("passed") is not True or not expected:
        return False
    checks = report.get("checks")
    if not isinstance(checks, list) or len(checks) != len(expected):
        return False
    if not all(isinstance(check, dict) and isinstance(check.get("kind"), str) for check in checks):
        return False
    if {check.get("kind") for check in checks} != set(expected):
        return False
    for check in checks:
        kind = check["kind"]
        if (
            check.get("passed") is not True
            or type(check.get("exitCode")) is not int
            or check["exitCode"] != 0
        ):
            return False
        if kind == "historical-typescript-dependency-build":
            if expected[kind] != 0:
                return False
        elif kind == "query":
            tests = check.get("tests")
            if (
                not isinstance(tests, list)
                or len(tests) != expected[kind]
                or not all(isinstance(t, dict) and t.get("status") == "passed" for t in tests)
            ):
                return False
        elif (
            type(check.get("cases")) is not int
            or check["cases"] != expected[kind]
            or expected[kind] <= 0
            or check.get("failed") != 0
            or check.get("skipped") != 0
            or check.get("errors", 0) != 0
        ):
            return False
    return True


def apply_patch(source: Path, patch: str, editable: list[str]) -> list[str]:
    if not patch.strip():
        raise RejectedPatch("empty patch")
    if any(line.startswith(("GIT binary patch", "rename from ")) for line in patch.splitlines()):
        raise RejectedPatch("only scoped text modifications are accepted")
    result = subprocess.run(
        ["git", "-C", str(source), "apply", "--numstat", "-z", "-"],
        input=patch.encode(),
        capture_output=True,
        env=git_environment(),
    )
    if result.returncode:
        raise RejectedPatch("invalid unified diff")
    paths = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        pieces = entry.split(b"\t", 2)
        if len(pieces) != 3 or pieces[0] == b"-" or pieces[1] == b"-":
            raise RejectedPatch("non-text or ambiguous patch path")
        path = pieces[2].decode()
        if path not in editable or path.startswith("/") or ".." in Path(path).parts:
            raise RejectedPatch("patch exceeds assigned edit scope")
        paths.append(path)
    if not paths:
        raise RejectedPatch("patch changes no source files")
    workflow = ".github/workflows/ci-acceptance.yml"
    original_workflow = (source / workflow).read_text() if workflow in paths else None
    check = subprocess.run(
        ["git", "-C", str(source), "apply", "--check", "-"],
        input=patch.encode(),
        capture_output=True,
        env=git_environment(),
    )
    if check.returncode:
        raise RejectedPatch("patch does not apply to its starting source")
    subprocess.run(
        ["git", "-C", str(source), "apply", "-"],
        input=patch.encode(),
        capture_output=True,
        env=git_environment(),
        check=True,
    )
    for path in paths:
        file = source / path
        if not file.exists() and not file.is_symlink():
            raise RejectedPatch("source deletions are outside this pilot scope")
        if file.exists() or file.is_symlink():
            if not stat.S_ISREG(file.lstat().st_mode):
                raise RejectedPatch("modified source must be a regular file")
    if original_workflow is not None and not workflow_preserved(
        original_workflow, (source / workflow).read_text()
    ):
        raise RejectedPatch(
            "existing CI jobs, steps, triggers, permissions and action pins must remain unchanged"
        )
    return sorted(set(paths))


def overlay_heldout(root: Path, source: Path, task: dict) -> list[dict]:
    import hashlib

    records = []
    for path in task["heldout"]:
        if path.endswith((".py", ".ts")):
            if path in task["editable"]:
                raise ValueError("held-out file would overwrite a permitted implementation change")
            data = git(root, "show", task["referenceRevision"] + ":" + path)
            file = source / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            records.append({"path": path, "sha256": hashlib.sha256(data).hexdigest()})
    return records


def link_environment(source: Path) -> None:
    path = source / "node_modules"
    path.symlink_to("/tmp/study-node-modules/root")
    for package in sorted((source / "packages/typescript").glob("*/package.json")):
        (package.parent / "node_modules").symlink_to(
            Path("/tmp/study-node-modules") / package.parent.name
        )
    python = source / "packages/python/.venv"
    if python.parent.exists():
        python.symlink_to("/environment/repository/packages/python/.venv")
    # These are fixed environment links, including when their container-only
    # targets do not exist on the host. Do not let them become source inputs.
    excluded = ["/node_modules", "/packages/python/.venv", "/packages/typescript/*/node_modules"]
    (source / ".git/info/exclude").write_text("\n".join(excluded) + "\n")


def execute_native(
    source: Path, output: Path, image: str, task: dict, *, context: str = "desktop-linux"
) -> dict:
    """Reuse the already-tested Docker ownership helper, never host-execute model code.

    Target's existing label namespace is shared; every study attempt has its own
    owner/name/ID and ledger. It never reuses a deployment resource or report.
    """
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("native validation requires an immutable image ID")
    output.mkdir()
    reports = output / "native"
    reports.mkdir(mode=0o777)
    reports.chmod(0o777)
    target = Target(output / "resources", context)
    result = {
        "passed": False,
        "image": image,
        "authority": "isolated candidate test output",
        "limits": "component held-outs, not full mandatory CI or independent attestation",
    }
    write(output / "pending.json", {"image": image, "task": task["id"], "source": str(source)})
    try:
        container = target.create(
            "container",
            "study-" + task["id"],
            "--user",
            "10001:10001",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=512m,mode=1777",
            "--mount",
            f"type=bind,src={source.resolve()},dst=/work,readonly",
            "--mount",
            f"type=bind,src={reports.resolve()},dst=/out",
            "--mount",
            f"type=bind,src={Path(__file__).parent.resolve()},dst=/validators,readonly",
            "--env",
            "PYTHONPATH=/work:/work/packages/python/src",
            "--env",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--env",
            "GIT_CONFIG_NOSYSTEM=1",
            "--env",
            "GIT_CONFIG_GLOBAL=/dev/null",
            "--env",
            "GIT_CONFIG_COUNT=1",
            "--env",
            "GIT_CONFIG_KEY_0=safe.directory",
            "--env",
            "GIT_CONFIG_VALUE_0=/work",
            "--env",
            "GIT_OPTIONAL_LOCKS=0",
            "--env",
            "XDG_DATA_HOME=/tmp/study-data",
            "--env",
            "XDG_CACHE_HOME=/tmp/study-cache",
            image,
            "python",
            "/validators/inside.py",
            json.dumps(task),
        )
        before_start = target.inspect(container)
        limits = before_start["HostConfig"]
        result["observedResources"] = {
            key: limits.get(key)
            for key in ["NanoCpus", "Memory", "PidsLimit", "ReadonlyRootfs", "NetworkMode", "Tmpfs"]
        }
        result["observedUser"] = before_start["Config"]["User"]
        if (
            limits.get("NanoCpus") != 2_000_000_000
            or limits.get("Memory") != 2_147_483_648
            or limits.get("PidsLimit") != 256
            or limits.get("ReadonlyRootfs") is not True
            or limits.get("NetworkMode") != "none"
            or result["observedUser"] != "10001:10001"
        ):
            raise RuntimeError("actual Docker restrictions differ from the frozen protocol")
        if limits.get("Tmpfs", {}).get("/tmp") != "rw,size=512m,mode=1777":
            raise RuntimeError("actual Docker tmpfs differs from frozen protocol")
        target.start(container)
        timed_out = False
        try:
            code = target.docker("container", "wait", container["id"], timeout=180)
        except subprocess.TimeoutExpired:
            # Confirm the daemon still answers; an unavailable daemon is infrastructure.
            target.inspect(container)
            timed_out = True
            code = None
            result["candidateTimedOut"] = True
            result["failureClass"] = "implementation-timeout"

        info = target.inspect(container)
        result["containerExitCode"] = info["State"]["ExitCode"]
        result["oomKilled"] = info["State"].get("OOMKilled", False)
        if (reports / "results.json").is_file():
            result["checks"] = json.loads((reports / "results.json").read_text())
        result["passed"] = (
            not timed_out
            and code == "0"
            and not result["oomKilled"]
            and valid_native(result.get("checks"), task.get("expectedChecks", {}))
        )
    finally:
        result["cleanupFailures"] = target.cleanup()
        if result["cleanupFailures"]:
            result["passed"] = False
        write(output / "result.json", result)
        if result["cleanupFailures"] and sys.exc_info()[0] is None:
            raise RuntimeError("native owned cleanup incomplete; stop new calls")
    return result
