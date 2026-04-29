"""Tests for /v1/room-agents/{id}/attest.

Exercises the FastAPI server end-to-end (via ASGI transport, no real
network) so we cover the auth dispatch, the role gate, the file digest
shape, and the image_digest fail-soft behaviour.

Postgres-backed (re-uses the live DB the rest of the tenants suite
needs); skips when ``HIVEMIND_TEST_DATABASE_URL`` is unreachable.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import asynccontextmanager

import httpx
import psycopg
import pytest
import pytest_asyncio

from hivemind.config import Settings
from hivemind.sandbox.models import AgentConfig
from hivemind.server import _image_digest, create_app
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


def _digest(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(files[path].encode("utf-8", errors="replace"))
        h.update(b"\0")
    return h.hexdigest()


@pytest_asyncio.fixture
async def app_and_registry():
    """Bring up create_app() with a per-test control DB + tenant registry.

    Bypasses lifespan (which would also try to bootstrap the agent-base
    image and dstack attestation — neither is needed here) and wires the
    registry onto app.state directly.
    """
    control_db = _unique("hm_attest")
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
    created_dbs: list[str] = [control_db]

    app = create_app(settings)
    app.state.registry = registry

    yield app, registry, created_dbs

    try:
        registry.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


@asynccontextmanager
async def _client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


def _seed_agent(
    registry: TenantRegistry,
    tenant_id: str,
    agent_id: str,
    files: dict[str, str],
    *,
    name: str = "scope-test",
    image: str = "hivemind-attest-test:latest",
    agent_type: str = "scope",
) -> None:
    """Insert an AgentConfig + extracted files directly via the per-tenant store."""
    hive = registry.for_tenant(tenant_id)
    assert hive is not None
    hive.agent_store.create(
        AgentConfig(
            agent_id=agent_id,
            name=name,
            description="test fixture",
            agent_type=agent_type,
            image=image,
            entrypoint=None,
            memory_mb=256,
            max_llm_calls=10,
            max_tokens=10_000,
            timeout_seconds=60,
        )
    )
    if files:
        hive.agent_store.save_files(agent_id, files)


# ── _image_digest helper ───────────────────────────────────────────────


def test_image_digest_missing_image_returns_empty():
    """No Docker daemon / unknown image → fail-soft empty result."""
    out = _image_digest("definitely-not-a-real-image:does-not-exist-99999")
    assert out == {"id": "", "repo_digests": []} or (
        out["id"] == "" and out["repo_digests"] == []
    )


# ── /v1/room-agents/{id}/attest ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_attest_any_agent(app_and_registry):
    app, registry, created = app_and_registry
    t = registry.provision("alpha")
    created.append(t["db_name"])

    files = {"Dockerfile": "FROM python:3.12-slim\n", "agent.py": "print('x')\n"}
    _seed_agent(registry, t["tenant_id"], "agent_alpha", files)
    _seed_agent(
        registry, t["tenant_id"], "agent_beta", {"main.py": "noop\n"},
        name="other", agent_type="query",
    )

    async with _client(app) as c:
        r = await c.get(
            "/v1/room-agents/agent_alpha/attest",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == "agent_alpha"
        assert body["files_count"] == 2
        assert body["files_digest_sha256"] == _digest(files)
        # image_digest is fail-soft (image isn't loaded in test daemon)
        assert "image_digest" in body
        assert set(body["image_digest"].keys()) == {"id", "repo_digests"}
        assert "attestation" in body
        # Owner can also attest the second agent.
        r2 = await c.get(
            "/v1/room-agents/agent_beta/attest",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert r2.status_code == 200
        assert r2.json()["agent_id"] == "agent_beta"


@pytest.mark.asyncio
async def test_query_token_can_only_attest_bound_agent(app_and_registry):
    app, registry, created = app_and_registry
    t = registry.provision("beta")
    created.append(t["db_name"])

    bound = "scope_bound"
    other = "scope_other"
    _seed_agent(registry, t["tenant_id"], bound, {"a.py": "1\n"})
    _seed_agent(registry, t["tenant_id"], other, {"a.py": "2\n"})

    qtoken = registry.mint_capability(
        t["tenant_id"], "query", "v", {"scope_agent_id": bound}
    )["token"]

    async with _client(app) as c:
        r_ok = await c.get(
            f"/v1/room-agents/{bound}/attest",
            headers={"Authorization": f"Bearer {qtoken}"},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["agent_id"] == bound

        r_404 = await c.get(
            f"/v1/room-agents/{other}/attest",
            headers={"Authorization": f"Bearer {qtoken}"},
        )
        assert r_404.status_code == 404


@pytest.mark.asyncio
async def test_unknown_agent_returns_404(app_and_registry):
    app, registry, created = app_and_registry
    t = registry.provision("delta")
    created.append(t["db_name"])
    async with _client(app) as c:
        r = await c.get(
            "/v1/room-agents/does_not_exist/attest",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_files_digest_is_stable_byte_for_byte(app_and_registry):
    """Sanity-check: re-fetching files + recomputing matches the server digest."""
    app, registry, created = app_and_registry
    t = registry.provision("epsilon")
    created.append(t["db_name"])
    files = {
        "Dockerfile": "FROM scratch\n",
        "agent.py": "import os\nprint(os.environ)\n",
        "lib/util.py": "def f(): pass\n",
    }
    _seed_agent(registry, t["tenant_id"], "agent_x", files)
    async with _client(app) as c:
        r = await c.get(
            "/v1/room-agents/agent_x/attest",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert r.status_code == 200
        server_digest = r.json()["files_digest_sha256"]

        # Re-fetch each file via /v1/room-agents/{id}/files{,/{path}} and recompute.
        rl = await c.get(
            "/v1/room-agents/agent_x/files",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert rl.status_code == 200
        refetched: dict[str, str] = {}
        for entry in rl.json()["files"]:
            rf = await c.get(
                f"/v1/room-agents/agent_x/files/{entry['path']}",
                headers={"Authorization": f"Bearer {t['api_key']}"},
            )
            assert rf.status_code == 200
            refetched[entry["path"]] = rf.text
        assert _digest(refetched) == server_digest


# ── sealed inspection_mode ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sealed_agent_files_endpoint_returns_403(app_and_registry):
    """A sealed-mode agent's plaintext source must NOT be readable via
    /v1/room-agents/{id}/files/{path} — even by the room owner. Image digest,
    file path list, and attested digest stay inspectable."""
    app, registry, created = app_and_registry
    t = registry.provision("sealed_a")
    created.append(t["db_name"])
    registry.resolve_any(t["api_key"])

    hive = registry.for_tenant(t["tenant_id"])
    hive.agent_store.create(
        AgentConfig(
            agent_id="agent_private",
            name="sealed-demo",
            description="",
            agent_type="query",
            image="hivemind/sealed-demo:latest",
            entrypoint=None,
            memory_mb=64,
            max_llm_calls=1,
            max_tokens=1,
            timeout_seconds=10,
            inspection_mode="sealed",
        )
    )
    hive.agent_store.save_files(
        "agent_private",
        {"main.py": "print('SECRET_TOKEN_42')\n"},
        inspection_mode="sealed",
    )

    async with _client(app) as c:
        rl = await c.get(
            "/v1/room-agents/agent_private/files",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert rl.status_code == 200
        assert any(f["path"] == "main.py" for f in rl.json()["files"])

        rf = await c.get(
            "/v1/room-agents/agent_private/files/main.py",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert rf.status_code == 403, rf.text
        assert "sealed" in rf.text.lower()
        assert "SECRET_TOKEN_42" not in rf.text

        ra = await c.get(
            "/v1/room-agents/agent_private/attest",
            headers={"Authorization": f"Bearer {t['api_key']}"},
        )
        assert ra.status_code == 200
        body = ra.json()
        assert body["inspection_mode"] == "sealed"
        assert body["files_count"] == 1
        assert body["files_digest_sha256"]
        assert body["agent"]["inspection_mode"] == "sealed"
