"""Default query agent: agentic tool loop with parallel tool calls and compaction.

Env vars:
  BRIDGE_URL, SESSION_TOKEN — bridge connection
  QUERY_PROMPT — the question to answer
  QUERY_CONTEXT — optional additional context

Outputs answer text to stdout.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.error
import urllib.request

BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_CONTEXT = os.environ.get("QUERY_CONTEXT", "")
REQUEST_TIMEOUT_SECONDS = 30

MAX_TURNS = 10
COMPACTION_CHAR_THRESHOLD = 80_000
COMPACTION_KEEP_RECENT = 4
TOOL_RESULT_PREVIEW = 8_000

SYSTEM_PROMPT = """\
You are a query agent with access to a scoped knowledge base. Answer accurately.

Available tools:
- search(query, limit=20): FTS over indexed records.
- read(record_id, offset=0, limit=20000): Read a record body with metadata header.
- list(limit=20, offset=0): Browse recent records.

To call tools, emit one or more blocks exactly like:
```tool
{"name":"search","arguments":{"query":"grpc migration","limit":10}}
```

Multiple tool blocks in one response are allowed and run in parallel.

When you have enough information, output plain answer text (no tool blocks).

Rules:
- Cite only what you can retrieve from tools.
- If you cannot find relevant information, say so clearly.
- Paraphrase. Do not dump raw record text verbatim.
- Never include credentials, API keys, passwords, tokens, or secrets.
"""


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SESSION_TOKEN}",
    }


def _post_json(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def llm_call(messages: list[dict], max_tokens: int = 4096) -> str:
    try:
        return _post_json(
            "/llm/chat",
            {"messages": messages, "max_tokens": max_tokens},
        ).get("content", "")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "(Budget exhausted — cannot make more LLM calls.)"
        return f"(LLM request failed with HTTP {e.code}.)"
    except urllib.error.URLError as e:
        return f"(LLM request failed: {e.reason})"
    except TimeoutError:
        return "(LLM request timed out.)"
    except Exception as e:  # pragma: no cover - defensive
        return f"(LLM request failed: {e})"


def get_tools() -> list[dict]:
    req = urllib.request.Request(f"{BRIDGE_URL}/tools", headers=_headers())
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def call_tool(name: str, arguments: dict) -> str:
    payload = _post_json(f"/tools/{name}", {"arguments": arguments})
    if payload.get("error"):
        return f"Error: {payload['error']}"
    return payload.get("result", "")


def parse_tool_calls(text: str) -> tuple[list[dict], str]:
    calls: list[dict] = []
    remaining_lines: list[str] = []
    in_block = False
    block_lines: list[str] = []

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "```tool":
            in_block = True
            block_lines = []
            continue
        if in_block and stripped == "```":
            in_block = False
            raw = "\n".join(block_lines).strip()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "name" in parsed:
                    calls.append(parsed)
            except json.JSONDecodeError:
                remaining_lines.append(f"(failed to parse tool block: {raw[:120]})")
            continue
        if in_block:
            block_lines.append(line)
        else:
            remaining_lines.append(line)

    return calls, "\n".join(remaining_lines).strip()


def _run_one_tool(call: dict) -> dict:
    name = str(call.get("name", ""))
    args = call.get("arguments", {})
    if not isinstance(args, dict):
        args = {}
    try:
        result = call_tool(name, args)
    except Exception as e:  # pragma: no cover - defensive
        result = f"Error: {e}"
    return {"name": name, "arguments": args, "result": result}


def execute_tools_parallel(calls: list[dict]) -> list[dict]:
    if not calls:
        return []
    workers = max(1, min(8, len(calls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_run_one_tool, calls))


def estimate_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


def compact_context(messages: list[dict]) -> list[dict]:
    turn_starts = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    if len(turn_starts) <= COMPACTION_KEEP_RECENT:
        return messages

    cutoff = turn_starts[-COMPACTION_KEEP_RECENT]
    old_section = messages[2:cutoff]
    recent_section = messages[cutoff:]

    summaries: list[str] = []
    for m in old_section:
        role = m.get("role", "")
        content = str(m.get("content", ""))
        preview = content[:180] + ("..." if len(content) > 180 else "")
        if role == "assistant":
            summaries.append(f"- Assistant: {preview}")
        elif role == "user" and "Tool results:" in content:
            summaries.append(f"- Tool results: {preview}")

    if not summaries:
        return messages

    summary_text = (
        "[Earlier tool interactions compacted to preserve context]\n"
        + "\n".join(summaries[:20])
    )
    return [
        messages[0],
        messages[1],
        {"role": "assistant", "content": summary_text},
        {"role": "user", "content": "(continue from compacted context)"},
    ] + recent_section


def build_initial_user_message() -> str:
    if QUERY_CONTEXT.strip():
        return f"Context: {QUERY_CONTEXT}\n\nQuestion: {QUERY_PROMPT}"
    return QUERY_PROMPT


def main() -> None:
    if not QUERY_PROMPT.strip():
        print("No question provided.")
        return

    tools = get_tools()
    tool_names = {t.get("function", {}).get("name") for t in tools}
    tool_names.discard(None)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_initial_user_message()},
    ]

    seeded_search = False
    for _ in range(MAX_TURNS):
        if estimate_chars(messages) > COMPACTION_CHAR_THRESHOLD:
            messages = compact_context(messages)

        response = llm_call(messages)
        tool_calls, remaining_text = parse_tool_calls(response)
        valid_calls = [c for c in tool_calls if c.get("name") in tool_names]

        if valid_calls:
            results = execute_tools_parallel(valid_calls)
            lines: list[str] = []
            for item in results:
                lines.append(f"[{item['name']}({json.dumps(item['arguments'])})]")
                lines.append(item["result"][:TOOL_RESULT_PREVIEW])
                lines.append("")
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": "Tool results:\n" + "\n".join(lines),
            })
            continue

        # If model answers too early, seed with one scoped search before finalizing.
        if not seeded_search and "search" in tool_names:
            seeded_search = True
            seeded = _run_one_tool({"name": "search", "arguments": {"query": QUERY_PROMPT, "limit": 12}})
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": (
                    "Use these scoped search results before finalizing.\n\n"
                    f"[search({json.dumps({'query': QUERY_PROMPT, 'limit': 12})})]\n"
                    f"{seeded['result'][:TOOL_RESULT_PREVIEW]}"
                ),
            })
            continue

        print(remaining_text or response)
        return

    print(remaining_text if "remaining_text" in locals() and remaining_text else "(Reached maximum turns without a final answer.)")


if __name__ == "__main__":
    main()
