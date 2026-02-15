import os
import sqlite3
import tempfile
import time

import pytest
from cryptography.fernet import Fernet

from hivemind.store import RecordStore


def test_write_and_read(tmp_db):
    tmp_db.write_record("r1", "hello world", {}, None, time.time())
    record = tmp_db.read("r1")
    assert record is not None
    assert record["data"] == "hello world"


def test_scope_record_ids(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {}, None, t)
    tmp_db.write_record("r2", "text2", {}, None, t)

    assert tmp_db.read("r1", scope=["r1"]) is not None
    assert tmp_db.read("r2", scope=["r1"]) is None


def test_scope_empty_list_matches_nothing(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {}, None, t)
    assert tmp_db.read("r1", scope=[]) is None


def test_scope_none_matches_all(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {}, None, t)
    tmp_db.write_record("r2", "text2", {}, None, t)

    assert tmp_db.read("r1", scope=None) is not None
    assert tmp_db.read("r2", scope=None) is not None


def test_scope_rejects_non_string_ids(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {}, None, t)
    with pytest.raises(ValueError, match=r"scope\[1\] must be a string"):
        tmp_db.read("r1", scope=["r1", {"bad": True}])  # type: ignore[list-item]


def test_fts_search(tmp_db):
    t = time.time()
    tmp_db.write_record(
        "r1", "original text", {}, "Payment Migration Stripe processing", t
    )

    results = tmp_db.search("stripe migration")
    assert len(results) == 1
    assert results[0]["id"] == "r1"


def test_fts_search_scoped(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {"user": "alice"}, "alice notes python", t)
    tmp_db.write_record("r2", "text2", {"user": "bob"}, "bob notes python", t)

    results = tmp_db.search("python", scope=["r1"])
    assert len(results) == 1
    assert results[0]["id"] == "r1"


def test_fts_search_invalid_syntax_returns_empty(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text", {}, "some index text", t)
    assert tmp_db.search('"') == []


def test_fts_search_unexpected_operational_error_raises(tmp_db, monkeypatch):
    class _Conn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(tmp_db, "_conn", _Conn())
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        tmp_db.search("tag")


def test_list_records(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {"user": "alice"}, "first entry", t)
    tmp_db.write_record("r2", "text2", {"user": "bob"}, "second entry", t + 1)

    results = tmp_db.list_records()
    assert len(results) == 2
    assert results[0]["id"] == "r2"  # most recent first

    results = tmp_db.list_records(scope=["r1"])
    assert len(results) == 1
    assert results[0]["id"] == "r1"


def test_update_index_text(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {}, "old index text", t)

    ok = tmp_db.update_index_text("r1", "new index text")
    assert ok is True

    results = tmp_db.search("new index text")
    assert len(results) == 1

    results = tmp_db.search("old index text")
    assert len(results) == 0


def test_update_index_text_from_null(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {}, None, t)

    ok = tmp_db.update_index_text("r1", "now indexed")
    assert ok is True

    results = tmp_db.search("now indexed")
    assert len(results) == 1


def test_update_metadata(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", {"old": True}, None, t)

    ok = tmp_db.update_metadata("r1", {"new": True, "version": 2})
    assert ok is True

    record = tmp_db.read("r1")
    assert record["metadata"] == {"new": True, "version": 2}


def test_get_record_meta(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "secret data", {"title": "Test"}, "test text", t)

    meta = tmp_db.get_record_meta("r1")
    assert meta is not None
    assert meta["metadata"]["title"] == "Test"
    assert meta["index_text"] == "test text"
    assert "data" not in meta  # data should NOT be in meta response

    assert tmp_db.get_record_meta("nonexistent") is None


def test_delete_record(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text", {}, "searchable text", t)

    assert tmp_db.delete_record("r1") is True
    assert tmp_db.read("r1") is None
    assert tmp_db.delete_record("r1") is False

    results = tmp_db.search("searchable text")
    assert len(results) == 0


def test_count_records(tmp_db):
    assert tmp_db.count_records() == 0
    tmp_db.write_record("r1", "text1", {}, None, time.time())
    assert tmp_db.count_records() == 1


def test_encryption_at_rest():
    key = Fernet.generate_key().decode()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = RecordStore(path, encryption_key=key)
        store.write_record("r1", "secret data", {}, None, time.time())

        record = store.read("r1")
        assert record["data"] == "secret data"

        conn = sqlite3.connect(path)
        raw = conn.execute("SELECT data FROM records WHERE id = 'r1'").fetchone()[0]
        conn.close()
        assert raw != "secret data"
        assert len(raw) > len("secret data")

        store.close()
    finally:
        os.unlink(path)


def test_no_encryption_by_default():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = RecordStore(path)
        store.write_record("r1", "plain data", {}, None, time.time())

        conn = sqlite3.connect(path)
        raw = conn.execute("SELECT data FROM records WHERE id = 'r1'").fetchone()[0]
        conn.close()
        assert raw == "plain data"

        store.close()
    finally:
        os.unlink(path)


def test_metadata_stored_as_json(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text", {"tags": ["a", "b"], "count": 5}, None, t)
    record = tmp_db.read("r1")
    assert record["metadata"] == {"tags": ["a", "b"], "count": 5}


def test_record_without_index_text(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "data", {}, None, t)
    record = tmp_db.read("r1")
    assert record["index_text"] is None
    # Should not appear in FTS search
    results = tmp_db.search("data")
    assert len(results) == 0


def test_legacy_records_schema_is_migrated():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE records (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                metadata TEXT,
                timestamp REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO records (id, text, metadata, timestamp) VALUES (?, ?, ?, ?)",
            (
                "legacy-1",
                "legacy plaintext body",
                '{"source":"legacy"}',
                time.time(),
            ),
        )
        conn.commit()
        conn.close()

        store = RecordStore(path)
        try:
            record = store.read("legacy-1")
            assert record is not None
            assert record["data"] == "legacy plaintext body"
            assert record["metadata"]["source"] == "legacy"

            search_results = store.search("legacy plaintext")
            assert len(search_results) == 1
            assert search_results[0]["id"] == "legacy-1"

            version_row = store._conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            assert version_row is not None
            assert version_row[0] == "0002_migrate_legacy_records_schema"
        finally:
            store.close()
    finally:
        os.unlink(path)


def test_legacy_records_schema_is_migrated_with_encryption():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    key = Fernet.generate_key().decode()
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE records (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                metadata TEXT,
                timestamp REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO records (id, text, metadata, timestamp) VALUES (?, ?, ?, ?)",
            (
                "legacy-enc-1",
                "legacy encrypted body",
                '{"source":"legacy"}',
                time.time(),
            ),
        )
        conn.commit()
        conn.close()

        store = RecordStore(path, encryption_key=key)
        try:
            record = store.read("legacy-enc-1")
            assert record is not None
            assert record["data"] == "legacy encrypted body"
            assert record["metadata"]["source"] == "legacy"

            search_results = store.search("legacy encrypted")
            assert len(search_results) == 1
            assert search_results[0]["id"] == "legacy-enc-1"
        finally:
            store.close()

        conn = sqlite3.connect(path)
        raw = conn.execute(
            "SELECT data FROM records WHERE id = ?",
            ("legacy-enc-1",),
        ).fetchone()
        conn.close()
        assert raw is not None
        assert raw[0] != "legacy encrypted body"
    finally:
        os.unlink(path)


def test_unsupported_records_schema_is_rejected():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE records (
                id TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                created REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="Unsupported records schema"):
            RecordStore(path)
    finally:
        os.unlink(path)
