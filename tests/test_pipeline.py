"""Tests for the Pipeline orchestrator with mocked Docker."""
import json
import time
from unittest.mock import AsyncMock

import pytest

import hivemind.pipeline as pipeline_module
from hivemind.config import Settings
from hivemind.core import Hivemind
from hivemind.models import QueryRequest, StoreRequest
from hivemind.sandbox.models import AgentConfig


@pytest.fixture
def hivemind(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        llm_api_key="test",
    )
    hm = Hivemind(settings)
    yield hm
    hm.store.close()


class TestRunStore:
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

        # Verify stored
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
    async def test_store_index_agent_not_found(self, hivemind):
        req = StoreRequest(
            data="Data",
            index_agent_id="nonexistent",
        )
        with pytest.raises(ValueError, match="not found"):
            await hivemind.pipeline.run_store(req)

    @pytest.mark.asyncio
    async def test_store_rejects_non_string_index_text_from_index_agent(
        self, tmp_path, monkeypatch
    ):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.agent_store.create(
                AgentConfig(
                    agent_id="idx1",
                    name="Index Agent",
                    image="img:index",
                )
            )

            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    pass

                async def run(self, **kwargs):
                    return json.dumps({"index_text": {"bad": True}})

            monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

            req = StoreRequest(data="doc", index_agent_id="idx1")
            with pytest.raises(ValueError, match="index_text must be a string or null"):
                await hm.pipeline.run_store(req)
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_store_empty_index_text_skips_index_agent(self, hivemind):
        req = StoreRequest(
            data="Data",
            index_text="",
            index_agent_id="nonexistent",
        )
        resp = await hivemind.pipeline.run_store(req)
        record = hivemind.store.read(resp.record_id)
        assert record["index_text"] == ""


class TestRunQuery:
    @pytest.mark.asyncio
    async def test_query_requires_agent(self, hivemind):
        req = QueryRequest(prompt="What happened?")
        with pytest.raises(ValueError, match="No query agent"):
            await hivemind.pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_query_agent_not_found(self, hivemind):
        req = QueryRequest(prompt="What?", query_agent_id="nonexistent")
        with pytest.raises(ValueError, match="not found"):
            await hivemind.pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_scope_agent_not_found(self, hivemind):
        req = QueryRequest(
            prompt="What?",
            query_agent_id="q1",
            scope_agent_id="nonexistent",
        )
        with pytest.raises(ValueError, match="not found"):
            await hivemind.pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_scope_agent_rejects_non_string_record_ids(self, tmp_path, monkeypatch):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.agent_store.create(AgentConfig(
                agent_id="scope1",
                name="Scope Agent",
                image="img:scope",
            ))

            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    pass

                async def run(self, **kwargs):
                    return json.dumps({"record_ids": ["r1", {"bad": True}]}), {
                        "total_tokens": 0
                    }

            monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

            req = QueryRequest(
                prompt="What?",
                query_agent_id="q1",
                scope_agent_id="scope1",
            )
            with pytest.raises(ValueError, match=r"record_ids\[1\] must be a string"):
                await hm.pipeline._run_scope_agent(req, max_tokens=1000)
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_scope_agent_deduplicates_and_strips_record_ids(self, tmp_path, monkeypatch):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.agent_store.create(AgentConfig(
                agent_id="scope1",
                name="Scope Agent",
                image="img:scope",
            ))

            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    pass

                async def run(self, **kwargs):
                    return json.dumps({"record_ids": [" r1 ", "r1", "r2"]}), {
                        "total_tokens": 0
                    }

            monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

            req = QueryRequest(
                prompt="What?",
                query_agent_id="q1",
                scope_agent_id="scope1",
            )
            record_ids, _ = await hm.pipeline._run_scope_agent(req, max_tokens=1000)
            assert record_ids == ["r1", "r2"]
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_explicit_scope_not_overridden_by_default_scope_agent(
        self, tmp_path
    ):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
            default_scope_agent="scope-default",
        )
        hm = Hivemind(settings)
        try:
            hm.pipeline._run_scope_agent = AsyncMock(
                return_value=(["from-agent"], {"total_tokens": 0})
            )
            hm.pipeline._run_query_agent = AsyncMock(
                return_value=("output", [], {"total_tokens": 0})
            )

            req = QueryRequest(
                prompt="What?",
                query_agent_id="q1",
                scope=["r1", "r2"],
            )
            await hm.pipeline.run_query(req)

            hm.pipeline._run_scope_agent.assert_not_called()
            _, kwargs = hm.pipeline._run_query_agent.await_args
            assert kwargs["scope"] == ["r1", "r2"]
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_default_scope_agent_used_when_scope_omitted(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
            default_scope_agent="scope-default",
        )
        hm = Hivemind(settings)
        try:
            hm.pipeline._run_scope_agent = AsyncMock(
                return_value=(["r9"], {"total_tokens": 0})
            )
            hm.pipeline._run_query_agent = AsyncMock(
                return_value=("output", [], {"total_tokens": 0})
            )

            req = QueryRequest(prompt="What?", query_agent_id="q1")
            await hm.pipeline.run_query(req)

            hm.pipeline._run_scope_agent.assert_awaited_once()
            _, kwargs = hm.pipeline._run_query_agent.await_args
            assert kwargs["scope"] == ["r9"]
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_explicit_scope_agent_takes_precedence_over_scope(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.pipeline._run_scope_agent = AsyncMock(
                return_value=(["from-agent"], {"total_tokens": 0})
            )
            hm.pipeline._run_query_agent = AsyncMock(
                return_value=("output", [], {"total_tokens": 0})
            )

            req = QueryRequest(
                prompt="What?",
                query_agent_id="q1",
                scope=["r1"],
                scope_agent_id="scope-explicit",
            )
            await hm.pipeline.run_query(req)

            hm.pipeline._run_scope_agent.assert_awaited_once()
            _, kwargs = hm.pipeline._run_query_agent.await_args
            assert kwargs["scope"] == ["from-agent"]
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_explicit_scope_is_rejected_when_too_large(self, tmp_path, monkeypatch):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            monkeypatch.setattr(pipeline_module, "MAX_SCOPE_RECORD_IDS", 2)
            req = QueryRequest(
                query="What?",
                query_agent_id="q1",
                scope=["r1", "r2", "r3"],
            )
            with pytest.raises(ValueError, match="scope exceeds maximum size"):
                await hm.pipeline.run_query(req)
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_scope_agent_output_is_rejected_when_too_large(self, tmp_path, monkeypatch):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.agent_store.create(
                AgentConfig(
                    agent_id="scope1",
                    name="Scope Agent",
                    image="img:scope",
                )
            )

            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    pass

                async def run(self, **kwargs):
                    return json.dumps({"record_ids": ["r1", "r2", "r3"]}), {
                        "total_tokens": 0
                    }

            monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)
            monkeypatch.setattr(pipeline_module, "MAX_SCOPE_RECORD_IDS", 2)

            req = QueryRequest(
                query="What?",
                query_agent_id="q1",
                scope_agent_id="scope1",
            )
            with pytest.raises(ValueError, match="scope exceeds maximum size"):
                await hm.pipeline._run_scope_agent(req, max_tokens=1000)
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_query_tracks_records_accessed_deterministically(
        self, tmp_path, monkeypatch
    ):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.store.write_record(
                id="r1",
                data="doc",
                metadata={},
                index_text="python",
                created_at=time.time(),
            )
            hm.agent_store.create(AgentConfig(
                agent_id="q1",
                name="Query Agent",
                image="img:v1",
            ))

            class FakeBackend:
                def __init__(self, *args, **kwargs):
                    pass

                async def run(self, **kwargs):
                    on_tool_call = kwargs["on_tool_call"]
                    await on_tool_call("search", {"query": "python"})
                    await on_tool_call("list", {"limit": 20, "offset": 0})
                    await on_tool_call("read", {"record_id": "r1"})
                    await on_tool_call("read", {"record_id": "r1"})
                    return "final answer"

            monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

            output, records_accessed = await hm.pipeline._run_query_agent(
                query_agent_id="q1",
                prompt="What?",
            )
            assert output == "final answer"
            assert records_accessed == ["r1"]
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_mediator_budget_is_reserved_for_query_agent(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.pipeline._run_query_agent = AsyncMock(
                return_value=("output", [], {"total_tokens": 0})
            )
            hm.pipeline._run_mediator_agent = AsyncMock(
                return_value=("mediated", {"total_tokens": 0})
            )

            req = QueryRequest(
                query="What?",
                query_agent_id="q1",
                mediator_agent_id="med1",
                max_tokens=1000,
            )
            await hm.pipeline.run_query(req)

            _, kwargs = hm.pipeline._run_query_agent.await_args
            assert kwargs["max_tokens"] == 488
            hm.pipeline._run_mediator_agent.assert_awaited_once()
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_mediator_failure_does_not_fail_query(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.pipeline._run_query_agent = AsyncMock(
                return_value=("raw output", ["r1"], {"total_tokens": 42})
            )
            hm.pipeline._run_mediator_agent = AsyncMock(
                side_effect=ValueError("mediator failed")
            )

            req = QueryRequest(
                query="What?",
                query_agent_id="q1",
                mediator_agent_id="med1",
            )
            resp = await hm.pipeline.run_query(req)

            assert resp.output == "raw output"
            assert resp.mediated is False
            assert resp.records_accessed == ["r1"]
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_mediator_agent_not_found_still_raises(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.pipeline._run_query_agent = AsyncMock(
                return_value=("raw output", ["r1"], {"total_tokens": 0})
            )
            req = QueryRequest(
                query="What?",
                query_agent_id="q1",
                mediator_agent_id="missing-mediator",
            )
            with pytest.raises(ValueError, match="Mediator agent 'missing-mediator' not found"):
                await hm.pipeline.run_query(req)
        finally:
            hm.store.close()

    @pytest.mark.asyncio
    async def test_mediator_is_skipped_when_remaining_budget_is_too_low(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "test.db"),
            llm_api_key="test",
        )
        hm = Hivemind(settings)
        try:
            hm.pipeline._run_query_agent = AsyncMock(
                return_value=("raw output", ["r1"], {"total_tokens": 49})
            )
            hm.pipeline._run_mediator_agent = AsyncMock(
                return_value=("mediated", {"total_tokens": 1})
            )

            req = QueryRequest(
                query="What?",
                query_agent_id="q1",
                mediator_agent_id="med1",
                max_tokens=50,
            )
            resp = await hm.pipeline.run_query(req)

            hm.pipeline._run_mediator_agent.assert_not_called()
            assert resp.output == "raw output"
            assert resp.mediated is False
            assert resp.usage["total_tokens"] == 49
            assert resp.usage["max_tokens"] == 50
        finally:
            hm.store.close()


class TestQueryRequestModel:
    def test_scope_is_list_or_none(self):
        req = QueryRequest(prompt="test", scope=["r1", "r2"])
        assert req.scope == ["r1", "r2"]

    def test_scope_defaults_to_none(self):
        req = QueryRequest(prompt="test")
        assert req.scope is None

    def test_mediator_agent_id(self):
        req = QueryRequest(prompt="test", mediator_agent_id="med1")
        assert req.mediator_agent_id == "med1"

    def test_query_field(self):
        req = QueryRequest(query="What happened?")
        assert req.query == "What happened?"

    def test_prompt_backward_compat(self):
        req = QueryRequest(prompt="What happened?")
        assert req.query == "What happened?"

    def test_query_wins_over_prompt(self):
        req = QueryRequest(query="canonical", prompt="deprecated")
        assert req.query == "canonical"

    def test_neither_query_nor_prompt_raises(self):
        with pytest.raises(ValueError, match="'query'.*required"):
            QueryRequest()

    def test_max_tokens(self):
        req = QueryRequest(query="test", max_tokens=50000)
        assert req.max_tokens == 50000

    def test_max_tokens_defaults_to_none(self):
        req = QueryRequest(query="test")
        assert req.max_tokens is None

    def test_max_tokens_must_be_positive(self):
        with pytest.raises(Exception):
            QueryRequest(query="test", max_tokens=0)
