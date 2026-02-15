"""Bootstrap core schema.

Revision ID: 0001_bootstrap_core_schema
Revises:
Create Date: 2026-02-14
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_bootstrap_core_schema"
down_revision = None
branch_labels = None
depends_on = None


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


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "records"):
        op.execute("""
            CREATE TABLE records (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                index_text TEXT,
                created_at REAL NOT NULL
            )
        """)

    if "created_at" in _table_columns(bind, "records"):
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_created "
            "ON records(created_at)"
        )

    if not _table_exists(bind, "agents"):
        op.execute("""
            CREATE TABLE agents (
                agent_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL,
                entrypoint TEXT,
                memory_mb INTEGER NOT NULL DEFAULT 256,
                max_llm_calls INTEGER NOT NULL DEFAULT 20,
                max_tokens INTEGER NOT NULL DEFAULT 100000,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                created_at REAL NOT NULL
            )
        """)

    if not _table_exists(bind, "agent_files"):
        op.execute("""
            CREATE TABLE agent_files (
                agent_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                PRIMARY KEY (agent_id, file_path)
            )
        """)

    record_columns = _table_columns(bind, "records")
    if "index_text" in record_columns and not _table_exists(bind, "records_fts"):
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


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "records_fts"):
        op.execute("DROP TABLE records_fts")
    if _table_exists(bind, "agent_files"):
        op.execute("DROP TABLE agent_files")
    if _table_exists(bind, "agents"):
        op.execute("DROP TABLE agents")
    if _table_exists(bind, "records"):
        op.execute("DROP TABLE records")
