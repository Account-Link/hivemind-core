from pathlib import Path

import hivemind.migrations as migrations


def test_migration_lock_path_uses_db_filename():
    path = migrations._migration_lock_path("./example.db")
    assert path.name == "example.db.migrate.lock"


def test_run_migrations_uses_process_lock(monkeypatch, tmp_path):
    events: list[str] = []

    class _LockCtx:
        def __enter__(self):
            events.append("lock-enter")
            return None

        def __exit__(self, exc_type, exc, tb):
            events.append("lock-exit")
            return False

    def fake_process_lock(db_path: str):
        events.append(f"lock-path:{Path(db_path).name}")
        return _LockCtx()

    def fake_upgrade(cfg, target):
        events.append(f"upgrade:{target}")

    monkeypatch.setattr(migrations, "_process_migration_lock", fake_process_lock)
    monkeypatch.setattr(migrations.command, "upgrade", fake_upgrade)

    db_path = str(tmp_path / "test.db")
    migrations.run_migrations(db_path)
    assert events == [
        "lock-path:test.db",
        "lock-enter",
        "upgrade:head",
        "lock-exit",
    ]
