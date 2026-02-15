from unittest.mock import patch

import pytest
import pytest_asyncio
import httpx

from hivemind.sandbox.bridge import BridgeServer
from hivemind.sandbox.budget import Budget
from hivemind.tools import Tool


def _make_tools():
    """Create mock tools for testing."""

    def search(query: str, limit: int = 20) -> str:
        return f'[{{"id": "r1", "metadata": {{}}, "query": "{query}"}}]'

    def read(record_id: str) -> str:
        if record_id == "r1":
            return "This is the record data for testing."
        return "Record not found"

    return [
        Tool(
            name="search",
            description="Search records",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=search,
        ),
        Tool(
            name="read",
            description="Read a record",
            parameters={
                "type": "object",
                "properties": {"record_id": {"type": "string"}},
                "required": ["record_id"],
            },
            handler=read,
        ),
    ]


async def _mock_llm_caller(messages, max_tokens, model=None, temperature=None, top_p=None, **kwargs):
    result = {
        "content": f"LLM response. model={model}, temp={temperature}",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "finish_reason": "stop",
    }
    if kwargs.get("tools"):
        result["tool_calls"] = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"location":"SF"}'},
            }
        ]
        result["finish_reason"] = "tool_calls"
    return result


async def _mock_on_tool_call(name, args):
    tools = {t.name: t.handler for t in _make_tools()}
    if name not in tools:
        return f"Error: unknown tool '{name}'"
    return tools[name](**args)


@pytest_asyncio.fixture
async def bridge():
    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="test-token-123",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    yield server, client, budget

    await client.aclose()
    await server.stop()


@pytest.mark.asyncio
async def test_health(bridge):
    server, client, budget = bridge
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "budget" in data


@pytest.mark.asyncio
async def test_auth_required(bridge):
    server, client, budget = bridge
    resp = await client.get("/tools")
    assert resp.status_code == 401

    resp = await client.get(
        "/tools", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401

    resp = await client.get(
        "/tools", headers={"Authorization": "Bearer test-token-123"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_tools(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.get("/tools", headers=headers)
    assert resp.status_code == 200
    tools = resp.json()
    assert len(tools) == 2
    names = {t["function"]["name"] for t in tools}
    assert names == {"search", "read"}


@pytest.mark.asyncio
async def test_llm_chat(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "LLM response" in data["content"]
    assert budget.summary()["calls"] == 1


@pytest.mark.asyncio
async def test_llm_chat_model_override(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "anthropic/claude-haiku-4.5",
            "temperature": 0.7,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "claude-haiku" in data["content"]
    assert "0.7" in data["content"]


@pytest.mark.asyncio
async def test_llm_chat_rejects_excessive_max_tokens(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 20000,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_tool_call(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/tools/search",
        headers=headers,
        json={"arguments": {"query": "test query"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "r1" in data["result"]
    assert data["error"] is None


@pytest.mark.asyncio
async def test_tool_call_unknown(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/tools/nonexistent_tool",
        headers=headers,
        json={"arguments": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] is not None
    assert "Unknown tool" in data["error"]


@pytest.mark.asyncio
async def test_budget_enforcement(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}

    budget.max_calls = 2
    budget.record(prompt_tokens=10, completion_tokens=10)
    budget.record(prompt_tokens=10, completion_tokens=10)

    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "Budget exhausted" in data["detail"]
    assert budget.summary()["calls"] == 2


@pytest.mark.asyncio
async def test_budget_enforcement_uses_prompt_size_estimate(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    budget.max_tokens = 50

    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "x" * 1000}],
            "max_tokens": 1,
        },
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "Budget exhausted" in data["detail"]


@pytest.mark.asyncio
async def test_scope_endpoints_not_available_for_query_role():
    """Query-role bridge should NOT have /sandbox/simulate endpoint."""
    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        role="query",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        resp = await client.post(
            "/sandbox/simulate",
            headers=headers,
            json={"query_agent_id": "q1", "prompt": "test", "record_ids": []},
        )
        # Should get 404 (endpoint not registered) or 405
        assert resp.status_code in (404, 405, 422)
    finally:
        await client.aclose()
        await server.stop()


# ── OpenAI-compatible /v1/chat/completions tests ──


@pytest.mark.asyncio
async def test_openai_chat_completions_basic(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 200
    data = resp.json()

    # OpenAI response format
    assert data["object"] == "chat.completion"
    assert data["id"].startswith("chatcmpl-")
    assert len(data["choices"]) == 1
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "LLM response" in choice["message"]["content"]
    assert choice["finish_reason"] == "stop"

    # Usage
    assert data["usage"]["prompt_tokens"] == 100
    assert data["usage"]["completion_tokens"] == 50
    assert data["usage"]["total_tokens"] == 150

    # Budget was charged
    assert budget.summary()["calls"] == 1


@pytest.mark.asyncio
async def test_openai_chat_completions_budget_enforcement(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}

    budget.max_calls = 2
    budget.record(prompt_tokens=10, completion_tokens=10)
    budget.record(prompt_tokens=10, completion_tokens=10)

    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 429
    assert "Budget exhausted" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_openai_chat_completions_with_tools(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "What's the weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {"location": {"type": "string"}}},
                    },
                }
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    choice = data["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert "tool_calls" in choice["message"]
    tc = choice["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert tc["id"] == "call_abc123"


@pytest.mark.asyncio
async def test_openai_chat_completions_auth_required(bridge):
    server, client, budget = bridge
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bridge_start_raises_if_server_fails_to_boot():
    class _FailingServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self, sockets=None):
            raise RuntimeError("boom")

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
    )

    with patch("hivemind.sandbox.bridge.uvicorn.Server", _FailingServer):
        with pytest.raises(RuntimeError, match="failed to start"):
            await server.start()
