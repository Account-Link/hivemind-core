"""Default scope agent: query-driven scope ordering + query-agent safety checks.

Scope agents are allowed to read the full DB, but this implementation enforces:
- deterministic query-based ordering
- restricted query-agent inspection (provided by bridge)
- optional simulation-based tightening of scope

Env vars:
  BRIDGE_URL, SESSION_TOKEN
  QUERY_PROMPT
  QUERY_AGENT_ID

Output JSON to stdout:
  {"record_ids": [...]}
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "")
REQUEST_TIMEOUT_SECONDS = 30

LIST_PAGE_SIZE = 200
MAX_SCOPE_DEFAULT = 500
SIMULATE_SCOPE_CAP = 24

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"\$[0-9]{2,}(?:,[0-9]{3})*(?:\.[0-9]{2})?"),
]

QUERY_AGENT_SUSPICIOUS_PATTERNS = [
    re.compile(r"subprocess\.", re.IGNORECASE),
    re.compile(r"os\.system\(", re.IGNORECASE),
    re.compile(r"eval\(", re.IGNORECASE),
    re.compile(r"exec\(", re.IGNORECASE),
    re.compile(r"base64\.b64encode", re.IGNORECASE),
]


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


def call_tool(name: str, arguments: dict | None = None) -> str:
    payload = _post_json(f"/tools/{name}", {"arguments": arguments or {}})
    if payload.get("error"):
        return f"Error: {payload['error']}"
    return payload.get("result", "")


def list_all_records() -> list[dict]:
    records: list[dict] = []
    offset = 0
    while True:
        raw = call_tool("list", {"limit": LIST_PAGE_SIZE, "offset": offset})
        try:
            page = json.loads(raw)
        except json.JSONDecodeError:
            break
        if not isinstance(page, list) or not page:
            break
        records.extend(r for r in page if isinstance(r, dict))
        if len(page) < LIST_PAGE_SIZE:
            break
        offset += LIST_PAGE_SIZE
    return records


def rank_by_search(prompt: str) -> list[str]:
    if not prompt.strip():
        return []
    raw = call_tool("search", {"query": prompt, "limit": 120})
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    ranked: list[str] = []
    seen: set[str] = set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        if not isinstance(rid, str):
            continue
        if rid in seen:
            continue
        seen.add(rid)
        ranked.append(rid)
    return ranked


def list_query_agent_files() -> list[dict]:
    raw = call_tool("list_query_agent_files", {})
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    files = data.get("files", [])
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, dict) and isinstance(f.get("path"), str)]


def read_query_agent_file(path: str) -> str:
    return call_tool("read_query_agent_file", {"file_path": path})


def query_agent_risk_score() -> int:
    if not QUERY_AGENT_ID:
        return 0
    files = list_query_agent_files()
    if not files:
        return 1  # unknown binary/no source => mildly conservative

    score = 0
    for f in files[:8]:
        path = f.get("path", "")
        if not path.endswith((".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".sh")):
            continue
        content = read_query_agent_file(path)[:25_000]
        for pattern in QUERY_AGENT_SUSPICIOUS_PATTERNS:
            if pattern.search(content):
                score += 1
    return score


def simulate_query(prompt: str, record_ids: list[str]) -> tuple[str, list[str]] | None:
    if not QUERY_AGENT_ID or not record_ids:
        return None
    payload = {
        "query_agent_id": QUERY_AGENT_ID,
        "prompt": prompt,
        "record_ids": record_ids,
    }
    req = urllib.request.Request(
        f"{BRIDGE_URL}/sandbox/simulate",
        data=json.dumps(payload).encode(),
        headers=_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None

    output = data.get("output", "")
    accessed = data.get("records_accessed", [])
    if not isinstance(output, str):
        output = ""
    if not isinstance(accessed, list):
        accessed = []
    return output, [x for x in accessed if isinstance(x, str)]


def contains_sensitive_text(text: str) -> bool:
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False


def unique_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def main() -> None:
    all_records = list_all_records()
    all_ids = [
        r["id"] for r in all_records
        if isinstance(r, dict) and isinstance(r.get("id"), str)
    ]

    max_scope = MAX_SCOPE_DEFAULT
    ranked = rank_by_search(QUERY_PROMPT)
    if ranked:
        ordered = ranked + all_ids
    else:
        ordered = all_ids
    final_ids = unique_keep_order(ordered)

    # Semi-trusted behavior: inspect active query-agent source for obviously risky primitives.
    risk_score = query_agent_risk_score()
    if risk_score >= 3:
        final_ids = final_ids[: min(10, max_scope)]

    # Run one safety simulation and tighten scope if output contains sensitive-looking material.
    if final_ids:
        sim_scope = final_ids[: min(SIMULATE_SCOPE_CAP, len(final_ids))]
        simulation_prompt = (
            "Answer cautiously. Never reveal secrets. "
            f"Question: {QUERY_PROMPT or 'Provide a safe summary.'}"
        )
        sim = simulate_query(simulation_prompt, sim_scope)
        if sim is not None:
            sim_output, _ = sim
            if contains_sensitive_text(sim_output):
                final_ids = final_ids[: min(8, max_scope)]

    final_ids = final_ids[:max_scope]
    print(json.dumps({"record_ids": final_ids}))


if __name__ == "__main__":
    main()
