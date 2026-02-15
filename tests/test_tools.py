"""Tests for the 3 agent tools + agent file inspection tools."""
import json
import sqlite3
import time

import pytest

from hivemind.sandbox.agents import AgentStore
from hivemind.tools import (
    MAX_TOOL_LIST_LIMIT,
    MAX_TOOL_READ_LIMIT,
    MAX_TOOL_SEARCH_LIMIT,
    build_agent_file_tools,
    build_tools,
)


def _setup_records(store):
    """Populate store with test records."""
    t = time.time()
    store.write_record("r1", "alice first doc", {"user": "alice"}, "python ml alice", t)
    store.write_record("r2", "alice second doc", {"user": "alice"}, "python web alice", t + 1)
    store.write_record("r3", "bob's document", {"user": "bob"}, "java web bob", t + 2)
    store.write_record("r4", "anonymous note", {}, "notes misc", t + 3)


class TestSearch:
    def test_search_returns_results(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db)
        search = next(t for t in tools if t.name == "search")
        results = json.loads(search.handler(query="python"))
        assert len(results) >= 1
        assert "id" in results[0]
        assert "metadata" in results[0]
        assert "score" in results[0]

    def test_search_scoped(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, scope=["r1"])
        search = next(t for t in tools if t.name == "search")
        results = json.loads(search.handler(query="python"))
        assert len(results) == 1
        assert results[0]["id"] == "r1"

    def test_search_limit_is_clamped(self, tmp_db):
        t = time.time()
        for i in range(MAX_TOOL_SEARCH_LIMIT + 50):
            tmp_db.write_record(
                f"s{i}",
                f"doc {i}",
                {},
                "common term",
                t + i,
            )
        tools = build_tools(tmp_db)
        search = next(t for t in tools if t.name == "search")
        results = json.loads(search.handler(query="common", limit=999999))
        assert len(results) == MAX_TOOL_SEARCH_LIMIT


class TestRead:
    def test_read_includes_metadata_header(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db)
        read = next(t for t in tools if t.name == "read")
        result = read.handler(record_id="r1")
        assert "record_id: r1" in result
        assert "alice first doc" in result

    def test_read_shows_metadata_keys(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db)
        read = next(t for t in tools if t.name == "read")
        result = read.handler(record_id="r1")
        assert "user: alice" in result

    def test_read_no_header_on_subsequent_chunks(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db)
        read = next(t for t in tools if t.name == "read")
        result = read.handler(record_id="r1", offset=5)
        assert "record_id:" not in result

    def test_read_not_found(self, tmp_db):
        tools = build_tools(tmp_db)
        read = next(t for t in tools if t.name == "read")
        assert read.handler(record_id="nonexistent") == "Record not found"

    def test_read_scoped_out(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, scope=["r2"])
        read = next(t for t in tools if t.name == "read")
        assert read.handler(record_id="r1") == "Record not found"

    def test_read_limit_and_offset_are_clamped(self, tmp_db):
        data = "x" * (MAX_TOOL_READ_LIMIT + 100)
        tmp_db.write_record("long", data, {}, "long data", time.time())
        tools = build_tools(tmp_db)
        read = next(t for t in tools if t.name == "read")

        result = read.handler(record_id="long", offset=-100, limit=10**9)
        first_chunk = result.split("\n\n--- offset", 1)[0]
        # Header is included at offset 0; payload should not exceed clamp.
        assert len(first_chunk) <= MAX_TOOL_READ_LIMIT + 128


class TestList:
    def test_list_returns_records(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db)
        list_tool = next(t for t in tools if t.name == "list")
        results = json.loads(list_tool.handler())
        assert len(results) == 4
        assert "id" in results[0]
        assert "metadata" in results[0]

    def test_list_scoped(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, scope=["r1", "r3"])
        list_tool = next(t for t in tools if t.name == "list")
        results = json.loads(list_tool.handler())
        assert len(results) == 2
        ids = {r["id"] for r in results}
        assert ids == {"r1", "r3"}

    def test_list_limit_is_clamped(self, tmp_db):
        t = time.time()
        for i in range(MAX_TOOL_LIST_LIMIT + 50):
            tmp_db.write_record(
                f"l{i}",
                f"doc {i}",
                {},
                f"index {i}",
                t + i,
            )
        tools = build_tools(tmp_db)
        list_tool = next(t for t in tools if t.name == "list")
        results = json.loads(list_tool.handler(limit=10**9))
        assert len(results) == MAX_TOOL_LIST_LIMIT


class TestToolSchemas:
    def test_three_tools(self, tmp_db):
        tools = build_tools(tmp_db)
        assert len(tools) == 3
        names = {t.name for t in tools}
        assert names == {"search", "read", "list"}

    def test_openai_format(self, tmp_db):
        tools = build_tools(tmp_db)
        for tool in tools:
            d = tool.to_openai_def()
            assert d["type"] == "function"
            assert "name" in d["function"]
            assert "parameters" in d["function"]


# ── Agent file inspection tools ──


@pytest.fixture
def agent_store_with_files():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = AgentStore(conn)

    from hivemind.sandbox.models import AgentConfig

    store.create(AgentConfig(
        agent_id="qa-1",
        name="Query Agent",
        image="myorg/qa:v1",
    ))
    store.save_files("qa-1", {
        "agent.py": "import httpx\nprint('hello')\n",
        "lib/utils.py": "def helper(): pass\n",
    })
    return store


class TestAgentFileTools:
    def test_list_files(self, agent_store_with_files):
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        list_tool = next(t for t in tools if t.name == "list_query_agent_files")
        result = json.loads(list_tool.handler())
        assert len(result["files"]) == 2

    def test_read_file(self, agent_store_with_files):
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")
        content = read_tool.handler(file_path="agent.py")
        assert "import httpx" in content

    def test_read_file_not_found(self, agent_store_with_files):
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")
        result = read_tool.handler(file_path="nonexistent.py")
        assert "not found" in result.lower()

    def test_tools_are_prescoped(self, agent_store_with_files):
        from hivemind.sandbox.models import AgentConfig

        agent_store_with_files.create(AgentConfig(
            agent_id="qa-other", name="Other", image="myorg/other:v1",
        ))
        agent_store_with_files.save_files("qa-other", {"secret.py": "SECRET"})

        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")
        result = read_tool.handler(file_path="secret.py")
        assert "not found" in result.lower()
