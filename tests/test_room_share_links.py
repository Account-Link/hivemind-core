"""Stable per-room share-link tests — registry storage + resolver path.

Mirrors ``test_capability_tokens.py`` shape: provisions a fresh control
DB per test, exercises ``enable_room_share_link`` /
``rotate_room_share_link`` / ``disable_room_share_link``, and verifies
that ``resolve_any`` returns ``role='share'`` for the minted bearer.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from hivemind.config import Settings
from hivemind.tenants import (
    TenantRegistry,
    _SHARE_TOKEN_PREFIX,
)


TEST_DSN = os.environ.get(
    "HIVEMIND_TEST_DATABASE_URL",
    "postgresql://hivemind:dev@localhost:5432/postgres",
)


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(TEST_DSN),
    reason=f"Postgres not reachable at {TEST_DSN}",
)


def _unique(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _drop_db(dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass


def _make_settings(control_db: str) -> Settings:
    return Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )


@pytest.fixture
def registry():
    control_db = _unique("hm_share")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = _make_settings(control_db)
    reg = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin
    reg._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs: list[str] = [control_db]
    reg._test_created_dbs = created_dbs  # type: ignore[attr-defined]

    yield reg

    try:
        reg.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


def _provision(registry, name: str) -> dict:
    t = registry.provision(name)
    registry._test_created_dbs.append(t["db_name"])
    return t


# ── enable / get / rotate / disable ──────────────────────────────────


def test_enable_returns_plaintext_and_link(registry):
    t = _provision(registry, "alice")
    out = registry.enable_room_share_link(
        t["tenant_id"], "room_alpha", t["api_key"],
    )
    assert out["share_token"].startswith(_SHARE_TOKEN_PREFIX)
    assert out["prefix"] and len(out["prefix"]) == 8
    assert out["created_at"] is not None
    assert out["rotated_at"] is None


def test_enable_is_idempotent_returns_same_token(registry):
    """Two consecutive enable calls return the same plaintext — the
    Google-Docs UX requires the link to be stable."""
    t = _provision(registry, "alice2")
    a = registry.enable_room_share_link(
        t["tenant_id"], "room_x", t["api_key"],
    )
    b = registry.enable_room_share_link(
        t["tenant_id"], "room_x", t["api_key"],
    )
    assert a["share_token"] == b["share_token"]
    assert a["prefix"] == b["prefix"]


def test_get_returns_plaintext_after_enable(registry):
    """Owner can re-fetch the link any time, no need to re-mint."""
    t = _provision(registry, "alice3")
    a = registry.enable_room_share_link(
        t["tenant_id"], "room_y", t["api_key"],
    )
    fetched = registry.get_room_share_link(t["tenant_id"], "room_y")
    assert fetched is not None
    assert fetched["share_token"] == a["share_token"]


def test_rotate_replaces_token_in_place(registry):
    t = _provision(registry, "alice4")
    a = registry.enable_room_share_link(
        t["tenant_id"], "room_z", t["api_key"],
    )
    b = registry.rotate_room_share_link(
        t["tenant_id"], "room_z", t["api_key"],
    )
    assert b["share_token"] != a["share_token"]
    # there's still exactly one row for this room — the old one is gone
    assert registry.get_room_share_link(
        t["tenant_id"], "room_z",
    )["share_token"] == b["share_token"]


def test_rotate_without_enable_raises(registry):
    t = _provision(registry, "alice5")
    with pytest.raises(KeyError):
        registry.rotate_room_share_link(
            t["tenant_id"], "room_missing", t["api_key"],
        )


def test_disable_hard_deletes_row(registry):
    t = _provision(registry, "alice6")
    registry.enable_room_share_link(
        t["tenant_id"], "room_q", t["api_key"],
    )
    assert registry.disable_room_share_link(t["tenant_id"], "room_q") is True
    assert registry.get_room_share_link(t["tenant_id"], "room_q") is None
    assert registry.disable_room_share_link(t["tenant_id"], "room_q") is False


# ── resolver: hms_ bearer → Caller(role='share') ─────────────────────


def test_resolve_share_token_returns_share_role(registry):
    t = _provision(registry, "alice7")
    out = registry.enable_room_share_link(
        t["tenant_id"], "room_r", t["api_key"],
    )
    caller = registry.resolve_any(out["share_token"])
    assert caller is not None
    assert caller.role == "share"
    assert caller.tenant_id == t["tenant_id"]
    assert caller.constraints == {"room_id": "room_r"}
    assert caller.token_id and len(caller.token_id) == 12


def test_resolve_after_rotate_old_token_dead(registry):
    t = _provision(registry, "alice8")
    a = registry.enable_room_share_link(
        t["tenant_id"], "room_s", t["api_key"],
    )
    registry.rotate_room_share_link(
        t["tenant_id"], "room_s", t["api_key"],
    )
    assert registry.resolve_any(a["share_token"]) is None


def test_resolve_after_disable_token_dead(registry):
    t = _provision(registry, "alice9")
    a = registry.enable_room_share_link(
        t["tenant_id"], "room_t", t["api_key"],
    )
    registry.disable_room_share_link(t["tenant_id"], "room_t")
    assert registry.resolve_any(a["share_token"]) is None


def test_resolve_unknown_share_token_returns_none(registry):
    assert registry.resolve_any(_SHARE_TOKEN_PREFIX + "deadbeef") is None
