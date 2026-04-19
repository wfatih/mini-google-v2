# Agent 03: Storage Agent
## mini-google-v2 Storage Layer Implementation

**Agent ID:** `03_storage`  
**Date:** 2026-04-19  
**Status:** Complete — Outputs handed off to Agents 02, 04, 06  
**Input Documents:**
- `agents/01_architect_agent.md` (architecture decisions)
- `product_prd.md` (functional requirements)

**Output Files:**
- `storage/__init__.py` — public re-exports
- `storage/database.py` — schema, migrations, and all DB classes (~310 lines)
- `storage/index.py` — InvertedIndex with search and scoring (~240 lines)
- `agents/03_storage_agent.md` (this file)
- `logs/agent_log.jsonl` (appended)

---

## 1. Agent Role & Responsibilities

The Storage Agent is responsible for the **entire persistence layer** of mini-google-v2.
Its outputs are consumed by:

- **Crawler Agent (02)** — calls `VisitedDB.mark_visited()`, `InvertedIndex.add_page()`,
  `SessionDB.create_session() / finish_session()`, `FailedURLDB.add_failure()`,
  `QueueStateDB.save_queue() / load_queue()`
- **API Agent (04)** — calls `InvertedIndex.search_scored()`, `SessionDB.list_sessions()`,
  `InvertedIndex.recent_pages()`, `InvertedIndex.get_stats()`, `InvertedIndex.reset()`
- **QA Agent (06)** — tests all public methods independently

The Storage Agent does **not** implement HTTP, crawling, or UI logic.

---

## 2. Architecture Overview

```
storage/
├── __init__.py       Re-exports all public symbols
├── database.py       _open, init_db, _ThreadLocalDB, VisitedDB,
│                     FailedURLDB, SessionDB, QueueStateDB
└── index.py          InvertedIndex (extends _ThreadLocalDB)
```

### Dependency Graph (storage layer only)
```
storage/index.py
  └── storage/database.py   (no upstream deps — stdlib only)
```

No circular imports. Both modules use only Python stdlib:
`sqlite3`, `threading`, `time`, `os`, `re`, `typing`.

---

## 3. SQLite Schema

### 3.1 Tables

| Table | Primary Key | Purpose |
|---|---|---|
| `pages` | `url` TEXT | One row per indexed page |
| `word_index` | `(word, url)` | Per-(word, URL) term frequency |
| `visited` | `url` TEXT | Permanent deduplication set |
| `crawl_sessions` | `id` INTEGER | Session lifecycle and counters |
| `failed_urls` | `id` INTEGER | Per-session HTTP/network failures |
| `crawl_queue_state` | `id` INTEGER | BFS queue snapshots for resume |

### 3.2 Indexes

| Index Name | Table | Column(s) | Purpose |
|---|---|---|---|
| `idx_word` | `word_index` | `word` | Fast exact + prefix lookup |
| `idx_word_url` | `word_index` | `url` | Fast pages-by-URL lookup |
| `idx_failed_session` | `failed_urls` | `session_id` | Per-session failure retrieval |
| `idx_queue_session` | `crawl_queue_state` | `session_id` | Queue restore by session |

### 3.3 Schema Differences from PRD v1.0

The PRD Section 10.1 schema was revised during implementation to better
serve the crawler agent's interface contract. Key differences:

| PRD Schema | Implemented Schema | Reason |
|---|---|---|
| `word_index.tf REAL` | `word_index.frequency INTEGER` | Avoids TF re-normalisation issues; scoring uses raw frequency with depth penalty |
| `pages.origin_url` | `pages.origin` | Shorter column name; consistent with session table |
| `crawl_sessions.origin_url` | `crawl_sessions.origin` | Consistency |
| No `crawl_queue_state` table | Added `crawl_queue_state` | Dedicated queue persistence table (cleaner than JSON blob in `crawl_sessions`) |
| `crawl_sessions.queue_snapshot TEXT` | Removed | Replaced by `crawl_queue_state` rows |

---

## 4. Implementation Decisions

### Decision 4.1: Thread-Local Connection Pattern

**Decision:** Each class that inherits `_ThreadLocalDB` keeps a `threading.local()`
instance. `_conn()` creates one `sqlite3.Connection` per OS thread on first access.

**Rationale:**
- SQLite connections are not safe to share across threads when mixing reads and writes
- `threading.local()` provides zero-overhead per-thread storage without locks
- Every `ThreadPoolExecutor` worker in the crawler engine gets its own connection
- The asyncio main thread and each FastAPI request handler each get their own connection
- WAL mode at the file level coordinates concurrent access safely

**Consequence:** `close()` only closes the *calling thread's* connection.
Callers responsible for calling `close()` on each thread that uses the DB.

---

### Decision 4.2: `changes()` Before `commit()`

**Decision:** In `VisitedDB.mark_visited()` and `InvertedIndex.add_page()`,
`SELECT changes()` is called **immediately after** the `INSERT OR IGNORE`, before
any `commit()` or subsequent SQL statement.

**Rationale:** SQLite's `changes()` function returns the row count of the most
recently completed DML statement. A `commit()` or any subsequent `execute()` call
would reset this counter. Checking changes before commit guarantees correctness
even under concurrent load.

```python
conn.execute("INSERT OR IGNORE INTO visited (url, visited_at) VALUES (?, ?)", ...)
newly_inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
conn.commit()   # <-- commit AFTER reading changes()
return newly_inserted
```

---

### Decision 4.3: Scoring Formula

The architect specified TF-IDF with exact/prefix weights. After reconciling
the PRD and the concrete storage spec, the scoring formula adopted is:

```
score(url) = SUM over all matched (token, word) pairs:
    (frequency * 10)
  + (1000 if word == token else 0)     ← exact match bonus
  - (depth * 5)                         ← shallow-page bonus
```

**Why this formula instead of pure TF×IDF×weight:**
- The `word_index` schema stores raw `frequency INTEGER` (not pre-computed TF)
- Computing IDF at query time requires a COUNT per distinct word — adds one
  query per token per search call
- The adopted formula embeds a sufficient ranking signal:
  - High-frequency terms score proportionally higher (linear with count)
  - Exact keyword matches get a large +1000 bonus, ensuring they always outrank
    prefix-only matches for the same page (satisfies PRD US-06 acceptance criteria)
  - Depth penalty penalises deep pages slightly, favouring root-adjacent content
- IDF normalisation can be added later without changing the schema

---

### Decision 4.4: Two-Pass Search in Python vs. SQL UNION

**Decision:** For each query token, a single SQL query using
`WHERE word = ? OR word LIKE ?` is issued. Score aggregation across tokens
and URLs is done in Python.

**Alternatives considered:**

| Approach | Pro | Con |
|---|---|---|
| `UNION ALL` of exact + prefix subqueries | Pure SQL | Complex parameterisation for N tokens; harder to aggregate scores across tokens |
| Separate exact + prefix queries per token | Maximum index utilisation | 2× queries per token |
| Single `WHERE word = ? OR word LIKE ?` | One round-trip per token | Both exact and prefix served by same `idx_word` B-tree scan |

**Chosen:** Single query per token using `CASE WHEN word = ? THEN 1 ELSE 0 END`
to tag exact matches inline, then aggregate in Python. At 10 req/s crawl rate
and < 50 search tokens, Python-side aggregation adds negligible latency.

---

### Decision 4.5: `crawl_queue_state` as Rows vs. JSON Blob

**Decision:** Queue items are stored as individual rows in `crawl_queue_state`
rather than as a single JSON blob in `crawl_sessions.queue_snapshot`.

**PRD proposal:** `crawl_sessions.queue_snapshot TEXT` (JSON array).

**Rejection reason:**
- JSON blob requires deserialising the entire queue on every `save_queue()` call
- Rows support efficient `DELETE WHERE session_id = ?` without parsing
- Individual rows are inspectable/debuggable via standard SQL tools
- Atomic replace: DELETE + INSERT is one transaction; crash-safe with WAL

**Trade-off:** Slightly more rows in the database. At 500-item queue limit, this
adds at most 500 rows — negligible for SQLite.

---

### Decision 4.6: `update_session_stats` Column Whitelist

**Decision:** The `update_session_stats()` method accepts `**kwargs` but validates
column names against an explicit whitelist before interpolating them into the
SQL `SET` clause.

**Rationale:** SQLite parameterised queries cannot bind column names — only values.
A whitelist approach is the safe alternative: user-controlled column names cannot
cause SQL injection because only pre-approved names reach the query string.

```python
_allowed = {"pages_indexed", "urls_processed", ..., "status", "finished_at"}
updates = {k: v for k, v in kwargs.items() if k in _allowed}
```

---

### Decision 4.7: `init_db` Called in `_ThreadLocalDB.__init__`

**Decision:** `init_db(path)` is called once during `_ThreadLocalDB.__init__`,
which creates a temporary connection solely for schema setup. Thread-local
connections are subsequently created lazily on first `_conn()` access.

**Rationale:**
- Schema setup only needs to happen once — not once per thread
- Separating schema init from per-thread connection creation is cleaner
- `init_db` is idempotent (`CREATE TABLE IF NOT EXISTS`) — safe to call multiple
  times if multiple `_ThreadLocalDB` subclass instances are created

---

## 5. Public API Summary

### `storage/database.py`

```python
DB_PATH: str                          # "data/mini_google.db"

def _open(path: str) -> sqlite3.Connection
def init_db(path: str) -> None

class _ThreadLocalDB:
    def __init__(self, path: str) -> None
    def _conn(self) -> sqlite3.Connection
    def close(self) -> None

class VisitedDB(_ThreadLocalDB):
    def mark_visited(self, url: str) -> bool
    def is_visited(self, url: str) -> bool
    def count(self) -> int
    def reset(self) -> None

class FailedURLDB(_ThreadLocalDB):
    def add_failure(self, session_id: int, url: str, error: str) -> None
    def failures_for_session(self, session_id: int, limit: int = 200) -> List[Dict]
    def count_for_session(self, session_id: int) -> int

class SessionDB(_ThreadLocalDB):
    def create_session(self, origin: str, depth: int, same_domain: bool = True) -> int
    def finish_session(self, session_id, pages_indexed, urls_processed,
                       urls_failed, urls_skipped, urls_dropped=0) -> None
    def update_session_stats(self, session_id: int, **kwargs) -> None
    def get_session(self, session_id: int) -> Optional[Dict]
    def list_sessions(self, limit: int = 20) -> List[Dict]
    def get_active_session(self) -> Optional[Dict]

class QueueStateDB(_ThreadLocalDB):
    def save_queue(self, session_id: int, items: List[tuple]) -> None
    def load_queue(self, session_id: int) -> List[tuple]
    def clear_queue(self, session_id: int) -> None
    def find_resumable_session(self, origin: str) -> Optional[Dict]
```

### `storage/index.py`

```python
class InvertedIndex(_ThreadLocalDB):
    def add_page(self, url, origin, depth, word_counts,
                 session_id=None, title="") -> bool
    def search(self, query: str, limit: int = 100) -> List[Tuple[str, str, int]]
    def search_scored(self, query: str, limit: int = 100) -> List[Tuple[str, str, int, float]]
    def page_count(self) -> int
    def word_count(self) -> int
    def recent_pages(self, limit: int = 10) -> List[dict]
    def pages_for_session(self, session_id: int, limit: int = 200) -> List[dict]
    def get_stats(self) -> dict   # {pages, words, visited, sessions}
    def reset(self) -> None
```

---

## 6. Thread-Safety Guarantees

| Operation | Safety Mechanism |
|---|---|
| Concurrent `add_page()` from multiple crawler threads | Each thread has its own connection; WAL serialises writes |
| Concurrent `search()` during active crawl | WAL allows readers to proceed without waiting for writers |
| `mark_visited()` called from multiple threads simultaneously | `INSERT OR IGNORE` is atomic at the SQLite level; `changes()` read before commit |
| `save_queue()` crash recovery | WAL + single-transaction DELETE+INSERT; partial write never committed |

---

## 7. Performance Characteristics

| Operation | Complexity | Notes |
|---|---|---|
| `mark_visited(url)` | O(log n) | B-tree lookup on PK |
| `add_page(url, ...)` | O(w log n) | w = unique words in page |
| `search_scored(query)` | O(t × m) | t = tokens, m = matching rows per token |
| `recent_pages(limit)` | O(limit) | Covered by `indexed_at DESC` sort |
| `page_count()` | O(1) | SQLite maintains internal count |

For an index of 100,000 pages with 50M word rows, search latency is
expected to be < 200ms at P99 on commodity hardware — within the 500ms
PRD target.

---

## 8. Migration Strategy

On every `init_db()` call, `_run_migrations()` attempts `ALTER TABLE ADD COLUMN`
for every column that might be missing in an older schema. `OperationalError`
(column already exists) is silently ignored. This ensures forward compatibility
as new columns are added without requiring a manual migration script.

---

## 9. Handoff Notes for Dependent Agents

### → Crawler Agent (02)

- Use `VisitedDB(DB_PATH)` for URL deduplication — create one instance; each thread uses it safely via `_conn()`
- `mark_visited()` returns `True` on first visit — use this to decide whether to enqueue the URL
- `InvertedIndex(DB_PATH)` accepts `word_counts: Dict[str, int]` — the output of `tokenize()` in `crawler/parser.py`
- `SessionDB.finish_session()` expects `urls_dropped` (queue-full count) as a separate counter
- `QueueStateDB.save_queue()` items format: `(url: str, origin: str, depth: int, max_depth: int)`
- `QueueStateDB.find_resumable_session(origin)` returns `None` if no resumable session exists

### → API Agent (04)

- `InvertedIndex.search_scored(query, limit)` returns `(url, origin, depth, score)` tuples
- `InvertedIndex.get_stats()` returns `{"pages", "words", "visited", "sessions"}` — use for `/api/status`
- `InvertedIndex.reset()` deletes pages, word_index, and visited — does NOT delete sessions/failures
- `SessionDB.list_sessions(limit=50)` returns dicts with keys matching the PRD `Session` JSON shape
- `FailedURLDB.failures_for_session(session_id)` used to populate the History tab's failure list

### → QA Agent (06)

Key invariants to test:
1. `mark_visited(url)` returns `True` on first call, `False` on second call with the same URL
2. `add_page(url, ...)` returns `True` on first call, `False` on duplicate URL
3. `search_scored("exact_word")` ranks exact-match pages above prefix-only pages
4. Concurrent `add_page()` from 10 threads does not corrupt the `word_index` table
5. `save_queue()` + `load_queue()` round-trips all items with correct types
6. `find_resumable_session(origin)` returns `None` for cleared queues
7. `reset()` does not affect `crawl_sessions` or `failed_urls` rows

---

*End of Storage Agent Documentation*  
*Agent 03 — 2026-04-19 — Handoff complete*
