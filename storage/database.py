"""
storage/database.py — SQLite database management for mini-google-v2.

Provides thread-safe, WAL-mode SQLite access via per-thread connections.
All writes are isolated per thread; WAL mode enables concurrent reads
during active writes from the crawler's ThreadPoolExecutor workers.
"""

import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join("data", "mini_google.db")


# ---------------------------------------------------------------------------
# Low-level connection factory
# ---------------------------------------------------------------------------

def _open(path: str) -> sqlite3.Connection:
    """Open a SQLite connection with production-grade PRAGMAs applied."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db(path: str) -> None:
    """Create all tables and indexes; idempotent (CREATE … IF NOT EXISTS)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = _open(path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            url        TEXT PRIMARY KEY,
            origin     TEXT NOT NULL,
            depth      INTEGER NOT NULL,
            indexed_at REAL NOT NULL,
            session_id INTEGER,
            title      TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS word_index (
            word      TEXT NOT NULL,
            url       TEXT NOT NULL,
            origin    TEXT NOT NULL,
            depth     INTEGER NOT NULL,
            frequency INTEGER NOT NULL,
            PRIMARY KEY (word, url)
        );
        CREATE INDEX IF NOT EXISTS idx_word     ON word_index(word);
        CREATE INDEX IF NOT EXISTS idx_word_url ON word_index(url);

        CREATE TABLE IF NOT EXISTS visited (
            url        TEXT PRIMARY KEY,
            visited_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS crawl_sessions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            origin         TEXT NOT NULL,
            depth          INTEGER NOT NULL,
            started_at     REAL NOT NULL,
            finished_at    REAL,
            pages_indexed  INTEGER DEFAULT 0,
            urls_processed INTEGER DEFAULT 0,
            urls_failed    INTEGER DEFAULT 0,
            urls_skipped   INTEGER DEFAULT 0,
            urls_dropped   INTEGER DEFAULT 0,
            same_domain    INTEGER DEFAULT 1,
            status         TEXT NOT NULL DEFAULT 'running'
        );

        CREATE TABLE IF NOT EXISTS failed_urls (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            url        TEXT NOT NULL,
            error      TEXT,
            failed_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_failed_session ON failed_urls(session_id);

        CREATE TABLE IF NOT EXISTS crawl_queue_state (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            url        TEXT NOT NULL,
            origin     TEXT NOT NULL,
            depth      INTEGER NOT NULL,
            max_depth  INTEGER NOT NULL,
            saved_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_queue_session ON crawl_queue_state(session_id);
        """)
        conn.commit()
        _run_migrations(conn)
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations for existing databases."""
    candidates = [
        ("ALTER TABLE pages ADD COLUMN title TEXT DEFAULT ''",),
        ("ALTER TABLE pages ADD COLUMN session_id INTEGER",),
        ("ALTER TABLE crawl_sessions ADD COLUMN urls_dropped INTEGER DEFAULT 0",),
        ("ALTER TABLE crawl_sessions ADD COLUMN urls_processed INTEGER DEFAULT 0",),
        ("ALTER TABLE crawl_sessions ADD COLUMN urls_skipped INTEGER DEFAULT 0",),
        ("ALTER TABLE crawl_sessions ADD COLUMN urls_failed INTEGER DEFAULT 0",),
        ("ALTER TABLE crawl_sessions ADD COLUMN pages_indexed INTEGER DEFAULT 0",),
        ("ALTER TABLE crawl_sessions ADD COLUMN same_domain INTEGER DEFAULT 1",),
    ]
    for (sql,) in candidates:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists — expected on subsequent startups


# ---------------------------------------------------------------------------
# Base class: thread-local SQLite connection
# ---------------------------------------------------------------------------

class _ThreadLocalDB:
    """Base class providing one SQLite connection per OS thread.

    Each ThreadPoolExecutor worker and the asyncio main thread all call
    ``_conn()`` independently, receiving their own dedicated connection.
    This eliminates all cross-thread locking concerns at the Python level
    while SQLite's WAL mode handles concurrent read/write access at the
    file level.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._local = threading.local()
        init_db(path)

    def _conn(self) -> sqlite3.Connection:
        """Return the calling thread's SQLite connection, creating it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._local.conn = _open(self._path)
        return self._local.conn

    def close(self) -> None:
        """Close the current thread's connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


# ---------------------------------------------------------------------------
# VisitedDB — atomic deduplication set
# ---------------------------------------------------------------------------

class VisitedDB(_ThreadLocalDB):
    """Persistent set of visited URLs for cross-session deduplication."""

    def mark_visited(self, url: str) -> bool:
        """Insert URL atomically; return True if newly inserted, False if already present."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO visited (url, visited_at) VALUES (?, ?)",
                (url, time.time()),
            )
            # Read changes() before commit so the count is not reset.
            newly_inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
            conn.commit()
            return newly_inserted
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    def is_visited(self, url: str) -> bool:
        """Return True if the URL has been recorded in the visited table."""
        row = self._conn().execute(
            "SELECT 1 FROM visited WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def count(self) -> int:
        """Return total number of visited URLs."""
        row = self._conn().execute("SELECT COUNT(*) FROM visited").fetchone()
        return int(row[0]) if row else 0

    def reset(self) -> None:
        """Delete all visited records (used for full database reset)."""
        conn = self._conn()
        conn.execute("DELETE FROM visited")
        conn.commit()


# ---------------------------------------------------------------------------
# FailedURLDB — per-session HTTP/network failure log
# ---------------------------------------------------------------------------

class FailedURLDB(_ThreadLocalDB):
    """Records every URL that could not be fetched, grouped by session."""

    def add_failure(self, session_id: int, url: str, error: str) -> None:
        """Record a single URL failure with its error description."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO failed_urls (session_id, url, error, failed_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, url, error, time.time()),
            )
            conn.commit()
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass

    def failures_for_session(self, session_id: int, limit: int = 200) -> List[Dict]:
        """Return up to *limit* failure records for the given session, newest first."""
        rows = self._conn().execute(
            "SELECT id, session_id, url, error, failed_at "
            "FROM failed_urls WHERE session_id = ? "
            "ORDER BY failed_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_for_session(self, session_id: int) -> int:
        """Return the total number of failures recorded for *session_id*."""
        row = self._conn().execute(
            "SELECT COUNT(*) FROM failed_urls WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# SessionDB — crawl session lifecycle management
# ---------------------------------------------------------------------------

class SessionDB(_ThreadLocalDB):
    """Creates, updates, and queries crawl session records."""

    def create_session(
        self, origin: str, depth: int, same_domain: bool = True
    ) -> int:
        """Insert a new 'running' session row and return its auto-incremented ID."""
        conn = self._conn()
        try:
            cursor = conn.execute(
                "INSERT INTO crawl_sessions "
                "(origin, depth, started_at, same_domain, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (origin, depth, time.time(), 1 if same_domain else 0),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def finish_session(
        self,
        session_id: int,
        pages_indexed: int,
        urls_processed: int,
        urls_failed: int,
        urls_skipped: int,
        urls_dropped: int = 0,
    ) -> None:
        """Mark a session as 'completed' and record its final counters."""
        conn = self._conn()
        try:
            conn.execute(
                """UPDATE crawl_sessions SET
                       finished_at    = ?,
                       pages_indexed  = ?,
                       urls_processed = ?,
                       urls_failed    = ?,
                       urls_skipped   = ?,
                       urls_dropped   = ?,
                       status         = 'completed'
                   WHERE id = ?""",
                (
                    time.time(),
                    pages_indexed,
                    urls_processed,
                    urls_failed,
                    urls_skipped,
                    urls_dropped,
                    session_id,
                ),
            )
            conn.commit()
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass

    def update_session_stats(self, session_id: int, **kwargs: Any) -> None:
        """Perform a partial UPDATE of any valid session columns.

        Column names are validated against a whitelist before being
        interpolated into the SQL string — values remain parameterised.
        """
        if not kwargs:
            return
        _allowed = {
            "pages_indexed", "urls_processed", "urls_failed",
            "urls_skipped", "urls_dropped", "status", "finished_at",
        }
        updates = {k: v for k, v in kwargs.items() if k in _allowed}
        if not updates:
            return
        conn = self._conn()
        try:
            set_clause = ", ".join(f"{col} = ?" for col in updates)
            values = list(updates.values()) + [session_id]
            conn.execute(
                f"UPDATE crawl_sessions SET {set_clause} WHERE id = ?", values
            )
            conn.commit()
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass

    def get_session(self, session_id: int) -> Optional[Dict]:
        """Return a single session row as a dict, or None if not found."""
        row = self._conn().execute(
            "SELECT * FROM crawl_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 20) -> List[Dict]:
        """Return the most recent *limit* sessions, newest first."""
        rows = self._conn().execute(
            "SELECT * FROM crawl_sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_active_session(self) -> Optional[Dict]:
        """Return the most recently started session with status='running', or None."""
        row = self._conn().execute(
            "SELECT * FROM crawl_sessions "
            "WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# QueueStateDB — persisted BFS frontier for resume-after-interruption
# ---------------------------------------------------------------------------

class QueueStateDB(_ThreadLocalDB):
    """Serialises and restores the BFS crawl queue to SQLite.

    On ``stop()`` the crawler calls ``save_queue()`` with all pending work.
    On the next startup for the same origin, ``find_resumable_session()``
    locates the incomplete session, and ``load_queue()`` reconstructs the
    frontier so crawling continues exactly where it left off.
    """

    def save_queue(self, session_id: int, items: List[tuple]) -> None:
        """Replace the stored queue for *session_id* with *items*.

        Args:
            session_id: The active session's ID.
            items:      List of ``(url, origin, depth, max_depth)`` tuples.
        """
        conn = self._conn()
        now = time.time()
        try:
            conn.execute(
                "DELETE FROM crawl_queue_state WHERE session_id = ?",
                (session_id,),
            )
            if items:
                conn.executemany(
                    "INSERT INTO crawl_queue_state "
                    "(session_id, url, origin, depth, max_depth, saved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (session_id, url, origin, depth, max_depth, now)
                        for url, origin, depth, max_depth in items
                    ],
                )
            conn.commit()
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass

    def load_queue(self, session_id: int) -> List[tuple]:
        """Return stored queue items as ``(url, origin, depth, max_depth)`` tuples."""
        rows = self._conn().execute(
            "SELECT url, origin, depth, max_depth "
            "FROM crawl_queue_state WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        return [(row["url"], row["origin"], row["depth"], row["max_depth"]) for row in rows]

    def clear_queue(self, session_id: int) -> None:
        """Delete all queued items for the given session (called after successful resume)."""
        conn = self._conn()
        conn.execute(
            "DELETE FROM crawl_queue_state WHERE session_id = ?", (session_id,)
        )
        conn.commit()

    def find_resumable_session(self, origin: str) -> Optional[Dict]:
        """Return the most recent interrupted session for *origin* that has a saved queue.

        A session is considered resumable when:
        - ``status = 'running'`` (either gracefully stopped or crashed mid-run)
        - At least one row exists in ``crawl_queue_state`` for that session
        """
        row = self._conn().execute(
            """SELECT cs.*
               FROM crawl_sessions cs
               WHERE cs.origin = ?
                 AND cs.status = 'running'
                 AND EXISTS (
                     SELECT 1 FROM crawl_queue_state cqs
                     WHERE cqs.session_id = cs.id
                 )
               ORDER BY cs.id DESC
               LIMIT 1""",
            (origin,),
        ).fetchone()
        return dict(row) if row else None
