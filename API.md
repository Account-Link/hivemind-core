# API

The public API is centered on signed rooms: attested recall agreements between
an owner and a participant.

Use an owner key (`hmk_...`) to create rooms, upload room agents, add room data,
and update room trust. Use an invite token from an `hmroom://` link to inspect,
open, and run inside that room.

## Signup And Billing

### `POST /v1/signup`

Disabled unless the operator sets `HIVEMIND_SELF_SERVE_SIGNUP_ENABLED=true`.
Creates a tenant owner key with `$0.00` starting balance. Signup does not use
credit codes; users redeem credit codes separately after signup.

```json
{
  "name": "alice"
}
```

Response includes the plaintext `hmk_...` key once:

```json
{
  "tenant_id": "t_...",
  "name": "alice",
  "api_key": "hmk_...",
  "starter_credit_micro_usd": 0,
  "balance_micro_usd": 0
}
```

### `GET /v1/billing`

Owner-only. Returns the authenticated tenant's current balance and recent
ledger entries.

### `POST /v1/billing/credit-codes/redeem`

Owner-only. Redeems an admin-minted credit code into an existing tenant.
Credit codes are not signup codes and are never required to create the tenant.

```json
{
  "credit_code": "hmcc_..."
}
```

### Admin Billing

`POST /v1/admin/credit-codes` creates a tracked credit code and returns the
plaintext code once. `GET /v1/admin/credit-codes` lists code status without
the plaintext code. `GET /v1/admin/billing` shows every tenant's current
balance, total credited, and total spent. `GET /v1/admin/billing/ledger` shows
recent ledger entries across tenants.

Credit enforcement uses the existing `HIVEMIND_BILLING_ENFORCE_CREDITS=true`
switch, so operators should configure model prices before enabling it.

## Rooms

### `POST /v1/rooms`

Create a signed room manifest and invite token.

```json
{
  "name": "diligence",
  "rules": "Only answer aggregate questions.",
  "policy": "Optional scope-agent policy text.",
  "scope_agent_id": "abc123",
  "query_mode": "uploadable",
  "query_agent_id": null,
  "query_visibility": "sealed",
  "output_visibility": "querier_only",
  "egress": {
    "llm_providers": ["tinfoil"],
    "allow_artifacts": false
  },
  "trust": {
    "mode": "operator_updates",
    "allowed_composes": []
  }
}
```

Response includes:

```json
{
  "room_id": "room_...",
  "room": {"manifest": {}, "manifest_hash": "..."},
  "token": "hmq_...",
  "token_id": "...",
  "link": "hmroom://..."
}
```

### `GET /v1/rooms/{room_id}/attest`

Returns the signed room envelope, scope-agent attestation, fixed query-agent
attestation when present, and the live CVM attestation bundle. Clients should
verify the room envelope against the owner public key embedded in the invite
link before presenting private data or agent code.

### `POST /v1/rooms/{room_id}/open`

Presents the current bearer and opens the room key in process memory. This is
also performed by room data writes and room runs when needed.

### `POST /v1/rooms/{room_id}/data`

Owner-only. Adds encrypted room data.

```json
{
  "text": "private document text",
  "metadata": {"source": "dataset"}
}
```

### `GET /v1/rooms/{room_id}/data`

Owner-only. Lists owner-visible room data after opening the room key.

### `POST /v1/rooms/{room_id}/runs`

Run the room's fixed query agent or a previously uploaded query agent allowed by
the manifest. If the room query visibility is `inspectable`, the plaintext
prompt is stored with the run history. If query visibility is `sealed`, only
the signed run attestation's prompt hash is retained.

```json
{
  "query": "What changed this month?",
  "query_agent_id": "optional-for-uploadable-rooms",
  "model": "optional",
  "provider": "tinfoil"
}
```

Response:

```json
{
  "run_id": "...",
  "query_agent_id": "...",
  "scope_agent_id": "...",
  "room_id": "room_...",
  "status": "pending"
}
```

Poll `GET /v1/runs/{run_id}`.

### `POST /v1/rooms/{room_id}/query-agents`

Upload and run a participant query agent in an uploadable room. Multipart form:

- `archive`: `.tar.gz` containing the Dockerfile and agent source.
- `name`
- `prompt`
- optional `model`, `provider`, `memory_mb`, `max_llm_calls`, `max_tokens`,
  `timeout_seconds`

The server applies the room query-agent visibility, egress allowlist, policy,
output visibility, and run attestation binding.

### `POST /v1/rooms/{room_id}/trust`

Owner-only. Re-signs the same room with an updated deployment trust policy.

```json
{
  "mode": "owner_approved",
  "allowed_composes": ["abc..."],
  "append_live": true
}
```

## Room Agents

### `POST /v1/room-agents`

Owner-only. Upload a reusable scope, query, index, or mediator agent.

Multipart form:

- `archive`
- `name`
- `agent_type`: `scope`, `query`, `index`, or `mediator`
- `inspection_mode`: `full` or `sealed`
- `private_paths`: JSON list of archive paths excluded from public source digest

`sealed` agents cannot be read through the files API. Their source remains
available to internal rebuild and digest paths inside the CVM.

### `GET /v1/room-agents`

List room agents visible to the caller.

### `GET /v1/room-agents/{agent_id}/attest`

Returns agent config, source digests, image digest, inspection mode, and live CVM
attestation.

### `GET /v1/room-agents/{agent_id}/files`

List extracted source file paths and sizes. Sealed agents list paths but do not
serve plaintext file bodies.

## Runs

### `GET /v1/runs/{run_id}`

Returns run status, output when visible to the caller, artifacts when enabled,
and the CVM-signed run attestation envelope.

The signed body includes the room id, room manifest hash, output visibility,
allowed LLM providers, artifact setting, and output hash.

### `GET /v1/runs`

List recent runs visible to the caller.

### `GET /v1/runs/{run_id}/artifacts/{filename}`

Fetch a visible artifact for a run. Rooms disable artifact egress by default.

## Attestation

### `GET /v1/attestation`

Public dstack attestation bundle. Clients use this to verify the live CVM before
presenting room data, invite tokens, or agent code.
