from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def load_env(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    env_path = _resolve_env_path(path)
    if env_path is None or not env_path.exists():
        return None

    for key, value in dotenv_values(env_path).items():
        if value is None:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    return env_path


def read_env_file_values(path: str | Path | None = None) -> dict[str, str]:
    """The dotenv file's values WITHOUT mutating ``os.environ`` (#760).

    Same file discovery as :func:`load_env` (explicit path > ``TYPEFLUX_ENV_FILE`` >
    cwd ``.env``); returns ``{}`` when no file exists. The hermetic loader path uses
    this to merge dotenv values into an injected interpolation map only — the process
    environment stays untouched.
    """
    env_path = _resolve_env_path(path)
    if env_path is None or not env_path.exists():
        return {}
    return {key: value for key, value in dotenv_values(env_path).items() if value is not None}


def _resolve_env_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path).expanduser()

    explicit = os.getenv("TYPEFLUX_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()

    current = Path.cwd().resolve()
    return current / ".env"


__all__ = ["load_env", "read_env_file_values"]
