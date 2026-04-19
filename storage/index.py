"""
storage/index.py — TF-IDF inverted index over SQLite for mini-google-v2.

InvertedIndex extends _ThreadLocalDB so every thread (crawler workers,
the asyncio main thread, and FastAPI request handlers) operates on its own
SQLite connection.  WAL journal mode allows search() to run concurrently
with add_page() without blocking.

Scoring formula (search_scored):
    score(url) = SUM over matched tokens of:
        (frequency * 10) + (1000 if exact_match else 0) - (depth * 5)

This rewards:
  - High-frequency terms          (+frequency * 10)
  - Exact keyword hits            (+1000 per token)
  - Shallow pages (closer to root) (+depth penalty removed)
"""

import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from storage.database import _ThreadLocalDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenize_query(query: str) -> List[str]:
    """Lowercase and extract unique alphabetic tokens of length >= 2."""
    tokens = re.findall(r"[a-z]{2,}", query.lower())
    seen: set = set()
    result: List[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


# ---------------------------------------------------------------------------
# InvertedIndex
# ---------------------------------------------------------------------------

class InvertedIndex(_ThreadLocalDB):
    """SQLite-backed inverted index with exact/prefix search and TF-weighted scoring.

    Usage:
        idx = InvertedIndex("data/mini_google.db")
        idx.add_page(url, origin, depth, word_counts, session_id, title)
        results = idx.search_scored("python asyncio")
    """

    def __init__(self, db_path: str):
        super().__init__(db_path)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def add_page(
        self,
        url: str,
        origin: str,
        depth: int,
        word_counts: Dict[str, int],
        session_id: Optional[int] = None,
        title: str = "",
    ) -> bool:
        """Index a page and its word-frequency map.

        The operation is idempotent: if *url* already exists in the
        ``pages`` table the method returns ``False`` without modifying
        any data.  On success it inserts/replaces all word rows and
        returns ``True``.

        Args:
            url:         Canonical URL of the page.
            origin:      Root URL that seeded the crawl session.
            depth:       BFS depth at which this page was discovered.
            word_counts: Mapping of lowercase token → occurrence count
                         (output of ``crawler.parser.tokenize()``).
            session_id:  ID of the active ``crawl_sessions`` row (nullable).
            title:       Page ``<title>`` text, if extracted.

        Returns:
            True  — page was newly inserted.
            False — page already existed; index not modified.
        """
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO pages "
                "(url, origin, depth, indexed_at, session_id, title) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (url, origin, depth, time.time(), session_id, title),
            )
            newly_inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
            if not newly_inserted:
                conn.rollback()
                return False

            if word_counts:
                conn.executemany(
                    "INSERT OR REPLACE INTO word_index "
                    "(word, url, origin, depth, frequency) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (word, url, origin, depth, freq)
                        for word, freq in word_counts.items()
                        if word and freq > 0
                    ],
                )

            conn.commit()
            return True

        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Read path — search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 100) -> List[Tuple[str, str, int]]:
        """Return ranked ``(url, origin, depth)`` triples for *query*.

        Internally delegates to ``search_scored`` and strips the score
        component, preserving the same ranking order.

        Args:
            query: Raw search string (tokenised internally).
            limit: Maximum number of results to return.

        Returns:
            List of ``(url, origin, depth)`` tuples, best match first.
        """
        scored = self.search_scored(query, limit)
        return [(url, origin, depth) for url, origin, depth, _ in scored]

    def search_scored(
        self, query: str, limit: int = 100
    ) -> List[Tuple[str, str, int, float]]:
        """Return ranked ``(url, origin, depth, score)`` tuples for *query*.

        Two passes are executed per token:
        - **Exact match** — ``word = token``   contributes score as-is
        - **Prefix match** — ``word LIKE token%`` (excluding exact)  also contributes

        Per-token, per-URL scores are accumulated in Python, then sorted
        descending.  WAL mode allows this read to run concurrently with
        ongoing ``add_page()`` writes.

        Score formula per (token, url, word) match:
            ``(frequency * 10) + (1000 if word == token else 0) - (depth * 5)``

        Args:
            query: Raw search string.
            limit: Maximum results.

        Returns:
            List of ``(url, origin, depth, score)`` tuples, best match first.
        """
        tokens = _tokenize_query(query)
        if not tokens:
            return []

        conn = self._conn()
        scores: Dict[str, float] = {}
        page_meta: Dict[str, Tuple[str, int]] = {}  # url -> (origin, depth)

        for token in tokens:
            prefix = token + "%"
            try:
                rows = conn.execute(
                    """SELECT wi.url,
                              p.origin,
                              p.depth,
                              wi.frequency,
                              CASE WHEN wi.word = ? THEN 1 ELSE 0 END AS is_exact
                       FROM word_index wi
                       JOIN pages p ON wi.url = p.url
                       WHERE wi.word = ? OR wi.word LIKE ?""",
                    (token, token, prefix),
                ).fetchall()
            except sqlite3.OperationalError:
                continue

            for row in rows:
                url: str = row["url"]
                freq: int = row["frequency"]
                is_exact: int = row["is_exact"]
                depth: int = row["depth"]

                contribution = (freq * 10) + (1000 if is_exact else 0) - (depth * 5)
                scores[url] = scores.get(url, 0.0) + contribution
                if url not in page_meta:
                    page_meta[url] = (row["origin"], depth)

        if not scores:
            return []

        sorted_urls = sorted(scores, key=lambda u: scores[u], reverse=True)[:limit]
        return [
            (url, page_meta[url][0], page_meta[url][1], float(scores[url]))
            for url in sorted_urls
        ]

    # ------------------------------------------------------------------
    # Aggregate statistics
    # ------------------------------------------------------------------

    def page_count(self) -> int:
        """Return the total number of indexed pages."""
        row = self._conn().execute("SELECT COUNT(*) FROM pages").fetchone()
        return int(row[0]) if row else 0

    def word_count(self) -> int:
        """Return the number of distinct words in the inverted index."""
        row = self._conn().execute(
            "SELECT COUNT(DISTINCT word) FROM word_index"
        ).fetchone()
        return int(row[0]) if row else 0

    def recent_pages(self, limit: int = 10) -> List[dict]:
        """Return the *limit* most recently indexed pages as dicts.

        Each dict contains: url, origin, depth, indexed_at, title.
        """
        rows = self._conn().execute(
            "SELECT url, origin, depth, indexed_at, title "
            "FROM pages ORDER BY indexed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def pages_for_session(self, session_id: int, limit: int = 200) -> List[dict]:
        """Return up to *limit* pages indexed during *session_id*, newest first."""
        rows = self._conn().execute(
            "SELECT url, origin, depth, indexed_at, title "
            "FROM pages WHERE session_id = ? "
            "ORDER BY indexed_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict:
        """Return a summary dict with counts across all major tables.

        Returns:
            ``{"pages": int, "words": int, "visited": int, "sessions": int}``
        """
        conn = self._conn()
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        words = conn.execute(
            "SELECT COUNT(DISTINCT word) FROM word_index"
        ).fetchone()[0]
        visited = conn.execute("SELECT COUNT(*) FROM visited").fetchone()[0]
        sessions = conn.execute(
            "SELECT COUNT(*) FROM crawl_sessions"
        ).fetchone()[0]
        return {
            "pages": int(pages),
            "words": int(words),
            "visited": int(visited),
            "sessions": int(sessions),
        }

    def export_pdata(self, output_path: str) -> int:
        """Export ``word_index`` rows into legacy ``p.data`` text format.

        Output format (space-separated, one row per line):
            ``word url origin depth frequency``

        Args:
            output_path: Target file path (e.g. ``data/p.data``).

        Returns:
            Number of lines written.
        """
        abs_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        rows = self._conn().execute(
            "SELECT word, url, origin, depth, frequency "
            "FROM word_index ORDER BY word, url"
        ).fetchall()

        with open(abs_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(
                    f"{row['word']} {row['url']} {row['origin']} "
                    f"{row['depth']} {row['frequency']}\n"
                )
        return len(rows)

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Delete all indexed data: pages, word index, and visited records.

        Called from the API ``DELETE /api/reset`` endpoint.  Does NOT
        touch ``crawl_sessions`` or ``failed_urls`` (history is preserved).
        """
        conn = self._conn()
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM word_index")
        conn.execute("DELETE FROM visited")
        conn.commit()
