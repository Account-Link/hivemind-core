"""Minimal query agent: search, read top results, synthesize an answer.

This is the simplest possible query agent. It does one search, reads the
top results, makes one LLM call to synthesize, and prints the answer.

Env vars (set by hivemind):
  BRIDGE_URL      — HTTP endpoint for the bridge server
  SESSION_TOKEN   — Bearer token for bridge auth
  QUERY_PROMPT    — The user's question
  QUERY_CONTEXT   — Optional additional context
"""

import json
import os

import httpx

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
PROMPT = os.environ.get("QUERY_PROMPT", "")
CONTEXT = os.environ.get("QUERY_CONTEXT", "")

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


def llm_call(messages: list[dict], max_tokens: int = 2048) -> str:
    resp = client.post("/llm/chat", json={"messages": messages, "max_tokens": max_tokens})
    resp.raise_for_status()
    return resp.json()["content"]


def main():
    if not PROMPT.strip():
        print("No question provided.")
        return

    # Step 1: Search for relevant records
    results_json = call_tool("search", {"query": PROMPT, "limit": 10})
    try:
        results = json.loads(results_json)
    except json.JSONDecodeError:
        results = []

    if not results:
        print("No relevant records found.")
        return

    # Step 2: Read the top 3 results
    texts = []
    for item in results[:3]:
        record_id = item.get("id", "")
        if not record_id:
            continue
        data = call_tool("read", {"record_id": record_id})
        if data and "not found" not in data.lower():
            texts.append(data)

    if not texts:
        print("Found search results but could not read any records.")
        return

    # Step 3: Synthesize an answer
    context_block = "\n\n---\n\n".join(texts)
    user_msg = f"Based on these records:\n\n{context_block}\n\nAnswer this question: {PROMPT}"
    if CONTEXT:
        user_msg = f"Context: {CONTEXT}\n\n{user_msg}"

    answer = llm_call([
        {"role": "system", "content": "Answer based on the provided records. Be concise and accurate. Do not reproduce text verbatim — paraphrase."},
        {"role": "user", "content": user_msg},
    ])

    print(answer)


if __name__ == "__main__":
    main()
