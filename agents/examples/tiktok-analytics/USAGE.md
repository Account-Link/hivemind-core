# TikTok Analytics Agent - Room-Native Usage

This example is a query agent for an uploadable room. It expects the room's
scope policy to allow aggregate reads over the
`data_xordi_tiktok_oauth_watch_history` table.

## Prerequisites

- A running Hivemind service, local or hosted.
- An active `hmk_...` profile configured with `hmctl init` or `hmctl signup`.
- A room invite whose manifest allows participant query-agent uploads.
- Artifact egress enabled on the room if you want `report.json`.

## Run With The CLI

```bash
hmctl profile use my-tenant
ROOM='hmroom://...'

hmctl room inspect "$ROOM"
hmctl room accept "$ROOM"

hmctl room ask "$ROOM" \
  --agent agents/examples/tiktok-analytics \
  --timeout 900 \
  --max-llm-calls 10 \
  --max-tokens 200000 \
  --fetch \
  "Analyse TikTok watch history and produce aggregate hashtag statistics."
```

`--fetch` downloads visible artifacts to `./hivemind-artifacts/<run_id>/`.
Without `--fetch`, inspect the run and artifact list with:

```bash
hmctl room runs <run_id> --json
```

Artifacts are served by the public run API:

```bash
curl "$CORE_URL/v1/runs/$RUN_ID/artifacts/report.json" \
  -H "Authorization: Bearer $TENANT_API_KEY" \
  -o report.json
```

## What The Agent Does

```text
tiktok-analytics agent
  -> POST /tools/execute_sql
     reads aggregate TikTok watch-history rows allowed by the room scope
  -> local statistics
     unique users, unique authors, hashtag counts
  -> POST /llm/chat
     summarizes themes and viewing patterns
  -> POST /sandbox/artifact-upload
     writes report.json into the Postgres-backed artifact store
  -> stdout
     JSON summary for the room run output
```

## Report Shape

`report.json` is JSON like:

```json
{
  "run_id": "a1b2c3d4e5f6",
  "statistics": {
    "total_videos": 50,
    "unique_users": 4,
    "unique_authors": 45,
    "total_hashtags_used": 120,
    "unique_hashtags": 85,
    "top_20_hashtags": [
      {"tag": "fyp", "count": 8},
      {"tag": "tiktok", "count": 3}
    ]
  },
  "llm_analysis": {
    "themes": ["Entertainment and comedy", "Music nostalgia"],
    "categories": ["Comedy/Humor", "Music", "Lifestyle"],
    "patterns": ["High engagement on comedy content"]
  }
}
```

## Register For Reuse

To register the agent as a reusable room agent without running it:

```bash
tar czf /tmp/tiktok-analytics.tar.gz -C agents/examples/tiktok-analytics .

curl -s -X POST "$CORE_URL/v1/room-agents" \
  -H "Authorization: Bearer $TENANT_API_KEY" \
  -F "name=tiktok-analytics" \
  -F "agent_type=query" \
  -F "inspection_mode=full" \
  -F "archive=@/tmp/tiktok-analytics.tar.gz;type=application/gzip" \
  | python3 -m json.tool
```

The response includes an `agent_id` and a background build `run_id`. Poll the
build with:

```bash
hmctl room runs <build_run_id> --json
```
