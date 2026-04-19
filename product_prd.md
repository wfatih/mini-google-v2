# Product Requirements Document (PRD)
## mini-google-v2: Web Crawler & Search Engine

**Version:** 1.0.0  
**Date:** 2026-04-19  
**Author:** Architect Agent (Agent 01)  
**Status:** Approved — Handoff to Implementation Agents  
**Classification:** Internal Engineering Specification

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Goals & Non-Goals](#3-goals--non-goals)
4. [Stakeholders](#4-stakeholders)
5. [User Stories](#5-user-stories)
6. [Functional Requirements](#6-functional-requirements)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [System Architecture](#8-system-architecture)
9. [Component Specifications](#9-component-specifications)
10. [Data Models](#10-data-models)
11. [API Specification](#11-api-specification)
12. [UI Specification](#12-ui-specification)
13. [CLI Specification](#13-cli-specification)
14. [Resume & Fault Tolerance](#14-resume--fault-tolerance)
15. [Constraints & Assumptions](#15-constraints--assumptions)
16. [Success Metrics](#16-success-metrics)
17. [Risks & Mitigations](#17-risks--mitigations)
18. [Glossary](#18-glossary)

---

## 1. Executive Summary

**mini-google-v2** is a self-contained, single-machine web crawler and full-text search engine built entirely with Python's standard library plus FastAPI and SQLite. It supports breadth-first crawling of arbitrary origin URLs to configurable depths, real-time TF-IDF indexed search, a browser-based single-page application (SPA), a command-line interface (CLI), and the ability to resume interrupted crawls from persistent state.

The system is designed for researchers, developers, and power users who need to crawl, archive, and search a domain's content without relying on cloud infrastructure or heavyweight search frameworks. It is intentionally constrained to a single machine, using SQLite with WAL mode and Python's `asyncio` event loop for maximum performance within that boundary.

This PRD defines the complete product requirements, architecture, data models, API contracts, and acceptance criteria. All implementation agents (Crawler, Storage, API, UI, QA) shall treat this document as the authoritative specification.

---

## 2. Problem Statement

### 2.1 Background

Existing web crawling and search solutions fall into two categories:

1. **Heavyweight distributed systems** (Scrapy + Elasticsearch, Apache Nutch + Solr): Require significant infrastructure, operational overhead, and expertise. Overkill for single-machine, domain-scoped research tasks.

2. **Simple scripts**: Lack rate limiting, back-pressure, resumability, proper inverted indexing, and search UIs. Not suitable for large crawls.

### 2.2 The Gap

There is no lightweight, fully self-contained tool that:
- Crawls large sites with proper rate limiting and back-pressure
- Maintains a real-time searchable inverted index during crawling
- Persists state so crawls can be interrupted and resumed
- Exposes both a modern web UI and a CLI
- Is simple enough to run with `pip install -r requirements.txt && python main.py`

### 2.3 Impact

Without such a tool, users must either over-engineer with heavy stacks or under-engineer with fragile scripts. mini-google-v2 fills this gap with a carefully scoped, production-quality implementation.

---

## 3. Goals & Non-Goals

### 3.1 Goals

| # | Goal | Priority |
|---|------|----------|
| G1 | Crawl any HTTP/HTTPS URL to configurable BFS depth | P0 |
| G2 | Never revisit the same URL within a session | P0 |
| G3 | Apply token-bucket rate limiting (configurable requests/sec) | P0 |
| G4 | Bound the crawler queue (back-pressure, configurable max size) | P0 |
| G5 | Build a TF-IDF inverted index in real time during crawling | P0 |
| G6 | Support concurrent search queries while indexing is active | P0 |
| G7 | Expose a RESTful FastAPI server with Server-Sent Events for live stats | P0 |
| G8 | Provide a single-page dark-mode web UI (Index, Search, History tabs) | P0 |
| G9 | Provide a CLI for headless/scripted operation | P0 |
| G10 | Persist crawl queue state to SQLite; resume on next invocation | P0 |
| G11 | Store everything in SQLite (WAL mode) — no external services | P0 |
| G12 | Use only language-native HTTP (urllib) and asyncio — no Scrapy, etc. | P0 |

### 3.2 Non-Goals

| # | Non-Goal | Reason |
|---|----------|--------|
| NG1 | Distributed crawling across multiple machines | Out of scope; single-machine by design |
| NG2 | JavaScript rendering (SPA crawling) | Complexity; urllib + html.parser is sufficient |
| NG3 | Authentication (crawling behind logins) | Security & scope |
| NG4 | Full-text ranking beyond TF-IDF | Complexity; TF-IDF sufficient for MVP |
| NG5 | Elasticsearch, Whoosh, or any external search backend | Explicit constraint |
| NG6 | Multi-user access control | Localhost-only deployment |
| NG7 | Production HTTPS for the API server | Localhost-only; HTTP sufficient |
| NG8 | Scraping dynamic content with headless browsers | Complexity |
| NG9 | Scheduled/periodic crawls | Scope; manual trigger is sufficient |

---

## 4. Stakeholders

| Role | Stakeholder | Concern |
|------|-------------|---------|
| Primary User | Developer / Researcher | Fast crawl + search without ops overhead |
| Secondary User | Data Analyst | Querying indexed content, exporting results |
| Implementor | Crawler Agent (02) | Crawler engine spec, back-pressure, resumability |
| Implementor | Storage Agent (03) | Schema, TF-IDF, thread safety |
| Implementor | API Agent (04) | FastAPI endpoints, SSE, concurrency |
| Implementor | UI Agent (05) | SPA design, dark mode, real-time updates |
| Implementor | QA Agent (06) | Test coverage, acceptance criteria |
| Architect | Architect Agent (01) | System integrity, cross-agent contracts |

---

## 5. User Stories

### 5.1 Crawling

**US-01** — *As a developer*, I want to start a BFS crawl from a given URL at a given depth so that all reachable pages within that domain are indexed.

**Acceptance Criteria:**
- `index("https://example.com", 3)` crawls all pages reachable within 3 BFS hops
- Already-visited URLs within the same crawl session are skipped
- Crawl respects configurable rate limit (default: 10 req/s)
- Queue is bounded (default max: 500 items); new URLs beyond the limit are dropped gracefully

**US-02** — *As a developer*, I want the crawler to stay within the same domain by default so that I don't accidentally crawl the entire internet.

**Acceptance Criteria:**
- `same_domain=True` (default) restricts crawling to URLs sharing the origin's netloc
- Cross-domain links are logged as "skipped" in stats

**US-03** — *As a developer*, I want the crawler to handle network errors gracefully so that a single failing URL does not crash the entire crawl.

**Acceptance Criteria:**
- Failed URLs are recorded in `failed_urls` table with error message and timestamp
- Crawler continues after any individual URL failure
- Timeout per request: configurable (default 10s)

**US-04** — *As a developer*, I want to pause and resume an active crawl without losing progress.

**Acceptance Criteria:**
- `pause()` stops dispatching new fetch tasks but keeps the queue alive
- `resume()` restarts dispatching
- Paused state is visible in `/api/status`

### 5.2 Resume After Interruption

**US-05** — *As a developer*, I want to resume a crawl after an unexpected interruption (process kill, crash) so that I don't have to restart from scratch.

**Acceptance Criteria:**
- On stop/interruption, queue state (pending URLs + depths) is saved to `crawl_sessions` / a queue snapshot table in SQLite
- On next `index(same_origin, same_depth)` call, the system detects the prior incomplete session and restores the queue
- Already-visited URLs are not re-fetched (persistent `visited` table)

### 5.3 Search

**US-06** — *As a user*, I want to search indexed content with a text query and receive ranked results with the origin URL and crawl depth.

**Acceptance Criteria:**
- `search("python asyncio")` returns a list of `(url, origin_url, depth)` triples
- Results are ranked by TF-IDF score (descending)
- Exact keyword matches are weighted 3× relative to prefix matches
- Search works even when a crawl is actively running (no exclusive write locks block reads)
- Response time < 500ms for indexes up to 100,000 pages

**US-07** — *As a user*, I want prefix-match search so that partial queries return useful results.

**Acceptance Criteria:**
- Searching "asynci" returns pages containing "asyncio"
- Prefix match weight is 1× (vs 3× for exact)

### 5.4 Web UI

**US-08** — *As a user*, I want a web interface where I can start crawls, monitor progress in real time, and search results.

**Acceptance Criteria:**
- Single-page application accessible at `http://localhost:8000`
- Three tabs: **Index** (crawl control), **Search** (query interface), **History** (past sessions)
- Live stats (pages crawled, queue size, URLs/sec) update via Server-Sent Events without page refresh
- Dark mode by default; light mode toggle available
- No JavaScript frameworks — vanilla JS only

**US-09** — *As a user*, I want to see a live feed of recently crawled pages in the UI.

**Acceptance Criteria:**
- A "recent pages" panel updates every 2s with the 20 most recently indexed pages
- Each entry shows: URL (truncated), depth, word count, timestamp

### 5.5 CLI

**US-10** — *As a developer*, I want a CLI to start crawls and searches without opening a browser.

**Acceptance Criteria:**
- `python main.py crawl --url https://example.com --depth 3` starts a crawl and streams progress
- `python main.py search --query "python"` prints ranked results to stdout
- `python main.py status` prints current crawl status
- `python main.py reset` wipes the database after confirmation prompt

**US-11** — *As a developer*, I want the CLI to block until the crawl is complete (or Ctrl-C is pressed), printing periodic progress updates.

**Acceptance Criteria:**
- Progress line printed every 5 seconds: `[progress] pages=123 queue=45 failed=2 rate=8.3/s`
- Ctrl-C triggers a graceful stop and saves queue state before exit

### 5.6 API

**US-12** — *As an integrator*, I want a REST API so I can control the crawler programmatically from any HTTP client.

**Acceptance Criteria:**
- All endpoints documented in Section 11
- Responses use `application/json`
- SSE endpoint streams newline-delimited JSON events

---

## 6. Functional Requirements

### 6.1 Crawler Requirements

#### FR-C1: BFS Traversal
The crawler SHALL implement breadth-first search using `asyncio.Queue` as the frontier. Each queue item is a `(url, depth)` tuple. The root URL starts at depth 0.

#### FR-C2: Depth Limiting
URLs at depth equal to `max_depth` SHALL be fetched and indexed, but their outgoing links SHALL NOT be enqueued.

#### FR-C3: Deduplication
Before enqueuing any URL, the system SHALL check `VisitedDB.mark_visited(url)`. If the URL has already been visited in any prior session associated with the same origin, it SHALL NOT be re-enqueued.

#### FR-C4: Back-Pressure via Bounded Queue
The `asyncio.Queue` SHALL be initialized with `maxsize=max_queue` (default: 500). When the queue is full, `put_nowait()` SHALL raise `QueueFull` and the URL SHALL be dropped and counted as "skipped".

#### FR-C5: Token-Bucket Rate Limiter
A token-bucket rate limiter SHALL enforce at most `rate` requests per second (default: 10.0). The limiter SHALL be `asyncio`-native (no `time.sleep` blocking the event loop).

#### FR-C6: Concurrent Workers
Fetching SHALL use a `ThreadPoolExecutor` with `max_workers` threads (default: 10) and `loop.run_in_executor()` so blocking urllib calls do not block the asyncio event loop.

#### FR-C7: HTML Parsing
The crawler SHALL extract:
- All `<a href="...">` links (absolute and relative, resolved against current page URL)
- Visible text content (excluding `<script>`, `<style>`, `<meta>`, `<head>` tags)

#### FR-C8: Error Handling
Any HTTP error (non-2xx status), connection error, or timeout SHALL:
- Record the URL in `failed_urls` with `error_message` and `http_status`
- Increment `stats.failed` counter
- Not halt the crawler

#### FR-C9: Same-Domain Filtering
When `same_domain=True`, only URLs whose netloc matches the origin's netloc SHALL be enqueued. Mismatched URLs are counted as "skipped".

#### FR-C10: Stats Snapshot
`crawler.stats.snapshot()` SHALL return a dict with at minimum:
```
{
  "pages_indexed": int,
  "urls_queued": int,
  "urls_failed": int,
  "urls_skipped": int,
  "urls_visited": int,
  "rate_per_sec": float,
  "elapsed_sec": float,
  "active": bool,
  "paused": bool,
  "origin": str,
  "max_depth": int
}
```

### 6.2 Storage Requirements

#### FR-S1: SQLite WAL Mode
All SQLite connections SHALL enable WAL (Write-Ahead Log) mode via `PRAGMA journal_mode=WAL` immediately after opening. This allows concurrent readers during writes.

#### FR-S2: Thread-Local Connections
Each thread (including ThreadPoolExecutor workers) SHALL use its own SQLite connection via `threading.local()`. Connections SHALL NOT be shared across threads.

#### FR-S3: Inverted Index — Indexing
`InvertedIndex.add_page()` SHALL:
1. Tokenize page text into lowercase alphabetic tokens (minimum 2 characters)
2. Compute word frequency counts per token
3. Compute TF (term frequency) = count / total_words
4. Store in `word_index` table: `(word, url, tf, count, total_words)`
5. Store page metadata in `pages` table
6. Return `True` if page was newly inserted, `False` if URL already exists in pages

#### FR-S4: Inverted Index — Search
`InvertedIndex.search(query)` SHALL:
1. Tokenize the query string
2. For each token, perform an exact lookup AND a prefix lookup (`LIKE 'token%'`)
3. Score each matching page: `score = sum(tf * idf * weight)` where:
   - `idf = log(N / df)` (N = total pages, df = document frequency for that word)
   - `weight = 3.0` for exact match, `1.0` for prefix-only match
4. Return top results sorted by score descending, as `List[Tuple[str, str, int]]` = `(url, origin_url, depth)`

#### FR-S5: Visited Deduplication
`VisitedDB.mark_visited(url)` SHALL use an `INSERT OR IGNORE` SQL statement and return `True` if the row was newly inserted (URL not previously seen), `False` otherwise.

#### FR-S6: Session Tracking
`SessionDB.create_session()` SHALL create a row in `crawl_sessions` with status `"running"` and return the session ID. `finish_session()` SHALL update status to `"completed"` or `"interrupted"`.

#### FR-S7: Queue Snapshot for Resume
On `crawler.stop()`, the system SHALL serialize the current queue contents (list of `(url, depth)` tuples) to a `queue_snapshot` JSON field in the `crawl_sessions` row for that session.

#### FR-S8: Failed URL Logging
Every failed URL SHALL be recorded in `failed_urls` with: `url`, `origin_url`, `depth`, `error_message`, `http_status` (nullable), `timestamp`.

### 6.3 API Requirements

#### FR-A1: POST /api/index
Accepts `{"url": str, "depth": int, "options": {...}}`. Starts a crawl asynchronously. Returns 409 if a crawl is already active.

#### FR-A2: GET /api/search
Accepts query parameter `q`. Returns JSON list of result objects. Returns 400 if `q` is missing or empty.

#### FR-A3: GET /api/status
Returns current crawler stats snapshot. Always 200.

#### FR-A4: GET /api/events
Server-Sent Events stream. Pushes a stats JSON event every 2 seconds while a crawl is active. Clients must handle reconnection.

#### FR-A5: GET /api/sessions
Returns list of past crawl sessions (most recent first, limit 50).

#### FR-A6: DELETE /api/reset
Drops and recreates all tables. Returns 409 if a crawl is currently active.

#### FR-A7: GET /api/pages/recent
Returns the 20 most recently indexed pages with url, origin, depth, word_count, indexed_at.

### 6.4 UI Requirements

#### FR-U1: Index Tab
- URL input field and Depth selector (1–10)
- Start / Stop / Pause / Resume buttons
- Real-time stats panel (pages, queue, failed, rate)
- Recent pages live feed (auto-refreshes via SSE)
- Progress bar (indeterminate while active)

#### FR-U2: Search Tab
- Query input with instant-search on Enter
- Results list with: rank number, URL (clickable), origin URL, depth, score (if available)
- "No results found" empty state
- Result count and query time displayed

#### FR-U3: History Tab
- Table of past sessions: origin, depth, pages indexed, duration, status, timestamp
- Clickable rows expand to show failed URLs for that session

#### FR-U4: Global UI
- Dark mode by default, persisted to localStorage
- Light/dark toggle in header
- Responsive layout (min-width: 768px supported)
- No external CDN dependencies (all CSS/JS inline or same-origin)

### 6.5 CLI Requirements

#### FR-CL1: crawl subcommand
```
python main.py crawl --url URL --depth N [--rate R] [--workers W] [--max-queue Q] [--no-same-domain]
```

#### FR-CL2: search subcommand
```
python main.py search --query QUERY [--limit L]
```

#### FR-CL3: status subcommand
```
python main.py status
```
Connects to running server or reads DB directly.

#### FR-CL4: reset subcommand
```
python main.py reset [--yes]
```
Prompts for confirmation unless `--yes` flag provided.

#### FR-CL5: server subcommand
```
python main.py server [--host HOST] [--port PORT]
```
Starts the FastAPI server. Default: `localhost:8000`.

---

## 7. Non-Functional Requirements

### 7.1 Performance

| Metric | Target | Notes |
|--------|--------|-------|
| Crawl throughput | ≥ 5 pages/sec (network-permitting) | At rate=10, workers=10 |
| Search latency (P99) | < 500 ms | For index ≤ 100K pages |
| Search latency (P50) | < 100 ms | Typical case |
| API response time | < 200 ms | For all non-SSE endpoints |
| Queue enqueue | O(1) amortized | asyncio.Queue |
| Index write | < 50 ms per page | SQLite WAL |
| DB startup (WAL restore) | < 5 sec | Any DB size |

### 7.2 Scalability

| Dimension | Constraint | Approach |
|-----------|-----------|---------|
| Pages indexed | Up to 1,000,000 pages | SQLite + indexed queries |
| Concurrent searches | Up to 10 simultaneous | WAL allows parallel reads |
| Crawl workers | 10 threads default, configurable | ThreadPoolExecutor |
| Queue depth | 500 items default, configurable | asyncio.Queue maxsize |
| Word index entries | Up to 50M rows | SQLite with covering indexes |

### 7.3 Reliability

**NFR-R1:** The system SHALL not lose already-indexed data on process crash. SQLite WAL provides crash-safe writes.

**NFR-R2:** The system SHALL be able to resume any interrupted crawl by reading `queue_snapshot` from the last incomplete session for the given origin.

**NFR-R3:** Failed individual URL fetches SHALL NOT propagate as exceptions to the crawler main loop.

**NFR-R4:** The SSE endpoint SHALL handle client disconnects gracefully without leaking resources.

**NFR-R5:** All database operations SHALL use parameterized queries (no string interpolation) to prevent SQL injection.

### 7.4 Maintainability

**NFR-M1:** Each module SHALL have a single responsibility matching the file structure.

**NFR-M2:** All public interfaces SHALL have type annotations.

**NFR-M3:** No circular imports between modules.

**NFR-M4:** Each agent's output shall be independently testable.

### 7.5 Security

**NFR-SEC1:** The system is designed for localhost-only deployment. No authentication is required but SHALL NOT be exposed to public networks.

**NFR-SEC2:** All SQL queries SHALL use parameterized statements.

**NFR-SEC3:** User-supplied URLs SHALL be validated with `urllib.parse.urlparse` before use.

**NFR-SEC4:** The system SHALL enforce `robots.txt` respect as a configurable option (default: off for research use).

### 7.6 Portability

**NFR-P1:** SHALL run on Python 3.10+ on Linux, macOS, and Windows.

**NFR-P2:** External dependencies limited to: `fastapi`, `uvicorn[standard]`, `aiofiles`. Everything else from stdlib.

---

## 8. System Architecture

### 8.1 High-Level Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        mini-google-v2 System                            │
│                                                                         │
│   ┌──────────────┐        ┌──────────────────────────────────────┐      │
│   │   Browser    │◄──────►│         FastAPI Server               │      │
│   │  (SPA UI)    │  HTTP  │  api/server.py                       │      │
│   └──────────────┘        │                                      │      │
│                           │  POST /api/index                     │      │
│   ┌──────────────┐        │  GET  /api/search                    │      │
│   │   CLI        │◄──────►│  GET  /api/status                    │      │
│   │  main.py     │  HTTP  │  GET  /api/events (SSE)              │      │
│   └──────────────┘        │  GET  /api/sessions                  │      │
│                           │  GET  /api/pages/recent              │      │
│                           │  DEL  /api/reset                     │      │
│                           └───────────────┬──────────────────────┘      │
│                                           │                              │
│                           ┌───────────────▼──────────────────────┐      │
│                           │         AsyncCrawler                 │      │
│                           │  crawler/engine.py                   │      │
│                           │                                      │      │
│                           │  asyncio event loop                  │      │
│                           │  ┌─────────────────────────────┐    │      │
│                           │  │   asyncio.Queue (bounded)   │    │      │
│                           │  │   maxsize = max_queue        │    │      │
│                           │  └──────────────┬──────────────┘    │      │
│                           │                 │ (url, depth)       │      │
│                           │  ┌──────────────▼──────────────┐    │      │
│                           │  │  Token Bucket Rate Limiter  │    │      │
│                           │  │  rate = 10 req/s            │    │      │
│                           │  └──────────────┬──────────────┘    │      │
│                           │                 │                    │      │
│                           │  ┌──────────────▼──────────────┐    │      │
│                           │  │  ThreadPoolExecutor          │    │      │
│                           │  │  max_workers = 10            │    │      │
│                           │  │  urllib HTTP fetch           │    │      │
│                           │  └──────────────┬──────────────┘    │      │
│                           └─────────────────┼────────────────────┘      │
│                                             │                            │
│                           ┌─────────────────▼──────────────────────┐    │
│                           │           Parser                        │    │
│                           │  crawler/parser.py                      │    │
│                           │  LinkParser (html.parser)               │    │
│                           │  TextParser → tokenize()                │    │
│                           └─────────────────┬──────────────────────┘    │
│                                             │                            │
│    ┌────────────────────────────────────────▼──────────────────────┐    │
│    │                    Storage Layer                               │    │
│    │                                                                │    │
│    │  ┌──────────────────────┐   ┌──────────────────────────────┐  │    │
│    │  │   InvertedIndex      │   │   VisitedDB                  │  │    │
│    │  │   storage/index.py   │   │   storage/database.py        │  │    │
│    │  │                      │   │                              │  │    │
│    │  │  add_page()          │   │  mark_visited()              │  │    │
│    │  │  search()            │   │  count()                     │  │    │
│    │  │  search_scored()     │   └──────────────────────────────┘  │    │
│    │  │  page_count()        │                                      │    │
│    │  │  word_count()        │   ┌──────────────────────────────┐  │    │
│    │  │  recent_pages()      │   │   SessionDB                  │  │    │
│    │  └──────────────────────┘   │   storage/database.py        │  │    │
│    │                             │                              │  │    │
│    │                             │  create_session()            │  │    │
│    │                             │  finish_session()            │  │    │
│    │                             │  list_sessions()             │  │    │
│    │                             └──────────────────────────────┘  │    │
│    │                                                                │    │
│    │  ┌──────────────────────────────────────────────────────────┐ │    │
│    │  │              SQLite Database (WAL mode)                  │ │    │
│    │  │              data/mini_google.db                         │ │    │
│    │  │                                                          │ │    │
│    │  │  Tables: pages | word_index | visited | crawl_sessions   │ │    │
│    │  │          failed_urls                                     │ │    │
│    │  └──────────────────────────────────────────────────────────┘ │    │
│    └────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

### 8.2 Data Flow: Indexing

```
User/API
  │
  ▼
POST /api/index {url, depth}
  │
  ▼
AsyncCrawler.start(origin, max_depth)
  │
  ├─► asyncio.Queue.put((origin, 0))          # Seed the queue
  │
  └─► _dispatcher_loop()
        │
        ├─► rate_limiter.acquire()            # Wait for token
        │
        ├─► loop.run_in_executor(             # Fetch in thread
        │     executor, fetch_url, url
        │   )
        │   │
        │   └─► urllib.request.urlopen(url)   # Blocking HTTP call
        │         │
        │         └─► returns (html, status)
        │
        ├─► parser.extract_links(html, url)   # Back on event loop
        │         │
        │         └─► for each link:
        │               VisitedDB.mark_visited(link)
        │               if new and depth+1 <= max_depth:
        │                 Queue.put_nowait((link, depth+1))
        │
        ├─► parser.extract_text(html)
        │         │
        │         └─► tokenize(text) → word_counts
        │
        └─► InvertedIndex.add_page(
              url, origin, depth, word_counts, session_id
            )
```

### 8.3 Data Flow: Search

```
User/API
  │
  ▼
GET /api/search?q=python+asyncio
  │
  ▼
InvertedIndex.search_scored("python asyncio")
  │
  ├─► tokenize("python asyncio") → ["python", "asyncio"]
  │
  ├─► For each token:
  │     SELECT url, tf, count FROM word_index
  │     WHERE word = token          ← exact match (weight=3)
  │     OR word LIKE 'token%'       ← prefix match (weight=1)
  │
  ├─► Compute IDF: log(total_pages / doc_frequency)
  │
  ├─► Score aggregation per URL:
  │     score[url] += tf * idf * weight
  │
  ├─► JOIN with pages table for origin, depth
  │
  └─► Return sorted [(url, origin, depth, score), ...]
```

### 8.4 Concurrency Model

```
Main Thread (asyncio event loop)
├── _dispatcher_loop (coroutine)          # Dequeues URLs, rate-limits, dispatches
├── _stats_updater (coroutine)            # Updates rate_per_sec every 1s
├── _sse_broadcaster (coroutine)          # Pushes events to SSE clients
└── FastAPI request handlers (coroutines) # Search, status, etc.

ThreadPoolExecutor (10 threads)
├── Thread-1: urllib fetch + DB write (thread-local connection)
├── Thread-2: urllib fetch + DB write (thread-local connection)
├── ...
└── Thread-10: urllib fetch + DB write (thread-local connection)
```

---

## 9. Component Specifications

### 9.1 crawler/engine.py — AsyncCrawler

**Class:** `AsyncCrawler`

**Constructor Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `index` | `InvertedIndex` | required | Storage backend |
| `max_workers` | `int` | `10` | ThreadPoolExecutor size |
| `max_queue` | `int` | `500` | asyncio.Queue maxsize |
| `rate` | `float` | `10.0` | Requests per second (token bucket) |
| `timeout` | `float` | `10.0` | Per-request timeout in seconds |
| `same_domain` | `bool` | `True` | Restrict crawl to origin domain |
| `db_path` | `str` | `"data/mini_google.db"` | SQLite DB path |

**Internal State:**
- `_queue: asyncio.Queue` — BFS frontier
- `_executor: ThreadPoolExecutor` — HTTP thread pool
- `_active: asyncio.Event` — set while crawling
- `_paused: asyncio.Event` — set while paused
- `_stop_event: asyncio.Event` — signals shutdown
- `_loop: asyncio.AbstractEventLoop` — event loop reference
- `_session_id: int` — current session ID from SessionDB
- `_stats: CrawlerStats` — thread-safe stats accumulator

**Key Methods:**

`start(origin: str, max_depth: int) -> None`
- Validates URL format
- Checks for resumable prior session via SessionDB
- Seeds queue: if resuming, loads snapshot; else puts `(origin, 0)`
- Creates asyncio task for `_run()`
- Non-blocking: returns immediately

`stop() -> None`
- Sets `_stop_event`
- Serializes current queue to session's `queue_snapshot` field
- Calls `_executor.shutdown(wait=False)`
- Updates session status to "interrupted" or "completed"

`_run() -> None` (coroutine, internal)
- Creates `_executor`
- Runs `_dispatcher_loop()` and `_stats_updater()` concurrently with `asyncio.gather()`

`_dispatcher_loop() -> None` (coroutine, internal)
- Loops until `_stop_event` is set and queue is empty
- For each item: checks `_paused`, acquires rate limit token, dispatches `_fetch_and_index()` to executor

`_fetch_and_index(url, depth, origin, session_id) -> None` (sync, runs in thread)
- Opens URL with urllib, reads response, checks content-type
- Calls parser to extract links and text
- Marks each new link as visited, enqueues if depth allows
- Calls `InvertedIndex.add_page()`
- On error: logs to `FailedURLDB`

### 9.2 crawler/parser.py — Parsers

**Class:** `LinkParser(html.parser.HTMLParser)`
- Extracts all `href` attributes from `<a>` tags
- `get_links(html: str, base_url: str) -> List[str]` — returns resolved absolute URLs

**Class:** `TextParser(html.parser.HTMLParser)`
- Suppresses content inside `<script>`, `<style>`, `<head>`, `<meta>` tags
- `get_text(html: str) -> str` — returns visible text

**Function:** `tokenize(text: str) -> Dict[str, int]`
- Lowercases text
- Extracts alphabetic tokens with `re.findall(r'[a-z]{2,}', text)`
- Returns `Counter` as plain dict

### 9.3 storage/database.py — Database Classes

**Class:** `ThreadLocalDB`
- Base class providing thread-local `sqlite3.Connection`
- Constructor: `__init__(db_path: str)`
- Property: `conn -> sqlite3.Connection` — returns or creates thread-local connection with WAL enabled
- `execute(sql, params=())` — shorthand for `self.conn.execute()`
- `executemany(sql, params_list)` — shorthand
- `commit()` — commits current thread's transaction

**Class:** `VisitedDB(ThreadLocalDB)`
- `mark_visited(url: str) -> bool`
- `count() -> int`
- `clear() -> None`

**Class:** `SessionDB(ThreadLocalDB)`
- `create_session(origin: str, depth: int, same_domain: bool) -> int`
- `finish_session(session_id: int, pages: int, visited: int, failed: int, skipped: int, status: str) -> None`
- `save_queue_snapshot(session_id: int, snapshot: List[Tuple[str, int]]) -> None`
- `load_queue_snapshot(session_id: int) -> List[Tuple[str, int]]`
- `find_incomplete_session(origin: str) -> Optional[dict]`
- `list_sessions(limit: int = 50) -> List[dict]`

**Class:** `FailedURLDB(ThreadLocalDB)`
- `record_failure(url: str, origin: str, depth: int, error: str, http_status: Optional[int]) -> None`
- `list_failures(session_id: int) -> List[dict]`

**Function:** `init_db(db_path: str) -> None`
- Creates all tables if not exists
- Sets WAL mode
- Creates all indexes

### 9.4 storage/index.py — InvertedIndex

**Class:** `InvertedIndex(ThreadLocalDB)`

Constructor: `__init__(db_path: str)`

`add_page(url: str, origin: str, depth: int, word_counts: Dict[str, int], session_id: int) -> bool`
- Idempotent: returns False if URL already in pages
- Computes TF for each word
- Batch inserts to word_index via executemany
- Updates pages table

`search(query: str) -> List[Tuple[str, str, int]]`
- Returns `(url, origin_url, depth)` tuples, sorted by score descending

`search_scored(query: str) -> List[Tuple[str, str, int, float]]`
- Same as search but includes score as 4th element

`_compute_idf(word: str, exact: bool) -> float`
- Queries `word_index` for document frequency
- Returns `log(N / max(df, 1))`

`page_count() -> int`
- `SELECT COUNT(*) FROM pages`

`word_count() -> int`
- `SELECT COUNT(DISTINCT word) FROM word_index`

`recent_pages(limit: int = 20) -> List[dict]`
- Returns list of dicts with url, origin, depth, word_count, indexed_at

### 9.5 api/server.py — FastAPI Application

**Application Setup:**
- `app = FastAPI(title="mini-google-v2")`
- CORS middleware: `allow_origins=["*"]` (localhost only)
- Single global `AsyncCrawler` instance
- `StaticFiles` serving `static/` at `/`

**SSE Implementation:**
- `GET /api/events` uses `StreamingResponse` with an async generator
- Generator pushes `data: {json}\n\n` every 2 seconds
- Handles `asyncio.CancelledError` on client disconnect

---

## 10. Data Models

### 10.1 SQLite Schema

```sql
-- Crawl session tracking
CREATE TABLE IF NOT EXISTS crawl_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_url      TEXT NOT NULL,
    max_depth       INTEGER NOT NULL,
    same_domain     INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'running',
    -- status: 'running' | 'completed' | 'interrupted'
    pages_indexed   INTEGER DEFAULT 0,
    urls_visited    INTEGER DEFAULT 0,
    urls_failed     INTEGER DEFAULT 0,
    urls_skipped    INTEGER DEFAULT 0,
    queue_snapshot  TEXT DEFAULT NULL,  -- JSON array of [url, depth] pairs
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT DEFAULT NULL
);

-- Indexed pages
CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE NOT NULL,
    origin_url  TEXT NOT NULL,
    depth       INTEGER NOT NULL,
    word_count  INTEGER NOT NULL DEFAULT 0,
    session_id  INTEGER REFERENCES crawl_sessions(id),
    indexed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Inverted index with TF scores
CREATE TABLE IF NOT EXISTS word_index (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word        TEXT NOT NULL,
    url         TEXT NOT NULL,
    tf          REAL NOT NULL,     -- term frequency: count/total_words
    count       INTEGER NOT NULL,  -- raw occurrence count
    total_words INTEGER NOT NULL,  -- total tokens in this page
    UNIQUE(word, url)
);

-- Visited URL deduplication set
CREATE TABLE IF NOT EXISTS visited (
    url         TEXT PRIMARY KEY,
    visited_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Failed URL log
CREATE TABLE IF NOT EXISTS failed_urls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    origin_url   TEXT NOT NULL,
    depth        INTEGER NOT NULL,
    session_id   INTEGER REFERENCES crawl_sessions(id),
    error_message TEXT NOT NULL,
    http_status  INTEGER DEFAULT NULL,
    failed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 10.2 SQLite Indexes

```sql
-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_word_index_word  ON word_index(word);
CREATE INDEX IF NOT EXISTS idx_word_index_url   ON word_index(url);
CREATE INDEX IF NOT EXISTS idx_pages_origin     ON pages(origin_url);
CREATE INDEX IF NOT EXISTS idx_pages_indexed_at ON pages(indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_origin  ON crawl_sessions(origin_url);
CREATE INDEX IF NOT EXISTS idx_sessions_status  ON crawl_sessions(status);
CREATE INDEX IF NOT EXISTS idx_failed_session   ON failed_urls(session_id);

-- Covering index for search queries (most critical)
CREATE INDEX IF NOT EXISTS idx_word_url_tf ON word_index(word, url, tf);
```

### 10.3 API Data Shapes

#### IndexRequest
```json
{
  "url": "https://example.com",
  "depth": 3,
  "options": {
    "rate": 10.0,
    "max_workers": 10,
    "max_queue": 500,
    "timeout": 10.0,
    "same_domain": true
  }
}
```

#### IndexResponse
```json
{
  "session_id": 42,
  "message": "Crawl started",
  "origin": "https://example.com",
  "max_depth": 3,
  "resuming": false
}
```

#### SearchResult
```json
{
  "url": "https://example.com/page",
  "origin_url": "https://example.com",
  "depth": 2,
  "score": 0.4821,
  "rank": 1
}
```

#### SearchResponse
```json
{
  "query": "python asyncio",
  "results": [...],
  "count": 15,
  "query_time_ms": 47
}
```

#### StatusResponse
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
  "urls_visited": 1344,
  "rate_per_sec": 9.2,
  "elapsed_sec": 134.5,
  "total_pages_in_db": 1234,
  "total_words_in_db": 84221
}
```

#### SSE Event
```
data: {"event":"stats","pages_indexed":1234,"urls_queued":87,"rate_per_sec":9.2,"active":true}

data: {"event":"page_indexed","url":"https://example.com/about","depth":1,"word_count":342}
```

#### Session
```json
{
  "id": 42,
  "origin_url": "https://example.com",
  "max_depth": 3,
  "same_domain": true,
  "status": "completed",
  "pages_indexed": 5821,
  "urls_visited": 6003,
  "urls_failed": 12,
  "urls_skipped": 170,
  "started_at": "2026-04-19T10:00:00",
  "finished_at": "2026-04-19T10:23:41"
}
```

---

## 11. API Specification

### 11.1 Base URL
`http://localhost:8000`

### 11.2 Endpoints

#### POST /api/index
**Purpose:** Start a new crawl session.  
**Request Body:** `IndexRequest` (application/json)  
**Responses:**
- `200 OK` — Crawl started. Body: `IndexResponse`
- `409 Conflict` — Crawl already active. Body: `{"error": "Crawl already in progress", "status": {...}}`
- `422 Unprocessable Entity` — Invalid URL or depth. Body: FastAPI validation error

**Behavior:**
- If prior incomplete session found for same origin, sets `resuming: true` in response and restores queue
- Starts `AsyncCrawler.start()` as background asyncio task

---

#### GET /api/search
**Purpose:** Search the inverted index.  
**Query Parameters:**
- `q` (required): URL-encoded query string
- `limit` (optional, default=50): Max results to return

**Responses:**
- `200 OK` — Body: `SearchResponse`
- `400 Bad Request` — Missing or empty `q`. Body: `{"error": "Query parameter 'q' is required"}`

**Notes:** This endpoint reads from SQLite in WAL mode; does not block writers.

---

#### GET /api/status
**Purpose:** Get current crawler stats.  
**Responses:**
- `200 OK` — Body: `StatusResponse`

**Notes:** Always returns 200, even if no crawl is active (all numeric fields will be 0).

---

#### GET /api/events
**Purpose:** Server-Sent Events stream for real-time updates.  
**Response Content-Type:** `text/event-stream`  
**Event Types:**
- `stats` — Pushed every 2 seconds. Contains full stats snapshot.
- `page_indexed` — Pushed each time a new page is added (rate-limited to max 5/s to avoid flooding)
- `crawl_complete` — Pushed when a crawl finishes

**Client Handling:** Use `EventSource` API in browser. Reconnects automatically on disconnect.

---

#### GET /api/sessions
**Purpose:** List past crawl sessions.  
**Query Parameters:**
- `limit` (optional, default=50): Max sessions to return

**Responses:**
- `200 OK` — Body: `{"sessions": [Session, ...], "count": int}`

---

#### GET /api/pages/recent
**Purpose:** Get recently indexed pages.  
**Query Parameters:**
- `limit` (optional, default=20): Max pages

**Responses:**
- `200 OK` — Body: `{"pages": [{url, origin_url, depth, word_count, indexed_at}, ...], "total_pages": int}`

---

#### DELETE /api/reset
**Purpose:** Wipe all data and reset the database.  
**Responses:**
- `200 OK` — Body: `{"message": "Database reset complete"}`
- `409 Conflict` — Body: `{"error": "Cannot reset while crawl is active"}`

---

#### POST /api/stop
**Purpose:** Stop the active crawl and save queue state.  
**Responses:**
- `200 OK` — Body: `{"message": "Crawl stopped", "queue_saved": int}`
- `400 Bad Request` — No active crawl

---

#### POST /api/pause
**Purpose:** Pause dispatching without clearing the queue.  
**Responses:**
- `200 OK` — Body: `{"message": "Crawl paused"}`
- `400 Bad Request` — Not active or already paused

---

#### POST /api/resume
**Purpose:** Resume a paused crawl.  
**Responses:**
- `200 OK` — Body: `{"message": "Crawl resumed"}`
- `400 Bad Request` — Not paused

---

## 12. UI Specification

### 12.1 Layout Structure

```
┌─────────────────────────────────────────────────────┐
│  🔍 mini-google-v2          [●] Dark  [○] Light     │
├─────────────────────────────────────────────────────┤
│  [  Index  ]  [  Search  ]  [  History  ]           │
├─────────────────────────────────────────────────────┤
│                                                     │
│   (Tab content area)                                │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 12.2 Index Tab

```
┌─────────────────────────────────────────────────────┐
│  URL: [https://example.com________________]         │
│  Depth: [3▼]  Rate: [10▼]  Workers: [10▼]          │
│  [▶ Start]  [⏸ Pause]  [⏹ Stop]                    │
├─────────────────────────────────────────────────────┤
│  ████████░░░░░░░░░░ Crawling...                     │
├─────────────────────────────────────────────────────┤
│  Pages: 1,234    Queue: 87    Failed: 3             │
│  Rate: 9.2/s     Elapsed: 2m 14s                   │
├─────────────────────────────────────────────────────┤
│  Recent Pages                                       │
│  ─────────────────────────────────────────────────  │
│  [1] https://example.com/about  depth=1  words=342  │
│  [2] https://example.com/docs   depth=2  words=891  │
│  ...                                                │
└─────────────────────────────────────────────────────┘
```

### 12.3 Search Tab

```
┌─────────────────────────────────────────────────────┐
│  Query: [python asyncio_____________________] [🔍]  │
│  15 results  (47ms)                                 │
├─────────────────────────────────────────────────────┤
│  1. https://example.com/docs/asyncio                │
│     Origin: https://example.com  Depth: 2           │
│     Score: 0.482                                    │
│  ─────────────────────────────────────────────────  │
│  2. https://example.com/tutorial                    │
│     Origin: https://example.com  Depth: 1           │
│     Score: 0.341                                    │
└─────────────────────────────────────────────────────┘
```

### 12.4 History Tab

```
┌─────────────────────────────────────────────────────┐
│  Past Crawl Sessions                                │
│  ─────────────────────────────────────────────────  │
│  Origin                    Depth  Pages  Status     │
│  https://example.com       3      5,821  ✅ done    │
│  https://docs.python.org   2      1,203  ⚠️ stopped │
│  ─────────────────────────────────────────────────  │
│  ▶ https://example.com (2026-04-19 10:00)           │
│    Failed URLs: (3)                                 │
│    • https://example.com/broken — 404               │
└─────────────────────────────────────────────────────┘
```

### 12.5 Color Scheme (Dark Mode)
| Element | Color |
|---------|-------|
| Background | `#0f1117` |
| Card/Panel | `#1a1d27` |
| Accent | `#4f8ef7` |
| Success | `#22c55e` |
| Warning | `#f59e0b` |
| Error | `#ef4444` |
| Text primary | `#e2e8f0` |
| Text secondary | `#94a3b8` |
| Border | `#2d3748` |

---

## 13. CLI Specification

### 13.1 Entry Point: main.py

```
usage: python main.py [-h] {crawl,search,status,reset,server} ...

mini-google-v2: Web Crawler & Search Engine

subcommands:
  crawl     Start a BFS crawl
  search    Query the search index
  status    Show current crawl status
  reset     Wipe all data (with confirmation)
  server    Start the FastAPI server
```

### 13.2 crawl subcommand
```
python main.py crawl \
  --url https://example.com \
  --depth 3 \
  [--rate 10.0] \
  [--workers 10] \
  [--max-queue 500] \
  [--timeout 10.0] \
  [--no-same-domain] \
  [--db data/mini_google.db]
```

Output format (every 5 seconds):
```
[2026-04-19 10:05:23] pages=142  queue=38  failed=1  rate=9.8/s  elapsed=15s
[2026-04-19 10:05:28] pages=191  queue=44  failed=1  rate=9.6/s  elapsed=20s
...
[DONE] Crawl complete. Pages indexed: 5821 | Failed: 12 | Time: 23m 41s
```

### 13.3 search subcommand
```
python main.py search --query "python asyncio" [--limit 20] [--db data/mini_google.db]
```

Output format:
```
Query: "python asyncio"  (15 results, 47ms)
──────────────────────────────────────────
 1. https://example.com/docs/asyncio
    Origin: https://example.com  Depth: 2  Score: 0.482

 2. https://example.com/tutorial
    Origin: https://example.com  Depth: 1  Score: 0.341
...
```

### 13.4 server subcommand
```
python main.py server [--host 127.0.0.1] [--port 8000] [--db data/mini_google.db]
```

---

## 14. Resume & Fault Tolerance

### 14.1 Resume Strategy

The system implements a two-layer resume mechanism:

**Layer 1: Visited URL deduplication (permanent)**
- The `visited` table persists across all sessions
- Any URL previously fetched is never re-fetched, regardless of which session crawled it
- This prevents duplicate indexing and unnecessary network traffic

**Layer 2: Queue snapshot (session-level)**
- When `crawler.stop()` is called (graceful) or on SIGINT, the current queue is serialized to JSON and stored in `crawl_sessions.queue_snapshot`
- On next `index(same_origin, same_depth)`, `SessionDB.find_incomplete_session(origin)` checks for an existing "interrupted" session
- If found, the queue snapshot is deserialized and used to seed the new crawl
- The old session is marked "superseded" and a new session ID is created

### 14.2 SIGINT Handler (CLI)
```python
signal.signal(signal.SIGINT, lambda sig, frame: crawler.stop())
```
- `stop()` serializes queue before exit
- Progress printed with confirmation: `"Queue saved: 87 URLs. Resume with same URL and depth."`

### 14.3 Crash Recovery
- SQLite WAL ensures no partial writes corrupt the database
- On restart after crash: the last session will show `status="running"` — treated as "interrupted"
- System auto-detects this and offers resume (or a fresh start if queue_snapshot is NULL)

---

## 15. Constraints & Assumptions

### 15.1 Constraints
| # | Constraint |
|---|-----------|
| C1 | Python 3.10+ required |
| C2 | Single-machine deployment only |
| C3 | No Scrapy, Elasticsearch, Whoosh, or similar frameworks |
| C4 | HTTP/HTTPS only (no FTP, mailto, etc.) |
| C5 | SQLite as the only data store |
| C6 | Localhost-only API server |
| C7 | Vanilla JavaScript only (no React, Vue, etc.) |
| C8 | No CDN dependencies in the UI |
| C9 | No authentication or authorization |
| C10 | Only one concurrent crawl at a time |

### 15.2 Assumptions
| # | Assumption |
|---|-----------|
| A1 | Target websites serve HTML content (not pure JS SPAs) |
| A2 | Network bandwidth is not the bottleneck (10 req/s is modest) |
| A3 | Available disk space: ≥ 10 GB for very large crawls |
| A4 | Available RAM: ≥ 512 MB (for asyncio queue, thread pool, SQLite cache) |
| A5 | Target sites do not require authentication |
| A6 | Text encoding is UTF-8 or detectable via charset headers |
| A7 | The SQLite WAL mode provides sufficient read/write concurrency |
| A8 | Crawled pages are primarily English-language (tokenizer is Latin-alphabet) |

---

## 16. Success Metrics

### 16.1 Functional Metrics (Pass/Fail)
- [ ] BFS crawl completes correctly for 3 test origins at depths 1, 2, 3
- [ ] No URL is indexed twice in the same session
- [ ] Search returns results while crawl is actively running
- [ ] Exact match results always outrank prefix-only matches (given equal page content)
- [ ] Queue state is preserved and restored after process kill (SIGKILL)
- [ ] CLI `crawl` command streams progress every 5 seconds
- [ ] SPA loads with no external network requests
- [ ] SSE connection delivers events within 3 seconds of page indexing

### 16.2 Performance Metrics
| Metric | Target | Measurement Method |
|--------|--------|-------------------|
| Crawl throughput | ≥ 5 pages/sec | Measure at 1000 pages |
| Search P50 latency | < 100 ms | 1000 queries on 100K-page index |
| Search P99 latency | < 500 ms | Same |
| Memory usage | < 512 MB RSS | During active crawl |
| SQLite DB size | < 10 GB per 1M pages | Estimate from test crawl |
| Resume time | < 5 sec | Time from start to first fetch |

### 16.3 Quality Metrics
- Unit test coverage ≥ 80% for storage and crawler modules
- Zero SQL injection vulnerabilities (parameterized queries enforced)
- All API endpoints respond with correct Content-Type headers
- UI renders correctly on Chrome, Firefox, Edge (latest)

---

## 17. Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|-----------|
| Target site blocks crawl (rate limiting/IP ban) | Medium | Medium | Configurable rate limiter; honor 429/503 with backoff |
| SQLite write contention under high load | Low | High | WAL mode + thread-local connections eliminate this |
| asyncio event loop blocking from sync code | Medium | High | All blocking I/O in ThreadPoolExecutor; enforced by design |
| Memory overflow from huge word_index | Low | High | Batch inserts; SQLite page cache configurable |
| HTML parsing errors on malformed pages | High | Low | `html.parser` is lenient; errors are caught and pages skipped |
| Queue snapshot corruption on hard crash | Low | Medium | WAL + atomic `UPDATE` for snapshot write |
| Cross-domain crawl consuming excessive resources | Low | High | `same_domain=True` by default |

---

## 18. Glossary

| Term | Definition |
|------|-----------|
| **BFS** | Breadth-First Search — crawl strategy that explores all pages at depth N before depth N+1 |
| **Back-pressure** | Mechanism to prevent producer from overwhelming consumer; implemented via bounded asyncio.Queue |
| **TF-IDF** | Term Frequency–Inverse Document Frequency — relevance scoring formula |
| **TF** | Term Frequency: `count(word in page) / total_words_in_page` |
| **IDF** | Inverse Document Frequency: `log(total_pages / pages_containing_word)` |
| **WAL** | Write-Ahead Log — SQLite journal mode enabling concurrent reads during writes |
| **Token Bucket** | Rate limiting algorithm: tokens accumulate at rate R/s, each request costs 1 token |
| **SSE** | Server-Sent Events — HTTP/1.1 mechanism for server-to-client push streams |
| **Thread-local** | Storage isolated per-thread; used for SQLite connections to avoid sharing |
| **Origin URL** | The starting URL passed to `index()` — defines the crawl boundary for same-domain filter |
| **Session** | One complete invocation of `index()` — tracked in `crawl_sessions` table |
| **Inverted Index** | Data structure mapping words → list of documents containing that word |
| **Exact match** | Query token matches stored word exactly (weight 3×) |
| **Prefix match** | Stored word starts with query token (weight 1×) — enables partial query matching |
| **Queue snapshot** | JSON serialization of pending `(url, depth)` pairs saved on crawler stop |
| **Resume** | Reloading a queue snapshot to continue an interrupted crawl |
| **SPA** | Single-Page Application — the UI is one HTML file with JavaScript-managed routing |

---

*End of Product Requirements Document*  
*Version 1.0.0 — 2026-04-19 — Architect Agent*  
*Next: Implementation agents (02–05) consume this document to build their respective components.*
