"""Reviewed executable policy. Graph declarations only reference these IDs."""

import json
from pathlib import Path

CI_CHECKS = {
    key: (value["name"], value["verifier"])
    for key, value in json.loads(Path(__file__).with_name("checks.json").read_text()).items()
}

DEFAULT = (
    "quality",
    "python",
    "typescript",
    "web",
    "mcp",
    "conformance-python",
    "conformance-typescript",
    "browser-python",
    "browser-typescript",
    "temporal",
    "graph",
)
# Independent checks remain separate from the CI acceptance result.
REQUIRED_CONTEXTS = (
    "CI acceptance",
    "Dependency Review",
    "DCO sign-off",
    "Analyze (python)",
    "Analyze (javascript-typescript)",
    "Analyze (actions)",
)
