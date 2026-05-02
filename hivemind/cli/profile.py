"""``profile`` command group: list / show / delete / use."""

import click
import yaml

from ._config import (
    _clear_active_profile_if,
    _config_path,
    _profile_name,
    _set_active_profile,
)


# ── Profiles ──
#
# Each profile is a separate identity: service URL + tenant API key,
# stored at ``~/.hivemind/profiles/<name>.yaml``. Trust pins
# (``trust.json``, ``enclave-tls-*.pem``) live alongside, so all
# profiles share the same set of approved enclaves.


@click.group("profile")
def profile_cli():
    """Manage named identities (admin / tenant_a / tenant_b / …)."""
    pass


@profile_cli.command("list")
def profile_list():
    """List all profiles in ~/.hivemind/profiles/."""
    from . import _PROFILES_DIR  # parent-owned (test-patchable)

    active = _profile_name()
    if not _PROFILES_DIR.exists():
        click.echo(f"No profiles yet. Run 'hmctl init …' to create '{active}'.")
        return
    rows: list[tuple[str, str, str, str]] = []
    for p in sorted(_PROFILES_DIR.glob("*.yaml")):
        name = p.stem
        try:
            cfg = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError:
            cfg = {}
        service = str(cfg.get("service") or "?")
        has_key = "yes" if cfg.get("api_key") else "no"
        role = str(cfg.get("role") or "tenant")
        rows.append((name, service, has_key, role))
    if not rows:
        click.echo(f"No profiles in {_PROFILES_DIR}.")
        return
    click.echo(
        f"{'ACTIVE':<7} {'NAME':<24} {'ROLE':<8} {'API_KEY':<8} SERVICE"
    )
    for name, service, has_key, role in rows:
        marker = "*" if name == active else " "
        click.echo(
            f"  {marker:<5} {name:<24} {role:<8} {has_key:<8} {service}"
        )


@profile_cli.command("show")
@click.argument("name", required=False)
def profile_show(name: str | None):
    """Print the YAML for the active profile (or NAME)."""
    p = _config_path(name) if name else _config_path()
    if not p.exists():
        click.echo(f"Error: profile not found at {p}", err=True)
        raise SystemExit(1)
    click.echo(f"# {p}")
    click.echo(p.read_text(), nl=False)


@profile_cli.command("delete")
@click.argument("name")
@click.confirmation_option(
    prompt="Delete this profile's local config? "
    "(does NOT revoke the API key on the server)"
)
def profile_delete(name: str):
    """Delete a profile's local config file."""
    from . import _ACTIVE_POINTER  # parent-owned (test-patchable)

    p = _config_path(name)
    if not p.exists():
        click.echo(f"Error: profile not found at {p}", err=True)
        raise SystemExit(1)
    p.unlink()
    pointer_was_set = (
        _ACTIVE_POINTER.exists()
        and _ACTIVE_POINTER.read_text().strip() == name
    )
    _clear_active_profile_if(name)
    click.echo(f"Deleted {p}")
    if pointer_was_set:
        click.echo(
            f"Active-profile pointer cleared (it was pointing at '{name}'). "
            "Run 'hmctl profile use NAME' to set a new one."
        )
    click.echo(
        "Note: the API key on the server is still valid until you "
        "rotate it via 'hmctl rotate-key' or delete the tenant."
    )


@profile_cli.command("rename")
@click.argument("old_name")
@click.argument("new_name")
def profile_rename(old_name: str, new_name: str):
    """Rename a local profile.

    Moves ``~/.hivemind/profiles/<old_name>.yaml`` to
    ``~/.hivemind/profiles/<new_name>.yaml`` and rewrites the active-profile
    pointer if it was pointing at ``old_name``. The API key, service URL,
    and any server-side state are unchanged — this is purely a local
    label rename.
    """
    from . import _ACTIVE_POINTER  # parent-owned (test-patchable)

    src = _config_path(old_name)
    dst = _config_path(new_name)
    if not src.exists():
        click.echo(f"Error: profile not found at {src}", err=True)
        raise SystemExit(1)
    if dst.exists():
        click.echo(
            f"Error: a profile named '{new_name}' already exists at {dst}. "
            "Delete it first with 'hmctl profile delete' or pick another name.",
            err=True,
        )
        raise SystemExit(1)
    if not new_name or "/" in new_name or new_name.startswith("."):
        click.echo(
            f"Error: '{new_name}' is not a valid profile name "
            "(no slashes, no leading dot, must be non-empty).",
            err=True,
        )
        raise SystemExit(1)
    src.rename(dst)
    pointer_was_set = (
        _ACTIVE_POINTER.exists()
        and _ACTIVE_POINTER.read_text().strip() == old_name
    )
    if pointer_was_set:
        _set_active_profile(new_name)
    click.echo(f"Renamed {src} → {dst}")
    if pointer_was_set:
        click.echo(f"Active-profile pointer updated to '{new_name}'.")


@profile_cli.command("use")
@click.argument("name")
def profile_use(name: str):
    """Make NAME the persistent active profile.

    Plain ``hmctl <cmd>`` (no --profile flag, no HIVEMIND_PROFILE env)
    will use this profile from now on. Per-command overrides via
    ``--profile`` and ``HIVEMIND_PROFILE`` still win.
    """
    from . import _ACTIVE_POINTER  # parent-owned (test-patchable)

    p = _config_path(name)
    if not p.exists():
        click.echo(
            f"Error: profile '{name}' not found at {p}. "
            f"Run 'hmctl --profile {name} init …' first.",
            err=True,
        )
        raise SystemExit(1)
    _set_active_profile(name)
    click.echo(f"Active profile is now '{name}' (pointer: {_ACTIVE_POINTER}).")
