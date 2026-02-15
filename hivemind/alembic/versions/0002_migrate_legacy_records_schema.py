"""Migrate legacy records schema to v0.2 shape.

Revision ID: 0002_migrate_legacy_records_schema
Revises: 0001_bootstrap_core_schema
Create Date: 2026-02-14
"""
from __future__ import annotations

import json
import os
import time

from alembic import op
from cryptography.fernet import Fernet
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_migrate_legacy_records_schema"
down_revision = "0001_bootstrap_core_schema"
branch_labels = None
depends_on = None

_REQUIRED_RECORD_COLUMNS = {"id", "data", "metadata", "index_text", "created_at"}
_LEGACY_RECORD_COLUMNS = {"id", "text", "timestamp"}


def _table_exists(bind, table: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:table"
        ),
        {"table": table},
    ).fetchone()
    return row is not None


def _table_columns(bind, table: str) -> set[str]:
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def _normalize_legacy_metadata(value: object) -> str:
    if value is None:
        return "{}"

    parsed: object = value
    if isinstance(parsed, (bytes, bytearray)):
        parsed = parsed.decode(errors="replace")

    if isinstance(parsed, str):
        if not parsed.strip():
            return "{}"
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return json.dumps({"legacy_metadata": parsed})

    if isinstance(parsed, dict):
        return json.dumps(parsed)
    return json.dumps({"legacy_metadata": parsed}, default=str)


def _coerce_created_at(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _fernet_from_config() -> Fernet | None:
    config = op.get_context().config
    encryption_key = ""
    if config is not None:
        encryption_key = str(config.attributes.get("hivemind_encryption_key") or "")
    if not encryption_key:
        encryption_key = os.getenv("HIVEMIND_ENCRYPTION_KEY", "")
    return Fernet(encryption_key.encode()) if encryption_key else None


def _create_records_table() -> None:
    op.execute("""
        CREATE TABLE records (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            index_text TEXT,
            created_at REAL NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_records_created "
        "ON records(created_at)"
    )


def _rebuild_records_fts(bind) -> None:
    if _table_exists(bind, "records_fts"):
        op.execute("DROP TABLE records_fts")
    op.execute("""
        CREATE VIRTUAL TABLE records_fts USING fts5(
            index_text,
            content=records, content_rowid=rowid
        )
    """)
    op.execute(
        "INSERT INTO records_fts(rowid, index_text) "
        "SELECT rowid, index_text FROM records WHERE index_text IS NOT NULL"
    )


def _next_legacy_table_name(bind) -> str:
    base = "records_legacy_v01"
    if not _table_exists(bind, base):
        return base
    suffix = 1
    while _table_exists(bind, f"{base}_{suffix}"):
        suffix += 1
    return f"{base}_{suffix}"


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "records"):
        _create_records_table()
        _rebuild_records_fts(bind)
        return

    record_columns = _table_columns(bind, "records")
    if _REQUIRED_RECORD_COLUMNS.issubset(record_columns):
        _rebuild_records_fts(bind)
        return

    if not _LEGACY_RECORD_COLUMNS.issubset(record_columns):
        raise RuntimeError(
            "Unsupported records schema and no migration path is available. "
            f"Found columns: {sorted(record_columns)}"
        )

    legacy_table = _next_legacy_table_name(bind)
    op.execute(f"ALTER TABLE records RENAME TO {legacy_table}")
    _create_records_table()

    select_columns = ["id", "text", "timestamp"]
    if "metadata" in record_columns:
        select_columns.insert(2, "metadata")
    rows = bind.execute(
        sa.text(f"SELECT {', '.join(select_columns)} FROM {legacy_table}")
    ).fetchall()

    fernet = _fernet_from_config()
    for row in rows:
        row_data = row._mapping
        record_id = str(row_data.get("id") or "")
        if not record_id:
            continue
        plain_text = str(row_data.get("text") or "")
        stored_data = (
            fernet.encrypt(plain_text.encode()).decode()
            if fernet
            else plain_text
        )
        metadata_json = _normalize_legacy_metadata(row_data.get("metadata"))
        created_at = _coerce_created_at(row_data.get("timestamp"))
        index_text = plain_text if plain_text else None
        bind.execute(
            sa.text(
                "INSERT INTO records (id, data, metadata, index_text, created_at) "
                "VALUES (:id, :data, :metadata, :index_text, :created_at)"
            ),
            {
                "id": record_id,
                "data": stored_data,
                "metadata": metadata_json,
                "index_text": index_text,
                "created_at": created_at,
            },
        )

    op.execute(f"DROP TABLE {legacy_table}")
    _rebuild_records_fts(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _rebuild_records_fts(bind)
