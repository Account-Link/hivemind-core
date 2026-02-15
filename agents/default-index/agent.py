"""Default index agent with tool loop + structured JSON output.

Env vars:
  BRIDGE_URL, SESSION_TOKEN — bridge connection
  DOCUMENT_DATA — raw document content to index
  DOCUMENT_METADATA — existing metadata JSON

Output JSON to stdout:
  {"index_text": "...", "metadata": {...}}
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import urllib.error
import urllib.request

BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]
DOCUMENT_DATA = os.environ.get("DOCUMENT_DATA", "")
DOCUMENT_METADATA = os.environ.get("DOCUMENT_METADATA", "{}")
REQUEST_TIMEOUT_SECONDS = 30

MAX_TURNS = 8
COMPACTION_CHAR_THRESHOLD = 80_000
COMPACTION_KEEP_RECENT = 4
TOOL_RESULT_PREVIEW = 4_000

SYSTEM_PROMPT = """\
You are an indexing agent. Build a high-quality retrieval index for the provided document.

You may use tools to inspect related records:
- search(query, limit=20)
- read(record_id, offset=0, limit=20000)
- list(limit=20, offset=0)

When calling tools, output one or more blocks in this exact format:
```tool
{"name": "search", "arguments": {"query": "payments migration", "limit": 10}}
```

You may emit multiple tool blocks in one response; they will run in parallel.

When ready, output ONLY valid JSON with this exact schema:
{
  "title": "<string, <= 100 chars>",
  "summary": "<string, 2-3 sentences>",
  "tags": ["<string>", "..."],
  "key_claims": ["<string>", "..."]
}

Rules:
- Ground claims in the document content.
- Prefer factual, retrieval-friendly phrasing.
- Do not include markdown fences in the final answer.
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


def llm_call(messages: list[dict], max_tokens: int = 2048) -> str:
    try:
        return _post_json(
            "/llm/chat",
            {"messages": messages, "max_tokens": max_tokens},
        ).get("content", "")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "(Budget exhausted)"
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


def execute_tools_parallel(calls: list[dict]) -> list[dict]:
    def run_one(call: dict) -> dict:
        name = str(call.get("name", ""))
        args = call.get("arguments", {})
        if not isinstance(args, dict):
            args = {}
        try:
            result = call_tool(name, args)
        except Exception as e:  # pragma: no cover - defensive
            result = f"Error: {e}"
        return {"name": name, "arguments": args, "result": result}

    if not calls:
        return []
    workers = max(1, min(6, len(calls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run_one, calls))


def estimate_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


def compact_context(messages: list[dict]) -> list[dict]:
    turn_starts = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    if len(turn_starts) <= COMPACTION_KEEP_RECENT:
        return messages
    cutoff = turn_starts[-COMPACTION_KEEP_RECENT]
    old_section = messages[2:cutoff]
    recent = messages[cutoff:]

    summary_lines: list[str] = []
    for msg in old_section:
        role = msg.get("role", "")
        content = str(msg.get("content", ""))
        preview = content[:180] + ("..." if len(content) > 180 else "")
        if role == "assistant":
            summary_lines.append(f"- Assistant: {preview}")
        elif role == "user" and "Tool results:" in content:
            summary_lines.append(f"- Tool results: {preview}")

    if not summary_lines:
        return messages

    summary = (
        "[Earlier steps compacted to preserve context]\n"
        + "\n".join(summary_lines[:20])
    )
    return [
        messages[0],
        messages[1],
        {"role": "assistant", "content": summary},
        {"role": "user", "content": "(continue indexing from compacted context)"},
    ] + recent


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
        return "\n".join(lines[1:]).strip()
    return t


def _extract_json_obj(text: str) -> dict | None:
    cleaned = _strip_fences(text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _repair_json(candidate: str) -> dict | None:
    repaired = llm_call(
        [
            {
                "role": "system",
                "content": (
                    "Convert the input into strict JSON with keys: "
                    "title, summary, tags, key_claims. Return JSON only."
                ),
            },
            {"role": "user", "content": candidate[:12_000]},
        ],
        max_tokens=900,
    )
    return _extract_json_obj(repaired)


def _normalize_list(value, max_items: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = " ".join(item.strip().split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


def _heuristic_index(text: str) -> dict:
    stripped = text.strip()
    first_line = stripped.splitlines()[0] if stripped else "Untitled"
    title = " ".join(first_line.split())[:100] or "Untitled"

    sentence_candidates = re.split(r"(?<=[.!?])\s+", stripped)
    summary = " ".join(sentence_candidates[:2]).strip()
    if not summary:
        summary = stripped[:300]

    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", stripped.lower())
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    tags = [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:6]]

    claims = []
    for s in sentence_candidates:
        sentence = s.strip()
        if len(sentence) >= 24:
            claims.append(sentence[:220])
        if len(claims) >= 6:
            break

    return {
        "title": title,
        "summary": summary,
        "tags": tags,
        "key_claims": claims,
    }


def _normalize_index(raw: dict, fallback_text: str) -> dict:
    fallback = _heuristic_index(fallback_text)

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        title = fallback["title"]
    title = " ".join(title.strip().split())[:100]

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = fallback["summary"]
    summary = " ".join(summary.strip().split())

    tags = _normalize_list(raw.get("tags"), max_items=8)
    if not tags:
        tags = fallback["tags"]

    claims = _normalize_list(raw.get("key_claims"), max_items=12)
    if not claims:
        claims = fallback["key_claims"]

    return {
        "title": title or "Untitled",
        "summary": summary,
        "tags": tags,
        "key_claims": claims,
    }


def build_index_text(index: dict) -> str:
    parts = [index.get("title", ""), index.get("summary", "")]
    tags = index.get("tags", [])
    claims = index.get("key_claims", [])
    if tags:
        parts.append(" ".join(tags))
    if claims:
        parts.append(" ".join(claims))
    return "\n".join(p for p in parts if p).strip()


def run_index_loop() -> dict:
    tools = get_tools()
    tool_names = {t["function"]["name"] for t in tools}

    prompt = (
        "Create a structured index for this document.\n\n"
        f"DOCUMENT:\n{DOCUMENT_DATA[:20_000]}\n\n"
        "Use tools only if needed (for cross-document consistency). "
        "Return strict JSON."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    for _ in range(MAX_TURNS):
        if estimate_chars(messages) > COMPACTION_CHAR_THRESHOLD:
            messages = compact_context(messages)

        response = llm_call(messages)
        tool_calls, remaining = parse_tool_calls(response)
        valid_calls = [c for c in tool_calls if c.get("name") in tool_names]

        if valid_calls:
            results = execute_tools_parallel(valid_calls)
            result_lines: list[str] = []
            for r in results:
                result_lines.append(f"[{r['name']}({json.dumps(r['arguments'])})]")
                result_lines.append(r["result"][:TOOL_RESULT_PREVIEW])
                result_lines.append("")
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": (
                    "Tool results:\n"
                    + "\n".join(result_lines)
                    + "\nNow return final JSON with title, summary, tags, key_claims."
                ),
            })
            continue

        candidate = remaining or response
        parsed = _extract_json_obj(candidate)
        if parsed is not None:
            return parsed

        repaired = _repair_json(candidate)
        if repaired is not None:
            return repaired

        messages.append({"role": "assistant", "content": response})
        messages.append({
            "role": "user",
            "content": (
                "Your previous response was not valid JSON. "
                "Return JSON only with keys: title, summary, tags, key_claims."
            ),
        })

    return _heuristic_index(DOCUMENT_DATA)


def main() -> None:
    if not DOCUMENT_DATA.strip():
        print(json.dumps({"index_text": "", "metadata": {}}))
        return

    try:
        existing_metadata = json.loads(DOCUMENT_METADATA)
        if not isinstance(existing_metadata, dict):
            existing_metadata = {}
    except json.JSONDecodeError:
        existing_metadata = {}

    try:
        raw_index = run_index_loop()
    except Exception:
        raw_index = _heuristic_index(DOCUMENT_DATA)

    index = _normalize_index(raw_index, DOCUMENT_DATA)
    index_text = build_index_text(index)

    metadata = dict(existing_metadata)
    metadata.update(index)

    print(json.dumps({"index_text": index_text, "metadata": metadata}))


if __name__ == "__main__":
    main()
