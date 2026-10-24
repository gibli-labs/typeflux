"""Install the repository-maintained workflow instructions without changing other skill files."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

NAMES = ("gh-resolve-issue", "gh-resolve-bugbot-review", "typeflux-auto-fix-issue")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "skills"
    drift = []
    for name in NAMES:
        expected = (source / name / "SKILL.md").read_bytes()
        target = args.destination / name / "SKILL.md"
        if target.is_file() and target.read_bytes() == expected:
            continue
        drift.append(name)
        if not args.check:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Keep installed scripts and UI metadata intact.
            target.write_bytes(expected)
    print(("Different: " if args.check else "Updated: ") + (", ".join(drift) or "none"))
    return int(args.check and bool(drift))


if __name__ == "__main__":
    raise SystemExit(main())
