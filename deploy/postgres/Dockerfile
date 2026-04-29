# postgres:16, resolved 2026-04-29.
FROM postgres:16@sha256:71e27bf60b70bded003791b5573f8b808365613f341df20ffcf0c1ed7bc13ddf

# Install wal-g and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    jq \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Install wal-g binary
ARG WALG_VERSION=v3.0.8
RUN curl -fsSL "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-22.04-amd64.tar.gz" \
    | tar xz -C /usr/local/bin/ \
    && mv /usr/local/bin/wal-g-pg-22.04-amd64 /usr/local/bin/wal-g \
    && chmod +x /usr/local/bin/wal-g

# WAL-G runtime defaults. Secret values are injected by compose/runtime
# instead of being declared in image metadata.
ENV AWS_S3_FORCE_PATH_STYLE="true"
ENV AWS_REGION="auto"

# Cron job for daily base backups
RUN echo "0 3 * * * root /usr/local/bin/backup-push.sh >> /var/log/walg-backup.log 2>&1" > /etc/cron.d/walg-backup \
    && chmod 0644 /etc/cron.d/walg-backup

# Build context is deploy/ (set in docker-compose.yaml)
COPY postgres/backup-push.sh /usr/local/bin/backup-push.sh
COPY postgres/init-archive.sh /docker-entrypoint-initdb.d/init-archive.sh
COPY postgres/entrypoint-wrapper.sh /usr/local/bin/entrypoint-wrapper.sh
COPY postgres/reset-password.sh /usr/local/bin/reset-password.sh

RUN chmod +x /usr/local/bin/backup-push.sh \
    /docker-entrypoint-initdb.d/init-archive.sh \
    /usr/local/bin/entrypoint-wrapper.sh \
    /usr/local/bin/reset-password.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
CMD ["postgres"]
