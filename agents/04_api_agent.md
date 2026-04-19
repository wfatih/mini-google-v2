# Agent 04: API Agent
## mini-google-v2 FastAPI REST Server Documentation

**Agent ID:** `04_api`  
**Date:** 2026-04-19  
**Status:** Complete — Output handed off to Agent 05 (UI) and Agent 06 (QA)  
**Output Files:**
- `api/__init__.py` — package exports
- `api/server.py` — complete FastAPI implementation (~370 lines)
- `agents/04_api_agent.md` (this file)
- `logs/agent_log.jsonl` (appended)

---

## 1. Agent Role & Responsibilities

The API Agent is the **fourth agent in the multi-agent pipeline**. It implements the FastAPI REST server that bridges the browser-based UI, the CLI, and the underlying crawler/storage layers.

Specifically, the API Agent:

1. **Implements all HTTP endpoints** as specified in PRD Section 11
2. **Manages application state** via a module-level `AppState` singleton
3. **Bridges two asyncio event loops** — the uvicorn loop (main thread) and the crawler's background loop — using only thread-safe primitives
4. **Exposes Server-Sent Events** for real-time crawler status streaming
5. **Enforces conflict rules** — 409 on `POST /api/index` if crawl active; 409 on `DELETE /api/reset` if crawl active
6. **Serves the SPA** — `GET /` returns `static/index.html`; `/static/*` serves assets

---

## 2. Inputs Consumed

| Source | What was read |
|--------|--------------|
| `agents/01_architect_agent.md` | Section 4.2–4.4 interface contracts, Section 3.7 API framework decisions |
| `product_prd.md` | Section 11 (API Specification), Section 9.5 (server component spec), Section 10.3 (API data shapes) |
| `crawler/engine.py` | `AsyncCrawler` public API: `start()`, `stop()`, `pause()`, `resume()`, `is_active()`, `stats.snapshot()` |
| `crawler/__init__.py` | Import path confirmation |
| `storage/database.py` | `SessionDB`, `FailedURLDB`, `QueueStateDB`, `init_db`, `DB_PATH` |
| `storage/index.py` | `InvertedIndex`: `search()`, `search_scored()`, `page_count()`, `word_count()`, `recent_pages()`, `pages_for_session()`, `get_stats()`, `reset()` |
| `storage/__init__.py` | Export list and import paths |

---

## 3. Key Design Decisions

### Decision 3.1: Two-Event-Loop Architecture

**Problem:** `AsyncCrawler.start()` spawns a new daemon thread that creates its own `asyncio` event loop (via `asyncio.new_event_loop()`). The FastAPI/uvicorn server runs on a separate event loop in the main thread. The two loops must **never share asyncio primitives**.

**Solution implemented:**
```
FastAPI handler (main loop)
    │
    │  thread-safe call: crawler.stop()       → sets threading.Event
    │  thread-safe call: crawler.pause()      → clears threading.Event
    │  thread-safe call: crawler.is_active()  → reads bool via Lock
    │  thread-safe call: crawler.stats.snapshot() → Lock-protected dict copy
    ▼
AsyncCrawler background thread (its own event loop)
    └── polling threading.Event every 50ms → reacts to stop/pause signals
```

**Rejected approaches:**
- `asyncio.run_coroutine_threadsafe()` — would require accessing the crawler's loop from the API thread; the crawler doesn't expose its loop publicly
- Shared `asyncio.Queue` for communication — cross-loop queues are not safe; different loops have different thread-affinity

**Evidence in code:**
```python
# In start_index(): synchronous call, no await
crawler.start(req.url, req.depth)          # spawns background thread

# In stop_index(): synchronous call, no await  
state.crawler.stop()                        # sets threading.Event

# In SSE generator: reads thread-safe stats, no crawler loop interaction
snap = state.crawler.stats.snapshot()      # acquires threading.Lock
```

---

### Decision 3.2: AppState as Module-Level Singleton

**Decision:** Use a single `AppState` instance at module scope (`state = AppState()`), populated during the FastAPI `startup` event.

**Reasoning:**
- FastAPI handles one process with one shared memory space — module globals are safe for single-process deployment
- Avoids dependency injection complexity for a small server
- The `state` object is the single source of truth for: which crawler is active, which DB objects exist, and what the current DB path is

**Alternatives rejected:**
| Alternative | Why rejected |
|-------------|-------------|
| Passing state via `request.app.state` | More verbose; no benefit in a single-process server |
| Database-as-truth only (no in-process state) | Cannot track the in-memory `AsyncCrawler` object via DB |
| Multiple `AppState` instances | Would create multiple DB connection pools; wasteful |

---

### Decision 3.3: SSE Generator Error Handling

**Decision:** The SSE async generator catches `asyncio.CancelledError` (client disconnect) silently and exits. All other exceptions are also silently swallowed to prevent the generator from raising into the StreamingResponse machinery.

**Reasoning:**
- When a client closes the SSE connection, uvicorn cancels the generator coroutine by raising `CancelledError`
- Not catching `CancelledError` would propagate it up and log a spurious error in uvicorn's output
- The generator loop is stateless (no resources to release) so a silent exit is correct

**SSE data format:**
```
data: {"event": "stats", "active": true, "pages_indexed": 142, "timestamp": 1713523200.0, ...}\n\n
```
- Two newlines are mandatory per the SSE specification (end-of-event sentinel)
- `json.dumps(payload, default=str)` handles any non-JSON-serialisable values (e.g., float timestamps) gracefully

---

### Decision 3.4: IndexRequest — Flat vs. Nested Options

**Decision:** `IndexRequest` accepts both a flat format (all fields at root level) and the PRD's nested `options` object. The `effective_options()` method resolves the merger, with the nested `options` block taking precedence when present.

**Reasoning:**
- The PRD specifies a nested `options` block (Section 10.3)
- The task specification uses a flat format: `{"url": ..., "depth": 2, "max_workers": 10, ...}`
- Supporting both makes the API callable from both UI (flat) and human/scripts (PRD format)
- Pydantic's `Optional[IndexOptions] = None` makes the nested block optional with zero boilerplate

---

### Decision 3.5: Session ID in POST /api/index Response

**Problem:** `AsyncCrawler.start()` is non-blocking — it spawns a thread and returns immediately. The `session_id` is created inside the background thread's coroutine (`_crawl()`), so it's not immediately available on the calling thread.

**Solution:** After calling `crawler.start()`, the endpoint `await asyncio.sleep(0.15)` to give the background thread time to create the session row in SQLite. It then calls `session_db.get_active_session()` (which queries for the most recent `status='running'` row) to retrieve the newly-created session ID.

**Trade-off:** The 150ms delay slightly increases the time before the response is sent, but this is imperceptible to the user and avoids a race condition. An alternative (exposing a threading.Event + session_id on the crawler) would couple the crawler layer to the API layer.

---

### Decision 3.6: Static File Mounting Order

**Decision:** Static file mount (`app.mount("/static", StaticFiles(...))`) is added **after** all `@app.get`/`@app.post` route decorators inside `create_app()`.

**Reasoning:**
- FastAPI matches routes in registration order. Routes registered first take precedence.
- Mounting at `/static` only intercepts paths starting with `/static/` — it cannot capture `/api/...` routes regardless of order
- However, registering API routes first is the correct pattern to avoid any future confusion if mount paths overlap
- The `GET /` route (serves `index.html`) is registered as a named route before the mount, ensuring it's always reachable

---

### Decision 3.7: Reset Scope

**Decision:** `DELETE /api/reset` calls `index.reset()` which deletes rows from `pages`, `word_index`, and `visited`. It does **not** delete `crawl_sessions` or `failed_urls`.

**Reasoning:** As documented in `storage/index.py`, session history and failure logs are preserved for auditing. The reset only clears the searchable index and deduplication set. This matches the behaviour expected by the UI's History tab (sessions remain visible after a reset).

---

### Decision 3.8: Pause as Toggle vs. Separate Endpoints

**Decision:** `POST /api/pause` is a **toggle** (pause if running, resume if paused). `POST /api/resume` is also provided as a separate explicit endpoint for clients that prefer non-toggle semantics.

**Reasoning:**
- The task specification explicitly calls for a toggle: "Pause/resume toggle — Return `{"status": "paused"}` or `{"status": "resumed"}`"
- The PRD specifies separate `/api/pause` and `/api/resume` endpoints
- Both are implemented to satisfy both specs without contradiction

---

## 4. Endpoint Reference

### POST /api/index

| Field | Details |
|-------|---------|
| Request body | `IndexRequest` (JSON) |
| Success | 200 `{"status": "started", "session_id": N, "origin": url, "depth": N}` |
| Conflict | 409 `{"error": "Crawl already in progress", "status": {...}}` |
| Invalid URL | 422 FastAPI validation error |
| Not ready | 503 `{"detail": "Storage not initialised"}` |

Starts `AsyncCrawler` in background thread. Returns immediately.

---

### POST /api/stop

| Field | Details |
|-------|---------|
| Success | 200 `{"status": "stopped", "message": "Crawl stop signal sent"}` |
| Not active | 400 `{"detail": "No active crawl to stop"}` |

Sends stop signal via `threading.Event`. Queue snapshot is persisted by the crawler's `_finalize()` before the background thread exits.

---

### POST /api/pause

| Field | Details |
|-------|---------|
| Success (paused) | 200 `{"status": "paused"}` |
| Success (resumed) | 200 `{"status": "resumed"}` |
| Not active | 400 `{"detail": "No active crawl"}` |

Toggle: reads `crawler.stats.snapshot()["paused"]` to decide direction.

---

### POST /api/resume

| Field | Details |
|-------|---------|
| Success | 200 `{"status": "resumed", "message": "Crawl resumed"}` |
| Not paused | 400 `{"detail": "Crawl is not currently paused"}` |

Explicit resume (non-toggle complement to `/api/pause`).

---

### GET /api/search

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | required | Search query |
| `limit` | int | 20 | Max results (1–500) |
| `scored` | bool | false | Include TF-IDF scores |

Returns:
```json
{
  "query": "python asyncio",
  "count": 15,
  "results": [
    {"url": "...", "origin": "...", "depth": 2, "score": 0.4821, "rank": 1}
  ],
  "query_time_ms": 47.3
}
```

`score` is `null` when `scored=false`. Works during active indexing (SQLite WAL).

---

### GET /api/status

Always returns 200. Returns the full `AppState.get_status()` dict:
```json
{
  "active": true,
  "paused": false,
  "origin": "https://example.com",
  "max_depth": 3,
  "pages_indexed": 1234,
  "urls_queued": 87,
  "urls_failed": 3,
  "urls_skipped": 22,
  "urls_dropped": 0,
  "urls_visited": 1344,
  "rate_per_sec": 9.2,
  "elapsed_sec": 134.5,
  "total_pages_in_db": 1234,
  "total_words_in_db": 84221,
  "sessions": [...],
  "recent_pages": [...]
}
```

---

### GET /api/events

SSE stream. Push interval: 2 seconds. Format:
```
data: {"event": "stats", "active": true, "timestamp": 1713523200.0, ...}\n\n
```

Client disconnect handled via `asyncio.CancelledError` — no resource leak.

---

### GET /api/sessions

Returns last *limit* sessions (default 50) with `failed_count` augmented per session.

---

### GET /api/sessions/{session_id}

Returns full session detail including:
- `failed_urls`: list of failed URL records (up to 100)
- `recent_pages`: list of most recently indexed pages in this session (up to 50)

Returns 404 if session not found.

---

### GET /api/pages/recent

Returns most recently indexed pages:
```json
{"pages": [...], "total_pages": 5821}
```

---

### DELETE /api/reset

Wipes `pages`, `word_index`, `visited` tables. Preserves session history.

| Condition | Response |
|-----------|----------|
| Active crawl | 409 `{"error": "Cannot reset while crawl is active"}` |
| Success | 200 `{"status": "reset", "message": "Database reset complete"}` |

---

### GET /

Serves `static/index.html`. Falls back to HTML error message if file not found.

### GET /static/{path}

Serves files from the `static/` directory (mounted via FastAPI `StaticFiles`).
Only mounted if the `static/` directory exists at startup.

---

## 5. Module Dependencies

```
api/server.py
  ├── fastapi                   (HTTP framework, Pydantic validation)
  ├── fastapi.staticfiles       (StaticFiles mount)
  ├── fastapi.middleware.cors   (CORS headers)
  ├── crawler.engine            (AsyncCrawler, CrawlerStats)
  ├── storage.database          (DB_PATH, SessionDB, FailedURLDB, QueueStateDB, init_db)
  └── storage.index             (InvertedIndex)
```

No circular imports. `api/server.py` sits at the top of the dependency graph (imports from both `crawler` and `storage`; nothing imports from `api`).

---

## 6. AppState.get_status() Field Map

| Field | Source |
|-------|--------|
| `active` | `crawler.stats.snapshot()["active"]` |
| `paused` | `crawler.stats.snapshot()["paused"]` |
| `origin` | `crawler.stats.snapshot()["origin"]` |
| `max_depth` | `crawler.stats.snapshot()["max_depth"]` |
| `pages_indexed` | `crawler.stats.snapshot()["pages_indexed"]` |
| `urls_queued` | `crawler.stats.snapshot()["urls_queued"]` |
| `urls_failed` | `crawler.stats.snapshot()["urls_failed"]` |
| `urls_skipped` | `crawler.stats.snapshot()["urls_skipped"]` |
| `urls_dropped` | `crawler.stats.snapshot()["urls_dropped"]` |
| `urls_visited` | `crawler.stats.snapshot()["urls_visited"]` |
| `rate_per_sec` | `crawler.stats.snapshot()["rate_per_sec"]` |
| `elapsed_sec` | `crawler.stats.snapshot()["elapsed_sec"]` |
| `total_pages_in_db` | `index.get_stats()["pages"]` |
| `total_words_in_db` | `index.get_stats()["words"]` |
| `sessions` | `session_db.list_sessions(limit=5)` |
| `recent_pages` | `index.recent_pages(10)` |

All sources are thread-safe. No asyncio involved.

---

## 7. Schema Reconciliation Notes

The actual `storage/database.py` schema differs from the PRD in the following ways that affect the API:

| PRD column | Actual column | Impact |
|------------|---------------|--------|
| `crawl_sessions.origin_url` | `crawl_sessions.origin` | GET /api/sessions response uses `origin` key |
| `crawl_sessions.max_depth` | `crawl_sessions.depth` | GET /api/sessions response uses `depth` key |
| `crawl_sessions.urls_visited` | not stored; derived at runtime | `/api/status` uses crawler stats instead |
| `pages.indexed_at` | `pages.indexed_at` (REAL, not TEXT) | JSON serialization via `default=str` handles float timestamps |
| `failed_urls.origin_url` | not stored; only `url` and `error` | `GET /api/sessions/{id}` returns available fields |

All of these discrepancies are transparent to API consumers because the response shapes are derived from the actual DB rows, not hardcoded against the PRD schema.

---

## 8. Concurrency Safety Analysis

| Operation | Thread | Safe? | Mechanism |
|-----------|--------|-------|-----------|
| `crawler.start()` | FastAPI handler (main) | ✅ | Spawns new daemon thread |
| `crawler.stop()` | FastAPI handler (main) | ✅ | `threading.Event.set()` |
| `crawler.pause()` | FastAPI handler (main) | ✅ | `threading.Event.clear()` |
| `crawler.resume()` | FastAPI handler (main) | ✅ | `threading.Event.set()` |
| `crawler.is_active()` | FastAPI handler (main) | ✅ | Reads bool (GIL-protected) |
| `crawler.stats.snapshot()` | SSE generator (main) | ✅ | `threading.Lock` in CrawlerStats |
| `index.search()` | FastAPI handler (main) | ✅ | Thread-local SQLite conn + WAL |
| `index.recent_pages()` | SSE generator (main) | ✅ | Thread-local SQLite conn + WAL |
| `session_db.list_sessions()` | FastAPI handler (main) | ✅ | Thread-local SQLite conn |

No asyncio primitives cross the event loop boundary.

---

## 9. Error Code Summary

| Code | Condition | Endpoints |
|------|-----------|-----------|
| 200 | Success | All |
| 400 | Bad request (missing param, not active) | `/api/stop`, `/api/pause`, `/api/resume`, `/api/search` |
| 404 | Session not found | `/api/sessions/{id}` |
| 409 | Conflict (crawl active / reset blocked) | `/api/index`, `/api/reset` |
| 422 | Validation error (bad URL, bad depth) | `/api/index` (via Pydantic) |
| 500 | Internal server error | All (catch-all) |
| 503 | Storage not ready | Any endpoint before startup completes |

---

## 10. Handoff to Agent 05 (UI) and Agent 06 (QA)

### For Agent 05 (UI Agent):

- Base URL: `http://localhost:8000`
- SSE endpoint: `GET /api/events` — use `new EventSource("/api/events")`
- Start crawl: `POST /api/index` with `{"url": "...", "depth": 3}`
- Search: `GET /api/search?q=python&limit=50&scored=true`
- Recent pages: `GET /api/pages/recent?limit=20`
- Sessions: `GET /api/sessions`
- Stop: `POST /api/stop`
- Pause toggle: `POST /api/pause`
- Reset: `DELETE /api/reset`
- Swagger docs available at: `http://localhost:8000/docs`

### For Agent 06 (QA Agent):

**Test cases to verify:**

1. `POST /api/index` with active crawl → 409
2. `POST /api/index` with invalid URL → 422
3. `POST /api/index` starts crawl → 200, session_id in response
4. `GET /api/search?q=test` returns results while crawl active
5. `GET /api/search` without `q` → 400
6. `GET /api/events` streams data every ~2s; disconnects cleanly
7. `POST /api/stop` stops crawler; `GET /api/status` shows `active: false`
8. `POST /api/pause` toggles pause; second call unpauses
9. `DELETE /api/reset` while active → 409
10. `DELETE /api/reset` after stop → 200, DB empty
11. `GET /api/sessions/{bad_id}` → 404
12. `GET /` returns HTML (index.html if exists, error page otherwise)
13. `GET /docs` returns Swagger UI

---

*End of API Agent Documentation*  
*Agent 04 — 2026-04-19 — Handoff complete*
