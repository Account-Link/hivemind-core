#!/usr/bin/env python3
"""Minimal MCP stdio server that proxies hivemind bridge tool endpoints.

Implements just enough of MCP JSON-RPC 2.0 over stdin/stdout to expose
bridge tools (execute_sql, get_schema, list_query_agent_files, etc.)
as MCP tools for the claw CLI.

No external dependencies — uses only Python stdlib.
"""

import json
import os
import sys
import urllib.request

BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]

_HEADERS = {
    "Authorization": f"Bearer {SESSION_TOKEN}",
    "Content-Type": "application/json",
}


def _bridge_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}", headers=_HEADERS, method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _bridge_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}", data=data, headers=_HEADERS, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _fetch_tools() -> list[dict]:
    """Fetch tool definitions from bridge GET /tools."""
    raw = _bridge_get("/tools")
    tools = []
    for t in raw:
        fn = t.get("function", t)
        tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "inputSchema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return tools


def _call_tool(name: str, arguments: dict) -> str:
    """Call bridge POST /tools/{name} and return result text."""
    resp = _bridge_post(f"/tools/{name}", {"arguments": arguments})
    if resp.get("error"):
        return f"Error: {resp['error']}"
    return resp.get("result", "")


# ── JSON-RPC helpers ──

def _ok(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _err(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _handle(msg: dict) -> dict | None:
    method = msg.get("method", "")
    id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return _ok(id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hivemind-bridge", "version": "1.0.0"},
        })

    if method == "notifications/initialized":
        return None  # no response for notifications

    if method == "tools/list":
        try:
            tools = _fetch_tools()
        except Exception as e:
            return _err(id, -32000, str(e))
        return _ok(id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = _call_tool(name, arguments)
        except Exception as e:
            return _ok(id, {
                "content": [{"type": "text", "text": f"Error calling {name}: {e}"}],
                "isError": True,
            })
        return _ok(id, {
            "content": [{"type": "text", "text": result}],
        })

    if method == "ping":
        return _ok(id, {})

    if method.startswith("notifications/"):
        return None

    return _err(id, -32601, f"Method not found: {method}")


def main():
    """Read JSON-RPC messages from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
