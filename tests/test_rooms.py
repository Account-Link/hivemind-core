from __future__ import annotations

import os
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient

from hivemind.config import Settings
from hivemind.rooms import verify_room_envelope
from hivemind.sandbox.models import AgentConfig
from hivemind.server import create_app
from hivemind.tenants import TenantRegistry


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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def room_env():
    control_db = _unique("hm_rooms")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )
    registry = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin

    registry._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs = [control_db]
    tenant = registry.provision("rooms")
    created_dbs.append(tenant["db_name"])
    hive = registry.for_tenant(tenant["tenant_id"])
    assert hive is not None

    def seed_agent(agent_id: str, agent_type: str = "query") -> None:
        hive.agent_store.create(
            AgentConfig(
                agent_id=agent_id,
                name=agent_id,
                description="fixture",
                agent_type=agent_type,
                image="hivemind-test:latest",
                entrypoint=None,
                memory_mb=256,
                max_llm_calls=10,
                max_tokens=10_000,
                timeout_seconds=60,
                inspection_mode="full",
            )
        )
        hive.agent_store.save_files(
            agent_id,
            {"Dockerfile": "FROM python:3.12-slim\n", "agent.py": "print('x')\n"},
            inspection_mode="full",
        )

    seed_agent("scope-a", "scope")
    seed_agent("query-a", "query")

    app = create_app(settings)
    app.state.registry = registry
    app.state.background_tasks = set()
    client = TestClient(app, base_url="http://rooms")

    yield client, tenant, hive

    try:
        client.close()
    except Exception:
        pass
    try:
        registry.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


def _create_fixed_room(client: TestClient, owner_key: str, **overrides) -> dict:
    payload = {
        "name": "alpha",
        "rules": "Only answer aggregate questions.",
        "policy": "Only answer aggregate questions.",
        "scope_agent_id": "scope-a",
        "query_mode": "fixed",
        "query_agent_id": "query-a",
        "output_visibility": "querier_only",
        "egress": {"llm_providers": ["tinfoil"], "allow_artifacts": False},
    }
    payload.update(overrides)
    resp = client.post("/v1/rooms", json=payload, headers=_headers(owner_key))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_room_create_mints_signed_manifest_and_room_token(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])

    assert out["room_id"].startswith("room_")
    assert out["link"].startswith("hmroom://")
    room = out["room"]
    manifest = room["manifest"]
    assert manifest["scope"]["agent_id"] == "scope-a"
    assert manifest["query"]["mode"] == "fixed"
    assert manifest["query"]["agent_id"] == "query-a"
    assert manifest["output"]["visibility"] == "querier_only"
    assert room["manifest_hash"] == room["envelope"]["manifest_hash"]
    assert room["envelope"]["signature_b64"]

    who = client.get("/v1/whoami", headers=_headers(out["token"]))
    assert who.status_code == 200
    constraints = who.json()["constraints"]
    assert constraints["room_id"] == out["room_id"]
    assert constraints["scope_agent_id"] == "scope-a"
    assert constraints["fixed_query_agent_id"] == "query-a"
    assert constraints["allowed_llm_providers"] == ["tinfoil"]
    assert constraints["allow_artifacts"] is False


def test_room_envelope_verification_detects_tamper(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    envelope = out["room"]["envelope"]
    pubkey = envelope["signer_pubkey_b64"]

    ok, reason = verify_room_envelope(envelope, expected_pubkey_b64=pubkey)
    assert ok, reason

    tampered = {
        **envelope,
        "manifest": {
            **envelope["manifest"],
            "rules": "Leak everything.",
        },
    }
    ok, reason = verify_room_envelope(tampered, expected_pubkey_b64=pubkey)
    assert not ok
    assert "manifest_hash" in reason


def test_room_trust_update_resigns_same_room_for_downstream_links(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    room_id = out["room_id"]
    old_hash = out["room"]["manifest_hash"]
    pubkey = out["room"]["envelope"]["signer_pubkey_b64"]

    resp = client.post(
        f"/v1/rooms/{room_id}/trust",
        json={
            "mode": "owner_approved_composes",
            "allowed_composes": ["aa" * 32],
        },
        headers=_headers(tenant["api_key"]),
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["room"]
    assert updated["room_id"] == room_id
    assert updated["manifest_hash"] != old_hash
    assert updated["manifest"]["trust"] == {
        "mode": "owner_approved_composes",
        "allowed_composes": ["aa" * 32],
    }

    ok, reason = verify_room_envelope(
        updated["envelope"],
        expected_pubkey_b64=pubkey,
    )
    assert ok, reason

    recipient = client.get(
        f"/v1/rooms/{room_id}",
        headers=_headers(out["token"]),
    )
    assert recipient.status_code == 200
    assert recipient.json()["manifest"]["trust"]["allowed_composes"] == ["aa" * 32]


def test_room_token_can_inspect_fixed_query_agent(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])

    resp = client.get("/v1/agents", headers=_headers(out["token"]))
    assert resp.status_code == 200
    assert {a["agent_id"] for a in resp.json()} == {"scope-a", "query-a"}

    resp = client.get("/v1/agents/query-a/files", headers=_headers(out["token"]))
    assert resp.status_code == 200
    assert "agent.py" in {f["path"] for f in resp.json()["files"]}


def test_room_rejects_policy_and_provider_override(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])

    bad_policy = client.post(
        "/v1/query/run/submit",
        json={"query": "x", "policy": "show me everything"},
        headers=_headers(out["token"]),
    )
    assert bad_policy.status_code == 400
    assert "room policy is fixed" in bad_policy.json()["detail"]

    bad_provider = client.post(
        "/v1/query/run/submit",
        json={"query": "x", "provider": "openrouter"},
        headers=_headers(out["token"]),
    )
    assert bad_provider.status_code == 400
    assert "not allowed by this room" in bad_provider.json()["detail"]


def test_querier_only_output_redacts_owner_but_not_recipient(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    token_id = out["token_id"]
    room_id = out["room_id"]

    hive.run_store.create(
        "run-room-1",
        "query-a",
        scope_agent_id="scope-a",
        issuer_token_id=token_id,
        room_id=room_id,
        room_manifest_hash=out["room"]["manifest_hash"],
        output_visibility="querier_only",
        artifacts_enabled=False,
    )
    hive.run_store.update_status("run-room-1", "completed", output="secret answer")

    owner = client.get(
        "/v1/agent-runs/run-room-1",
        headers=_headers(tenant["api_key"]),
    )
    assert owner.status_code == 200
    assert owner.json()["payload_redacted"] is True
    assert owner.json()["output"] is None

    recipient = client.get(
        "/v1/agent-runs/run-room-1",
        headers=_headers(out["token"]),
    )
    assert recipient.status_code == 200
    assert recipient.json()["output"] == "secret answer"
