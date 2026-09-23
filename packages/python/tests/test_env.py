from __future__ import annotations

import os
import socket

import pytest

from typeflux.env import load_env


def test_load_env_reads_file_without_overriding_existing_values(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "OPENAI_API_KEY=from-file",
                "export TYPEFLUX_OPENAI_MODEL='gpt-4o-mini'",
                'LANGFUSE_HOST="https://cloud.langfuse.com" # inline comment',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    monkeypatch.delenv("TYPEFLUX_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    loaded = load_env(env_file)

    assert loaded == env_file
    assert os.environ["OPENAI_API_KEY"] == "from-shell"
    assert os.environ["TYPEFLUX_OPENAI_MODEL"] == "gpt-4o-mini"
    assert os.environ["LANGFUSE_HOST"] == "https://cloud.langfuse.com"


def test_non_live_tests_start_without_runtime_env_leakage() -> None:
    assert "OPENAI_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "TEMPORAL_API_KEY" not in os.environ
    assert os.environ["TYPEFLUX_ENV_FILE"].endswith(".typeflux-tests-do-not-load.env")


def test_non_live_tests_block_external_network() -> None:
    with socket.socket() as sock:
        with pytest.raises(RuntimeError, match="external network is disabled"):
            sock.connect(("example.com", 443))


def test_load_env_can_override_existing_values(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")

    load_env(env_file, override=True)

    assert os.environ["OPENAI_API_KEY"] == "from-file"


def test_load_env_uses_typeflux_env_file_exactly(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("TYPEFLUX_OPENAI_MODEL=gpt-4o\n", encoding="utf-8")
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(env_file))
    monkeypatch.delenv("TYPEFLUX_OPENAI_MODEL", raising=False)

    loaded = load_env()

    assert loaded == env_file
    assert os.environ["TYPEFLUX_OPENAI_MODEL"] == "gpt-4o"


def test_load_env_reads_only_cwd_env_by_default(tmp_path, monkeypatch) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    parent_env = parent / ".env"
    child_env = child / ".env"
    parent_env.write_text("TYPEFLUX_OPENAI_MODEL=from-parent\n", encoding="utf-8")
    child_env.write_text("TYPEFLUX_OPENAI_MODEL=from-child\n", encoding="utf-8")
    monkeypatch.chdir(child)
    monkeypatch.delenv("TYPEFLUX_ENV_FILE", raising=False)
    monkeypatch.delenv("TYPEFLUX_OPENAI_MODEL", raising=False)

    loaded = load_env()

    assert loaded == child_env
    assert os.environ["TYPEFLUX_OPENAI_MODEL"] == "from-child"


def test_load_env_does_not_walk_to_parent_env(tmp_path, monkeypatch) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    parent_env = parent / ".env"
    parent_env.write_text("TYPEFLUX_OPENAI_MODEL=from-parent\n", encoding="utf-8")
    monkeypatch.chdir(child)
    monkeypatch.delenv("TYPEFLUX_ENV_FILE", raising=False)
    monkeypatch.delenv("TYPEFLUX_OPENAI_MODEL", raising=False)

    loaded = load_env()

    assert loaded is None
    assert "TYPEFLUX_OPENAI_MODEL" not in os.environ


def test_load_env_missing_explicit_file_returns_none(tmp_path) -> None:
    assert load_env(tmp_path / "missing.env") is None


def test_load_env_expands_explicit_paths(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TYPEFLUX_OPENAI_MODEL=gpt-4o\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TYPEFLUX_OPENAI_MODEL", raising=False)

    loaded = load_env("~/.env")

    assert loaded == env_file
    assert os.environ["TYPEFLUX_OPENAI_MODEL"] == "gpt-4o"
