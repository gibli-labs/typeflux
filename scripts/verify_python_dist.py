"""Python dist-content gate (#891).

Validates the built wheel + sdist in packages/python/dist/ before any
publish:

1. Exactly one wheel and one sdist, versions matching pyproject.
2. Wheel: only the package + dist-info; LICENSE and py.typed present.
3. Sdist: only the buildable source surface (src/, LICENSE, README,
   pyproject, PKG-INFO, .gitignore) — never tests, examples, or fixtures.
4. If the repo carries the private forbidden-terms list
   (docs/open-source/forbidden-terms.txt — never part of the public tree),
   no archive member's PATH or TEXT CONTENT may match a term. Trees
   without the list (the public repository) skip the scan.

Run from packages/python:  uv run python ../../scripts/verify_python_dist.py
"""

from __future__ import annotations

import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "packages" / "python"
REPO = Path(__file__).resolve().parents[1]
DIST = PKG / "dist"

ALLOWED_SDIST_TOPS = {"src", "LICENSE", "README.md", "pyproject.toml", "PKG-INFO", ".gitignore"}

failures: list[str] = []


def read_terms() -> list[str]:
    terms_path = REPO / "docs" / "open-source" / "forbidden-terms.txt"
    if not terms_path.exists():
        return []
    return [
        line.strip().lower()
        for line in terms_path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def scan(name: str, data: bytes, terms: list[str]) -> None:
    lower_name = name.lower()
    for term in terms:
        if term in lower_name:
            failures.append(f"forbidden term in member path: {name}")
        try:
            text = data.decode("utf-8", errors="ignore").lower()
        except Exception:  # pragma: no cover - decode with ignore cannot raise
            continue
        if term in text:
            failures.append(f"forbidden term in member content: {name}")


def main() -> int:
    project = tomllib.loads((PKG / "pyproject.toml").read_text())["project"]
    version = project["version"]
    # The import package intentionally matches the distribution name (#892);
    # wheel metadata normalizes dashes to underscores.
    module = project["name"].replace("-", "_")
    wheels = sorted(DIST.glob("*.whl"))
    sdists = sorted(DIST.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        failures.append(
            f"expected exactly one wheel and one sdist in {DIST}, got {wheels} / {sdists}"
        )
        report()
        return 1
    wheel, sdist = wheels[0], sdists[0]
    for artifact in (wheel, sdist):
        if version not in artifact.name:
            failures.append(f"{artifact.name} does not carry pyproject version {version}")

    terms = read_terms()

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        tops = {n.split("/")[0] for n in names}
        expected_tops = {module, f"{module}-{version}.dist-info"}
        if tops != expected_tops:
            failures.append(f"wheel top-level entries {sorted(tops)} != {sorted(expected_tops)}")
        if not any(n.endswith("dist-info/licenses/LICENSE") for n in names):
            failures.append("wheel missing dist-info LICENSE")
        if f"{module}/py.typed" not in names:
            failures.append("wheel missing py.typed")
        for n in names:
            scan(n, zf.read(n), terms) if terms else None

    with tarfile.open(sdist) as tf:
        members = tf.getmembers()
        tops = {m.name.split("/")[1] for m in members if "/" in m.name}
        unexpected = tops - ALLOWED_SDIST_TOPS
        if unexpected:
            failures.append(f"sdist carries unexpected top-level entries: {sorted(unexpected)}")
        for banned in ("tests", "examples", "fixtures", ".env"):
            hits = [m.name for m in members if f"/{banned}" in m.name]
            if hits:
                failures.append(f"sdist must not contain {banned}: {hits[:3]}")
        if terms:
            for m in members:
                if not m.isfile():
                    continue
                fobj = tf.extractfile(m)
                if fobj is None:
                    continue
                scan(m.name, fobj.read(), terms)

    if terms:
        print(
            f"verify-python-dist: content-scanned both archives against {len(terms)} terms",
            file=sys.stderr,
        )
    else:
        print(
            "verify-python-dist: no forbidden-terms list in this tree; content scan skipped",
            file=sys.stderr,
        )
    report()
    return 1 if failures else 0


def report() -> None:
    for f in failures:
        print(f"verify-python-dist: FAIL {f}", file=sys.stderr)
    if not failures:
        print("verify-python-dist: OK", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
