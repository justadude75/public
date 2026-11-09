"""Vault storage: one SQLite file per vault, optionally encrypted with SQLCipher."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterator


class VaultError(Exception):
    """A vault could not be opened or used."""


def _dict_row(cursor, row) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def is_encrypted(path: str | Path) -> bool:
    """True if the file exists and is not readable as plain SQLite."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as fh:
        return fh.read(16) != b"SQLite format 3\x00"


def _open_connection(path: Path, passphrase: str | None):
    if passphrase:
        try:
            from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on install
            raise VaultError(
                "Encrypted vaults need the sqlcipher3 package. "
                "Install it with: pip install 'ties[encryption]'"
            ) from exc
        conn = sqlcipher.connect(str(path), check_same_thread=False)
        escaped = passphrase.replace("'", "''")
        conn.execute(f"PRAGMA key = '{escaped}'")
        try:
            conn.execute("select count(*) from sqlite_master").fetchone()
        except sqlcipher.DatabaseError as exc:
            conn.close()
            raise VaultError("Wrong passphrase, or the file is not a vault.") from exc
    else:
        if is_encrypted(path):
            raise VaultError("This vault is encrypted. Provide its passphrase.")
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = _dict_row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Performance for large vaults: WAL with NORMAL sync is crash-safe for the database
    # (a power cut can lose only the last transaction), bigger page cache, memory temp.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -65536")       # 64 MB
    conn.execute("PRAGMA temp_store = MEMORY")
    try:
        conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB (ignored where unsupported)
    except Exception:
        pass
    return conn


def _migrations() -> list[tuple[int, str]]:
    folder = resources.files("ties") / "migrations"
    found = []
    for item in folder.iterdir():
        name = item.name
        if name.endswith(".sql") and name[:3].isdigit():
            found.append((int(name[:3]), item.read_text(encoding="utf-8")))
    return sorted(found)


class Vault:
    """A single-user vault. One shared connection guarded by a lock."""

    def __init__(self, path: str | Path, passphrase: str | None = None):
        self.path = Path(path)
        self.encrypted = bool(passphrase)
        self._lock = threading.RLock()
        self.conn = _open_connection(self.path, passphrase)
        self._migrate()

    # ── change tracking and caching ───────────────────────────────────────
    @property
    def version(self) -> tuple:
        """Changes with every write: ours (total_changes) or another process's (data_version)."""
        with self._lock:
            other = self.conn.execute("PRAGMA data_version").fetchone()
        return (self.conn.total_changes, next(iter(other.values())))

    def cached(self, key: tuple, compute):
        """Memoize `compute()` until the next write to the vault."""
        cache = self.__dict__.setdefault("_cache", {})
        hit = cache.get(key)
        if hit is not None and hit[0] == self.version:
            return hit[1]
        value = compute()
        cache[key] = (self.version, value)
        if len(cache) > 256:
            for k in list(cache)[:64]:
                cache.pop(k, None)
        return value

    # ── connection helpers ────────────────────────────────────────────────
    def _migrate(self) -> None:
        with self._lock:
            has_meta = self.conn.execute(
                "select 1 from sqlite_master where type='table' and name='meta'"
            ).fetchone()
            current = 0
            if has_meta:
                row = self.conn.execute(
                    "select value from meta where key='schema_version'"
                ).fetchone()
                current = int(row["value"]) if row else 0
            for version, sql in _migrations():
                if version > current:
                    self.conn.executescript(sql)
                    self.conn.execute(
                        "insert into meta(key, value) values('schema_version', ?) "
                        "on conflict(key) do update set value=excluded.value",
                        (str(version),),
                    )
                    self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[Any]:
        """Run statements in one transaction; commit on success, roll back on error."""
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else next(iter(row.values()))

    def get_meta(self, key: str) -> str | None:
        return self.scalar("select value from meta where key=?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "insert into meta(key, value) values(?, ?) "
                "on conflict(key) do update set value=excluded.value",
                (key, value),
            )

    def audit(self, c, action: str, target: str | None, detail: dict | None = None,
              actor: str = "user") -> None:
        c.execute(
            "insert into audit_log(actor, action, target, detail) values(?,?,?,?)",
            (actor, action, target, json.dumps(detail or {})),
        )

    # ── maintenance ───────────────────────────────────────────────────────
    def backup(self, dest: str | Path) -> Path:
        """Copy the vault file (still encrypted, if it was) to dest."""
        dest = Path(dest)
        if dest.is_dir():
            dest = dest / self.path.name
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copy2(self.path, dest)
        return dest

    def close(self) -> None:
        with self._lock:
            self.conn.close()
