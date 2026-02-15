# hivemind-core

A neutral encrypted storage and Docker agent sandbox platform. Like Postgres for AI-mediated knowledge — apps define their own metadata, access control, and query logic by registering Docker agent images.

Core provides only the irreducible primitives: encrypted record storage, FTS5 full-text search, Docker sandboxes, scope enforcement, and pipeline orchestration.

## Quickstart

```bash
# Install
uv sync --all-extras

# Configure
cp .env.example .env
# Edit .env — at minimum set HIVEMIND_LLM_API_KEY

# Build default local agent images (used by .env.example profile)
docker build -t hivemind-default-index:local agents/default-index
docker build -t hivemind-default-query:local agents/default-query
docker build -t hivemind-default-scope:local agents/default-scope
docker build -t hivemind-default-mediator:local agents/default-mediator

# Run
uv run python -m hivemind.server

# Verify
curl http://localhost:8100/v1/health
```

## How It Works

### System overview

```
                       ┌────────────────────────────────┐
                       │        CLIENT / CALLER          │
                       │   (curl, httpx, any HTTP client) │
                       └────────┬──────────┬─────────────┘
                                │          │
                       POST /v1/store   POST /v1/query
                                │          │
                       ┌────────▼──────────▼─────────────┐
                       │    FastAPI Server (server.py)    │
                       │    http://localhost:8100         │
                       │                                  │
                       │    Auth: Bearer HIVEMIND_API_KEY  │
                       └────────┬──────────┬─────────────┘
                                │          │
                       ┌────────▼──────────▼─────────────┐
                       │    Pipeline (pipeline.py)        │
                       │                                  │
                       │  Store: data → index → write     │
                       │  Query: scope → query → mediator │
                       │                                  │
                       │  Tracks token budgets per stage   │
                       └──┬────────┬────────┬────────────┘
                          │        │        │
                 ┌────────▼─┐  ┌───▼───┐  ┌─▼────────────┐
                 │RecordStore│  │Agent  │  │Sandbox       │
                 │           │  │Store  │  │Backend       │
                 │SQLite+FTS5│  │       │  │              │
                 │Fernet enc │  │CRUD + │  │Docker runner │
                 │Scope WHERE│  │files  │  │Bridge server │
                 └───────────┘  └───────┘  └──────────────┘
```

### Store pipeline (`POST /v1/store`)

```
Client sends:
  { data: "Sprint retro notes...",
    metadata: {"author": "alice"},
    index_agent_id: "idx-1"  }       ← OR index_text: "precomputed"
            │
            ▼
  Priority: index_text > index_agent_id > default index agent > nothing
            │
    ┌───────▼────────────────────────────────┐
    │ Index Agent Container (Docker)         │
    │                                        │
    │ ENV (advisory):                        │
    │   DOCUMENT_DATA = "Sprint retro…"      │
    │   DOCUMENT_METADATA = {"author":…}     │
    │                                        │
    │ TOOLS: search, read, list              │
    │                                        │
    │ stdout → JSON:                         │
    │   {"index_text": "...",                │
    │    "metadata": {"tags": [...]}}        │
    └───────┬────────────────────────────────┘
            │
            ▼
  RecordStore.write_record()
    - Encrypt data with Fernet
    - Store metadata JSON
    - Insert FTS index if index_text set
            │
            ▼
  Response: { record_id, created_at, metadata }
```

### Query pipeline (`POST /v1/query`)

```
Client sends:
  { query: "What decisions were made?",
    query_agent_id: "qa-1",
    scope_agent_id: "scope-1",          ← optional
    mediator_agent_id: "med-1",         ← optional
    max_tokens: 100000 }                ← optional budget cap
            │
            ▼
═══ STAGE 0: SCOPE (optional) ═══════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Scope Agent Container                                │
  │                                                      │
  │ ENV: QUERY_PROMPT, QUERY_AGENT_ID                    │
  │ TOOLS: search, read, list (FULL access, no scope)    │
  │        list_query_agent_files, read_query_agent_file  │
  │ BRIDGE EXTRAS:                                       │
  │   POST /sandbox/simulate  ← run nested query          │
  │   GET  /sandbox/agents/{id}/files                      │
  │                                                      │
  │ stdout → {"record_ids": ["r1", "r2", "r3"]}         │
  └─────────────────────────┬────────────────────────────┘
                            │
                  scope = ["r1","r2","r3"]
                  remaining_tokens -= scope_usage
                            │
                            ▼
═══ STAGE 1: QUERY ══════════════════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Query Agent Container                                │
  │                                                      │
  │ ENV: QUERY_PROMPT                                    │
  │ TOOLS: search, read, list (SCOPED to r1,r2,r3)      │
  │                                                      │
  │   search("migration") → only hits from r1,r2,r3     │
  │   read("r4")          → "Record not found" (scoped) │
  │   list()              → only shows r1,r2,r3          │
  │                                                      │
  │ stdout → "The team decided to migrate to Stripe…"   │
  └─────────────────────────┬────────────────────────────┘
                            │
                  output + records_accessed = ["r1","r3"]
                  remaining_tokens -= query_usage
                            │
                            ▼
═══ STAGE 2: MEDIATOR (optional) ════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Mediator Agent Container                             │
  │                                                      │
  │ ENV: RAW_OUTPUT, QUERY_PROMPT, RECORDS_ACCESSED      │
  │ TOOLS: none (mediator has NO data access)            │
  │                                                      │
  │ stdout → "[filtered] The team decided to migrate…"  │
  └─────────────────────────┬────────────────────────────┘
                            │
                            ▼
  Response:
    { output: "[filtered] The team decided…",
      records_accessed: ["r1", "r3"],
      mediated: true,
      usage: { total_tokens: 8500, max_tokens: 100000 } }
```

### What every agent container receives

```
┌──────────── ENFORCED (all agents, cannot bypass) ─────────┐
│                                                           │
│  BRIDGE_URL         http://host.docker.internal:<port>    │
│  SESSION_TOKEN      random 32-byte urlsafe token          │
│  AGENT_ROLE         query | scope | index | mediator      │
│  BUDGET_MAX_TOKENS  remaining token budget for this run   │
│  BUDGET_MAX_CALLS   remaining call budget for this run    │
│  OPENAI_BASE_URL    http://host.docker.internal:<port>/v1 │
│  OPENAI_API_KEY     same as SESSION_TOKEN                 │
│                                                           │
│  The bridge is the only network exit. OpenAI SDKs         │
│  auto-route through the bridge with zero code changes.    │
└───────────────────────────────────────────────────────────┘

┌──────────── ADVISORY (role-specific, ignorable) ──────────┐
│                                                           │
│  Index:    DOCUMENT_DATA, DOCUMENT_METADATA               │
│  Scope:    QUERY_PROMPT, QUERY_AGENT_ID                   │
│  Query:    QUERY_PROMPT                                   │
│  Mediator: RAW_OUTPUT, QUERY_PROMPT, RECORDS_ACCESSED     │
│                                                           │
│  Default agents use these. Custom agents may ignore       │
│  them entirely — the agent is a Docker container that     │
│  decides its own behavior.                                │
└───────────────────────────────────────────────────────────┘
```

### Inside a container: bridge as the single exit

```
┌───────────────────────────────────────────────────────────────┐
│                 Docker Internal Network                        │
│               (hivemind-sandbox, internal=true)                │
│                                                               │
│  ┌─────────────────────┐        ┌──────────────────────────┐  │
│  │  Agent Container    │        │  Bridge Server           │  │
│  │                     │        │  (ephemeral, per-run)    │  │
│  │  read-only rootfs   │  HTTP  │                          │  │
│  │  dropped ALL caps   │◄─────►│  GET  /health            │  │
│  │  no-new-privileges  │  only  │  GET  /tools             │  │
│  │  256MB mem limit    │  exit  │  POST /tools/{name}      │  │
│  │  1 CPU, 256 PIDs    │        │  POST /llm/chat          │  │
│  │                     │        │  POST /v1/chat/completions│  │
│  │  ┌───────────────┐  │        │       (OpenAI compat)    │  │
│  │  │ Agent code    │  │        │                          │  │
│  │  │ (any language │  │        │  Auth: Bearer token      │  │
│  │  │  any SDK)     │  │        │  Budget: 429 when out    │  │
│  │  └───────────────┘  │        │                          │  │
│  │                     │        │  Scope-only extras:      │  │
│  │  stdout = output    │        │  POST /sandbox/simulate  │  │
│  └─────────────────────┘        │  GET  /sandbox/agents/…  │  │
│                                 └────────────┬─────────────┘  │
│       ✗ No internet                          │                │
│       ✗ No other containers                  │                │
│       ✗ Linux: iptables per-container rules  │                │
└──────────────────────────────────────────────┼────────────────┘
                                               │
                                  ┌────────────▼──────────────┐
                                  │  LLM Provider             │
                                  │  (OpenRouter, OpenAI,     │
                                  │   Anthropic, etc.)        │
                                  │                           │
                                  │  Only the bridge talks    │
                                  │  to the outside world     │
                                  └───────────────────────────┘
```

### Scope enforcement

```
RecordStore has records: r1, r2, r3, r4, r5

scope = ["r1", "r2", "r3"]  (from scope agent or request)
       │
       ▼
build_tools(store, scope=["r1","r2","r3"])
       │  Creates tool handlers with scope baked into closures
       ▼
search("migration")
  → SQL: … WHERE records_fts MATCH 'migration'
         AND r.id IN ('r1','r2','r3')      ← enforced
  → Only r1, r2, r3 can appear in results

read("r4")
  → SQL: … WHERE r.id = 'r4'
         AND r.id IN ('r1','r2','r3')      ← r4 blocked
  → "Record not found"

The agent CANNOT bypass this. Scope is baked into the Python
closure at pipeline construction time. There is no bridge
endpoint to change it. The SQL WHERE clause is the boundary.
```

### Budget flow across pipeline stages

```
max_tokens = 100,000 (from request or global cap)
       │
       ▼
┌─ Stage 0: Scope Agent ─────────────────────────┐
│  Budget: 100,000 tokens                         │
│  Used: 2,000 tokens → remaining = 98,000        │
└─────────────────────────────────────────────────┘
       │  (512 tokens reserved for mediator if configured)
       ▼
┌─ Stage 1: Query Agent ──────────────────────────┐
│  Budget: 97,488 tokens                           │
│  Used: 45,000 tokens → remaining = 53,000        │
└─────────────────────────────────────────────────┘
       │
       ▼
┌─ Stage 2: Mediator Agent ───────────────────────┐
│  Budget: 53,000 tokens                           │
│  Used: 3,000 tokens                              │
│  (skipped if remaining < 128 tokens)            │
└─────────────────────────────────────────────────┘
       │
       ▼
Response: usage = { total_tokens: 50,000, max_tokens: 100,000 }

Within each stage, the bridge enforces per-call:
  Agent calls /llm/chat or /v1/chat/completions
    → Bridge checks budget.check() (preflight estimate)
    → If over limit → 429 "Budget exhausted"
    → If OK → forward to LLM provider
    → Record actual usage from provider response
    → Return response to agent
```

### Security layers

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: SCOPE (SQL-level, unbypassable)               │
│  ───────────────────────────────────────                │
│  WHERE r.id IN (scope_list)                             │
│  Agent tools physically cannot access out-of-scope      │
│  records. Baked into tool closures at pipeline level.    │
├─────────────────────────────────────────────────────────┤
│  Layer 2: DOCKER ISOLATION (runtime-level)              │
│  ───────────────────────────────────────                │
│  • Read-only root filesystem (+tmpfs for /tmp)          │
│  • ALL Linux capabilities dropped                       │
│  • no-new-privileges security option                    │
│  • Internal Docker network (bridge is only exit)        │
│  • Memory limit (256MB), CPU quota (1 core)             │
│  • PID limit (256)                                      │
│  • Linux: iptables DOCKER-USER rules per container      │
│    allowing ONLY bridge IP:port, DROP everything else   │
├─────────────────────────────────────────────────────────┤
│  Layer 3: BUDGET ENFORCEMENT (bridge-level)             │
│  ───────────────────────────────────────                │
│  • max_calls and max_tokens hard caps                   │
│  • Pre-flight check before each LLM call                │
│  • 429 rejection when exhausted                         │
│  • Serialized via asyncio Lock (no races)               │
├─────────────────────────────────────────────────────────┤
│  Layer 4: ENCRYPTION AT REST                            │
│  ───────────────────────────────────────                │
│  • records.data encrypted with Fernet                   │
│  • DB file useless without HIVEMIND_ENCRYPTION_KEY      │
├─────────────────────────────────────────────────────────┤
│  Layer 5: MEDIATOR (soft, LLM-based)                    │
│  ───────────────────────────────────────                │
│  • Optional agent audits query output                   │
│  • Has NO tool access (can't exfiltrate data)           │
│  • Defense in depth — LLM-dependent, not a hard boundary│
└─────────────────────────────────────────────────────────┘
```

## Configuration

All settings are loaded from `.env` with the `HIVEMIND_` prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `HIVEMIND_DB_PATH` | `./hivemind.db` | SQLite database path |
| `HIVEMIND_ENCRYPTION_KEY` | — | Fernet key for at-rest encryption (empty = plaintext) |
| `HIVEMIND_API_KEY` | — | Shared secret for HTTP auth. Required when binding non-local host |
| `HIVEMIND_HOST` | `127.0.0.1` | Server bind host |
| `HIVEMIND_PORT` | `8100` | Server bind port |
| `HIVEMIND_CORS_ALLOW_ORIGINS` | — | Comma-separated browser CORS origins. Empty = no CORS headers |
| `HIVEMIND_LLM_API_KEY` | — | API key for LLM provider (passed through bridge to agents) |
| `HIVEMIND_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `HIVEMIND_LLM_MODEL` | `anthropic/claude-sonnet-4.5` | Default LLM model |
| `HIVEMIND_LLM_TIMEOUT_SECONDS` | `45` | Timeout for outbound LLM provider calls from bridge |
| `HIVEMIND_BRIDGE_HOST` | `0.0.0.0` | Bridge bind host (must be reachable from Docker containers) |
| `HIVEMIND_DOCKER_HOST` | — | Optional Docker daemon host/socket (e.g. `unix:///Users/me/.docker/run/docker.sock`) |
| `HIVEMIND_DOCKER_NETWORK` | `hivemind-sandbox` | Docker network name used for sandbox containers |
| `HIVEMIND_DOCKER_NETWORK_INTERNAL` | `true` | Use Docker internal network mode when compatible with host bridge |
| `HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS` | `true` | Linux-only: install per-container `DOCKER-USER` firewall rules allowing only bridge IP:port (ignored on macOS/Windows) |
| `HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS_FAIL_CLOSED` | `true` | Linux-only: if firewall setup fails, terminate agent run instead of continuing |
| `HIVEMIND_CONTAINER_MEMORY_MB` | `256` | Max container memory limit (MB) |
| `HIVEMIND_CONTAINER_CPU_QUOTA` | `1.0` | Container CPU quota (1.0 = one core) |
| `HIVEMIND_CONTAINER_PIDS_LIMIT` | `256` | Max process count per sandbox container |
| `HIVEMIND_CONTAINER_READ_ONLY_FS` | `true` | Run containers with read-only root filesystem |
| `HIVEMIND_CONTAINER_DROP_ALL_CAPS` | `true` | Drop all Linux capabilities inside sandbox containers |
| `HIVEMIND_CONTAINER_NO_NEW_PRIVILEGES` | `true` | Enable Docker `no-new-privileges` security option |
| `HIVEMIND_MAX_LLM_CALLS` | `50` | Global max LLM calls per agent run |
| `HIVEMIND_MAX_TOKENS` | `200000` | Global max tokens per agent run |
| `HIVEMIND_AGENT_TIMEOUT` | `300` | Max agent runtime (seconds) |
| `HIVEMIND_AUTOLOAD_DEFAULT_AGENTS` | `true` | Auto-register defaults from configured default images using stable IDs |
| `HIVEMIND_DEFAULT_INDEX_AGENT` | `default-index` (example) | Default index agent ID (empty = caller provides index_text) |
| `HIVEMIND_DEFAULT_QUERY_AGENT` | `default-query` (example) | Default query agent ID (empty = query_agent_id required) |
| `HIVEMIND_DEFAULT_SCOPE_AGENT` | `default-scope` (example) | Default scope agent ID (empty = no scoping) |
| `HIVEMIND_DEFAULT_MEDIATOR_AGENT` | — | Default mediator agent ID (empty = no mediation) |
| `HIVEMIND_DEFAULT_INDEX_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_INDEX_AGENT` |
| `HIVEMIND_DEFAULT_QUERY_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_QUERY_AGENT` |
| `HIVEMIND_DEFAULT_SCOPE_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_SCOPE_AGENT` |
| `HIVEMIND_DEFAULT_MEDIATOR_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_MEDIATOR_AGENT` |

If `HIVEMIND_HOST` is non-local (not `127.0.0.1`/`localhost`), startup fails unless `HIVEMIND_API_KEY` is set.

The repository's `.env.example` provides a ready-to-run local profile with
`default-*` agent IDs and `hivemind-default-*:local` image tags. Build those
images before starting the server.

When `HIVEMIND_AUTOLOAD_DEFAULT_AGENTS=true`, startup upserts defaults by ID from the configured default image fields. This keeps stable IDs across DB resets so `.env` does not need per-run UUID edits.
If a configured default image is missing, startup now fails fast.

Generate an encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Uploading Agents

Agents are Docker containers. Upload source files as a tarball — the server builds the image:

```bash
# Create agent source directory with a Dockerfile
mkdir my-agent && cd my-agent
cat > Dockerfile <<'EOF'
FROM python:3.12-slim
RUN pip install httpx
COPY . /app
WORKDIR /app
CMD ["python", "agent.py"]
EOF
cat > agent.py <<'EOF'
import os, httpx
# ... your agent logic using BRIDGE_URL and SESSION_TOKEN ...
print("Agent output goes to stdout")
EOF

# Pack and upload
tar czf ../agent.tar.gz .
curl -X POST http://localhost:8100/v1/agents/upload \
  -F "name=my-agent" \
  -F "archive=@../agent.tar.gz"
# Returns: {"agent_id": "abc123", "name": "my-agent", "files_extracted": 2}
```

No Docker CLI needed on the client. No registry. No auth complexity.

## Agent Roles

All agents are Docker containers. Core defines four roles:

| Role | Purpose | Tools Available | Bridge Extras |
|------|---------|-----------------|---------------|
| **Index** | Extract index_text + metadata from documents | search, read, list | — |
| **Scope** | Determine record_id whitelist for queries | search, read, list (full access) | `/sandbox/simulate`, query-agent file inspection (same query agent only) |
| **Query** | Search and answer questions | search, read, list (scoped) | — |
| **Mediator** | Audit/filter query output | None | — |

Agents write their output to **stdout** and exit with code 0.

## Data Model

```
┌─────────────────────── SQLite DB ───────────────────────┐
│                                                         │
│  records                          records_fts (FTS5)    │
│  ┌──────────────────────┐         ┌──────────────────┐  │
│  │ id         TEXT PK   │         │ index_text       │  │
│  │ data       TEXT      │◄────────│ (virtual table   │  │
│  │   (Fernet encrypted) │  rowid  │  over records)   │  │
│  │ metadata   TEXT      │         └──────────────────┘  │
│  │   (schemaless JSON)  │                               │
│  │ index_text TEXT      │         agents                │
│  │   (nullable, FTS)    │         ┌──────────────────┐  │
│  │ created_at REAL      │         │ agent_id     PK  │  │
│  └──────────────────────┘         │ name, image      │  │
│                                   │ memory_mb        │  │
│  agent_files                      │ max_llm_calls    │  │
│  ┌──────────────────────┐         │ max_tokens       │  │
│  │ agent_id   TEXT      │────────►│ timeout_seconds  │  │
│  │ file_path  TEXT      │         └──────────────────┘  │
│  │ content    TEXT      │                               │
│  │ size_bytes INT       │                               │
│  └──────────────────────┘                               │
└─────────────────────────────────────────────────────────┘
```

**What's stored:**
- `records.data` — encrypted ciphertext (unreadable without `HIVEMIND_ENCRYPTION_KEY`)
- `records.metadata` — schemaless JSON (app-defined)
- `records.index_text` — FTS-searchable plaintext (nullable)

**What leaves via the API:**
- Agent-produced answers, optionally audited by a mediator agent
- Record metadata + index_text (via `GET /v1/admin/records/{id}`) — never raw data

## Database

SQLite with FTS5 full-text search and WAL mode.

Schema migrations run automatically on startup via Alembic.

```bash
# Manual migration commands
uv run alembic -c alembic.ini upgrade head   # upgrade to latest
uv run alembic -c alembic.ini current        # show current revision

# Inspect directly
sqlite3 hivemind.db ".schema"
sqlite3 hivemind.db "SELECT id, metadata, index_text FROM records"
sqlite3 hivemind.db "SELECT * FROM records_fts WHERE records_fts MATCH 'migration'"
```

## Project Structure

```
hivemind/
  __init__.py          # Public API exports
  version.py           # Version resolution (from package metadata)
  config.py            # Settings (env vars)
  core.py              # Hivemind class — thin wrapper (store + pipeline + health)
  server.py            # FastAPI HTTP server
  models.py            # Pydantic request/response models
  store.py             # RecordStore — SQLite + FTS5 + Fernet encryption
  pipeline.py          # Pipeline orchestrator (store + query pipelines)
  tools.py             # Agent tools (search, read, list, agent file tools)
  migrations.py        # Alembic migration runner
  alembic/             # Alembic env + version scripts
  sandbox/
    __init__.py        # Sandbox exports
    models.py          # AgentConfig, SandboxSettings, bridge models, SimulateRequest/Response
    settings.py        # build_sandbox_settings() — maps app config to sandbox config
    budget.py          # Per-query budget tracking (calls + tokens)
    bridge.py          # Ephemeral HTTP bridge server (LLM proxy + tools + simulation)
    docker_runner.py   # DockerRunner — container lifecycle, image extraction, cleanup
    backend.py         # SandboxBackend (implements run() interface)
    agents.py          # Agent registration + source file storage (SQLite)
agents/
  default-index/       # Default index agent (Docker image)
  default-query/       # Default query agent (Docker image)
  default-scope/       # Default scope agent (Docker image)
  default-mediator/    # Default mediator agent (Docker image)
  examples/            # Example agents — ready to upload (see agents/examples/README.md)
    simple-query/      # Minimal search + synthesize
    tool-loop-query/   # Agentic loop with parallel tools + auto-compaction
    metadata-scope/    # Team-based access control
    redact-mediator/   # PII redaction
tests/
  conftest.py                # Shared fixtures (tmp_db)
  test_store.py              # RecordStore + encryption unit tests
  test_api.py                # FastAPI endpoint unit tests
  test_pipeline.py           # Pipeline orchestrator tests
  test_simulate.py           # Simulation + budget carving tests
  test_tools.py              # Agent tools + agent file inspection tools
  test_core_store.py         # Core integration tests
  test_migrations.py         # Alembic migration tests
  test_sandbox_budget.py     # Budget tracking tests
  test_sandbox_agents.py     # Agent CRUD + file storage tests
  test_sandbox_backend.py    # Sandbox backend tests
  test_sandbox_bridge.py     # Bridge server tests
  test_docker_runner.py      # Docker runner tests (mocked)
  test_integration_docker.py # Docker integration tests (real containers)
  fixtures/
    Dockerfile.test-agent    # Minimal test image for integration tests
```

## API Reference

See [API.md](API.md) for the full API reference with all endpoints, request/response schemas, and examples.

## Tests

```bash
# Unit tests
uv run pytest tests/ -q

# Lint
uv tool run ruff check .

# Docker integration tests (requires Docker + test image)
docker build -t hivemind-test-agent:latest -f tests/fixtures/Dockerfile.test-agent tests/fixtures/
uv run pytest tests/test_integration_docker.py -v
```
