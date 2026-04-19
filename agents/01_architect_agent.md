# Agent 01: Architect Agent
## mini-google-v2 System Architecture Documentation

**Agent ID:** `01_architect`  
**Date:** 2026-04-19  
**Status:** Complete — Outputs handed off to Agents 02–06  
**Output Files:**
- `product_prd.md` (system-wide specification)
- `agents/01_architect_agent.md` (this file)
- `logs/agent_log.jsonl` (structured interaction log)

---

## 1. Agent Role & Responsibilities

The Architect Agent is the **first agent in the multi-agent pipeline**. Its sole responsibility is to produce a complete, unambiguous, cross-agent specification before any code is written. Specifically, the Architect Agent:

1. **Analyzes requirements** — Translates high-level product goals into concrete functional and non-functional requirements
2. **Makes architectural decisions** — Chooses technologies, patterns, and data structures with documented rationale
3. **Defines interfaces** — Specifies every public API, data model, and module boundary that other agents must respect
4. **Produces the PRD** — A canonical specification document that all implementation agents treat as ground truth
5. **Documents trade-offs** — Records what was decided, what was rejected, and why
6. **Enables parallel work** — Produces specifications detailed enough that Agents 02–05 can work concurrently without ambiguity

The Architect Agent does **not** write implementation code. It creates the blueprint.

---

## 2. Prompt Used

The following prompt was provided to this agent:

```
You are the Architect Agent for a multi-agent web crawler and search system called "mini-google-v2".
Your job is to design the complete system architecture and produce the PRD and architecture
specification that all other agents will use.

[Requirements: BFS crawl with depth k, asyncio + ThreadPoolExecutor, token-bucket rate limiting,
bounded asyncio.Queue, SQLite WAL, thread-local connections, TF-IDF inverted index with exact/prefix
weights, FastAPI + uvicorn, SSE for real-time updates, single-page HTML/CSS/JS UI with dark mode,
CLI, resume after interruption, no Scrapy/Elasticsearch/Whoosh, localhost only, Python-native HTTP]

[File structure, interface contracts, and output file paths were specified]
```

---

## 3. Architectural Decisions

### Decision 3.1: Concurrency Model — asyncio + ThreadPoolExecutor

**Decision:** Use Python's `asyncio` event loop as the primary concurrency engine. All blocking HTTP calls (via `urllib`) are offloaded to a `ThreadPoolExecutor` using `loop.run_in_executor()`.

**Reasoning:**
- `asyncio` is Python-native and requires no external concurrency library
- The event loop can manage thousands of coroutines with minimal overhead compared to threads
- HTTP fetching is inherently I/O-bound — the thread pool liberates the event loop while urllib blocks on network
- `asyncio.Queue` provides a natural, thread-safe (via the event loop) BFS frontier
- `ThreadPoolExecutor` thread count (default 10) controls actual concurrency without event loop overhead

**Alternatives Rejected:**
| Alternative | Why Rejected |
|-------------|-------------|
| `threading.Thread` directly | No clean back-pressure mechanism; harder to integrate with asyncio ecosystem |
| `multiprocessing` | Overkill for I/O-bound task; IPC adds complexity; SQLite sharing is harder |
| `aiohttp` | External dependency; `urllib` is stdlib and sufficient for this use case |
| `Scrapy` | Explicitly prohibited by requirements; also heavyweight |
| Pure sync (requests + threading) | Cannot support the asyncio-native Queue and SSE streaming naturally |

**Trade-off:** Using `loop.run_in_executor()` introduces a small overhead per fetch compared to a pure asyncio HTTP library. This is acceptable because fetch latency (network I/O) dwarfs scheduling overhead by orders of magnitude.

---

### Decision 3.2: HTTP Client — urllib (stdlib)

**Decision:** Use `urllib.request.urlopen()` for all HTTP operations.

**Reasoning:**
- Explicitly required by the "language-native functionality" constraint
- `urllib` handles redirects, chunked transfer, content-encoding (gzip) out of the box
- Runs synchronously inside a ThreadPoolExecutor thread — clean separation from asyncio
- No dependency management required

**Trade-offs:**
- No connection pooling (each `urlopen()` opens a new TCP connection). At rate=10 req/s, this is acceptable.
- No async-native HTTP (mitigated by run_in_executor)
- Limited cookie/session support (not needed for read-only crawling)

---

### Decision 3.3: Back-Pressure — Bounded asyncio.Queue

**Decision:** The BFS frontier is an `asyncio.Queue(maxsize=max_queue)`. When full, `put_nowait()` raises `QueueFull` and the URL is silently dropped (counted as "skipped").

**Reasoning:**
- A bounded queue is the canonical back-pressure mechanism: the producer (link extractor) cannot outpace the consumer (fetcher) beyond the configured limit
- `asyncio.Queue` is thread-safe with respect to the asyncio event loop (operations are serialized through the event loop)
- `put_nowait()` + catch `QueueFull` is the correct async-safe idiom (vs `put()` which would `await` and could cause deadlock when all workers are waiting to enqueue while no one is dequeuing)
- `maxsize=500` is a conservative default that prevents memory exhaustion during wide BFS on large sites

**Alternative Considered:** `asyncio.Queue` with `put()` (blocking await). Rejected because workers calling `put()` would block their coroutine slot waiting for space, potentially causing all workers to deadlock if the queue is full and no consumer is running.

---

### Decision 3.4: Rate Limiting — Token Bucket

**Decision:** Implement a token-bucket rate limiter as a pure asyncio construct. Each request must acquire a token before dispatching.

**Token Bucket Algorithm:**
```
- Bucket capacity = rate (e.g., 10 tokens)
- Tokens replenish at rate R tokens/second
- Each fetch costs 1 token
- If bucket is empty, asyncio.sleep() for 1/R seconds
```

**Reasoning:**
- Token bucket allows short bursts (up to bucket capacity) while enforcing a long-term average rate
- This is more natural for web crawling than a strict "one request every 1/R seconds" leaky bucket
- Pure asyncio implementation (no threading.Event, no time.sleep) — does not block the event loop
- Configurable per-crawl via `rate` parameter

**Alternatives Rejected:**
| Alternative | Why Rejected |
|-------------|-------------|
| `time.sleep(1/rate)` per fetch | Blocks the thread; prevents concurrent workers |
| `asyncio.sleep(1/rate)` | Leaky bucket, not token bucket; no burst handling |
| External `aiohttp-retry` or `ratelimit` | External deps violate "language-native" constraint |

---

### Decision 3.5: Storage — SQLite with WAL Mode

**Decision:** Use a single SQLite database file at `data/mini_google.db` with WAL journal mode, thread-local connections, and a schema of 5 tables.

**WAL Mode Benefits:**
- Writers do not block readers — critical for concurrent search during indexing
- Multiple simultaneous readers are supported
- Crash-safe: WAL is checkpointed atomically
- No server required — file-based

**Thread-Local Connections:**
- SQLite connections are **not thread-safe** (when using `check_same_thread=False` without care)
- Solution: `threading.local()` stores one connection per thread
- Each ThreadPoolExecutor worker creates its own connection on first use
- The asyncio main thread has its own connection
- Eliminates all locking concerns at the Python level

**Alternatives Rejected:**
| Alternative | Why Rejected |
|-------------|-------------|
| PostgreSQL | Requires a running server; violates "no external services" goal |
| MySQL/MariaDB | Same as above |
| Redis | Volatile by default; requires a server |
| Elasticsearch | Explicitly prohibited |
| In-memory dict | Not persistent; can't resume; memory-limited |
| Single shared SQLite connection with lock | Serializes all DB access; kills throughput |

**Known Limitations:**
- SQLite has a global write lock (WAL relaxes this but writes are still serialized)
- Very high write throughput (>1000 writes/sec) would saturate SQLite. At 10 req/s crawl rate, peak write rate is ~10 pages/sec — well within SQLite's capability.

---

### Decision 3.6: Inverted Index — TF-IDF in SQLite

**Decision:** Build the inverted index directly in SQLite using a `word_index` table storing per-(word, url) TF values. IDF is computed at query time. Scoring uses exact match weight 3× and prefix match weight 1×.

**Schema Design:**
```sql
word_index(word TEXT, url TEXT, tf REAL, count INT, total_words INT)
```

**Query Strategy:**
```sql
-- For each query token:
SELECT wi.word, wi.url, wi.tf, p.origin_url, p.depth
FROM word_index wi JOIN pages p ON wi.url = p.url
WHERE wi.word = :token        -- exact (weight=3)
   OR wi.word LIKE :token_pct -- prefix (weight=1)
```

**IDF Computation:**
```
IDF(word) = log(total_pages / count(distinct pages containing word))
```
Computed in Python after fetching per-word aggregates.

**Reasoning:**
- No external search library required
- SQLite's `LIKE` operator with prefix patterns (`word%`) uses the btree index efficiently (unlike `%word%` which scans)
- TF-IDF is the industry-standard baseline retrieval model — well understood and sufficient for MVP
- Exact match weight (3×) ensures that pages with exact keyword matches always outrank pages with only prefix matches

**Alternatives Rejected:**
| Alternative | Why Rejected |
|-------------|-------------|
| Whoosh | Explicitly prohibited |
| Xapian | C extension; not language-native |
| BM25 | More complex to implement; TF-IDF sufficient for MVP |
| Pure Python dict (in-memory) | Not persistent; lost on restart |

**Known Limitation:** Full-text search via `LIKE` is O(n) for wildcard patterns. The covering index on `(word, url, tf)` mitigates this for prefix patterns. For very large indexes (>10M words), performance may degrade — acceptable for single-machine scope.

---

### Decision 3.7: API Framework — FastAPI + uvicorn

**Decision:** Use FastAPI as the HTTP framework and uvicorn as the ASGI server.

**Reasoning:**
- FastAPI is async-native — integrates cleanly with the asyncio crawler
- Automatic request validation via Pydantic models
- Built-in OpenAPI/Swagger documentation at `/docs`
- `StreamingResponse` with async generators is the cleanest SSE implementation available in Python
- uvicorn is lightweight and production-quality for single-machine use
- Both are lightweight external dependencies that do not violate "language-native logic" constraint (they are transport/framework, not business logic)

**SSE Implementation Pattern:**
```python
async def event_generator():
    while True:
        await asyncio.sleep(2)
        stats = crawler.stats.snapshot()
        yield f"data: {json.dumps(stats)}\n\n"

@app.get("/api/events")
async def events():
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**Note on CORS:** `allow_origins=["*"]` is set because the system is localhost-only. This is safe in that context.

---

### Decision 3.8: UI — Vanilla JS Single-Page Application

**Decision:** Implement the UI as a single `static/index.html` file with embedded CSS and JavaScript. No external CDN dependencies. Dark mode by default.

**Reasoning:**
- No build step required (no npm, webpack, etc.)
- Zero external network requests from the UI — works fully offline
- Vanilla JS `EventSource` API handles SSE natively in all modern browsers
- Single file served by FastAPI's `StaticFiles` — simple deployment
- Tabs implemented with CSS display: none/block toggling — no router needed
- `localStorage` used for dark/light mode preference persistence

**Technology Choices:**
- CSS Custom Properties (variables) for theming — one class on `<body>` switches all colors
- `fetch()` for search API calls
- `EventSource` for SSE stats stream
- `setInterval` for polling recent pages (2s)

**Alternatives Rejected:**
| Alternative | Why Rejected |
|-------------|-------------|
| React/Vue/Svelte | Requires build toolchain; overkill for 3-tab SPA |
| External CSS framework (Tailwind, Bootstrap) | CDN dependency; adds complexity |
| Server-side rendering (Jinja2) | Unnecessary; API + static SPA is cleaner separation |

---

### Decision 3.9: Resume Strategy — Queue Snapshot + Visited Dedup

**Decision:** Two-layer resume mechanism: (1) permanent visited URL deduplication via SQLite `visited` table, and (2) session-level queue snapshot stored as JSON in `crawl_sessions.queue_snapshot`.

**Layer 1 — Visited Dedup:**
- `INSERT OR IGNORE INTO visited(url)` — O(1) per check, O(log n) for index lookup
- Permanent across all sessions — even after a reset the visited table preserves dedup state for the same DB
- This alone prevents re-indexing already-seen pages

**Layer 2 — Queue Snapshot:**
- On `stop()` or SIGINT: `json.dumps(list(queue._queue))` serialized to DB
- On next start for same origin: `find_incomplete_session(origin)` restores the frontier
- Handles crash (session stays in "running" status — treated as interrupted)

**Why Both Layers?**
- Layer 1 alone would restart the crawl from depth 0, re-discovering (but not re-indexing) every visited URL before reaching the frontier — wasteful for deep crawls
- Layer 2 alone would skip starting from the frontier but not prevent re-visiting pages if the visited table was cleared
- Together: maximum efficiency on resume

**Limitation:** Queue snapshot is best-effort. If the process receives SIGKILL (immediate kill), the Python signal handler may not run. WAL ensures DB integrity, but the queue snapshot may be stale or missing. In this case, the crawl resumes from depth 0 with visited dedup protecting against re-indexing.

---

### Decision 3.10: Stats Tracking — Thread-Safe CrawlerStats

**Decision:** Use a `CrawlerStats` class with `threading.Lock` protecting integer counters. The `snapshot()` method returns a consistent dict.

**Reasoning:**
- Counter increments happen in ThreadPoolExecutor threads (the fetchers)
- The asyncio event loop reads stats for SSE events and `/api/status`
- A simple lock around a dataclass of integers is sufficient for this cardinality of access
- Avoids `queue.Queue` or `asyncio.Queue` for stats — overkill for simple counters

**Stats tracked:**
- `pages_indexed`, `urls_queued`, `urls_failed`, `urls_skipped`, `urls_visited`
- `start_time` (for elapsed calculation)
- Rolling window for `rate_per_sec` (pages indexed in last 10 seconds)

---

## 4. Interface Contracts Produced

The Architect Agent defines the following contracts that all implementation agents must honor:

### 4.1 Module Boundaries

```
crawler/engine.py    ← implements AsyncCrawler (uses InvertedIndex, VisitedDB, SessionDB, FailedURLDB)
crawler/parser.py    ← implements LinkParser, TextParser, tokenize (no storage dependencies)
storage/database.py  ← implements ThreadLocalDB, VisitedDB, SessionDB, FailedURLDB, init_db
storage/index.py     ← implements InvertedIndex (extends ThreadLocalDB)
api/server.py        ← implements FastAPI app (uses AsyncCrawler, InvertedIndex, SessionDB)
static/index.html    ← consumes only HTTP API endpoints
main.py              ← CLI entry point (uses AsyncCrawler, InvertedIndex, SessionDB via HTTP or direct)
```

**Critical rule:** No circular imports. The dependency graph is:
```
api/server.py
  └── crawler/engine.py
        ├── crawler/parser.py  (no upstream deps)
        ├── storage/index.py
        │     └── storage/database.py  (no upstream deps)
        ├── storage/database.py
        └── storage/index.py
```

### 4.2 AsyncCrawler Public API
```python
class AsyncCrawler:
    def __init__(
        self,
        index: InvertedIndex,
        max_workers: int = 10,
        max_queue: int = 500,
        rate: float = 10.0,
        timeout: float = 10.0,
        same_domain: bool = True,
        db_path: str = "data/mini_google.db"
    ): ...

    def start(self, origin: str, max_depth: int) -> None:
        """Non-blocking. Schedules crawl as asyncio task. Raises ValueError for invalid URL."""

    def stop(self) -> None:
        """Signals stop, saves queue snapshot, updates session status."""

    def pause(self) -> None:
        """Pauses dispatch loop. Queue preserved."""

    def resume(self) -> None:
        """Resumes dispatch from paused state."""

    def is_active(self) -> bool:
        """True if crawl is running (not stopped, not paused)."""

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until crawl completes or timeout. Returns True if completed."""

    @property
    def stats(self) -> CrawlerStats:
        """Access to stats accumulator. Call .snapshot() for dict."""
```

### 4.3 InvertedIndex Public API
```python
class InvertedIndex:
    def add_page(
        self,
        url: str,
        origin: str,
        depth: int,
        word_counts: Dict[str, int],
        session_id: int
    ) -> bool: ...
    # Returns True if page was new, False if already indexed

    def search(self, query: str) -> List[Tuple[str, str, int]]:
        # Returns [(url, origin_url, depth), ...] sorted by TF-IDF score desc

    def search_scored(self, query: str) -> List[Tuple[str, str, int, float]]:
        # Returns [(url, origin_url, depth, score), ...] sorted desc

    def page_count(self) -> int: ...
    def word_count(self) -> int: ...
    def recent_pages(self, limit: int = 20) -> List[dict]: ...
```

### 4.4 Storage Database Classes
```python
class VisitedDB:
    def mark_visited(self, url: str) -> bool: ...  # True=new
    def count(self) -> int: ...
    def clear(self) -> None: ...

class SessionDB:
    def create_session(self, origin: str, depth: int, same_domain: bool) -> int: ...
    def finish_session(
        self, session_id: int, pages: int, visited: int,
        failed: int, skipped: int, status: str
    ) -> None: ...
    def save_queue_snapshot(self, session_id: int, snapshot: List[Tuple[str, int]]) -> None: ...
    def load_queue_snapshot(self, session_id: int) -> List[Tuple[str, int]]: ...
    def find_incomplete_session(self, origin: str) -> Optional[dict]: ...
    def list_sessions(self, limit: int = 50) -> List[dict]: ...

class FailedURLDB:
    def record_failure(
        self, url: str, origin: str, depth: int,
        error: str, http_status: Optional[int]
    ) -> None: ...
    def list_failures(self, session_id: int) -> List[dict]: ...
```

---

## 5. Trade-offs Considered

### 5.1 TF-IDF vs BM25

**TF-IDF chosen.** BM25 is generally superior for modern information retrieval (handles document length normalization better, has a saturation parameter k1 for term frequency). However:
- TF-IDF requires 40% less code to implement correctly
- For the MVP and single-machine scope, TF-IDF gives adequate ranking quality
- BM25 can be substituted as a future enhancement without changing the schema

### 5.2 asyncio.Queue drop-on-full vs await-on-full

**Drop-on-full chosen.** The alternative (awaiting when queue is full) would cause the event loop to block dispatcher coroutines, potentially reducing crawl throughput if the queue stays full. Dropping and logging as "skipped" provides cleaner back-pressure — the crawler proceeds at its sustainable rate.

The skipped URL count is visible in stats, so users can see if back-pressure is occurring and increase `max_queue` if needed.

### 5.3 Storing TF per-row vs recomputing at search time

**Store TF per-row chosen.** The alternative (storing only raw counts and computing TF at query time) would require fetching `total_words` per page at search time — adding a JOIN or subquery. Pre-computing TF at index time makes search faster at the cost of slightly more storage. This is the correct trade-off for a read-heavy search use case.

### 5.4 Single SQLite file vs separate files per concern

**Single file chosen.** Separate files for visited, index, sessions would require managing multiple connections and potentially ATTACH operations. A single file with WAL mode and thread-local connections handles all access patterns cleanly. SQLite performs well with large single files.

### 5.5 SSE vs WebSockets for real-time updates

**SSE chosen.** WebSockets are bidirectional — this use case is server-to-client only (stats stream). SSE is simpler to implement (plain `StreamingResponse`), natively reconnects, and is supported by `EventSource` in all modern browsers without a library.

### 5.6 Rate at which SSE page_indexed events are emitted

The crawler may index pages faster than 5/sec. Emitting one SSE event per page would flood the SSE stream with hundreds of events per second. Decision: emit `page_indexed` events at most 5/sec (debounced), while `stats` events fire every 2 seconds regardless.

---

## 6. Handoff to Implementation Agents

### Agent 02 — Crawler Agent
**Files to implement:** `crawler/engine.py`, `crawler/parser.py`  
**Key contracts:**
- `AsyncCrawler` class with exact interface from Section 4.2
- Must use `asyncio.Queue(maxsize=max_queue)` for BFS frontier
- Must use `ThreadPoolExecutor` + `run_in_executor` for all urllib calls
- Token bucket rate limiter must be asyncio-native (no `time.sleep`)
- `_fetch_and_index` must call `VisitedDB.mark_visited()` before enqueuing new links
- `stop()` must serialize queue to `SessionDB.save_queue_snapshot()`
- `LinkParser` must resolve relative URLs against base using `urllib.parse.urljoin`
- `TextParser` must exclude `<script>`, `<style>`, `<head>` content
- `tokenize()` returns `Dict[str, int]` — lowercase alpha tokens ≥ 2 chars

**Reference:** PRD Sections 6.1, 9.1, 9.2, 8.2

---

### Agent 03 — Storage Agent
**Files to implement:** `storage/database.py`, `storage/index.py`  
**Key contracts:**
- `init_db(db_path)` must run on startup; idempotent (CREATE TABLE IF NOT EXISTS)
- `ThreadLocalDB` must set `PRAGMA journal_mode=WAL` on every new connection
- `VisitedDB.mark_visited()` must use `INSERT OR IGNORE` and return True/False
- `InvertedIndex.add_page()` must use `executemany` for word_index inserts
- TF = `count / total_words` (float)
- IDF computed at query time: `log(N / max(df, 1))`
- Exact match weight: 3.0, prefix match weight: 1.0
- All SQL must use parameterized queries (no f-string interpolation)
- `queue_snapshot` stored as `json.dumps(list_of_tuples)` in `crawl_sessions`

**Reference:** PRD Sections 6.2, 9.3, 9.4, 10.1, 10.2

---

### Agent 04 — API Agent
**Files to implement:** `api/server.py`  
**Key contracts:**
- Endpoints exactly as specified in PRD Section 11
- Single global `AsyncCrawler` instance (created at startup)
- `POST /api/index` returns 409 if `crawler.is_active()` is True
- `GET /api/events` uses `StreamingResponse` with async generator
- SSE format: `data: {json}\n\n` (two newlines)
- `DELETE /api/reset` returns 409 if crawl active
- Mount `StaticFiles("static/", name="static")` at `/`
- All endpoints must handle exceptions and return proper HTTP error codes

**Reference:** PRD Sections 6.3, 11, 12

---

### Agent 05 — UI Agent
**Files to implement:** `static/index.html`  
**Key contracts:**
- Single self-contained HTML file (no external CDN)
- Three tabs: Index, Search, History
- SSE via `new EventSource("/api/events")`
- Dark mode default, localStorage-persisted toggle
- Search calls `GET /api/search?q=...&limit=50`
- History calls `GET /api/sessions`
- Recent pages calls `GET /api/pages/recent`
- Color scheme from PRD Section 12.5
- No JavaScript frameworks

**Reference:** PRD Sections 6.4, 12

---

### Agent 06 — QA Agent
**Files to implement:** Test files, `requirements.txt`, `readme.md`  
**Key areas to verify:**
- Unit tests for `tokenize()`, `InvertedIndex.search()`, `VisitedDB.mark_visited()`
- Integration test: crawl a local HTTP server, verify pages are indexed
- Integration test: search returns results ranked by TF-IDF
- Integration test: stop and resume restores correct queue state
- API tests: all endpoints return correct status codes
- Performance test: search on 100K-page index under 500ms P99
- Verify no SQL injection: parameterized queries throughout
- Verify SSE events arrive within 3 seconds of page indexing

**Reference:** PRD Section 16

---

## 7. Architecture Validation Checklist

Before any agent ships code, the Architect Agent certifies these invariants:

- [x] No blocking I/O on the asyncio event loop (all urllib in ThreadPoolExecutor)
- [x] No shared mutable state across threads without synchronization
- [x] SQLite WAL mode enables concurrent reads during writes
- [x] Thread-local connections eliminate SQLite thread-safety issues
- [x] asyncio.Queue bounded — prevents unbounded memory growth
- [x] Token bucket rate limiter is asyncio-native
- [x] All SQL queries parameterized
- [x] Queue snapshot saved on stop/interrupt for resumability
- [x] No circular module dependencies
- [x] Single external-dependency constraint honored (fastapi, uvicorn, aiofiles only)
- [x] UI has zero external network dependencies

---

## 8. Open Questions & Future Work

The following items are out of scope for v1.0 but should be tracked:

1. **robots.txt compliance** — Currently optional (default off). Should be made default-on in a future version per ethical crawling standards.
2. **BM25 upgrade** — Replace TF-IDF with BM25 for better ranking. Schema is compatible; only `storage/index.py` changes.
3. **Gzip decompression** — urllib handles this, but explicit `Accept-Encoding: gzip` header should be set for efficiency.
4. **Encoding detection** — Currently assumes UTF-8. `chardet` or charset from Content-Type header should be used.
5. **Connection pooling** — urllib opens a new TCP connection per request. An `http.client.HTTPConnection` pool per thread would improve throughput.
6. **Phrase search** — Current inverted index doesn't support phrase queries. Future: store positional index.
7. **Stemming/lemmatization** — Current tokenizer is exact. Adding Porter stemmer (stdlib `nltk` is not standard) would require a pure-Python implementation.
8. **Multi-domain support** — Currently one crawl at a time. Future: per-domain crawl tasks running concurrently.
9. **Export functionality** — Export index as CSV/JSON for offline analysis.
10. **Rate limit per domain** — Current rate limiter is global. Per-domain buckets would be more polite.

---

*End of Architect Agent Documentation*  
*Agent 01 — 2026-04-19 — Handoff complete*
