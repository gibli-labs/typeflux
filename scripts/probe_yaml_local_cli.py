"""Exercise the installed console command with an offline application and no network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = """
import importlib.abc
import os
import socket
import sys
from pathlib import Path

class NoTemporal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "temporalio" or fullname.startswith("temporalio."):
            raise ModuleNotFoundError("No module named 'temporalio'", name="temporalio")

sys.meta_path.insert(0, NoTemporal())
def no_network(*args, **kwargs):
    raise AssertionError("offline CLI attempted a network connection")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
Path(os.environ["TYPEFLUX_CLI_GUARD_MARKER"]).touch()
"""


def main() -> None:
    executable, example = map(Path, sys.argv[1:])
    assert executable.is_file()
    with tempfile.TemporaryDirectory(prefix="typeflux-local-cli-probe-") as directory:
        root = Path(directory)
        application = root / "application"
        shutil.copytree(example, application)
        guard = root / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(GUARD)
        env = {
            **{k: v for k, v in os.environ.items() if k != "TYPEFLUX_EXPECTED_POLICY_HASH"},
            "PYTHONPATH": str(guard),
            "PYTHONNOUSERSITE": "1",
            "TYPEFLUX_CLI_GUARD_MARKER": str(root / "guard-active"),
        }
        commands = [
            [str(application / "workflow.yaml"), "--input", str(application / "input.json")],
            [str(application / "workflow.yaml"), "--input", "-"],
            [
                "--project",
                str(application / "typeflux.project.yaml"),
                "--workflow",
                "echo",
                "--environment",
                "offline",
                "--input",
                "-",
            ],
        ]
        for args in commands:
            (root / "guard-active").unlink(missing_ok=True)
            result = subprocess.run(
                [str(executable), *args],
                cwd=root,
                env=env,
                input='{"text":"hello"}',
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert (root / "guard-active").is_file(), "network/import guard did not load"
            assert json.loads(result.stdout) == {"text": "local result"}, result.stdout
    print(
        "Installed Python local CLI executes files, stdin and projects without Temporal or network"
    )


if __name__ == "__main__":
    main()
