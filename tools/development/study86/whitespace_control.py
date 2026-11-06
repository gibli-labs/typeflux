"""Prospective requirement, separate from the historical graph task's held-outs."""

import json

import pytest
from test_project_graph import edit_entities
from test_project_graph import graph_repo as graph_repo
from tools.project_graph.compiler import compile_graph
from tools.project_graph.repository import GraphError, Repository


@pytest.mark.parametrize("field", ["id", "name", "verifier"])
def test_whitespace_only_ci_registry_field_is_malformed(graph_repo, field):
    key = " \t" if field == "id" else "candidate-lane"
    entry = {"name": "Candidate lane", "verifier": "python"}
    if field != "id":
        entry[field] = " \t"
    path = graph_repo / "tools/development/checks.json"
    path.write_text(json.dumps({key: entry}))
    edit_entities(graph_repo, lambda entities: entities[-1].update(ci=[key]))
    with pytest.raises(GraphError, match=r"tools/development/checks\.json"):
        compile_graph(Repository(graph_repo))
