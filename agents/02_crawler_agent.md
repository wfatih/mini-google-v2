# Agent 02: Crawler Agent
## mini-google-v2 Crawler Implementation

**Agent ID:** `02_crawler`  
**Date:** 2026-04-19  
**Status:** Complete — Output handed off to Agents 03–06  
**Output Files:**
- `crawler/__init__.py`
- `crawler/parser.py`
- `crawler/engine.py`
- `agents/02_crawler_agent.md` (this file)

---

## 1. Agent Role & Responsibilities

The Crawler Agent is the **second agent in the multi-agent pipeline**. Its responsibility is to implement the complete async web crawler based on the architecture and interface contracts defined by the Architect Agent (Agent 01). Specifically, this agent:

1. **Implements `crawler/parser.py`** — HTML parsing utilities (`LinkParser`, `TextParser`, `tokenize`) with zero storage dependencies.
2. **Implements `crawler/engine.py`** — The full `AsyncCrawler` class including rate limiting, pause/resume, BFS traversal, URL filtering, and queue snapshot for resume support.
3. **Honors all interface contracts** defined in `agents/01_architect_agent.md` Section 4 and the PRD.
4. **Produces no test stubs** — all code is complete and runnable pending the Storage Agent's `storage/database.py` and `storage/index.py`.

---

## 2. Inputs Consumed

| Input | Source |
|-------|--------|
| Architecture document | `agents/01_architect_agent.md` |
| Product PRD | `product_prd.md` |
| Storage interface contract | PRD Section 9.3–9.4, Arch Section 4.3–4.4 |
| `InvertedIndex` public API | Arch Section 4.3 |
| `VisitedDB`, `SessionDB`, `FailedURLDB` APIs | Arch Section 4.4 |

---

## 3. Key Implementation Decisions

### Decision C1: `_worker` Task Structure vs. Async Pool

**Decision:** Each of the `max_workers` asyncio tasks runs an infinite `_worker()` coroutine loop that pulls from `asyncio.Queue`, checks dedup, and calls `_process()`. The executor is used only for the blocking HTTP fetch.

**Rationale:**
- Keeps URL deduplication (`mark_visited`) serialised on the event loop thread. Since coroutines don't interleave at non-`await` points, two workers cannot both see `mark_visited()` return `True` for the same URL simultaneously. This is a correctness guarantee, not just a performance choice.
- Parsing, link extraction, and `add_page()` are fast enough to run on the event loop thread without measurable latency impact at 10 req/s.
- If write throughput becomes a bottleneck, `add_page()` can be moved into the executor with a one-line change.

**Alternative Rejected:** Running each URL as a new `loop.run_in_executor()` call for the entire fetch+parse+index cycle. This would require a different deduplication strategy (e.g., a thread-safe set or per-URL DB check inside the thread) and would lose the elegant asyncio-native ordering guarantee.

---

### Decision C2: `asyncio.wait` for Stop vs. Direct `queue.join()`

**Decision:** `_crawl()` races `queue.join()` against a stop-poll coroutine using `asyncio.wait(return_when=FIRST_COMPLETED)` rather than directly `await`-ing `queue.join()`.

**Rationale:**
- `asyncio.Queue.join()` has no built-in cancellation or timeout. Calling `stop()` from an external thread sets `_stop_event` but cannot directly abort the `join()` wait.
- Wrapping both waits as asyncio tasks lets the event loop cleanly cancel the join when stop is requested, without any `threading.Event.wait()` busy loops on the asyncio thread.
- The pattern is idiomatic asyncio and avoids hacks like accessing `_queue._unfinished_tasks` (a private attribute).

**Stop flow:**
1. External thread calls `stop()` → sets `_stop_event`, sets `_pause_event` (unblocks paused workers).
2. `_poll_stop()` coroutine wakes within 50 ms and returns.
3. `asyncio.wait` returns with `stop_task` in `done`.
4. `join_task` and all workers are cancelled.
5. Workers receive `CancelledError` at their next `await` point; they call `task_done()` and exit.
6. `_finalize()` drains remaining queue items into the JSON snapshot and calls `SessionDB.finish_session()`.
7. Event loop exits; `_done_event` is set.

---

### Decision C3: `task_done()` Invariant in `_worker`

**Decision:** `task_done()` is called exactly once per `queue.get()` regardless of which code path exits `_worker`'s try block:
- Normal exit: called in `finally` block.
- `asyncio.CancelledError`: called explicitly in the `except` block before `return`; the `finally` block is still reached but its `task_done()` call is guarded by a `ValueError` catch to prevent double-calls.
- Other exceptions: swallowed by `except Exception`, then `finally` fires normally.

**Why this matters:** `asyncio.Queue.join()` tracks unfinished tasks via `task_done()` calls. A missing call means `join()` hangs forever. A double call raises `ValueError`. Both are silent bugs in production, hence the explicit invariant.

---

### Decision C4: `mark_visited()` in Executor Thread

**Decision:** `mark_visited()` is called via `loop.run_in_executor(None, ...)` so the SQLite INSERT runs in a thread-pool thread rather than blocking the event loop.

**Rationale:**
- `ThreadLocalDB` uses thread-local SQLite connections. Calling it from the event loop thread would use the event loop's connection, mixing async and sync paths through the same connection — which is fine with WAL, but can cause serialisation delays if the write lock is held.
- Running in the default executor (the event loop's built-in thread pool, `None` executor) keeps the event loop responsive.
- Ordering guarantee: `await run_in_executor` serialises the call per worker coroutine. Two coroutines that interleave at this `await` point still hit the DB-level `INSERT OR IGNORE` atomicity, so deduplication remains correct even if both dispatch simultaneously.

---

### Decision C5: URL Filtering with `_should_skip_url()`

**Decision:** A module-level function checks extensions, Wikipedia namespace prefixes, and edit/history query parameters before any rate-limit token is consumed.

**Rationale:**
- Filtering before rate-limit token acquisition means the token bucket is not drained on URLs that would immediately 404 or return binary content.
- Extension matching uses `endswith()` on the lowercased path (not the full URL) to avoid false positives from query string values that happen to contain `.pdf`.
- The path-prefix check handles MediaWiki-style URLs; these are matched case-insensitively via `.lower()`.

---

### Decision C6: Snapshot Persistence on Stop

**Decision:** On stop (either external or natural completion), `_finalize()` drains the asyncio Queue synchronously (since it's called from the event loop thread), serialises remaining items as `List[Tuple[str, int]]`, and calls `SessionDB.save_queue_snapshot()`.

**Rationale:**
- Draining from the event loop thread is safe: the queue's `get_nowait()` is event-loop-native and non-blocking.
- Items being actively processed by workers at the time of cancellation are **not** included in the snapshot (they were already `get()`-ted and dequeued). On resume, `mark_visited()` prevents re-indexing any pages that completed before the stop.
- This matches the "best-effort snapshot" design decision in the Architect Agent's Decision 3.9.

---

### Decision C7: `_RateLimiter` Uses `threading.Lock` (Not asyncio)

**Decision:** The token bucket uses `threading.Lock` rather than an `asyncio.Lock`.

**Rationale:**
- `try_acquire()` is called from asyncio coroutines (via a spin-wait loop with `await asyncio.sleep(0.05)`), but `wait_and_acquire()` is available for synchronous executor thread use if needed in future.
- An `asyncio.Lock` cannot be used from executor threads; a `threading.Lock` works from both contexts.
- The lock is held for microseconds (a few arithmetic operations), so contention is negligible.

---

## 4. File Structure Produced

```
crawler/
├── __init__.py          # Exports AsyncCrawler, CrawlerStats
├── parser.py            # LinkParser, TextParser, tokenize, STOP_WORDS
└── engine.py            # _RateLimiter, CrawlerStats, AsyncCrawler
```

### `crawler/parser.py` (~130 lines)

| Symbol | Type | Description |
|--------|------|-------------|
| `STOP_WORDS` | `frozenset` | ~75 common English stop words |
| `tokenize(text)` | `(str) → List[str]` | Lowercase alpha tokens ≥ 3 chars, stop-word filtered |
| `LinkParser` | `HTMLParser` subclass | Extracts, resolves, deduplicates `<a href>` links |
| `TextParser` | `HTMLParser` subclass | Extracts visible text, skips opaque tags, exposes `word_counts()` |

### `crawler/engine.py` (~430 lines)

| Symbol | Type | Description |
|--------|------|-------------|
| `_SKIP_EXTENSIONS` | `frozenset` | Binary/non-HTML file extensions |
| `_SKIP_PATH_PREFIXES` | `tuple` | MediaWiki namespace path prefixes |
| `_SKIP_QUERY_PATTERNS` | `tuple` | Edit/history/diff URL patterns |
| `_should_skip_url(url)` | `(str) → bool` | Applies all URL filters |
| `_RateLimiter` | class | Thread-safe token-bucket rate limiter |
| `CrawlerStats` | class | Thread-safe stats accumulator with `snapshot()` |
| `AsyncCrawler` | class | Complete BFS crawler; full public API |

---

## 5. Public Interface Contract (as implemented)

### `AsyncCrawler`

```python
AsyncCrawler(
    index: InvertedIndex,
    max_workers: int = 10,
    max_queue: int = 500,
    rate: float = 10.0,
    timeout: float = 10.0,
    same_domain: bool = True,
    db_path: str = DB_PATH,
)

.start(origin: str, max_depth: int) -> None   # non-blocking
.stop() -> None                                # non-blocking; triggers snapshot save
.pause() -> None
.resume() -> None
.is_active() -> bool
.wait(timeout: Optional[float] = None) -> bool
.stats -> CrawlerStats
```

### `CrawlerStats.snapshot()` keys

```
pages_indexed, urls_queued, urls_failed, urls_skipped, urls_dropped,
urls_visited, throttled, rate_per_sec, elapsed_sec, active, paused,
origin, max_depth
```

---

## 6. Dependencies on Other Agents

| Dependency | Agent | Interface |
|------------|-------|-----------|
| `storage.database.VisitedDB` | Agent 03 (Storage) | `mark_visited(url) → bool` |
| `storage.database.SessionDB` | Agent 03 (Storage) | `create_session`, `finish_session`, `save_queue_snapshot`, `load_queue_snapshot`, `find_incomplete_session` |
| `storage.database.FailedURLDB` | Agent 03 (Storage) | `record_failure(url, origin, depth, error, http_status)` |
| `storage.database.DB_PATH` | Agent 03 (Storage) | Default DB path constant |
| `storage.index.InvertedIndex` | Agent 03 (Storage) | `add_page(url, origin, depth, word_counts, session_id) → bool` |

The crawler will raise `ImportError` if `storage/` is not present; all storage calls are deferred until `start()` is called, so importing `crawler` is safe without `storage` present at module load time... **actually, the top-level import `from storage.database import ...` means `storage` must exist at import time**. Agent 03 must be completed before the full system can be imported.

---

## 7. Edge Cases Handled

| Edge Case | Handling |
|-----------|---------|
| Non-HTML Content-Type | `_fetch_sync` returns `(None, "skip")` |
| Unknown charset in Content-Type | Falls back to UTF-8 with `errors="replace"` |
| Malformed HTML | `html.parser` is lenient; bad tags are silently skipped |
| Relative URLs | `urllib.parse.urljoin(base, href)` resolves correctly |
| Protocol-relative URLs (`//example.com/`) | `urljoin` resolves against base URL scheme |
| Fragment-only links (`#section`) | Resolved then stripped of fragment |
| Queue overflow | `asyncio.QueueFull` caught, URL counted as `urls_dropped` |
| HTTP errors (4xx/5xx) | Recorded in `FailedURLDB`, not fatal |
| Connection timeout | Caught by `TimeoutError` / `URLError`, recorded as failure |
| SSL errors | Caught by `URLError`, recorded as failure |
| Worker CancelledError | Calls `task_done()` before exiting to prevent join() hang |
| Stop during pause | `stop()` sets `_pause_event` to unblock workers before cancellation |
| Resume with empty snapshot | Falls back to seeding from `origin` at depth 0 |
| Double `task_done()` | Guarded by `except ValueError` in finally block |

---

## 8. What Was NOT Implemented

The following are out of scope and noted for Agent 06 (QA) or future iterations:

1. **robots.txt compliance** — not implemented; configurable option referenced in PRD Section 7.5 NFR-SEC4.
2. **Per-domain rate limiting** — single global token bucket. Future: per-domain buckets.
3. **Gzip/deflate Accept-Encoding** — urllib handles decompression automatically when the server sends it; explicit `Accept-Encoding: gzip` header is not added.
4. **Connection reuse** — each `urlopen()` creates a new TCP connection. Acceptable at 10 req/s.
5. **429 / 503 backoff** — HTTP rate-limit responses are currently logged as failures. Future: exponential backoff with `Retry-After` header parsing.
6. **Content hash dedup** — identical page content at different URLs is indexed multiple times. Future: hash the HTML and skip if seen.

---

*End of Crawler Agent Documentation*  
*Agent 02 — 2026-04-19 — Handoff complete*
