# Architecture

Hivemind-core is organized around one product abstraction: a signed data room.

Three parties participate:

- A, the room owner, uploads private room data and defines the scope agent.
- B, the participant, verifies the room rules and may upload a query agent.
- The CVM operator runs the service and publishes observable deployment changes.

The room manifest is the contract A and B both verify before private material is
presented to the CVM.

## Room Contract

The manifest is canonical JSON signed by the owner key. It binds:

- `scope.agent_id` and scope source visibility,
- `query.mode`: fixed query agent or participant-uploaded query agent,
- query-agent visibility: `inspectable` or `sealed`,
- output visibility: `querier_only` or `owner_and_querier`,
- LLM egress allowlist and artifact egress setting,
- policy/rules text and hashes,
- deployment trust policy.

The invite link is `hmroom://...` and carries the room id, invite token, service
URL, and owner signing public key. The recipient verifies the room envelope
against that public key before asking anything.

## Storage

Room data is application-encrypted. Each room has a random DEK. The DEK is
wrapped to:

- the owner bearer,
- each invite bearer.

After a process restart or backend update, room data remains sealed until a
participant presents a bearer that has a wrap for that room.

Room-uploaded sealed query-agent files use the same room DEK. Reusable room
agents are encrypted under the tenant key when encrypted source storage is
active. Sealed agents never serve plaintext through the file-inspection API;
internal rebuild and digest paths can decrypt only inside the CVM after the
needed key is open.

## Execution

A room run always follows the same path:

```text
room manifest
  -> scope agent
  -> query agent
  -> mediator/output controls
  -> signed run record
```

Server-side enforcement applies even when a caller bypasses the CLI:

- the room scope agent is forced,
- fixed-query rooms force the configured query agent,
- uploadable rooms apply the room query-agent visibility,
- caller policy cannot override manifest policy,
- provider selection must be inside the room LLM allowlist,
- `llm_providers=[]` disables bridge LLM endpoints,
- `allow_artifacts=false` removes artifact upload egress,
- `querier_only` hides output and artifacts from the owner for participant runs.

The run attestation signs the room id, manifest hash, output visibility, allowed
LLM providers, artifact setting, room data count, and output hash.

## Egress

The intended room guarantee is narrow:

- final room output may leave according to `output.visibility`,
- LLM calls may leave only to allowed providers,
- artifacts may leave only when `allow_artifacts=true`.

Containers do not get direct internet access. They talk to the local sandbox
bridge, and the bridge enforces the room's LLM and artifact settings.

## Deployment Trust

The CVM operator can deploy new backends, but clients can observe and reason
about that through attestation and room trust policy.

Room trust modes:

- `operator_updates`: accept operator-approved deployments.
- `pinned`: accept only the compose hashes embedded in the room manifest.
- `owner_approved`: accept the room owner's compose allowlist.

A malicious future deployment cannot read old room data or room-sealed agent
source after a restart unless a room participant interacts with that deployment
and opens the room key. The CLI demonstrates the intended client-side pattern:
inspect the room, verify the owner signature, verify the CVM attestation, then
ask.

`--dangerously-skip-attestations` is a local-development escape hatch. It is not
a room trust policy.

## Public API Surface

Public routes are room-oriented:

```text
/v1/rooms
/v1/rooms/{room_id}/attest
/v1/rooms/{room_id}/open
/v1/rooms/{room_id}/data
/v1/rooms/{room_id}/runs
/v1/rooms/{room_id}/query-agents
/v1/room-agents
/v1/runs
/v1/attestation
```

Lower-level SQL, token, and generic agent endpoints are internal implementation
details and should not be used by clients.
