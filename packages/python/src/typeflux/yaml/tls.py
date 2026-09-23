"""The CANONICAL `runtime.temporal.tls` → `temporalio.client.TLSConfig` mapping (#685).

One place resolves the structured TLS block (custom CA / client mTLS certs) to the
connect-time config: the YAML runtime's client connect (`yaml/runtime.py`) and the
ts-plan-argument binding driver (`project/binding_ts.py`) both build through here,
so the two Python connection surfaces cannot drift. The TS editions mirror this
mapping in `temporal-yaml`'s `temporalTlsOptions` — behavior changes must land in
both.

A boolean passes through untouched (`tls: true` = system trust roots, `tls: false`
= plaintext). A structured block resolves each slot from its inline secret
reference (`value_from: {env|file}`) or its `*_file` path — the spec forbids both
per slot — reading file BYTES with `~` expansion, failing loudly on a missing or
unreadable file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typeflux.yaml.secrets import resolve_optional_secret_bytes
from typeflux.yaml.spec import TemporalTLSConfigSpec


def build_temporal_tls_config(config: bool | TemporalTLSConfigSpec) -> Any:
    """Map the spec's `tls` value to what `Client.connect(tls=...)` takes."""
    if isinstance(config, bool):
        return config
    try:
        from temporalio.client import TLSConfig
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard.
        raise RuntimeError("temporalio is required to build a Temporal TLS config") from exc
    client_cert = resolve_optional_secret_bytes(
        config.client_cert, runtime_path="runtime.temporal.tls.client_cert"
    ) or read_optional_tls_file(config.client_cert_file, field_name="client_cert_file")
    client_private_key = resolve_optional_secret_bytes(
        config.client_private_key, runtime_path="runtime.temporal.tls.client_private_key"
    ) or read_optional_tls_file(
        config.client_private_key_file, field_name="client_private_key_file"
    )
    # The spec pairs the DECLARATIONS; this pairs the RESOLUTIONS (the TS
    # mapping's exact guard): a half-resolved mTLS pair (one side
    # optional-and-unset) must not reach the Rust bridge as a silent
    # server-auth-only downgrade or an opaque native error.
    if (client_cert is None) != (client_private_key is None):
        raise ValueError(
            "runtime.temporal.tls resolved a client certificate without its private key "
            "(or vice versa) — an mTLS pair must resolve together"
        )
    return TLSConfig(
        server_root_ca_cert=(
            resolve_optional_secret_bytes(
                config.server_root_ca_cert,
                runtime_path="runtime.temporal.tls.server_root_ca_cert",
            )
            or read_optional_tls_file(
                config.server_root_ca_cert_file,
                field_name="server_root_ca_cert_file",
            )
        ),
        domain=config.domain,
        client_cert=client_cert,
        client_private_key=client_private_key,
    )


def read_optional_tls_file(path: str | None, *, field_name: str) -> bytes | None:
    """Read one cert/key file's bytes (`~` expanded); a missing file fails loudly."""
    if path is None:
        return None
    try:
        return Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read runtime.temporal.tls.{field_name}: {path}") from exc


__all__ = ["build_temporal_tls_config", "read_optional_tls_file"]
