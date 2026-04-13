# Build from this directory: docker build -t hivemind-claw-scope:local agents/claw-scope
#
# Stage 1: build claw binary from Rust source (claw-code/rust/)
FROM rust:1.88-slim AS builder

RUN apt-get update && apt-get install -y pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY claw-code/rust/ .

RUN cargo build --release -p rusty-claude-cli

# Stage 2: runtime — Python 3.12 + claw binary
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/*

# Copy claw binary
COPY --from=builder /build/target/release/claw /usr/local/bin/claw

# App files
WORKDIR /app
COPY mcp_bridge.py .
COPY entrypoint.sh .

# Prepare workspace (claw runs here)
RUN mkdir -p /workspace && \
    useradd -m -s /bin/bash agent && \
    chown agent:agent /workspace /app

USER agent
WORKDIR /workspace

CMD ["/app/entrypoint.sh"]
