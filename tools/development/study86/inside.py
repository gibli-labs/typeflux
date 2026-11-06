"""Fixed native validation entrypoint. This module runs only inside the study container."""

import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def prepare_typescript():
    """Compile dependencies from this historical candidate, never a future SDK build."""
    source = Path("/work/packages/typescript")
    workspace = Path("/tmp/study-sdk")
    shutil.copytree(source, workspace, ignore=shutil.ignore_patterns("node_modules", "dist"))
    packages = sorted(source.glob("*/package.json"))
    for label, image_modules in [
        ("root", Path("/environment/repository/node_modules")),
        *[
            (
                p.parent.name,
                Path("/environment/repository/packages/typescript")
                / p.parent.name
                / "node_modules",
            )
            for p in packages
        ],
    ]:
        directory = Path("/tmp/study-node-modules") / label
        directory.mkdir(parents=True)
        for dependency in image_modules.iterdir():
            if dependency.name != "@typeflux":
                (directory / dependency.name).symlink_to(dependency)
        local = directory / "@typeflux"
        local.mkdir()
        for package in packages:
            name = json.loads(package.read_text())["name"].split("/", 1)[1]
            (local / name).symlink_to(workspace / package.parent.name)
        if label != "root":
            (workspace / label / "node_modules").symlink_to(directory)
    result = subprocess.run(
        ["npm", "run", "--prefix", str(workspace / "temporal"), "build"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    Path("/out/dependency-build.log").write_text(result.stdout + "\n" + result.stderr)
    return result.returncode


def main():
    task = json.loads(sys.argv[1])
    checks = []
    python = [p for p in task["heldout"] if p.endswith(".py")]
    if "whitespace-registry-control" in task["heldout"]:
        python.append("/validators/whitespace_control.py")
    if python:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            "/dev/null",
            "-p",
            "no:cacheprovider",
            "-p",
            "pytest_asyncio.plugin",
            "-o",
            "asyncio_mode=auto",
            *python,
            "--junitxml=/out/python.xml",
        ]
        if task.get("pytestSelection"):
            command += ["-k", task["pytestSelection"]]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=150)
        Path("/out/python.log").write_text(result.stdout + "\n" + result.stderr)
        cases = (
            ET.parse("/out/python.xml").findall(".//testcase")
            if Path("/out/python.xml").exists()
            else []
        )
        checks.append(
            {
                "kind": "python",
                "exitCode": result.returncode,
                "cases": len(cases),
                "failed": sum(c.find("failure") is not None for c in cases),
                "errors": sum(c.find("error") is not None for c in cases),
                "skipped": sum(c.find("skipped") is not None for c in cases),
                "passed": result.returncode == 0
                and bool(cases)
                and all(c.find("skipped") is None for c in cases),
            }
        )
    groups = {}
    for path in task["heldout"]:
        if path.endswith(".ts"):
            package = path.split("/test/")[0]
            groups.setdefault(package, []).append(path.removeprefix(package + "/"))
    if groups:
        built = prepare_typescript()
        checks.append(
            {
                "kind": "historical-typescript-dependency-build",
                "exitCode": built,
                "passed": built == 0,
            }
        )
    for package, tests in groups.items():
        label = package.rsplit("/", 1)[-1]
        result = subprocess.run(
            ["node", "/validators/vitest.mjs", package, *tests],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        Path("/out/" + label + ".log").write_text(result.stdout + "\n" + result.stderr)
        report = Path("/out/" + label + ".json")
        data = json.loads(report.read_text()) if report.exists() else {}
        checks.append(
            {
                "kind": label,
                "exitCode": result.returncode,
                "cases": data.get("numTotalTests", 0),
                "failed": data.get("numFailedTests"),
                "skipped": data.get("numPendingTests"),
                "passed": result.returncode == 0
                and data.get("numTotalTests", 0) > 0
                and data.get("numFailedTests") == 0
                and data.get("numPendingTests") == 0,
            }
        )
    if "query-control" in task["heldout"]:
        result = subprocess.run(
            ["node", "/validators/query-control.cjs", "/work"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        Path("/out/query.log").write_text(result.stdout + "\n" + result.stderr)
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        checks.append(
            {
                "kind": "query",
                "exitCode": result.returncode,
                "tests": data.get("tests", []),
                "passed": result.returncode == 0
                and len(data.get("tests", [])) == 4
                and all(t["status"] == "passed" for t in data.get("tests", [])),
            }
        )
    passed = bool(checks) and all(c["passed"] for c in checks)
    Path("/out/results.json").write_text(json.dumps({"passed": passed, "checks": checks}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
