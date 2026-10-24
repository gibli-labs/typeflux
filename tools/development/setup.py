"""Frozen dependency setup; no private registries or publication credentials."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from tools.project_graph.collection import collection_environment
from tools.project_graph.repository import GraphError

TEMPORAL = {
    ("Linux", "x86_64"): (
        "linux_amd64",
        "8b5de72e622f4ae062d0d5d948ca398de6212d63b2f25766a1cb810a3dc2d0ed",
    ),
    ("Linux", "aarch64"): (
        "linux_arm64",
        "f78d8cf0353b4e04c9024f11e66eaba372f07efe9956de0f41cf03bd31c302f3",
    ),
    ("Darwin", "arm64"): (
        "darwin_arm64",
        "88a59ea7b1a51309a873e2a914360865123d5b36528d55f8d7c34c6d79007fda",
    ),
    ("Darwin", "x86_64"): (
        "darwin_amd64",
        "a289156e464bdbb667a125b5e94abeab98a74814771ea4828731970cab4e907c",
    ),
}


def temporal(root: Path) -> Path:
    """Install a checksum-pinned CLI into ignored local tool storage."""
    name, expected = TEMPORAL[(platform.system(), platform.machine())]
    directory = root / ".project-graph/bin"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"temporal_cli_1.7.0_{name}.tar.gz"
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        url = f"https://github.com/temporalio/cli/releases/download/v1.7.0/{archive.name}"
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise GraphError("Temporal CLI archive checksum mismatch")
        archive.write_bytes(data)
    with tarfile.open(fileobj=io.BytesIO(archive.read_bytes()), mode="r:gz") as tar:
        entry = tar.getmember("temporal")
        if not entry.isfile():
            raise GraphError("Temporal archive binary is not a regular file")
        stream = tar.extractfile(entry)
        if stream is None:
            raise GraphError("Temporal archive is missing its binary")
        binary = directory / "temporal"
        binary.write_bytes(stream.read())
        binary.chmod(0o755)
    return binary


def sync(root: Path, scope: str):
    env = collection_environment()

    def run(*argv, cwd="."):
        subprocess.run(argv, cwd=root / cwd, env=env, check=True)

    run(
        "uv",
        "sync",
        "--frozen",
        "--extra",
        "api",
        "--extra",
        "live",
        "--group",
        "dev",
        "--project",
        "packages/python",
    )
    if scope == "python":
        return
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    if not node.startswith("v24."):
        raise GraphError(f"Node 24 is required (found {node}); see docs/development.md")
    run("npm", "ci", "--prefix", "tools/openapi-generator")
    if scope not in {"python", "web"}:
        run("pnpm", "install", "--frozen-lockfile")
        run("pnpm", "-r", "build")
    if scope != "typescript":
        run("npm", "ci", cwd="clients/typescript")
        run("npm", "run", "build", cwd="clients/typescript")
    if scope in {"all", "web", "browser", "graph"}:
        run("npm", "ci", cwd="clients/console")
    if scope not in {"python", "web", "typescript"}:
        run("npm", "ci", cwd="clients/mcp")
    if scope in {"all", "temporal"}:
        temporal(root)
    if scope in {"all", "browser"}:
        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            run("npm", "exec", "--", "playwright", "install", "chromium", cwd="clients/console")
