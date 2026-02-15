# Hivemind-Core curl Examples

Copy-paste workflows for local development.

## 0) Start The Server

If you are using this repo's `.env.example`, build default agent images first:

```bash
docker build -t hivemind-default-index:local agents/default-index
docker build -t hivemind-default-query:local agents/default-query
docker build -t hivemind-default-scope:local agents/default-scope
docker build -t hivemind-default-mediator:local agents/default-mediator
```

Then start the API:

```bash
uv run python -m hivemind.server
```

## 1) Shell Setup

These helpers support both auth-enabled and auth-disabled servers.

```bash
export BASE="http://localhost:8100"
# Set this only if HIVEMIND_API_KEY is configured on the server
export API_KEY="${API_KEY:-}"

AUTH_ARGS=()
if [ -n "$API_KEY" ]; then
  AUTH_ARGS=(-H "Authorization: Bearer $API_KEY")
fi

api() {
  curl -sS "${AUTH_ARGS[@]}" "$@"
}
```

`jq` is used below for parsing JSON responses.
If commands return `401 Unauthorized`, set `API_KEY` to match server `HIVEMIND_API_KEY`.

## 2) Health Check

```bash
api "$BASE/v1/health" | jq
```

Example response:

```json
{
  "status": "ok",
  "record_count": 0,
  "version": "0.2.0"
}
```

## 3) Ensure A Query Agent Exists

If you built default images and kept `.env.example` defaults, `default-query` should already exist.

```bash
export QUERY_AGENT="${QUERY_AGENT:-default-query}"

if api "$BASE/v1/agents/$QUERY_AGENT" | jq -e '.agent_id' >/dev/null 2>&1; then
  echo "Using query agent: $QUERY_AGENT"
else
  echo "Query agent '$QUERY_AGENT' not found. Uploading a minimal one..."

  mkdir -p /tmp/my-query-agent

  cat > /tmp/my-query-agent/Dockerfile <<'DOCKERFILE'
FROM python:3.12-slim
RUN pip install httpx
COPY . /app
WORKDIR /app
CMD ["python", "agent.py"]
DOCKERFILE

  cat > /tmp/my-query-agent/agent.py <<'PYTHON'
import json
import os

import httpx

bridge = os.environ["BRIDGE_URL"]
token = os.environ["SESSION_TOKEN"]
query = os.environ["QUERY_PROMPT"]

client = httpx.Client(
    base_url=bridge,
    headers={"Authorization": f"Bearer {token}"},
    timeout=30,
)

search_resp = client.post("/tools/search", json={"arguments": {"query": query}}).json()
rows = json.loads(search_resp["result"])

if not rows:
    print("No relevant records found.")
    raise SystemExit(0)

record_id = rows[0]["id"]
read_resp = client.post("/tools/read", json={"arguments": {"record_id": record_id}}).json()
record_text = read_resp["result"]

llm_resp = client.post(
    "/llm/chat",
    json={
        "messages": [
            {"role": "system", "content": "Answer using only the provided record text."},
            {"role": "user", "content": f"Question: {query}\n\nRecord:\n{record_text}"},
        ],
        "max_tokens": 512,
    },
).json()

print(llm_resp["content"])
PYTHON

  tar czf /tmp/my-query-agent.tar.gz -C /tmp/my-query-agent .

  QUERY_AGENT=$(api -X POST "$BASE/v1/agents/upload" \
    -F "name=my-query-agent" \
    -F "description=Minimal search-and-answer agent" \
    -F "archive=@/tmp/my-query-agent.tar.gz" | jq -r '.agent_id')

  echo "Uploaded query agent: $QUERY_AGENT"
fi
```

## 4) Store Records

```bash
R1=$(api -X POST "$BASE/v1/store" \
  -H "Content-Type: application/json" \
  -d '{
    "data": "Q3 retro: payments migrated from PayPal to Stripe due to better APIs and international fee savings.",
    "metadata": {"team": "payments", "author": "alice", "type": "decision"},
    "index_text": "Q3 retro payments PayPal Stripe migration API fees"
  }' | jq -r '.record_id')

R2=$(api -X POST "$BASE/v1/store" \
  -H "Content-Type: application/json" \
  -d '{
    "data": "Backend team switched internal service-to-service calls from REST to gRPC for lower latency.",
    "metadata": {"team": "backend", "author": "bob", "type": "decision"},
    "index_text": "backend REST gRPC migration internal services latency"
  }' | jq -r '.record_id')

echo "Stored records: $R1 $R2"
```

## 5) Query Records

### 5.1 Basic query

```bash
api -X POST "$BASE/v1/query" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"What technical decisions were made recently?\",
    \"query_agent_id\": \"$QUERY_AGENT\"
  }" | jq
```

Example response:

```json
{
  "output": "Two decisions were made: migrate payments to Stripe and switch internal APIs to gRPC.",
  "records_accessed": ["482feb9fd696", "a1b2c3d4e5f6"],
  "mediated": false,
  "usage": {"total_tokens": 8421, "max_tokens": 200000}
}
```

### 5.2 Scoped query

```bash
api -X POST "$BASE/v1/query" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"What did the payments team decide?\",
    \"query_agent_id\": \"$QUERY_AGENT\",
    \"scope\": [\"$R1\"]
  }" | jq
```

`scope` is enforced at SQL level, so the query agent cannot access records outside that list.

### 5.3 Query with explicit token cap

```bash
api -X POST "$BASE/v1/query" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"Summarize decisions in one paragraph\",
    \"query_agent_id\": \"$QUERY_AGENT\",
    \"scope\": [\"$R1\", \"$R2\"],
    \"max_tokens\": 50000
  }" | jq
```

## 6) Admin Record Endpoints

### 6.1 Get metadata + index text

```bash
api "$BASE/v1/admin/records/$R1" | jq
```

### 6.2 Patch metadata

```bash
api -X PATCH "$BASE/v1/admin/records/$R1" \
  -H "Content-Type: application/json" \
  -d '{"metadata": {"team": "payments", "author": "alice", "reviewed": true}}' | jq
```

### 6.3 Patch index text

```bash
api -X PATCH "$BASE/v1/admin/records/$R1" \
  -H "Content-Type: application/json" \
  -d '{"index_text": "payments Stripe migration reviewed"}' | jq
```

### 6.4 Delete a record

```bash
api -X DELETE "$BASE/v1/admin/records/$R2" | jq
```

## 7) Agent CRUD Endpoints

### 7.1 List agents

```bash
api "$BASE/v1/agents" | jq
```

### 7.2 Get one agent

```bash
api "$BASE/v1/agents/$QUERY_AGENT" | jq
```

### 7.3 Register from pre-built local image

```bash
api -X POST "$BASE/v1/agents" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "prebuilt-agent",
    "image": "myorg/my-agent:v1",
    "description": "Agent from local Docker image",
    "memory_mb": 512,
    "max_llm_calls": 30,
    "max_tokens": 120000,
    "timeout_seconds": 180
  }' | jq
```

### 7.4 Delete an agent

```bash
api -X DELETE "$BASE/v1/agents/$QUERY_AGENT" | jq
```

## 8) Full Three-Stage Pipeline (Scope + Query + Mediator)

If you have separate scope/query/mediator agent tarballs:

```bash
SCOPE_ID=$(api -X POST "$BASE/v1/agents/upload" \
  -F "name=scope-agent" \
  -F "archive=@scope.tar.gz" | jq -r '.agent_id')

QUERY_ID=$(api -X POST "$BASE/v1/agents/upload" \
  -F "name=query-agent" \
  -F "archive=@query.tar.gz" | jq -r '.agent_id')

MEDIATOR_ID=$(api -X POST "$BASE/v1/agents/upload" \
  -F "name=mediator-agent" \
  -F "archive=@mediator.tar.gz" | jq -r '.agent_id')

api -X POST "$BASE/v1/query" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"What did the payments team decide?\",
    \"scope_agent_id\": \"$SCOPE_ID\",
    \"query_agent_id\": \"$QUERY_ID\",
    \"mediator_agent_id\": \"$MEDIATOR_ID\"
  }" | jq
```

Pipeline order:
1. Scope agent chooses `record_ids`
2. Query agent answers from scoped records only
3. Mediator optionally filters/audits output
