#!/bin/bash
set -euo pipefail

# push-ghcr.sh — Build and push hivemind images to GHCR
#
# Two CVMs: postgres (DB + SQL proxy) and core (hivemind + Docker agents)
#
# Usage:
#   ./deploy/phala/push-ghcr.sh              # push all images
#   ./deploy/phala/push-ghcr.sh core         # push only hivemind-core
#   ./deploy/phala/push-ghcr.sh postgres     # push only postgres
#
# Prerequisites:
#   export GHCR_TOKEN=ghp_xxx
#   docker login ghcr.io -u zzh --password-stdin <<< "$GHCR_TOKEN"

REGISTRY="ghcr.io/account-link"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TAG="${IMAGE_TAG:-latest}"

# --- Core services ---

push_core() {
    echo "==> Building hivemind-core..."
    docker build \
        -t "${REGISTRY}/hivemind-core:${TAG}" \
        -f "${REPO_ROOT}/deploy/Dockerfile" \
        "${REPO_ROOT}"
    echo "==> Pushing hivemind-core:${TAG}..."
    docker push "${REGISTRY}/hivemind-core:${TAG}"
    echo "    Done: ${REGISTRY}/hivemind-core:${TAG}"
}

push_postgres() {
    echo "==> Building hivemind-postgres..."
    docker build \
        -t "${REGISTRY}/hivemind-postgres:${TAG}" \
        -f "${REPO_ROOT}/deploy/postgres/Dockerfile" \
        "${REPO_ROOT}/deploy"
    echo "==> Pushing hivemind-postgres:${TAG}..."
    docker push "${REGISTRY}/hivemind-postgres:${TAG}"
    echo "    Done: ${REGISTRY}/hivemind-postgres:${TAG}"
}

push_sql_proxy() {
    echo "==> Building hivemind-sql-proxy..."
    docker build \
        -t "${REGISTRY}/hivemind-sql-proxy:${TAG}" \
        -f "${REPO_ROOT}/deploy/postgres/Dockerfile.sql-proxy" \
        "${REPO_ROOT}/deploy"
    echo "==> Pushing hivemind-sql-proxy:${TAG}..."
    docker push "${REGISTRY}/hivemind-sql-proxy:${TAG}"
    echo "    Done: ${REGISTRY}/hivemind-sql-proxy:${TAG}"
}

# --- Entry point ---

TARGET="${1:-all}"

case "$TARGET" in
    core)      push_core ;;
    sql-proxy) push_sql_proxy ;;
    postgres)  push_postgres ;;
    all)
        push_core
        push_postgres
        push_sql_proxy
        ;;
    *)
        echo "Usage: $0 [core|postgres|sql-proxy|all]"
        exit 1
        ;;
esac

echo ""
echo "==> Done. Deploy to Phala:"
echo "    1. phala deploy -n hivemind-pg   -c deploy/phala/docker-compose.postgres.yaml"
echo "    2. phala deploy -n hivemind-core -c deploy/phala/docker-compose.core.yaml"
