"""WAL-safe SQLite snapshots. Direct execution needs only the standard library.

Desktop invokes this file before stopping its backend, even when application
imports cannot load. Full and quick backups use the same SQLite copy operation.
"""
import json
import logging
import os
import signal
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class _SQLiteBackupTimeout(RuntimeError):
    """Raised when a SQLite snapshot remains busy past its deadline."""


# Transient Windows open-conflict retry budget: covers short-lived peer holds on
# the db/-wal/-shm file set that surface as SQLITE_CANTOPEN at open/first-use.
# ~4s of coverage (4 Ã— 1s) keeps a 94k-file archive member copy from aborting on
# a seconds-scale sharing cycle observed on keira wisdom.db, while persistent
# conflicts still fail closed fast.
OPEN_RETRY_ATTEMPTS = 5
OPEN_RETRY_DELAY = 1.0

# One one-second settle-and-retry covers the observed persistent-shm-deny case
# (card t_08e445b2): a worker session briefly holds the quarantined sidecar set
# during its own probes; after it closes, the empty-WAL byte copy succeeds. The
# retry runs only after a failed attempt and only clears when its own attempt
# differs (non-CANTOPEN error or a successful copy), so a hard-deny daemon set
# never doubles the archive pass.
EMPTY_WAL_RETRIE_AFTER_REFUSAL = 1


def _empty_wal_byte_copy(src: Path, dst: Path, *, attempts: int | None = None, delay: float | None = None) -> bool:
    """Byte-copy the main file when the WAL is provably empty and the file set is unreadable.

    Some Windows file sets carry sidecar -shm/-wal DACLs that deny even read
    access to the backup user, which makes every SQLite open of a WAL-mode
    database fail with CANTOPEN before the backup API can run (observed live:
    profiles/keira/wisdom/wisdom.db, 2026-10-17 and since). When the WAL file is
    provably empty â€” size 0 (stat works without file-read access) and no pending
    recovery per the page-1 header (change counter == version-valid-for) â€” the
    committed state lives entirely in the main file, so a raw byte copy with
    post-verify preserves the same consistency guarantee the backup() API would
    produce for that state. Never touches sidecars, DACLs, or the main file;
    returns False (fail closed) when any precondition or verification step
    disagrees (format mismatch, counter change mid-copy, quick_check failure);
    the exact refusal reason is returned via the ``detail`` out-param contract
    at :func:`_empty_wal_byte_copy_detail` for diagnosable logging.
    """
    ok, _ = _empty_wal_byte_copy_detail(src, dst, attempts=attempts, delay=delay)
    return ok


def _empty_wal_byte_copy_detail(
    src: Path, dst: Path, *, attempts: int | None = None, delay: float | None = None
) -> tuple[bool, str]:
    """:func:`_empty_wal_byte_copy` plus a reason-bearing refusal string.

    Returns ``(True, "ok:<n attempts>")`` on success or ``(False, reason)``:
    ``read-refused:<errno>`` (main file itself unreadable), ``not-sqlite`` /
    ``not-wal`` (header refused raw copy), ``pending-wal`` (change counter !=
    version-valid-for â†’ committed frames outside the main file), ``wal-sized`` /
    ``wal-sized-after`` (non-empty WAL â€” copy would tear), ``quick-check:...`` /
    ``counter-changed`` / ``engine:<sqlite error>`` (verification failed), or
    ``busy-writer`` (a content settlement occurred mid-attempt; retried within
    the attempt budget).
    """
    if attempts is None:
        attempts = max(1, EMPTY_WAL_RETRIE_AFTER_REFUSAL + 1)
    if delay is None:
        delay = 1.0
    reason = "unreachable: empty-wal attempt loop"
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(delay)
        ok, reason = _empty_wal_byte_copy_once(src, dst)
        if ok:
            return True, f"ok:{attempt + 1} attempt(s)"
        if reason != "busy-writer":
            return False, reason
    return False, reason


def _empty_wal_byte_copy_once(src: Path, dst: Path) -> tuple[bool, str]:
    try:
        try:
            f = open(src, "rb")
        except OSError as exc:
            return False, f"read-refused:{getattr(exc, 'errno', 'unknown')}"
        with f:
            header = f.read(100)
        if len(header) < 100 or not header.startswith(b"SQLite format 3"):
            return False, "not-sqlite"
        if header[18] != 2 or header[19] != 2:  # not a WAL-mode source
            return False, "not-wal"
        change_counter = header[24:28]
        version_valid_for = header[92:96]
        if change_counter != version_valid_for:
            return False, "pending-wal"  # committed frames exist outside the main file
        wal_path = Path(str(src) + "-wal")
        try:
            if wal_path.stat().st_size != 0:
                return False, "wal-sized"
        except FileNotFoundError:
            pass  # absent WAL is an empty WAL
        try:
            shutil.copyfile(src, dst)
        except OSError as exc:
            return False, f"copyfile:{getattr(exc, 'errno', 'unknown')}"
        connection = sqlite3.connect(str(dst))
        try:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise sqlite3.DatabaseError("quick_check failed on byte-copy snapshot")
        finally:
            connection.close()
        with open(src, "rb") as f:
            f.seek(24)
            post_counter = f.read(4)
            f.seek(92)
            post_valid = f.read(4)
        if post_counter != change_counter or post_valid != version_valid_for:
            raise sqlite3.DatabaseError("source changed during byte-copy fallback")
        try:
            if wal_path.stat().st_size != 0:
                raise sqlite3.DatabaseError(
                    "WAL became non-empty during byte-copy fallback"
                )
        except FileNotFoundError:
            pass
        return True, "ok"
    except (OSError, sqlite3.Error):
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False, "engine: copy-time verification failed (see traceback in debug mode)"


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        with suppress(Exception):
            conn.close()


def _open_pinned_snapshot_source(
    src: Path, *, attempts: int | None = None, delay: float | None = None
) -> tuple[sqlite3.Connection, bool]:
    """Open src read-only and pin a read snapshot for WAL sources.

    Retries the whole open sequence (connect, journal-mode probe, WAL pin) a few
    times: on Windows a peer holding the db/-wal/-shm set makes sqlite raise
    OperationalError "unable to open database file" (SQLITE_CANTOPEN) at connect
    or on first use â€” before any busy handler could engage with timeout=0.0 â€”
    and observed holds cycle on/off within seconds (short open/close windows on
    profiles/keira/wisdom/wisdom.db during backup runs). Only that transient
    open-conflict shape is retried; every other error (notably "database is
    locked") fails closed immediately, preserving prior semantics.

    Returns (connection, held_txn); held_txn is True when a WAL pin read
    transaction was started and must be rolled back after the backup.
    """
    if attempts is None:
        attempts = OPEN_RETRY_ATTEMPTS
    if delay is None:
        delay = OPEN_RETRY_DELAY
    last_exc: Exception = RuntimeError("unreachable: source open retry loop")
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(delay)
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(
                f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0, check_same_thread=False
            )
            held_txn = False
            if conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                conn.execute("BEGIN DEFERRED")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                held_txn = True
            return conn, held_txn
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc):
                raise
            last_exc = exc
            if conn is not None:
                _close_quietly(conn)
    raise last_exc


class _SQLiteBackupRestarted(RuntimeError):
    """Raised when a foreign write restarted a stepped snapshot from page one."""


# A rollback-journal reader blocks every writer's commit while it holds the read
# lock, so that copy releases it after each step; a waiting writer's PENDING lock
# refuses the next step, so it waits for one step at most.  Activity writes give
# up after 0.5s (SessionDB._ACTIVITY_WRITE_PATIENCE_S); every hold stays under it.
_ROLLBACK_STEP_SECONDS = 0.1
_ROLLBACK_FIRST_STEP_PAGES = 256
_ROLLBACK_BUSY_SLEEP_SECONDS = 0.01
# Each foreign write restarts a stepped copy, so a continuous writer gets a bounded
# number of attempts, then one whole-copy hold if it fits the budget, else a refusal.
_ROLLBACK_MAX_RESTARTS = 20
_ROLLBACK_LOCK_HOLD_SECONDS = 0.25


def _uses_wal(src: Path) -> bool:
    # Header byte 18 (file format write version) is 2 for WAL; reading it takes no lock.
    with open(src, "rb") as handle:
        header = handle.read(19)
    return len(header) == 19 and header[18] == 2


class _CopyRate:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.pages = 0
        self.total = 0

    def pages_per_second(self) -> float:
        return self.pages / max(self.seconds, 1e-6)


def _stepped_attempt(conn: sqlite3.Connection, backup_conn: sqlite3.Connection, pages: int,
                     check_busy, rate: _CopyRate) -> bool:
    """One stepped backup; False when a foreign write restarted it."""
    last_remaining: Optional[int] = None
    last_tick, last_busy = time.monotonic(), False

    def _progress(status: int, remaining: int, total: int) -> None:
        nonlocal last_remaining, last_tick, last_busy
        check_busy(status, remaining, total)
        now = time.monotonic()
        # Copy time only: drop the sleep sqlite3 takes after a busy step.
        step_seconds = max(0.0, now - last_tick - (_ROLLBACK_BUSY_SLEEP_SECONDS if last_busy else 0.0))
        last_tick, last_busy = now, status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        if status not in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
            return
        restarted = last_remaining is not None and remaining >= last_remaining
        rate.seconds += step_seconds
        rate.pages += total - remaining if last_remaining is None or restarted else last_remaining - remaining
        rate.total = total
        if restarted:
            raise _SQLiteBackupRestarted()
        last_remaining = remaining

    try:
        conn.backup(backup_conn, pages=pages, progress=_progress, sleep=_ROLLBACK_BUSY_SLEEP_SECONDS)
    except _SQLiteBackupRestarted:
        return False
    return True


def _stepped_rollback_journal_copy(conn: sqlite3.Connection, backup_conn: sqlite3.Connection,
                                   check_busy) -> None:
    rate = _CopyRate()
    pages = _ROLLBACK_FIRST_STEP_PAGES
    for _attempt in range(_ROLLBACK_MAX_RESTARTS + 1):
        if _stepped_attempt(conn, backup_conn, pages, check_busy, rate):
            return
        pages = max(_ROLLBACK_FIRST_STEP_PAGES, int(rate.pages_per_second() * _ROLLBACK_STEP_SECONDS))
    # Floored: a run where no step completed has no measured rate, and the estimate must not divide by 0.
    expected_hold = rate.total / max(rate.pages_per_second(), 1.0)
    if expected_hold > _ROLLBACK_LOCK_HOLD_SECONDS:
        raise RuntimeError(
            "state.db uses a rollback journal and is written continuously; a consistent snapshot "
            f"would block those writes for about {expected_hold:.1f}s, so it was not taken. "
            "Retry when no agent turn is running.")
    conn.backup(backup_conn, pages=-1, progress=check_busy, sleep=_ROLLBACK_BUSY_SLEEP_SECONDS)


class _OffThreadCopy:
    """One long sqlite3 call run off the main thread. Python runs signal handlers only on the main
    thread and only between bytecodes, so a one-step backup (a single C ``sqlite3_backup_step``,
    GIL released) would hold a SIGTERM until the whole copy finished; the main thread waits here in
    short joins instead, so the Desktop's pre-flight deadline interrupts the snapshot promptly."""

    def __init__(self, copy) -> None:
        self._copy, self._error = copy, None
        # health: allow HX012 -- stdlib-only file (the Desktop runs it when app imports cannot load); the copy reads no profile state
        self.thread = threading.Thread(target=self._run, name="sqlite-snapshot", daemon=True)

    def _run(self) -> None:
        try:
            self._copy()
        except BaseException as exc:  # health: allow BLE001 -- handed to the waiting main thread, which re-raises it
            self._error = exc

    def run(self) -> None:
        self.thread.start()
        while self.thread.is_alive():
            self.thread.join(0.05)
        if self._error is not None:
            raise self._error

    def running(self) -> bool:
        return self.thread.is_alive()


def _safe_copy_db(src: Path, dst: Path, *, timeout_seconds: float = 10.0) -> bool:
    """Copy a SQLite database with the backup() API (WAL-safe consistent snapshot).

    Fails closed when no consistent snapshot can be made: copying only the main file loses WAL data.
    """
    conn = backup_conn = None
    copier: Optional[_OffThreadCopy] = None
    try:
        # sqlite3.connect() creates a missing destination with the process
        # umask, which is commonly 0022 (0644).  Snapshot databases contain
        # session and tool state, so create the inode owner-only before SQLite
        # writes its first byte.  O_NOFOLLOW also refuses a planted symlink on
        # platforms that support it.  Tighten an existing internal staging
        # file as well (NamedTemporaryFile callers already create it 0600).
        if os.name != "nt":
            open_flags = os.O_WRONLY | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            secure_fd = os.open(dst, open_flags, 0o600)
            try:
                os.fchmod(secure_fd, 0o600)
            finally:
                os.close(secure_fd)
        # timeout=0.0 disables sqlite3's implicit busy wait so the progress callback owns the
        # full locked-source deadline instead of adding the default timeout before each callback.
        conn, _held_txn = _open_pinned_snapshot_source(src)
        backup_conn = sqlite3.connect(str(dst), check_same_thread=False)
        busy_deadline = time.monotonic() + max(0.0, timeout_seconds)

        def _check_backup_progress(status: int, _remaining: int, _total: int) -> None:
            nonlocal busy_deadline
            now = time.monotonic()
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if now >= busy_deadline:
                    raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds")
            else:
                busy_deadline = now + max(0.0, timeout_seconds)

        if _uses_wal(src):
            # One step: SQLite restarts a multi-step backup whenever another connection writes the
            # source, and a WAL reader never blocks a writer however long the copy takes.
            # Run off the main thread so a SIGTERM is serviced during the copy (_OffThreadCopy).
            copier = _OffThreadCopy(lambda: conn.backup(
                backup_conn, pages=-1, progress=_check_backup_progress, sleep=0.1))
            copier.run()
        else:
            _stepped_rollback_journal_copy(conn, backup_conn, _check_backup_progress)
        return True
    except sqlite3.OperationalError as exc:
        _close_quietly(backup_conn)
        backup_conn = None
        if _empty_wal_byte_copy(src, dst):
            return True
        _, fallback_reason = _empty_wal_byte_copy_detail(src, dst)
        logger.warning("SQLite safe copy failed for %s: %s; empty-WAL byte copy refused: %s",
                       src, exc, fallback_reason)
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        # Windows won't remove the partial destination while SQLite still has it open.
        _close_quietly(backup_conn)
        backup_conn = None
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    finally:
        # Interrupted mid-copy (SIGTERM -> SystemExit): the worker is still inside the C copy with
        # both connections, and close() would wait on their mutex until the copy ends. The process
        # is exiting; leave them to it, and the caller unlinks the staging file.
        if copier is None or not copier.running():
            _close_quietly(backup_conn)
            _close_quietly(conn)


# Longer than the Desktop's 30-minute pre-flight cap: a staging file this old
# has no live writer. A hard-killed pre-flight (Windows TerminateProcess)
# cannot unlink its own staging copy, and each one is a full-size database.
_ABANDONED_STAGING_SECONDS = 2 * 3600


def _reclaim_abandoned_staging(home: Path, prefix: str) -> None:
    horizon = time.time() - _ABANDONED_STAGING_SECONDS
    for staged in home.glob(f"{prefix}*.partial*"):  # + the copy's -journal
        with suppress(OSError):
            if staged.stat().st_mtime < horizon:
                staged.unlink()


def preflight_state_db(home: Path) -> dict:
    """Publish an emergency snapshot; do not prune recovery files on failure."""
    source = home / "state.db"
    if not source.exists():
        return {"path": None, "message": "state.db not found (fresh install?)"}
    prefix = "state.db.pre-update-emergency-"
    _reclaim_abandoned_staging(home, prefix)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    destination = home / f"{prefix}{stamp}-{os.getpid()}.bak"
    # Named before it exists, so a SIGTERM at any point after creation reaches the
    # cleanup below; _safe_copy_db creates it owner-only and refuses a symlink.
    staged = home / f"{prefix}{stamp}-{os.getpid()}.partial"
    try:
        if not _safe_copy_db(source, staged):
            raise RuntimeError("SQLite safe copy failed; previous emergency snapshots were retained")
        connection = sqlite3.connect(str(staged))
        try:
            result = connection.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise RuntimeError(f"SQLite snapshot integrity check failed: {result}")
        finally:
            connection.close()
        size = staged.stat().st_size
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
        # A copy stopped mid-write leaves the staging copy's rollback journal behind too.
        Path(f"{staged}-journal").unlink(missing_ok=True)
    for old in sorted(home.glob(f"{prefix}*.bak"), reverse=True)[2:]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Could not prune emergency snapshot %s: %s", old, exc)
    return {"path": str(destination), "bytes": size}


if __name__ == "__main__":
    # The Desktop stops a pre-flight past its deadline with SIGTERM; exit through
    # the staging cleanup instead of dying with a full-size .partial left behind.
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    print(json.dumps(preflight_state_db(Path(sys.argv[1]))))
