"""Scope agent that filters records by the caller's team.

Only records where metadata.team matches the caller's team are visible
to the query agent. If no team is specified in caller_context, all
records are returned (open access).

This shows how to implement access control using metadata queries.

Env vars (set by hivemind):
  BRIDGE_URL       — HTTP endpoint for the bridge server
  SESSION_TOKEN    — Bearer token for bridge auth
  QUERY_PROMPT     — The user's question (for context, not used here)
  CALLER_CONTEXT   — JSON with caller info, e.g. {"user_id": "alice", "team": "payments"}
  QUERY_AGENT_ID   — Which query agent will process (not used here)
"""

import json
import os

import httpx

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
CALLER_CONTEXT = os.environ.get("CALLER_CONTEXT", "{}")

client = httpx.Client(
    base_url=BRIDGE,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=60,
)


def call_tool(name: str, args: dict) -> str:
    resp = client.post(f"/tools/{name}", json={"arguments": args})
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        return f"Error: {data['error']}"
    return data["result"]


def main():
    # Parse caller context
    try:
        ctx = json.loads(CALLER_CONTEXT)
    except json.JSONDecodeError:
        ctx = {}

    caller_team = ctx.get("team", "")

    # Paginate through all records
    allowed_ids = []
    offset = 0
    while True:
        raw = call_tool("list", {"limit": 100, "offset": offset})
        try:
            records = json.loads(raw)
        except json.JSONDecodeError:
            break

        if not records:
            break

        for record in records:
            metadata = record.get("metadata", {})
            if not caller_team:
                # No team filter — allow everything
                allowed_ids.append(record["id"])
            elif metadata.get("team") == caller_team:
                allowed_ids.append(record["id"])

        if len(records) < 100:
            break
        offset += 100

    print(json.dumps({"record_ids": allowed_ids}))


if __name__ == "__main__":
    main()
