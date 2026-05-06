# python:3.12-slim, resolved 2026-04-29 (matches agents/base/Dockerfile).
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3

# git: needed to pip-install hermes-agent from a tag.
# ca-certificates: TLS for HTTPS pulls.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Pin to release tag v2026.4.30 (= hermes-agent 0.12.0, published 2026-04-30).
# Pin reasoning: Hermes is rapidly evolving; pin to a tag we've validated,
# bump deliberately. Switch to a commit SHA after a real eval pass.
RUN pip install --no-cache-dir \
    "hermes-agent @ git+https://github.com/NousResearch/hermes-agent@v2026.4.30"

# httpx is already a transitive dep of hermes-agent (base requirement).
# The hivemind plugin uses it directly — no extra install needed.

RUN useradd -m -s /bin/bash agent

# Bundled plugins layout: /opt/hivemind/plugins/<plugin-name>/
# Hermes resolves bundled plugins via HERMES_BUNDLED_PLUGINS — see
# hermes_cli/plugins.py:get_bundled_plugins_dir().
COPY --chown=agent:agent plugins/ /opt/hivemind/plugins/

WORKDIR /app
RUN chown agent:agent /app

# HERMES_HOME on /tmp so per-session state never persists across runs.
# Per-agent wrappers control toolset whitelist via `--toolsets hivemind`
# at invocation time, so no config.yaml is baked into the base image.
ENV PYTHONPATH=/app \
    HERMES_BUNDLED_PLUGINS=/opt/hivemind/plugins \
    HERMES_HOME=/tmp/hermes

USER agent
