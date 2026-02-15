# Hivemind-Core API Reference

This document covers:
- Public HTTP API (`/v1/*`) used by clients
- Internal bridge API used by Docker agents at runtime

Base URL (default): `http://localhost:8100`

## Authentication

If `HIVEMIND_API_KEY` is set, every public endpoint except `GET /v1/health` requires:

```http
Authorization: Bearer <your-api-key>
```

Startup safety rule: if `HIVEMIND_HOST` is non-local (not `127.0.0.1`, `localhost`, or `::1`), `HIVEMIND_API_KEY` must be set.

## Conventions And Gotchas

- IDs (`record_id`, `agent_id`) are opaque strings (currently 12-char hex).
- Most endpoints use JSON. `POST /v1/agents/upload` uses `multipart/form-data`.
- `POST /v1/query` canonical field is `query`. `prompt` is still accepted as a deprecated alias.
- OpenAPI schema still marks `query` as required (for generated clients, send `query`).
- `created_at` differs by endpoint:
  - `POST /v1/store` returns ISO datetime string
  - `GET /v1/admin/records/{id}` returns Unix timestamp (float seconds)
- Validation errors return `422` (Pydantic/FastAPI). Runtime errors return `400`/`404`/`503`/`500` with `{"detail": ...}`.

## Public API

### `GET /v1/health`

Health check (never requires auth).

**Response 200**

```json
{
  "status": "ok",
  "record_count": 42,
  "version": "0.2.0"
}
```

### `POST /v1/store`

Store a record. `data` is encrypted at rest when `HIVEMIND_ENCRYPTION_KEY` is configured.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `data` | string | yes | Min length 1 |
| `metadata` | object | no | Schemaless JSON, defaults to `{}` |
| `index_text` | string or null | no | If provided, used directly for FTS |
| `index_agent_id` | string | no | Runs index agent to produce `index_text` and optional metadata |

Indexing priority:
1. `index_text` provided -> use it
2. else `index_agent_id` (or configured default index agent) -> run agent
3. else -> record is stored without FTS index text

**Example**

```bash
curl -X POST http://localhost:8100/v1/store \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "data": "Q3 retro: decided to migrate payments from PayPal to Stripe.",
    "metadata": {"author": "alice", "team": "payments"},
    "index_text": "Q3 retro payment migration PayPal Stripe"
  }'
```

**Response 200**

```json
{
  "record_id": "482feb9fd696",
  "created_at": "2026-02-12T22:01:49.774955",
  "metadata": {"author": "alice", "team": "payments"}
}
```

**Common errors**
- `400` index agent not found / invalid index agent output
- `401` unauthorized (when API key enabled)
- `422` invalid request body

### `POST /v1/query`

Run query pipeline: optional scope agent -> query agent -> optional mediator.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Canonical field, min length 1 |
| `prompt` | string | no | Deprecated alias; used only if `query` missing/blank |
| `scope` | array[string] or null | no | Record whitelist. `null` = all records |
| `query_agent_id` | string | no | Required unless default query agent configured |
| `scope_agent_id` | string | no | Explicit scope agent for dynamic scope resolution |
| `mediator_agent_id` | string | no | Optional output auditing/filtering |
| `max_tokens` | integer | no | Per-request cap (min 1), clamped to server global max |

Scope resolution order:
1. `scope_agent_id` if provided
2. else explicit `scope` from request
3. else configured default scope agent
4. else unscoped (`null`)

Scope rules:
- Enforced in SQL layer (query agent tools cannot escape scope)
- IDs are trimmed + deduplicated
- Max scope size is 900 record IDs
- Empty scope list means no records visible

Mediator behavior:
- If mediator budget is too low (`<128` tokens remaining), mediator is skipped
- If mediator fails for a non-not-found reason, raw query output is returned (`mediated=false`)

**Example**

```bash
curl -X POST http://localhost:8100/v1/query \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What technical decisions were made recently?",
    "query_agent_id": "qa-1",
    "scope": ["rec_001", "rec_002"],
    "max_tokens": 50000
  }'
```

**Response 200**

```json
{
  "output": "Two decisions were made: migrate payments to Stripe and move internal APIs to gRPC.",
  "records_accessed": ["482feb9fd696", "a1b2c3d4e5f6"],
  "mediated": false,
  "usage": {"total_tokens": 12345, "max_tokens": 50000}
}
```

**Common errors**
- `400` no query agent configured, agent not found, invalid scope-agent output, scope too large
- `401` unauthorized (when API key enabled)
- `422` validation errors (for example `max_tokens <= 0`)

### `GET /v1/admin/records/{record_id}`

Get record metadata + `index_text`. Raw encrypted/decrypted `data` is never returned here.

**Response 200**

```json
{
  "id": "482feb9fd696",
  "metadata": {"author": "alice", "team": "payments"},
  "index_text": "Q3 retro payment migration PayPal Stripe",
  "created_at": 1739404909.774955
}
```

**Errors**
- `404` record not found

### `PATCH /v1/admin/records/{record_id}`

Update `metadata` and/or `index_text`.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `metadata` | object | no | Replaces metadata when present |
| `index_text` | string | no | Replaces FTS text when present |

At least one field must be present.

**Example**

```bash
curl -X PATCH http://localhost:8100/v1/admin/records/482feb9fd696 \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"metadata": {"reviewed": true}, "index_text": "updated search text"}'
```

**Responses**
- `200` `{"status": "ok"}`
- `400` missing fields, `metadata: null`, or `index_text: null`
- `404` record not found
- `422` invalid types (for example `metadata` array, `index_text` number)

### `DELETE /v1/admin/records/{record_id}`

Delete record and associated FTS index.

**Responses**
- `200` `{"status": "ok"}`
- `404` `{"detail": "Record not found"}`

### `POST /v1/agents`

Register a pre-built local Docker image as an agent.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Human-readable name |
| `image` | string | yes | Docker image ref available in local daemon |
| `description` | string | no | Defaults to `""` |
| `entrypoint` | string or null | no | Overrides image CMD |
| `memory_mb` | integer | no | Min 16, capped by server `HIVEMIND_CONTAINER_MEMORY_MB` |
| `max_llm_calls` | integer | no | Min 1 |
| `max_tokens` | integer | no | Min 1 |
| `timeout_seconds` | integer | no | Min 1 |

Server validates Docker image availability before registration.

**Response 200**

```json
{
  "agent_id": "abc123def456",
  "name": "my-agent",
  "files_extracted": 5
}
```

`files_extracted` is best-effort source extraction from the image and may be `0` even when registration succeeds.

**Common errors**
- `400` image missing locally
- `503` Docker daemon unavailable during validation
- `422` request validation errors

### `POST /v1/agents/upload`

Upload source archive, build Docker image on server, register resulting agent.

**Request**: `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `archive` | file | yes | Tar/tar.gz with `Dockerfile` |
| `name` | string | yes | Agent name |
| `description` | string | no | Defaults to `""` |
| `entrypoint` | string | no | Optional CMD override |
| `memory_mb` | integer | no | Min 16 |
| `max_llm_calls` | integer | no | Min 1 |
| `max_tokens` | integer | no | Min 1 |
| `timeout_seconds` | integer | no | Min 1 |

Archive safeguards:
- Max compressed upload: 50 MB
- Max archive entries: 2,000
- Max single file size: 15 MB
- Max extracted total size: 150 MB
- Symlinks/hardlinks and path traversal are rejected

**Example**

```bash
tar czf agent.tar.gz -C my-agent .

curl -X POST http://localhost:8100/v1/agents/upload \
  -H "Authorization: Bearer $API_KEY" \
  -F "name=my-agent" \
  -F "description=Custom query agent" \
  -F "archive=@agent.tar.gz"
```

**Response 200**

```json
{
  "agent_id": "abc123def456",
  "name": "my-agent",
  "files_extracted": 3
}
```

**Common errors**
- `400` missing Dockerfile, invalid archive, oversized archive/member, too many entries
- `422` invalid numeric form values
- `500` archive extraction/build failures (details are redacted)

### `GET /v1/agents`

List registered agents.

**Response 200**

```json
[
  {
    "agent_id": "abc123def456",
    "name": "my-agent",
    "description": "Custom query agent",
    "image": "hivemind-agent-abc123def456:latest",
    "entrypoint": null,
    "memory_mb": 256,
    "max_llm_calls": 20,
    "max_tokens": 100000,
    "timeout_seconds": 120
  }
]
```

### `GET /v1/agents/{agent_id}`

Get one agent config.

**Responses**
- `200` same schema as list item
- `404` `{"detail": "Agent not found"}`

### `DELETE /v1/agents/{agent_id}`

Delete agent config and extracted source files.

**Responses**
- `200` `{"status": "ok"}`
- `404` `{"detail": "Agent not found"}`

## Internal Bridge API (Agent Runtime)

Each running Docker agent talks to an ephemeral bridge server.

Auth for bridge endpoints (except `/health`):

```http
Authorization: Bearer <SESSION_TOKEN>
```

Agents automatically receive `BRIDGE_URL`, `SESSION_TOKEN`, `OPENAI_BASE_URL`, and `OPENAI_API_KEY` in env vars.

### Common Endpoints (All Agent Roles)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + current budget summary |
| `GET` | `/tools` | Tool schemas (OpenAI function format) |
| `POST` | `/tools/{tool_name}` | Invoke tool with `{"arguments": {...}}` |
| `POST` | `/llm/chat` | LLM proxy with budget enforcement |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions proxy |

`POST /llm/chat` request fields:
- `messages` (required)
- `model` (optional override)
- `max_tokens` (default 4096, max 16384)
- `temperature`, `top_p` (optional)

`POST /llm/chat` response:

```json
{
  "content": "...",
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 50
  }
}
```

Budget exhaustion returns `429` with `{"detail": "Budget exhausted: ..."}`.

### Scope-Agent-Only Bridge Endpoints

Available only when bridge role is `scope`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sandbox/simulate` | Nested query-agent run for allowed query agent only |
| `GET` | `/sandbox/agents/{agent_id}/files` | List extracted files for allowed query agent |
| `GET` | `/sandbox/agents/{agent_id}/files/{file_path}` | Read extracted file content |

`/sandbox/simulate` request:

```json
{
  "query_agent_id": "qa-1",
  "prompt": "What changed this week?",
  "record_ids": ["r1", "r2"]
}
```

### Agent Tools Exposed Through Bridge

Query/index/scope roles receive scoped storage tools:

| Tool | Signature | Behavior |
|---|---|---|
| `search` | `search(query, limit=20)` | FTS5 search, limit clamped to `1..200` |
| `read` | `read(record_id, offset=0, limit=20000)` | Chunked record read, limit clamped to `1..50000` |
| `list` | `list(limit=20, offset=0)` | List records by recency, limit clamped to `1..200` |

Scope agents additionally receive:
- `list_query_agent_files()`
- `read_query_agent_file(file_path)`

Mediator agents receive no data tools.

## Python Client Example

```python
import httpx

BASE = "http://localhost:8100"
API_KEY = "your-api-key"
QUERY_AGENT_ID = "default-query"

client = httpx.Client(
    base_url=BASE,
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=120,
)

store_resp = client.post(
    "/v1/store",
    json={
        "data": "Sprint retro: moved internal APIs from REST to gRPC.",
        "metadata": {"team": "backend", "type": "decision"},
        "index_text": "sprint retro REST gRPC migration",
    },
)
store_resp.raise_for_status()
record_id = store_resp.json()["record_id"]

query_resp = client.post(
    "/v1/query",
    json={
        "query": "What decisions were made?",
        "query_agent_id": QUERY_AGENT_ID,
        "scope": [record_id],
        "max_tokens": 50000,
    },
)
query_resp.raise_for_status()

print(query_resp.json()["output"])
print(query_resp.json()["usage"])
```
