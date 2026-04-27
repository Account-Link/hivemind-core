"""``compose`` command group: bless / pins / revoke.

Owner-side commands for managing :class:`hivemind.compose_pin.ComposePin`
envelopes — the signed records that authorize one or more
``compose_hash`` values for a scope agent.

Why this exists: ``hivemind share --mint`` (default) bakes a single
``compose_hash`` into the URI. That breaks every URI on every
redeploy. With ``hivemind compose bless`` the owner pre-signs an
envelope listing the composes they've approved; ``share --mint
--pin-rotation`` bakes the signer's pubkey instead, and the
recipient verifies "live compose ∈ allowed_composes" at request
time. Trust still flows from the owner's bearer-derived signing key
— no operator escape hatch.
"""

from __future__ import annotations

import json as _json
import time

import click
import httpx

from ..compose_pin import make_unsigned_pin
from ..tenant_signing import derive_signing_keypair
from ._config import _headers, _load_config
from ._http import _api_error


# ── Test-patchable HTTP wrappers (trampoline through parent module) ──
def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _hpost(*a, **kw):
    from . import _hpost as _f
    return _f(*a, **kw)


def _hdelete(*a, **kw):
    from . import _hdelete as _f
    return _f(*a, **kw)


@click.group("compose")
def compose_cli():
    """Manage signed compose-pin envelopes (redeploy-safe URIs)."""
    pass


def _resolve_tenant_id(service: str, headers: dict) -> str:
    """Look up the caller's tenant_id via ``GET /v1/whoami``.

    The control plane keys signing material on ``(api_key, tenant_id)``,
    so the CLI needs the tenant_id locally to derive the right keypair.
    ``/v1/whoami`` is the canonical reflection endpoint — works for any
    authenticated caller (owner or query), returns ``tenant_id`` plus
    the constraint snapshot.
    """
    try:
        r = _hget(f"{service}/v1/whoami", headers=headers, timeout=15)
    except httpx.RequestError as e:
        raise click.ClickException(f"GET /v1/whoami failed: {e}")
    if r.status_code >= 400:
        raise click.ClickException(
            f"Could not resolve tenant id (GET /v1/whoami → "
            f"{r.status_code} {_api_error(r)})"
        )
    tid = (r.json().get("tenant_id") or "").strip()
    if not tid:
        raise click.ClickException(
            "GET /v1/whoami returned no tenant_id. Pass --tenant-id."
        )
    return tid


def _fetch_live_compose(service: str) -> str:
    """Live ``compose_hash`` from /v1/attestation. Empty string if the
    endpoint can't be reached — caller decides whether to abort."""
    try:
        r = _hget(f"{service}/v1/attestation", timeout=15)
    except httpx.RequestError:
        return ""
    if r.status_code >= 400:
        return ""
    return ((r.json().get("attestation") or {}).get("compose_hash") or "").lower()


def _fetch_attested_files_digest(
    service: str, scope_id: str, headers: dict,
) -> str:
    """Fetch the scope agent's attested-files digest. Aborts on error."""
    try:
        r = _hget(
            f"{service}/v1/agents/{scope_id}/attest",
            headers=headers,
            timeout=30,
        )
    except httpx.RequestError as e:
        raise click.ClickException(f"Error fetching scope pins: {e}")
    if r.status_code >= 400:
        raise click.ClickException(
            f"Error {r.status_code} fetching scope pins: {_api_error(r)}"
        )
    body = r.json()
    return (
        body.get("attested_files_digest_sha256")
        or body.get("files_digest_sha256")
        or ""
    ).lower()


@compose_cli.command("bless")
@click.option(
    "--hash",
    "extra_hashes",
    multiple=True,
    help=(
        "compose_hash to authorize (64-hex). Repeat to allow multiple. "
        "Combined with --add-current; pass neither and you get just the "
        "current compose."
    ),
)
@click.option(
    "--add-current/--no-add-current",
    default=True,
    help="Include the live compose_hash from /v1/attestation. Default on.",
)
@click.option(
    "--ttl-hours",
    type=int,
    default=0,
    help="Pin expiry in hours (0 = no expiry, the default).",
)
@click.option("--tenant-id", default=None, help="Override resolved tenant id.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def compose_bless(
    extra_hashes: tuple[str, ...],
    add_current: bool,
    ttl_hours: int,
    tenant_id: str | None,
    as_json: bool,
):
    """Sign a fresh compose pin and POST it to the service.

    The pin authorizes the listed ``compose_hash`` values for the active
    scope agent. URIs minted with ``hivemind share --mint --pin-rotation``
    will continue to verify against any live compose listed here — so a
    redeploy that lands on an authorized compose doesn't invalidate
    outstanding URIs.

    The signing key is derived locally from this profile's ``hmk_`` (no
    private key is ever sent to the service). The server verifies the
    embedded ``signer_pubkey`` matches the pubkey it derives from the
    request's bearer.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        raise click.ClickException(
            "no api_key in config. Run 'hivemind init'."
        )
    api_key = config["api_key"]
    scope_id = (config.get("scope_agent_id") or "").strip()
    if not scope_id:
        raise click.ClickException(
            "no scope agent registered. Run 'hivemind scope ...' first."
        )

    tid = (tenant_id or "").strip() or _resolve_tenant_id(service, headers)

    composes: list[str] = []
    if add_current:
        live = _fetch_live_compose(service)
        if live:
            composes.append(live)
        elif not extra_hashes:
            raise click.ClickException(
                "live /v1/attestation unreachable and no --hash given"
            )
    for h in extra_hashes:
        h = h.strip().lower()
        if not h:
            continue
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise click.ClickException(f"--hash {h!r} is not 64 hex chars")
        if h not in composes:
            composes.append(h)
    if not composes:
        raise click.ClickException("nothing to bless (allowed_composes empty)")

    attested_files_digest = _fetch_attested_files_digest(
        service, scope_id, headers,
    )
    if not attested_files_digest:
        raise click.ClickException(
            "could not fetch attested_files_digest — no agent files?"
        )

    priv, _pub = derive_signing_keypair(api_key, tid)
    pin = make_unsigned_pin(
        tenant_id=tid,
        allowed_composes=composes,
        scope_agent_id=scope_id,
        attested_files_digest=attested_files_digest,
        ttl_seconds=max(0, ttl_hours) * 3600,
    ).sign(priv)

    try:
        r = _hpost(
            f"{service}/v1/tenants/compose-pin",
            headers=headers,
            json={"envelope": pin.model_dump()},
            timeout=30,
        )
    except httpx.RequestError as e:
        raise click.ClickException(f"POST failed: {e}")
    if r.status_code >= 400:
        raise click.ClickException(
            f"POST {r.status_code}: {_api_error(r)}"
        )
    body = r.json()

    if as_json:
        click.echo(_json.dumps(body, indent=2))
        return
    env = body.get("envelope") or {}
    click.echo(f"pin_id:                  {body.get('pin_id', '')}")
    click.echo(f"tenant_id:               {env.get('tenant_id', '')}")
    click.echo(f"scope_agent_id:          {env.get('scope_agent_id', '')}")
    click.echo(f"attested_files_digest:   {env.get('attested_files_digest', '')}")
    click.echo(f"signer_pubkey (b64):     {env.get('signer_pubkey', '')}")
    click.echo(f"issued_at:               {env.get('issued_at', '')}")
    exp = env.get("exp", 0)
    if exp:
        click.echo(f"exp:                     {exp} ({exp - int(time.time())}s left)")
    else:
        click.echo("exp:                     0 (no expiry)")
    click.echo("allowed_composes:")
    for c in env.get("allowed_composes") or []:
        click.echo(f"  {c}")


@compose_cli.command("pins")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def compose_pins(as_json: bool):
    """List signed compose pins for this tenant."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    try:
        r = _hget(
            f"{service}/v1/tenants/compose-pin/list",
            headers=headers,
            timeout=15,
        )
    except httpx.RequestError as e:
        raise click.ClickException(str(e))
    if r.status_code >= 400:
        raise click.ClickException(
            f"GET {r.status_code}: {_api_error(r)}"
        )
    body = r.json()
    pins = body.get("pins") or []
    if as_json:
        click.echo(_json.dumps(body, indent=2, default=str))
        return
    if not pins:
        click.echo("(no pins)")
        return
    for p in pins:
        env = p.get("envelope") or {}
        marker = "" if not p.get("revoked_at") else "  [revoked]"
        click.echo(
            f"{p.get('pin_id', '?'):<14} "
            f"scope={env.get('scope_agent_id', '?'):<14} "
            f"composes={len(env.get('allowed_composes') or [])}"
            f"{marker}"
        )


@compose_cli.command("revoke")
@click.argument("pin_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def compose_revoke(pin_id: str, as_json: bool):
    """Revoke a compose pin by id (use ``hivemind compose pins`` to list)."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    try:
        r = _hdelete(
            f"{service}/v1/tenants/compose-pin/{pin_id}",
            headers=headers,
            timeout=15,
        )
    except httpx.RequestError as e:
        raise click.ClickException(str(e))
    if r.status_code == 404:
        raise click.ClickException(f"pin '{pin_id}' not found")
    if r.status_code >= 400:
        raise click.ClickException(
            f"DELETE {r.status_code}: {_api_error(r)}"
        )
    if as_json:
        click.echo(_json.dumps(r.json(), indent=2))
    else:
        click.echo(f"revoked {pin_id}")


__all__ = ["compose_cli"]
