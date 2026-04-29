"""Tests for FastAPI server endpoints."""

from __future__ import annotations

import os
import secrets

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql as psql

from hivemind.config import Settings
from hivemind.server import create_app
from hivemind.tenants import TenantRegistry


@pytest.fixture
def test_dsn():
    dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
    return dsn


def _unique_db(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _drop_db(dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                psql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    psql.Identifier(db_name)
                )
            )
    except Exception:
        pass


def _owner_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@pytest_asyncio.fixture
async def server_env(test_dsn):
    """FastAPI client backed by a real tenant registry and tenant DB."""
    control_db = _unique_db("hm_ctrl")
    settings = Settings(
        database_url=test_dsn,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )

    registry = TenantRegistry(settings)
    tenant = registry.provision("server-tests")
    created_dbs = [control_db, tenant["db_name"]]

    app = create_app(settings)
    app.state.registry = registry
    app.state.background_tasks = set()
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client, tenant["api_key"]
    finally:
        try:
            registry.close()
        except Exception:
            pass
        for db_name in created_dbs:
            _drop_db(test_dsn, db_name)


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, server_env):
        client, api_key = server_env
        resp = await client.get("/v1/health", headers=_owner_headers(api_key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "table_count" in data
        assert "version" in data

    @pytest.mark.asyncio
    async def test_health_version_format(self, server_env):
        client, api_key = server_env
        resp = await client.get("/v1/health", headers=_owner_headers(api_key))
        data = resp.json()
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0


class TestAuth:
    @pytest.mark.asyncio
    async def test_owner_endpoint_requires_auth(self, server_env):
        client, _api_key = server_env
        resp = await client.post("/v1/store", json={"sql": "SELECT 1"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_owner_endpoint_rejects_wrong_key(self, server_env):
        client, _api_key = server_env
        resp = await client.post(
            "/v1/store",
            json={"sql": "SELECT 1"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_owner_endpoint_accepts_correct_key(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/store",
            json={"sql": "SELECT 1 AS val"},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_requires_auth(self, server_env):
        client, _api_key = server_env
        resp = await client.get("/v1/health")
        assert resp.status_code == 401


class TestStore:
    @pytest.mark.asyncio
    async def test_store_select(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/store",
            json={"sql": "SELECT 1 AS val"},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rows"] == [{"val": 1}]

    @pytest.mark.asyncio
    async def test_store_create_insert_read(self, server_env):
        client, api_key = server_env
        headers = _owner_headers(api_key)
        resp = await client.post(
            "/v1/store",
            json={
                "sql": (
                    "CREATE TABLE IF NOT EXISTS test_server_data "
                    "(id SERIAL PRIMARY KEY, val TEXT)"
                )
            },
            headers=headers,
        )
        assert resp.status_code == 200

        try:
            resp = await client.post(
                "/v1/store",
                json={
                    "sql": "INSERT INTO test_server_data (val) VALUES (%s)",
                    "params": ["hello"],
                },
                headers=headers,
            )
            assert resp.status_code == 200
            assert resp.json()["rowcount"] == 1

            resp = await client.post(
                "/v1/store",
                json={"sql": "SELECT val FROM test_server_data"},
                headers=headers,
            )
            assert resp.status_code == 200
            assert resp.json()["rows"] == [{"val": "hello"}]
        finally:
            await client.post(
                "/v1/store",
                json={"sql": "DROP TABLE IF EXISTS test_server_data"},
                headers=headers,
            )

    @pytest.mark.asyncio
    async def test_store_empty_body_422(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/store",
            json={},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_store_missing_sql_422(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/store",
            json={"params": [1]},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 422


class TestQuery:
    @pytest.mark.asyncio
    async def test_query_empty_body_422(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/query/run/submit",
            json={},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_query_no_scope_agent_400(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/query/run/submit",
            json={"query": "What happened?"},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 400
        assert "scope_agent_id is required" in resp.json()["detail"]


class TestAdminSchema:
    @pytest.mark.asyncio
    async def test_get_schema(self, server_env):
        client, api_key = server_env
        resp = await client.get(
            "/v1/admin/schema",
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "schema" in data
        assert isinstance(data["schema"], list)


class TestIndex:
    @pytest.mark.asyncio
    async def test_index_empty_body_422(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/index",
            json={},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_index_no_agent_400(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/index",
            json={"data": "Some document text"},
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 400


class TestAgentCRUD:
    @pytest.mark.asyncio
    async def test_list_agents(self, server_env):
        client, api_key = server_env
        resp = await client.get("/v1/agents", headers=_owner_headers(api_key))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent_404(self, server_env):
        client, api_key = server_env
        resp = await client.get(
            "/v1/agents/nonexistent-agent-id",
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent_agent_404(self, server_env):
        client, api_key = server_env
        resp = await client.delete(
            "/v1/agents/nonexistent-agent-id",
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 404
