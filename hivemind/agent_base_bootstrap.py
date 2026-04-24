"""Ensure `hivemind-agent-base:latest` exists in the local Docker daemon.

Agent Dockerfiles use `FROM hivemind-agent-base:latest` as a shared base.
In CVM deployments (Phala / dstack) the daemon starts empty, so the first
agent upload fails with `pull access denied for hivemind-agent-base`.

This module is called once at server startup. It first tries to pull the
image from GHCR; if that fails (private package, offline, etc.) it builds
the image locally from an inlined Dockerfile that matches
``agents/base/Dockerfile``. Embedding the Dockerfile text (rather than
shipping the file) keeps bootstrap working in container images that omit
``agents/`` from their COPY layers.
"""

from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_GHCR_IMAGE_DEFAULT = "ghcr.io/account-link/hivemind-agent-base:latest"
_LOCAL_TAG = "hivemind-agent-base:latest"

# Keep this in sync with agents/base/Dockerfile. The boot-time build is the
# fallback when GHCR pull fails, so the recipe must be self-sufficient.
_INLINE_DOCKERFILE = """\
FROM python:3.12-slim

RUN apt-get update && \\
    apt-get install -y --no-install-recommends curl ca-certificates && \\
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
    apt-get install -y --no-install-recommends nodejs && \\
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest

RUN pip install --no-cache-dir --upgrade "claude-agent-sdk>=0.1.61" aiohttp

RUN useradd -m -s /bin/bash agent

WORKDIR /app
RUN chown agent:agent /app
ENV PYTHONPATH=/app

USER agent
"""


def _client():
    import docker  # deferred — tests and CLI may not have docker
    return docker.from_env()


def _image_present(tag: str) -> bool:
    import docker.errors
    try:
        _client().images.get(tag)
        return True
    except docker.errors.ImageNotFound:
        return False
    except Exception as e:
        logger.warning("agent-base bootstrap: image inspect failed: %s", e)
        return False


def _pull_and_tag(source: str) -> bool:
    try:
        client = _client()
        logger.info("agent-base bootstrap: pulling %s", source)
        img = client.images.pull(source)
        img.tag(_LOCAL_TAG.split(":")[0], tag=_LOCAL_TAG.split(":")[1])
        logger.info("agent-base bootstrap: tagged %s from %s", _LOCAL_TAG, source)
        return True
    except Exception as e:
        logger.info("agent-base bootstrap: pull failed (%s)", e)
        return False


def _build_inline() -> bool:
    try:
        client = _client()
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Dockerfile"), "w", encoding="utf-8") as f:
                f.write(_INLINE_DOCKERFILE)
            logger.info("agent-base bootstrap: building %s from inline Dockerfile", _LOCAL_TAG)
            client.images.build(path=tmp, tag=_LOCAL_TAG, rm=True)
        logger.info("agent-base bootstrap: built %s", _LOCAL_TAG)
        return True
    except Exception as e:
        logger.error("agent-base bootstrap: inline build failed: %s", e)
        return False


def ensure_agent_base_image() -> bool:
    """Guarantee `hivemind-agent-base:latest` is in the daemon.

    Fast path: image already tagged → return True immediately.
    Slow path: pull from GHCR, else build from inline Dockerfile.

    Returns True on success. On failure, logs the error and returns False;
    the server still boots and agent uploads will surface the underlying
    error at build time.
    """
    if _image_present(_LOCAL_TAG):
        logger.info("agent-base bootstrap: %s already present", _LOCAL_TAG)
        return True

    source = os.environ.get("HIVEMIND_AGENT_BASE_IMAGE", _GHCR_IMAGE_DEFAULT)
    if _pull_and_tag(source):
        return True
    return _build_inline()
