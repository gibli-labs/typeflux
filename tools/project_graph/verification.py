"""Fixed local verifier commands. Catalog descriptions never become shell text."""

from __future__ import annotations

import hashlib
import os
import platform
import signal
import subprocess
from pathlib import Path

from tools.development.registry import DEFAULT

from .collection import collection_environment
from .records import Ledger
from .repository import GraphError, Repository

RUNNERS = {
    name: (
        ".",
        (
            "uv",
            "run",
            "--project",
            "packages/python",
            "python",
            "-m",
            "tools.development",
            "run",
            name,
        ),
    )
    for name in DEFAULT
}


def verify(root: Path, task: str, verifier: str, timeout=600) -> dict:
    if verifier not in RUNNERS:
        raise GraphError(f"unknown verifier; choose from {sorted(RUNNERS)}")
    repo = Repository(root)
    cwd, command = RUNNERS[verifier]
    ledger = Ledger(root, task)
    ledger.resume(repo)  # Refuse to run if there is no valid journal.
    with subprocess.Popen(
        command,
        cwd=root / cwd,
        env=collection_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            result = "passed" if process.returncode == 0 else "failed"
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            # A test runner may spawn servers/workers; stop its whole process group.
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            result, exit_code = "timeout", None
        output = stdout + stderr
    if Repository(root).tree_digest != repo.tree_digest:
        raise GraphError("verifier changed source inputs; results cannot be reused")
    # Persist a digest rather than potentially sensitive raw command output.
    payload = {
        "verifier": verifier,
        "command": list(command),
        "result": result,
        "exitCode": exit_code,
        "outputDigest": hashlib.sha256(output).hexdigest(),
        "platform": platform.platform(),
        "authority": "local-execution",
        "meaning": "command exit status; not proof of complete behavioral coverage",
    }
    return ledger.append("verification", payload, repo)
