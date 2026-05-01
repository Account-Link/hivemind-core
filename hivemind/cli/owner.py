"""Owner identity commands: init and rotate-key."""

import json as _json

import click
import httpx

from ._config import (
    _DEFAULT_PROFILE,
    _config_path,
    _headers,
    _load_config,
    _profile_name,
    _save_config,
)
from ._http import _api_error, _warm_pin_from_trust
from ._shared import _DEFAULT_SERVICE


def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _hpost(*a, **kw):
    from . import _hpost as _f
    return _f(*a, **kw)


@click.command()
@click.option(
    "--service",
    default=_DEFAULT_SERVICE,
    show_default=True,
    help="Hivemind service URL",
)
@click.option("--api-key", default="", help="API key for authentication")
def init(service: str, api_key: str):
    """Connect to a hivemind service and save config."""
    service = service.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    _warm_pin_from_trust(service)

    health: dict = {}
    role = "tenant"
    try:
        resp = _hget(f"{service}/v1/health", headers=headers, timeout=10)
        if resp.status_code == 401 and api_key:
            ar = _hget(f"{service}/v1/admin/tenants", headers=headers, timeout=10)
            if ar.status_code < 400:
                role = "admin"
                health = {"table_count": "(admin)", "version": "(admin)"}
            else:
                click.echo(
                    f"Error: 401 from {service} -- key authorizes neither "
                    "a tenant nor admin role.",
                    err=True,
                )
                raise SystemExit(1)
        else:
            resp.raise_for_status()
            health = resp.json()
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        click.echo(f"Error: {e.response.status_code} from {service}", err=True)
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo(f"Error: Connection timed out reaching {service}", err=True)
        raise SystemExit(1)

    _save_config({"service": service, "api_key": api_key, "role": role})
    profile = _profile_name()
    click.echo(
        f"Initialized profile '{profile}' (role={role}) at {_config_path()} "
        f"-- connected to {service}"
    )
    click.echo(f"  Tables: {health.get('table_count', '?')}")
    click.echo(f"  Version: {health.get('version', '?')}")
    if profile == _DEFAULT_PROFILE:
        click.echo(
            "  Tip: pass --profile NAME to keep separate identities "
            "(admin / tenant_a / tenant_b) on the same laptop."
        )


@click.command()
@click.argument("name")
@click.option(
    "--service",
    default=_DEFAULT_SERVICE,
    show_default=True,
    help="Hivemind service URL",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def signup(name: str, service: str, as_json: bool):
    """Create a self-serve tenant and save its API key to this profile."""
    service = service.rstrip("/")
    _warm_pin_from_trust(service)
    payload = {"name": name}
    try:
        resp = _hpost(
            f"{service}/v1/signup",
            json=payload,
            timeout=60,
        )
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo(f"Error: Connection timed out reaching {service}", err=True)
        raise SystemExit(1)
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(1)

    data = resp.json()
    _save_config({"service": service, "api_key": data["api_key"], "role": "tenant"})
    profile = _profile_name()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Initialized profile '{profile}' at {_config_path()}")
    click.echo(f"Tenant: {data['tenant_id']} ({data['name']})")
    click.echo(
        f"Starter balance: {data.get('balance_micro_usd', 0) / 1_000_000:.2f} USD"
    )
    click.echo("")
    click.echo("API key (saved locally; store it now if you need another copy):")
    click.echo(f"  {data['api_key']}")


@click.command("redeem-credit")
@click.argument("credit_code")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def redeem_credit(credit_code: str, as_json: bool):
    """Redeem an admin-issued credit code into the active tenant."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)
    try:
        resp = _hpost(
            f"{service}/v1/billing/credit-codes/redeem",
            headers=headers,
            json={"credit_code": credit_code},
            timeout=30,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    data = resp.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Redeemed credit code {data.get('code_id')}")
    click.echo(f"Credit: {data.get('credit_micro_usd', 0) / 1_000_000:.2f} USD")
    click.echo(f"Balance: {data.get('balance_micro_usd', 0) / 1_000_000:.2f} USD")


@click.command("rotate-key")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
@click.confirmation_option(
    prompt=(
        "Rotate this tenant's API key? The current key will stop working "
        "immediately. Continue?"
    )
)
def rotate_key(as_json: bool):
    """Rotate this tenant's API key and update local config."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)

    try:
        resp = _hpost(f"{service}/v1/tenant/rotate-key", headers=headers, timeout=30)
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)

    data = resp.json()
    new_key = data["api_key"]
    config["api_key"] = new_key
    _save_config(config)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Tenant: {data['tenant_id']}")
    click.echo(
        f"New API key (saved to profile '{_profile_name()}' at {_config_path()}):"
    )
    click.echo(f"  {new_key}")
    click.echo("")
    click.echo(
        "Previous key is now revoked. Anyone who held the old key can no longer "
        "reach your data."
    )
