# Phala Cloud Deployment Guide

hivemind-core runs on Phala Cloud TEE infrastructure as two CVMs (Confidential Virtual Machines).

## Architecture

```
Postgres CVM (persistent, never redeployed unless necessary)
+-- db         -- postgres:16, data on encrypted volume
+-- sql-proxy  -- HTTP-to-SQL proxy, port 8080
        |
        | HTTPS (Phala auto-TLS)
        v
App CVM (can redeploy freely)
+-- hivemind-core -- port 8100
+-- dind           -- Docker-in-Docker for agent containers
```

All agents (scope, query, index, mediator) run as Docker containers inside the App CVM.

## Prerequisites

- [Phala Cloud CLI](https://docs.phala.network/developers/getting-started) installed
- Images pushed to GHCR (via `push-ghcr.sh`)
- Generate secrets before starting

## Step 0: Generate Secrets

```bash
# DB password
python3 -c "import secrets; print(secrets.token_urlsafe(24))"

# SQL proxy shared secret
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Hivemind API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Step 1: Deploy Postgres CVM

Edit `deploy/phala/.env`, fill in `DB_PASS` and `SQL_PROXY_KEY`:

```bash
phala deploy -n hivemind-pg \
  -c deploy/phala/docker-compose.postgres.yaml \
  -e deploy/phala/.env --wait
```

After deploy, note the CVM ID. SQL proxy is at:

```
https://<pg_cvm_id>-8080.app.phala.network
```

Verify:

```bash
curl https://<pg_cvm_id>-8080.app.phala.network/health
# {"status": "ok"}
```

## Step 2: Deploy App CVM

Edit `deploy/phala/.env`, fill in the SQL proxy URL and keys:

```bash
phala deploy -n hivemind-core \
  -c deploy/phala/docker-compose.core.yaml \
  -e deploy/phala/.env --wait
```

Verify:

```bash
curl -H "Authorization: Bearer <api-key>" \
  https://<core_cvm_id>-8100.app.phala.network/v1/health
```

## Step 3: Import Data

```bash
export SQL_PROXY_URL="https://<pg_cvm_id>-8080.app.phala.network"
export SQL_PROXY_KEY="<your-proxy-secret>"

# Import SQL dump
./deploy/postgres/import-data.sh sql dump.sql

# Import CSV (table must exist first)
./deploy/postgres/import-data.sh csv users users.csv
```

## Updating

```bash
# Redeploy core (safe, stateless)
phala deploy --cvm-id hivemind-core \
  -c deploy/phala/docker-compose.core.yaml \
  -e deploy/phala/.env --wait

# DO NOT casually redeploy postgres (data loss!)
```

## Troubleshooting

```bash
# Check CVM status
phala list

# View logs
phala logs hivemind-core
phala logs hivemind-pg

# Test SQL proxy
curl https://<pg_cvm_id>-8080.app.phala.network/health

# Check DB schema
curl -H "X-Proxy-Key: $SQL_PROXY_KEY" \
  https://<pg_cvm_id>-8080.app.phala.network/schema
```
