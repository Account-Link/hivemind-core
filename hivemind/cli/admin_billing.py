"""Admin CLI commands for billing and credit codes."""

from __future__ import annotations

import json as _json
import shlex
from decimal import Decimal

import click
import httpx

from ._http import _api_error, _hget, _hpost
from ._shared import (
    _admin_headers,
    _resolve_admin_key,
    _resolve_admin_service,
)


def _micro_usd(value) -> str:
    dec = Decimal(int(value or 0)) / Decimal(1_000_000)
    return f"${dec.quantize(Decimal('0.000001'))}"


def _parse_duration_seconds(value: str) -> int:
    raw = (value or "").strip().lower()
    if not raw:
        raise ValueError("duration required")
    unit = raw[-1]
    number = raw[:-1] if unit in {"s", "m", "h", "d"} else raw
    try:
        amount = float(number)
    except ValueError as e:
        raise ValueError(f"invalid duration: {value!r}") from e
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    seconds = int(amount * multiplier)
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


def register_billing_commands(admin_cli: click.Group) -> None:
    """Attach billing and credit-code command groups to ``admin``."""

    @admin_cli.group("billing")
    def admin_billing():
        """Tenant credits, usage ledger, and model prices."""
        pass

    @admin_cli.group("credit-codes")
    def admin_credit_codes():
        """Create, list, and revoke redeemable credit codes."""
        pass

    @admin_credit_codes.command("create")
    @click.option("--credit", default="0.00", show_default=True, help="Credit USD.")
    @click.option("--uses", default=1, show_default=True, help="Maximum redemptions.")
    @click.option("--expires-in", default="", help="Duration like 7d, 24h, 60m.")
    @click.option("--label", default="", help="Admin label for this credit code.")
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_credit_code_create(
        credit: str,
        uses: int,
        expires_in: str,
        label: str,
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """Mint a credit code. Plaintext code is shown once."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        payload: dict = {
            "credit_usd": credit,
            "max_redemptions": uses,
            "label": label,
        }
        if expires_in:
            try:
                payload["expires_in_seconds"] = _parse_duration_seconds(expires_in)
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                raise SystemExit(2)
        try:
            resp = _hpost(
                f"{url}/v1/admin/credit-codes",
                headers=_admin_headers(admin_key),
                json=payload,
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
            click.echo(_json.dumps(data, indent=2, default=str))
            return
        click.echo(f"Credit code: {data['code_id']}")
        click.echo(f"Credit:  {_micro_usd(data.get('credit_micro_usd'))}")
        click.echo(f"Uses:    {data.get('max_redemptions')}")
        if data.get("expires_at"):
            click.echo(f"Expires: {data['expires_at']}")
        click.echo("")
        click.echo("Code (shown once):")
        click.echo(f"  {data['code']}")
        click.echo("")
        click.echo("Redeem:")
        click.echo(f"  hivemind redeem-credit {shlex.quote(data['code'])}")

    @admin_credit_codes.command("list")
    @click.option("--include-revoked", is_flag=True, help="Include revoked codes.")
    @click.option("--limit", default=100, show_default=True)
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_credit_code_list(
        include_revoked: bool,
        limit: int,
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """List credit codes. Plaintext codes are never returned."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        try:
            resp = _hget(
                f"{url}/v1/admin/credit-codes",
                headers=_admin_headers(admin_key),
                params={"include_revoked": include_revoked, "limit": limit},
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)
        if resp.status_code >= 400:
            click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
            raise SystemExit(3)
        codes = resp.json().get("credit_codes", [])
        if as_json:
            click.echo(_json.dumps(codes, indent=2, default=str))
            return
        if not codes:
            click.echo("(no credit codes)")
            return
        click.echo(f"{'CODE_ID':<20} {'CREDIT':>12} {'USED':>9} {'ACTIVE':>6} LABEL")
        for code in codes:
            used = f"{code.get('redeemed_count', 0)}/{code.get('max_redemptions', 0)}"
            click.echo(
                f"{code.get('code_id',''):<20} "
                f"{_micro_usd(code.get('credit_micro_usd')):>12} "
                f"{used:>9} "
                f"{str(code.get('active', False)):>6} "
                f"{code.get('label') or ''}"
            )

    @admin_credit_codes.command("revoke")
    @click.argument("code_id")
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    def admin_credit_code_revoke(
        code_id: str,
        service: str | None,
        admin_key: str,
    ):
        """Revoke an unexpired credit code."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        try:
            resp = _hpost(
                f"{url}/v1/admin/credit-codes/{code_id}/revoke",
                headers=_admin_headers(admin_key),
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)
        if resp.status_code >= 400:
            click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
            raise SystemExit(3)
        click.echo(f"Revoked credit code {code_id}.")

    @admin_billing.command("balance")
    @click.argument("tenant_id")
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_billing_balance(
        tenant_id: str,
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """Show tenant billing balance and recent ledger entries."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        try:
            resp = _hget(
                f"{url}/v1/admin/billing/{tenant_id}",
                headers=_admin_headers(admin_key),
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
            click.echo(_json.dumps(data, indent=2, default=str))
            return
        click.echo(f"Tenant:  {data['tenant_id']}")
        click.echo(f"Balance: {_micro_usd(data.get('balance_micro_usd'))}")
        ledger = data.get("ledger") or []
        if not ledger:
            return
        click.echo("")
        click.echo(f"{'WHEN':<12} {'KIND':<16} {'AMOUNT':>14} RUN")
        for row in ledger:
            click.echo(
                f"{str(row.get('created_at',''))[:12]:<12} "
                f"{str(row.get('kind',''))[:16]:<16} "
                f"{_micro_usd(row.get('amount_micro_usd')):>14} "
                f"{row.get('run_id') or ''}"
            )

    @admin_billing.command("accounts")
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_billing_accounts(
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """Show all tenants' credit totals, spend, and current balance."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        try:
            resp = _hget(
                f"{url}/v1/admin/billing",
                headers=_admin_headers(admin_key),
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)
        if resp.status_code >= 400:
            click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
            raise SystemExit(3)
        accounts = resp.json().get("accounts", [])
        if as_json:
            click.echo(_json.dumps(accounts, indent=2, default=str))
            return
        if not accounts:
            click.echo("(no tenants)")
            return
        click.echo(
            f"{'TENANT_ID':<16} {'NAME':<24} "
            f"{'BALANCE':>14} {'CREDITED':>14} {'SPENT':>14}"
        )
        for acct in accounts:
            click.echo(
                f"{acct.get('tenant_id',''):<16} "
                f"{str(acct.get('name',''))[:24]:<24} "
                f"{_micro_usd(acct.get('balance_micro_usd')):>14} "
                f"{_micro_usd(acct.get('total_credit_micro_usd')):>14} "
                f"{_micro_usd(acct.get('total_spent_micro_usd')):>14}"
            )

    @admin_billing.command("ledger")
    @click.option("--tenant-id", default="", help="Filter to one tenant.")
    @click.option("--limit", default=100, show_default=True)
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_billing_ledger(
        tenant_id: str,
        limit: int,
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """Show recent billing ledger entries across tenants."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        params = {"limit": limit}
        if tenant_id:
            params["tenant_id"] = tenant_id
        try:
            resp = _hget(
                f"{url}/v1/admin/billing/ledger",
                headers=_admin_headers(admin_key),
                params=params,
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)
        if resp.status_code >= 400:
            click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
            raise SystemExit(3)
        ledger = resp.json().get("ledger", [])
        if as_json:
            click.echo(_json.dumps(ledger, indent=2, default=str))
            return
        if not ledger:
            click.echo("(no ledger entries)")
            return
        click.echo(
            f"{'WHEN':<12} {'TENANT':<16} {'KIND':<16} {'AMOUNT':>14} RUN"
        )
        for row in ledger:
            click.echo(
                f"{str(row.get('created_at',''))[:12]:<12} "
                f"{row.get('tenant_id',''):<16} "
                f"{str(row.get('kind',''))[:16]:<16} "
                f"{_micro_usd(row.get('amount_micro_usd')):>14} "
                f"{row.get('run_id') or ''}"
            )

    @admin_billing.command("grant")
    @click.argument("tenant_id")
    @click.argument("amount_usd")
    @click.option("--note", default="", help="Ledger note.")
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_billing_grant(
        tenant_id: str,
        amount_usd: str,
        note: str,
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """Grant tenant prepaid billing credit in USD."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        try:
            resp = _hpost(
                f"{url}/v1/admin/billing/{tenant_id}/credits",
                headers=_admin_headers(admin_key),
                json={"amount_usd": amount_usd, "note": note},
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
            click.echo(_json.dumps(data, indent=2, default=str))
            return
        click.echo(
            f"Granted {_micro_usd(data.get('amount_micro_usd'))} "
            f"to {tenant_id}; balance {_micro_usd(data.get('balance_micro_usd'))}"
        )

    @admin_billing.command("prices")
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_billing_prices(
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """List model price snapshots used for run billing."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        try:
            resp = _hget(
                f"{url}/v1/admin/billing/prices",
                headers=_admin_headers(admin_key),
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)
        if resp.status_code >= 400:
            click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
            raise SystemExit(3)
        prices = resp.json().get("prices", [])
        if as_json:
            click.echo(_json.dumps(prices, indent=2, default=str))
            return
        click.echo(f"{'PROVIDER':<12} {'MODEL':<36} {'PROMPT/M':>12} {'OUT/M':>12}")
        for p in prices:
            click.echo(
                f"{p.get('provider',''):<12} "
                f"{str(p.get('model',''))[:36]:<36} "
                f"{_micro_usd(p.get('prompt_microusd_per_mtok')):>12} "
                f"{_micro_usd(p.get('completion_microusd_per_mtok')):>12}"
            )

    @admin_billing.command("set-price")
    @click.argument("provider")
    @click.argument("model")
    @click.option(
        "--prompt-usd-per-million",
        required=True,
        help="Input-token price in USD per 1M tokens.",
    )
    @click.option(
        "--completion-usd-per-million",
        required=True,
        help="Output-token price in USD per 1M tokens.",
    )
    @click.option("--source", default="admin", show_default=True)
    @click.option("--service", default=None, help="Hivemind service URL")
    @click.option(
        "--admin-key",
        envvar="HIVEMIND_ADMIN_KEY",
        default="",
        help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
        "the active profile's api_key when role=admin.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
    def admin_billing_set_price(
        provider: str,
        model: str,
        prompt_usd_per_million: str,
        completion_usd_per_million: str,
        source: str,
        service: str | None,
        admin_key: str,
        as_json: bool,
    ):
        """Create or update one model price snapshot."""
        admin_key = _resolve_admin_key(admin_key)
        url = _resolve_admin_service(service)
        payload = {
            "provider": provider,
            "model": model,
            "prompt_usd_per_million": prompt_usd_per_million,
            "completion_usd_per_million": completion_usd_per_million,
            "source": source,
        }
        try:
            resp = _hpost(
                f"{url}/v1/admin/billing/prices",
                headers=_admin_headers(admin_key),
                json=payload,
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
            click.echo(_json.dumps(data, indent=2, default=str))
            return
        click.echo(f"Set price for {data['provider']}/{data['model']}")
