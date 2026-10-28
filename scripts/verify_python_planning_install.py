"""Install the candidate base wheel and exercise planning with Temporal absent."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheels = list((root / "packages/python/dist").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("build exactly one candidate wheel before the installation probe")
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}}
    with tempfile.TemporaryDirectory(prefix="typeflux-planning-install-") as directory:
        venv = Path(directory) / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], env=env, check=True)
        python = venv / "bin/python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheels[0])], env=env, check=True
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import importlib.util; assert importlib.util.find_spec('temporalio') is None; assert importlib.util.find_spec('cryptography') is None",
            ],
            env=env,
            check=True,
        )
        subprocess.run(
            [str(python), "-I", str(root / "scripts/probe_yaml_planning.py")], env=env, check=True
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), f"{wheels[0]}[temporal]"],
            env=env,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from temporalio.client import Client; from temporalio.worker import Worker; from typeflux.yaml.subject_keystore import SubjectKeystore",
            ],
            env=env,
            check=True,
        )
        print("Python Temporal extra installed successfully")


if __name__ == "__main__":
    main()
