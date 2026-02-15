"""Tests for Hivemind core integration with store and pipeline."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import hivemind.core as core_module
from hivemind.config import Settings
from hivemind.core import Hivemind
from hivemind.models import StoreRequest
from hivemind.version import APP_VERSION


@pytest.fixture
def hivemind(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        llm_api_key="test",
    )
    hm = Hivemind(settings)
    yield hm
    hm.store.close()


class TestHivemindHealth:
    def test_health_returns_status(self, hivemind):
        health = hivemind.health()
        assert health["status"] == "ok"
        assert health["version"] == APP_VERSION
        assert health["record_count"] == 0

    def test_health_reflects_record_count(self, hivemind):
        import time
        hivemind.store.write_record(
            id="r1", data="data", metadata={},
            index_text="test", created_at=time.time(),
        )
        health = hivemind.health()
        assert health["record_count"] == 1


class TestHivemindComponents:
    def test_store_is_accessible(self, hivemind):
        assert hivemind.store is not None

    def test_agent_store_is_accessible(self, hivemind):
        assert hivemind.agent_store is not None

    def test_pipeline_is_accessible(self, hivemind):
        assert hivemind.pipeline is not None


class TestDefaultAgentAutoload:
    def test_autoload_registers_stable_defaults(self, tmp_path, monkeypatch):
        calls: list[str] = []

        class FakeRunner:
            def __init__(self, settings):
                self.settings = settings

            def cleanup_stale_containers(self):
                return None

            def image_exists(self, image):
                return True

            def extract_image_files(self, image, **kwargs):
                calls.append(image)
                return {"agent.py": f"# {image}"}

        monkeypatch.setattr("hivemind.core.DockerRunner", FakeRunner)

        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
            autoload_default_agents=True,
            default_index_image="img/default-index:v1",
            default_scope_image="img/default-scope:v1",
            default_query_image="img/default-query:v1",
            max_llm_calls=77,
            max_tokens=222_222,
            agent_timeout=456,
        )
        hm = Hivemind(settings)
        try:
            assert settings.default_index_agent == "default-index"
            assert settings.default_scope_agent == "default-scope"
            assert settings.default_query_agent == "default-query"

            assert hm.agent_store.get("default-index").image == "img/default-index:v1"
            assert hm.agent_store.get("default-scope").image == "img/default-scope:v1"
            assert hm.agent_store.get("default-query").image == "img/default-query:v1"
            assert hm.agent_store.get("default-index").max_llm_calls == 77
            assert hm.agent_store.get("default-index").max_tokens == 222_222
            assert hm.agent_store.get("default-index").timeout_seconds == 456

            assert len(hm.agent_store.list_file_paths("default-index")) == 1
            assert len(hm.agent_store.list_file_paths("default-scope")) == 1
            assert len(hm.agent_store.list_file_paths("default-query")) == 1
            assert set(calls) == {
                "img/default-index:v1",
                "img/default-scope:v1",
                "img/default-query:v1",
            }
        finally:
            hm.store.close()

    def test_autoload_disabled_does_not_register(self, tmp_path, monkeypatch):
        runner = MagicMock()
        monkeypatch.setattr("hivemind.core.DockerRunner", runner)

        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
            autoload_default_agents=False,
            default_index_agent="default-index",
            default_index_image="img/default-index:v1",
        )
        hm = Hivemind(settings)
        try:
            assert hm.agent_store.get("default-index") is None
            runner.assert_called_once()  # cleanup runner only
        finally:
            hm.store.close()

    def test_autoload_fails_fast_when_default_image_missing(self, tmp_path, monkeypatch):
        class FakeRunner:
            def __init__(self, settings):
                self.settings = settings

            def cleanup_stale_containers(self):
                return None

            def image_exists(self, image):
                return False

        monkeypatch.setattr("hivemind.core.DockerRunner", FakeRunner)

        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
            autoload_default_agents=True,
            default_query_image="missing:image",
        )
        with pytest.raises(RuntimeError, match="image not found"):
            Hivemind(settings)


class TestStoreRequest:
    @pytest.mark.asyncio
    async def test_store_with_index_text(self, hivemind):
        req = StoreRequest(
            data="The team decided to use Stripe.",
            metadata={"author": "alice"},
            index_text="payment stripe migration",
        )
        resp = await hivemind.pipeline.run_store(req)
        assert resp.record_id
        assert resp.metadata["author"] == "alice"

        record = hivemind.store.read(resp.record_id)
        assert record["data"] == "The team decided to use Stripe."
        assert record["index_text"] == "payment stripe migration"

    @pytest.mark.asyncio
    async def test_store_without_index(self, hivemind):
        req = StoreRequest(data="Plain data", metadata={"type": "note"})
        resp = await hivemind.pipeline.run_store(req)
        assert resp.record_id

        record = hivemind.store.read(resp.record_id)
        assert record["data"] == "Plain data"
        assert record["index_text"] is None


@pytest.mark.asyncio
async def test_hivemind_close_closes_llm_client_and_store(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        llm_api_key="test",
    )
    hm = Hivemind(settings)

    original_store_close = hm.store.close
    hm.store.close = MagicMock()
    hm.pipeline.llm_client = AsyncMock()

    try:
        await hm.close()
    finally:
        # Ensure the real connection is released for the test process.
        original_store_close()

    hm.pipeline.llm_client.close.assert_awaited_once()
    hm.store.close.assert_called_once()


def test_hivemind_init_failure_closes_store(tmp_path, monkeypatch):
    close_calls = {"count": 0}
    original_close = core_module.RecordStore.close

    def tracking_close(self):
        close_calls["count"] += 1
        return original_close(self)

    def raise_bootstrap_error(self):
        raise RuntimeError("bootstrap exploded")

    monkeypatch.setattr(core_module.RecordStore, "close", tracking_close)
    monkeypatch.setattr(
        core_module.Hivemind,
        "_bootstrap_default_agents",
        raise_bootstrap_error,
    )

    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        llm_api_key="test",
    )
    with pytest.raises(RuntimeError, match="bootstrap exploded"):
        Hivemind(settings)

    assert close_calls["count"] == 1
