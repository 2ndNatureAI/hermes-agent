"""Focused regression for CANTOPEN retry + provably-empty-WAL byte-copy fallback.

Covers the 2026-10 coverage defect: profiles/keira/wisdom/wisdom.db was silently
dropped from full-system backups ("SQLite safe copy failed") when sidecar
-shm/-wal DACLs denied the backup user, making every SQLite open of the
WAL-mode database fail with OperationalError("unable to open database file").
"""

import sqlite3
from pathlib import Path

from hermes_cli.backup_sqlite import (
    _empty_wal_byte_copy,
    _open_pinned_snapshot_source,
    _safe_copy_db,
)


def _make_wal_db_with_committed_rows(db_path: Path) -> None:
    connect = sqlite3.connect(str(db_path))
    try:
        connect.execute("PRAGMA journal_mode=WAL")
        connect.execute("CREATE TABLE messages (body TEXT)")
        connect.execute("INSERT INTO messages VALUES ('committed only in wal')")
        connect.commit()
        # Leave WAL empty or absent (clean checkpoints) but keep mode=wal in the main file.
        connect.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connect.commit()
    finally:
        connect.close()
    wal_after = Path(str(db_path) + "-wal")
    assert not wal_after.exists() or wal_after.stat().st_size == 0


def _make_pending_wal_db(db_path: Path):
    """Return an OPEN connection whose WAL holds committed, un-checkpointed frames."""
    connect = sqlite3.connect(str(db_path))
    connect.execute("PRAGMA journal_mode=WAL")
    connect.execute("PRAGMA wal_autocheckpoint=0")
    connect.execute("CREATE TABLE messages (body TEXT)")
    connect.execute("INSERT INTO messages VALUES ('committed only in wal')")
    connect.commit()
    wal_after = Path(str(db_path) + "-wal")
    assert wal_after.exists() and wal_after.stat().st_size > 0
    return connect


def test_open_pinned_retries_on_unable_to_open_then_succeeds(tmp_path, monkeypatch):
    db = tmp_path / "src.db"
    _make_wal_db_with_committed_rows(db)

    calls = {"n": 0}

    def flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("unable to open database file")
        return sqlite3_original(*args, **kwargs)

    sqlite3_original = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", flaky_connect)
    try:
        _conn, held = _open_pinned_snapshot_source(db, attempts=5, delay=0.0)
    finally:
        monkeypatch.setattr(sqlite3, "connect", sqlite3_original)
    assert calls["n"] == 3  # two transient failures retried, third attempt opened
    assert held is not None or held is False  # WAL pin state returned for rollback


def test_open_pinned_fails_fast_on_locked(tmp_path, monkeypatch):
    db = tmp_path / "src.db"
    _make_wal_db_with_committed_rows(db)
    calls = {"n": 0}

    def locked_connect(*args, **kwargs):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", locked_connect)
    try:
        raised = False
        try:
            _open_pinned_snapshot_source(db, attempts=5, delay=0.0)
        except sqlite3.OperationalError as exc:
            raised = "database is locked" in str(exc)
        assert raised
    finally:
        pass
    assert calls["n"] == 1  # non-CANTOPEN error is not retried


def test_empty_wal_byte_copy_verifies_and_fails_closed(tmp_path):
    clean = tmp_path / "clean.db"
    _make_wal_db_with_committed_rows(clean)
    dst = tmp_path / "snap.db"
    assert _empty_wal_byte_copy(clean, dst) is True
    with sqlite3.connect(str(dst)) as snap:
        assert snap.execute("SELECT count(*) FROM messages").fetchone() == (1,)
        assert snap.execute("PRAGMA quick_check").fetchone() == ("ok",)

    # Source with pending WAL content must NOT be raw-copied (fail closed).
    live = tmp_path / "live.db"
    live_writer = _make_pending_wal_db(live)
    try:
        blocked = tmp_path / "blocked.db"
        assert _empty_wal_byte_copy(live, blocked) is False
        assert not blocked.exists()
    finally:
        live_writer.close()

    # Corrupted main file must not pass preconditions or verification.
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database")
    refused = tmp_path / "refused.db"
    assert _empty_wal_byte_copy(broken, refused) is False
    assert not refused.exists()


def test_safe_copy_db_falls_back_to_byte_copy_when_open_denied(tmp_path, monkeypatch):
    db = tmp_path / "wise.db"
    _make_wal_db_with_committed_rows(db)
    dst = tmp_path / "snap.db"

    sqlite3_original = sqlite3.connect

    def always_denied(*args, **kwargs):
        # Every source open fails like the live sidecar-deny case; destination
        # opens (bare filename args) must keep working.
        uri = args[0]
        if isinstance(uri, str) and "?mode=ro" in uri:
            raise sqlite3.OperationalError("unable to open database file")
        return sqlite3_original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", always_denied)
    try:
        assert _safe_copy_db(db, dst, timeout_seconds=0.0) is True
    finally:
        monkeypatch.setattr(sqlite3, "connect", sqlite3_original)
    with sqlite3.connect(str(dst)) as snap:
        assert snap.execute("SELECT * FROM messages").fetchall() == [
            ("committed only in wal",)
        ]
        assert snap.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_safe_copy_db_still_fails_closed_when_fallback_preconditions_fail(
    tmp_path, monkeypatch
):
    live = tmp_path / "live.db"
    live_writer = _make_pending_wal_db(live)
    try:
        dst = tmp_path / "snap.db"
        sqlite3_original = sqlite3.connect

        def always_denied(*args, **kwargs):
            uri = args[0]
            if isinstance(uri, str) and "?mode=ro" in str(uri):
                raise sqlite3.OperationalError("unable to open database file")
            return sqlite3_original(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", always_denied)
        try:
            assert _safe_copy_db(live, dst, timeout_seconds=0.0) is False
        finally:
            monkeypatch.setattr(sqlite3, "connect", sqlite3_original)
        assert not dst.exists()
    finally:
        live_writer.close()
