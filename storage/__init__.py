"""
storage — SQLite persistence layer for mini-google-v2.

Public API:
    DB_PATH       — Default database file path ("data/mini_google.db")
    InvertedIndex — TF-IDF inverted index (add_page, search, search_scored, …)
    VisitedDB     — Atomic URL deduplication set
    SessionDB     — Crawl session lifecycle management
    FailedURLDB   — Per-session HTTP failure log
    QueueStateDB  — BFS queue snapshot for resume-after-interruption
    init_db       — Idempotent schema creation / migration function
"""

from storage.database import (
    DB_PATH,
    FailedURLDB,
    QueueStateDB,
    SessionDB,
    VisitedDB,
    init_db,
)
from storage.index import InvertedIndex

__all__ = [
    "DB_PATH",
    "InvertedIndex",
    "VisitedDB",
    "SessionDB",
    "FailedURLDB",
    "QueueStateDB",
    "init_db",
]
