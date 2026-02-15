# Example Agents

Ready-to-upload example agents for hivemind-core. Each directory is self-contained with a `Dockerfile`, `agent.py`, and `requirements.txt`.

## Upload an Example

```bash
# Pack any example into a tarball
tar czf agent.tar.gz -C agents/examples/simple-query .

# Upload to hivemind
curl -X POST http://localhost:8100/v1/agents/upload \
  -F "name=simple-query" \
  -F "archive=@agent.tar.gz"
```

## Examples

### `simple-query/` — Minimal Query Agent

The simplest possible query agent. Search for records, read the top results, make one LLM call to synthesize an answer.

**Role:** query | **Tools:** search, read | **LLM calls:** 2 (search + synthesize)

Good starting point for understanding the bridge contract.

### `tool-loop-query/` — Agentic Query Agent

Full agentic loop with:
- **Multi-turn tool use** — LLM decides which tools to call each turn (up to 10 turns)
- **Parallel execution** — multiple tool calls in one turn run concurrently via asyncio
- **Auto-compaction** — when context grows large, old tool results are summarized to free space
- **Structured tool calling** — LLM outputs ` ```tool ` JSON blocks, agent parses and executes

**Role:** query | **Tools:** search, read, list | **LLM calls:** variable (up to budget)

Use this as a base for production query agents.

### `metadata-scope/` — Team-Based Scope Agent

Filters records by `caller_context.team`. Only records where `metadata.team` matches the caller's team are visible to the query agent. If no team is specified, all records are allowed.

**Role:** scope | **Tools:** list | **LLM calls:** 0

Shows how to implement access control using metadata.

### `redact-mediator/` — PII Redaction Mediator

Uses an LLM to strip emails, phone numbers, API keys, and other sensitive data from query output before it reaches the caller.

**Role:** mediator | **Tools:** none | **LLM calls:** 1

Shows the mediator audit pattern.

## Agent Contract

### Environment Variables

**Enforced** (all agents receive, cannot bypass):
- `BRIDGE_URL` — HTTP endpoint for the bridge server (only allowed network exit)
- `SESSION_TOKEN` — Bearer token for bridge authentication
- `AGENT_ROLE` — Role identifier (query, scope, mediator, index)
- `BUDGET_MAX_TOKENS` — Total token budget allocated for this run
- `BUDGET_MAX_CALLS` — Total LLM call budget allocated for this run
- `OPENAI_BASE_URL` — Points to bridge's `/v1` path. Standard OpenAI SDKs auto-route through the bridge
- `OPENAI_API_KEY` — Same as `SESSION_TOKEN`. OpenAI SDKs send this as Bearer auth automatically

**Advisory** (default agents use these, custom agents may ignore them entirely):

| Role | Advisory Env Vars |
|------|---------------|
| **query** | `QUERY_PROMPT` |
| **scope** | `QUERY_PROMPT`, `QUERY_AGENT_ID` |
| **mediator** | `RAW_OUTPUT`, `QUERY_PROMPT`, `RECORDS_ACCESSED` |
| **index** | `DOCUMENT_DATA`, `DOCUMENT_METADATA` |

### Bridge API

All requests require `Authorization: Bearer {SESSION_TOKEN}`.

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Budget status (no auth needed) |
| `GET /tools` | List available tool schemas |
| `POST /tools/{name}` | Call a tool: `{"arguments": {...}}` -> `{"result": "...", "error": null}` |
| `POST /llm/chat` | LLM proxy: `{"messages": [...], "max_tokens": N}` -> `{"content": "...", "usage": {...}}` |
| `POST /v1/chat/completions` | OpenAI-compatible LLM proxy (same budget enforcement; standard SDKs use this automatically) |

### Output

Agents write their output to **stdout** and exit with code 0.

| Role | Output Format |
|------|--------------|
| **query** | Plain text answer |
| **scope** | `{"record_ids": ["id1", "id2", ...]}` |
| **mediator** | Filtered/audited text |
| **index** | `{"index_text": "...", "metadata": {...}}` |
