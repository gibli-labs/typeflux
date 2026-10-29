"""Dependency boundary for the optional Temporal integration."""

from importlib import import_module


def require_temporal() -> None:
    try:
        import_module("temporalio")
    except ModuleNotFoundError as exc:
        if exc.name != "temporalio":
            raise
        raise RuntimeError(
            "Temporal execution requires the optional integration; "
            "install it with: pip install 'typeflux[temporal]'"
        ) from exc
