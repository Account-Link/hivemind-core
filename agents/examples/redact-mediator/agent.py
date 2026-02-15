"""Mediator agent that redacts PII and secrets from query output.

Uses an LLM to identify and strip email addresses, phone numbers,
API keys, passwords, and other sensitive data from the query agent's
raw output before it reaches the caller.

Mediators have no tool access — they only see the output and context
via env vars, and use the LLM to process it.

Env vars (set by hivemind):
  BRIDGE_URL        — HTTP endpoint for the bridge server
  SESSION_TOKEN     — Bearer token for bridge auth
  RAW_OUTPUT        — The query agent's raw response to audit
  QUERY_PROMPT      — The original question (for context)
  RECORDS_ACCESSED  — JSON list of record IDs the query agent accessed
"""

import os

import httpx

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
RAW_OUTPUT = os.environ.get("RAW_OUTPUT", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
RECORDS_ACCESSED = os.environ.get("RECORDS_ACCESSED", "[]")

REDACTION_PROMPT = """\
You are a PII redaction filter. Your job is to remove sensitive information \
from the text below while preserving the meaning and usefulness of the response.

Redact the following types of information by replacing them with [REDACTED]:
- Email addresses
- Phone numbers
- API keys, tokens, passwords, secrets
- Social security numbers, credit card numbers
- Physical addresses (street addresses, not city/country names)
- IP addresses

Rules:
- If the text contains NONE of the above, return it EXACTLY unchanged.
- Only redact the specific sensitive values, not surrounding context.
- Do not add commentary, preamble, or explanation.
- Return ONLY the processed text.\
"""


def main():
    if not RAW_OUTPUT.strip():
        print("")
        return

    client = httpx.Client(
        base_url=BRIDGE,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=60,
    )

    resp = client.post("/llm/chat", json={
        "messages": [
            {"role": "system", "content": REDACTION_PROMPT},
            {"role": "user", "content": RAW_OUTPUT},
        ],
        "max_tokens": 4096,
    })

    if resp.status_code == 429:
        # Fail closed: never pass through unredacted content when redaction fails.
        print("Unable to provide a redacted response within budget.")
        return

    resp.raise_for_status()
    print(resp.json()["content"])


if __name__ == "__main__":
    main()
