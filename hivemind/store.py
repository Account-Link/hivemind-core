import json
import logging
import sqlite3
import threading

from cryptography.fernet import Fernet
from .migrations import run_migrations

logger = logging.getLogger(__name__)

_REQUIRED_RECORD_COLUMNS = {"id", "data", "metadata", "index_text", "created_at"}
_REQUIRED_AGENT_COLUMNS = {
    "agent_id",
    "name",
    "description",
    "image",
    "entrypoint",
    "memory_mb",
    "max_llm_calls",
    "max_tokens",
    "timeout_seconds",
    "created_at",
}
_REQUIRED_AGENT_FILE_COLUMNS = {"agent_id", "file_path", "content", "size_bytes"}


class RecordStore:
    """Simplified record storage with schemaless metadata and FTS5.

    Schema:
      records: id, data (encrypted), metadata (JSON), index_text (FTS, nullable), created_at
      records_fts: FTS5 virtual table over index_text
      agents / agent_files: unchanged from v1
    """

    def __init__(self, db_path: str, encryption_key: str = ""):
        self.db_path = db_path
        self._fernet = Fernet(encryption_key.encode()) if encryption_key else None
        self._lock = threading.RLock()
        run_migrations(db_path, encryption_key=encryption_key)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._assert_required_schema()

    def _table_exists(self, table: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}

    def _assert_required_columns(self, table: str, required: set[str]) -> None:
        columns = self._table_columns(table)
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(
                f"Unsupported schema for table '{table}'. Missing columns: {missing}."
            )

    def _assert_required_schema(self) -> None:
        self._assert_required_columns("records", _REQUIRED_RECORD_COLUMNS)
        self._assert_required_columns("agents", _REQUIRED_AGENT_COLUMNS)
        self._assert_required_columns("agent_files", _REQUIRED_AGENT_FILE_COLUMNS)
        if not self._table_exists("records_fts"):
            raise RuntimeError(
                "Unsupported schema: missing FTS table 'records_fts'."
            )

    # ── Encryption ──

    def _encrypt(self, plaintext: str) -> str:
        if not self._fernet:
            return plaintext
        return self._fernet.encrypt(plaintext.encode()).decode()

    def _decrypt(self, stored: str) -> str:
        if not self._fernet:
            return stored
        return self._fernet.decrypt(stored.encode()).decode()

    # ── Scope helpers ──

    def _apply_scope(
        self, sql: str, params: list, scope: list[str] | None, alias: str = "r"
    ) -> tuple[str, list]:
        """Apply record_id whitelist scope. None = no restriction."""
        if scope is not None:
            for idx, value in enumerate(scope):
                if not isinstance(value, str):
                    raise ValueError(f"scope[{idx}] must be a string")
            if not scope:
                # Empty list — match nothing
                sql += " AND 1=0"
            else:
                placeholders = ",".join("?" * len(scope))
                sql += f" AND {alias}.id IN ({placeholders})"
                params.extend(scope)
        return sql, params

    # ── Write ──

    def write_record(
        self,
        id: str,
        data: str,
        metadata: dict,
        index_text: str | None,
        created_at: float,
    ) -> None:
        """Insert a record with optional FTS index_text, atomically."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO records (id, data, metadata, index_text, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        id,
                        self._encrypt(data),
                        json.dumps(metadata),
                        index_text,
                        created_at,
                    ),
                )
                if index_text is not None:
                    rowid = self._conn.execute(
                        "SELECT rowid FROM records WHERE id = ?", (id,)
                    ).fetchone()[0]
                    self._conn.execute(
                        "INSERT INTO records_fts(rowid, index_text) VALUES (?, ?)",
                        (rowid, index_text),
                    )

    def update_record(
        self,
        record_id: str,
        *,
        metadata: dict | None = None,
        index_text: str | None = None,
        update_metadata: bool = False,
        update_index_text: bool = False,
    ) -> bool:
        """Atomically update metadata and/or index_text for a record.

        Returns False when the record does not exist.
        """
        if not update_metadata and not update_index_text:
            raise ValueError("Provide metadata and/or index_text to update")
        if update_metadata and metadata is None:
            raise ValueError("metadata is required when update_metadata is true")
        if update_index_text and index_text is None:
            raise ValueError("index_text is required when update_index_text is true")

        with self._lock:
            row = self._conn.execute(
                "SELECT rowid, index_text FROM records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if not row:
                return False

            rowid, old_text = row[0], row[1]
            with self._conn:
                if update_metadata:
                    self._conn.execute(
                        "UPDATE records SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata), record_id),
                    )
                if update_index_text:
                    if old_text is not None:
                        self._conn.execute(
                            "INSERT INTO records_fts(records_fts, rowid, index_text) "
                            "VALUES ('delete', ?, ?)",
                            (rowid, old_text),
                        )
                    self._conn.execute(
                        "UPDATE records SET index_text = ? WHERE id = ?",
                        (index_text, record_id),
                    )
                    self._conn.execute(
                        "INSERT INTO records_fts(rowid, index_text) VALUES (?, ?)",
                        (rowid, index_text),
                    )
            return True

    def update_index_text(self, record_id: str, index_text: str) -> bool:
        """Update a record's FTS index_text. Returns False if record not found."""
        return self.update_record(
            record_id,
            index_text=index_text,
            update_index_text=True,
        )

    def update_metadata(self, record_id: str, metadata: dict) -> bool:
        """Replace a record's metadata JSON. Returns False if not found."""
        return self.update_record(
            record_id,
            metadata=metadata,
            update_metadata=True,
        )

    # ── Scoped reads ──

    def search(
        self,
        query: str,
        scope: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """FTS5 search over index_text, scope-enforced."""
        sql = (
            "SELECT r.id, r.metadata, r.index_text, rank "
            "FROM records_fts "
            "JOIN records r ON records_fts.rowid = r.rowid "
            "WHERE records_fts MATCH ?"
        )
        params: list = [query]
        sql, params = self._apply_scope(sql, params, scope)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "syntax" in msg or "fts5" in msg or "malformed" in msg or "unterminated" in msg:
                logger.debug("FTS query parse error: %s", e)
                return []
            raise
        results = []
        for row in rows:
            results.append({
                "id": row[0],
                "metadata": json.loads(row[1]),
                "index_text": row[2],
                "score": row[3],
            })
        return results

    def read(self, record_id: str, scope: list[str] | None = None) -> dict | None:
        """Read and decrypt a record. Scope-checked."""
        sql = "SELECT id, data, metadata, index_text, created_at FROM records r WHERE r.id = ?"
        params: list = [record_id]
        sql, params = self._apply_scope(sql, params, scope)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "data": self._decrypt(row[1]),
            "metadata": json.loads(row[2]),
            "index_text": row[3],
            "created_at": row[4],
        }

    def list_records(
        self,
        scope: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """Browse records (metadata + index_text, NOT data). Scope-enforced."""
        sql = (
            "SELECT r.id, r.metadata, r.index_text, r.created_at "
            "FROM records r WHERE 1=1"
        )
        params: list = []
        sql, params = self._apply_scope(sql, params, scope)
        sql += " ORDER BY r.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            results.append({
                "id": row[0],
                "metadata": json.loads(row[1]),
                "index_text": row[2],
                "created_at": row[3],
            })
        return results

    def get_record_meta(self, record_id: str) -> dict | None:
        """Get metadata + index_text for a record (no data). For public API."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, metadata, index_text, created_at "
                "FROM records WHERE id = ?",
                (record_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "metadata": json.loads(row[1]),
            "index_text": row[2],
            "created_at": row[3],
        }

    # ── Admin ──

    def delete_record(self, record_id: str) -> bool:
        """Delete a record and its FTS entry."""
        with self._lock:
            row = self._conn.execute(
                "SELECT rowid, index_text FROM records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if row and row[1] is not None:
                self._conn.execute(
                    "INSERT INTO records_fts(records_fts, rowid, index_text) "
                    "VALUES ('delete', ?, ?)",
                    (row[0], row[1]),
                )
            cursor = self._conn.execute(
                "DELETE FROM records WHERE id = ?", (record_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def count_records(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()
            return row[0]

    def close(self):
        with self._lock:
            self._conn.close()
