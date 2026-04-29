# hivemind-core

Hivemind-core is a data-room primitive for mutually distrusting tenants and
their agents.

A room owner uploads private data and a scope agent. A participant inspects the
signed room rules before entering, optionally uploads their own query agent, and
gets only the room-approved output. The room manifest binds the scope agent,
query-agent mode, code visibility, output visibility, LLM egress allowlist, and
deployment trust policy.

The service is designed for a dstack Confidential VM. Tenants verify the live
CVM attestation before presenting data or agent code. Room data is encrypted
under a per-room key wrapped to the owner key and invite token. Room-uploaded
sealed query agents use that same room key, so after a restart or backend update
private room material stays unreadable until a participant interacts again.

## Install

```bash
uv tool install --editable .
hivemind --help
```

## Connect

```bash
hivemind init --service https://hivemind.example --api-key hmk_...
hivemind trust attest
```

For local development:

```bash
./scripts/quickstart.sh
hivemind init --service http://localhost:8100 --api-key hmk_...
```

## Owner Flow

Create a room from a local scope-agent directory:

```bash
hivemind room create ./scope-agent \
  --rules-file rules.md \
  --scope-visibility inspectable
```

Create a fixed-query room:

```bash
hivemind room create ./scope-agent \
  --query-agent ./query-agent \
  --query-visibility sealed \
  --rules-file rules.md
```

Create an uploadable room where the participant can bring their own sealed
query agent:

```bash
hivemind room create ./scope-agent \
  --query-visibility sealed \
  --rules-file rules.md
```

Add private room data:

```bash
hivemind room add-data <room_id> --file dataset.md --meta source=dataset
hivemind room data <room_id>
```

The create command prints one `hmroom://...` invite link. That link contains the
room id, invite token, service URL, and owner signing public key.

## Participant Flow

Inspect the room before entering:

```bash
hivemind room inspect 'hmroom://...'
```

Ask with a fixed query agent:

```bash
hivemind room ask 'hmroom://...' "What changed this month?"
```

Ask with a participant-uploaded query agent:

```bash
hivemind room ask 'hmroom://...' "What changed this month?" --agent ./my-query-agent
```

Every answer is checked against the accepted room manifest hash and the live CVM
run signer. The default behavior is fail-closed when the run attestation is
missing or does not match the room.

## Trust Policy

Rooms have one deployment trust policy:

- `operator_updates`: accept CVM deployments approved by the operator governance
  path.
- `pinned`: accept only the compose hashes listed in the room manifest.
- `owner_approved`: accept the owner-maintained compose hash allowlist for this
  room.

Update a room trust allowlist without changing the invite link:

```bash
hivemind room trust <room_id> --mode owner_approved --approve-live
```

The global `--dangerously-skip-attestations` flag exists only for local
development without a TEE. Production clients should inspect and verify the room.

## Public API

The public API is room-first:

```text
POST /v1/rooms
GET  /v1/rooms
GET  /v1/rooms/{room_id}
GET  /v1/rooms/{room_id}/attest
POST /v1/rooms/{room_id}/open
POST /v1/rooms/{room_id}/data
GET  /v1/rooms/{room_id}/data
POST /v1/rooms/{room_id}/runs
POST /v1/rooms/{room_id}/query-agents

POST /v1/room-agents
GET  /v1/room-agents
GET  /v1/room-agents/{agent_id}
GET  /v1/room-agents/{agent_id}/attest
GET  /v1/room-agents/{agent_id}/files
GET  /v1/room-agents/{agent_id}/files/{path}

GET  /v1/runs
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/artifacts/{filename}
GET  /v1/attestation
```

Lower-level SQL, token, and generic agent endpoints are internal implementation
details. New clients should use the room API only.
