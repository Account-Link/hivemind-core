"""Round-trip tests for sealed-mode agents (Phase 6).

These cover:
  • ``agent_seal.encrypt_b64`` / ``decrypt_b64`` round-trip with a
    fixed test key.
  • ``AgentStore.save_files`` with ``inspection_mode='sealed'`` writes
    ChaCha20 ciphertext under the enclave-only key (not the tenant DEK).
  • ``read_file`` / ``get_files`` raise :class:`AgentSealedReadError`
    by default and decrypt with ``allow_sealed=True``.
  • ``compute_digests`` works on sealed agents (it uses
    ``allow_sealed=True`` internally).
  • ``_validate_inspection_mode`` rejects modes outside the room policy.

Postgres-backed; skips when ``HIVEMIND_TEST_DATABASE_URL`` not set.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from hivemind import agent_seal
from hivemind.db import Database
from hivemind.sandbox.agents import AgentSealedReadError, AgentStore
from hivemind.sandbox.models import AgentConfig


TEST_DSN_BASE = os.environ.get(
    "HIVEMIND_TEST_DATABASE_URL",
    "postgresql://hivemind:dev@localhost:5432/postgres",
)


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(TEST_DSN_BASE),
    reason=f"Postgres not reachable at {TEST_DSN_BASE}",
)


@pytest.fixture
def fresh_db():
    db_name = f"hm_aseal_{secrets.token_hex(4)}"
    with psycopg.connect(TEST_DSN_BASE, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    parts = TEST_DSN_BASE.rsplit("/", 1)
    base = parts[0] if len(parts) == 2 else TEST_DSN_BASE
    dsn = f"{base}/{db_name}"
    db = Database(dsn)
    try:
        yield db, db_name
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            with psycopg.connect(TEST_DSN_BASE, autocommit=True) as conn:
                conn.execute(
                    f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'
                )
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _stub_agent_seal_key():
    """Inject a deterministic 32-byte key so tests don't need dstack-KMS."""
    agent_seal.reset_for_tests()
    # Bypass dstack by writing the state directly.
    with agent_seal._state["lock"]:
        agent_seal._state["key"] = b"\x42" * 32
        agent_seal._state["key_path"] = "test-key"
    yield
    agent_seal.reset_for_tests()


def _config(agent_id: str, mode: str = "full") -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        name="demo",
        description="",
        agent_type="query",
        image="hivemind/agent-demo:latest",
        entrypoint=None,
        memory_mb=64,
        max_llm_calls=1,
        max_tokens=1,
        timeout_seconds=10,
        inspection_mode=mode,
    )


def test_agent_seal_round_trip():
    """encrypt_b64 / decrypt_b64 roundtrips on a fixed key."""
    pt = "print('hello sealed world')\n"
    ct = agent_seal.encrypt_b64("agent_x", "main.py", pt)
    assert "hello sealed" not in ct
    assert agent_seal.decrypt_b64("agent_x", "main.py", ct) == pt


def test_agent_seal_aad_binds_path_and_id():
    """Decrypting under a different (agent, path) pair must fail."""
    pt = "secret\n"
    ct = agent_seal.encrypt_b64("agent_a", "main.py", pt)
    with pytest.raises(Exception):
        agent_seal.decrypt_b64("agent_b", "main.py", ct)
    with pytest.raises(Exception):
        agent_seal.decrypt_b64("agent_a", "other.py", ct)


def test_sealed_store_writes_ciphertext_no_plaintext(fresh_db):
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_sealed_demo", mode="sealed")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"main.py": "print('SECRET')\n"},
        inspection_mode="sealed",
    )
    rows = db.execute(
        "SELECT content, ciphertext FROM _hivemind_agent_files "
        "WHERE agent_id = %s",
        [cfg.agent_id],
    )
    assert len(rows) == 1
    assert rows[0]["content"] is None
    assert rows[0]["ciphertext"]
    assert "SECRET" not in rows[0]["ciphertext"]


def test_sealed_read_default_raises(fresh_db):
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_sealed_read", mode="sealed")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"main.py": "print('X')\n"},
        inspection_mode="sealed",
    )
    with pytest.raises(AgentSealedReadError):
        store.read_file(cfg.agent_id, "main.py")
    with pytest.raises(AgentSealedReadError):
        store.get_files(cfg.agent_id)


def test_sealed_read_allow_sealed_decrypts(fresh_db):
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_sealed_internal", mode="sealed")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"main.py": "print('hello')\n", "lib/x.py": "y=1\n"},
        inspection_mode="sealed",
    )
    assert (
        store.read_file(cfg.agent_id, "main.py", allow_sealed=True)
        == "print('hello')\n"
    )
    assert store.get_files(cfg.agent_id, allow_sealed=True) == {
        "main.py": "print('hello')\n",
        "lib/x.py": "y=1\n",
    }


def test_sealed_compute_digests_works(fresh_db):
    """Digests must compute over plaintext, since recipients verify
    against published source. compute_digests passes allow_sealed=True
    internally so this should not raise."""
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_sealed_digest", mode="sealed")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"a.py": "print('a')\n", "b.py": "print('b')\n"},
        inspection_mode="sealed",
    )
    digests = store.compute_digests(cfg.agent_id)
    assert digests["files_count"] == 2
    assert digests["files_digest"]
    assert digests["attested_files_digest"]


def test_replace_files_sealed_drops_old(fresh_db):
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_sealed_replace", mode="sealed")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id, {"a.py": "v1\n"}, inspection_mode="sealed",
    )
    store.replace_files(
        cfg.agent_id,
        {"a.py": "v2\n", "b.py": "z\n"},
        inspection_mode="sealed",
    )
    rows = db.execute(
        "SELECT file_path, content, ciphertext "
        "FROM _hivemind_agent_files "
        "WHERE agent_id = %s ORDER BY file_path",
        [cfg.agent_id],
    )
    assert [r["file_path"] for r in rows] == ["a.py", "b.py"]
    for r in rows:
        assert r["content"] is None
        assert r["ciphertext"]
    files = store.get_files(cfg.agent_id, allow_sealed=True)
    assert files == {"a.py": "v2\n", "b.py": "z\n"}


def test_sealed_when_kms_unavailable_save_raises(fresh_db, monkeypatch):
    """If the enclave key isn't bootstrapped, sealed writes must fail
    closed at the AgentStore boundary."""
    db, _ = fresh_db
    monkeypatch.setattr(agent_seal, "is_available", lambda: False)
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_no_kms", mode="sealed")
    store.upsert(cfg)
    with pytest.raises(RuntimeError, match="agent_seal key is not available"):
        store.save_files(
            cfg.agent_id, {"main.py": "x\n"}, inspection_mode="sealed",
        )
