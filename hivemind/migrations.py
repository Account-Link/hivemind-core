from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config

_MIGRATION_LOCK = threading.Lock()


def _sqlite_url(db_path: str) -> str:
    path = Path(db_path).expanduser().resolve()
    return f"sqlite:///{path}"


def _alembic_script_location() -> str:
    return str((Path(__file__).resolve().parent / "alembic"))


def _migration_lock_path(db_path: str) -> Path:
    path = Path(db_path).expanduser().resolve()
    lock_suffix = f"{path.suffix}.migrate.lock" if path.suffix else ".migrate.lock"
    return path.with_suffix(lock_suffix)


@contextmanager
def _process_migration_lock(db_path: str):
    lock_path = _migration_lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        # Some platforms require locking a non-empty region.
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()

        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def build_alembic_config(db_path: str, *, encryption_key: str = "") -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _alembic_script_location())
    cfg.set_main_option("sqlalchemy.url", _sqlite_url(db_path))
    cfg.attributes["hivemind_encryption_key"] = encryption_key
    return cfg


def run_migrations(db_path: str, *, encryption_key: str = "") -> None:
    cfg = build_alembic_config(db_path, encryption_key=encryption_key)
    with _MIGRATION_LOCK:
        with _process_migration_lock(db_path):
            command.upgrade(cfg, "head")
