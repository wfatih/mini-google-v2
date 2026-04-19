"""
api/server.py — FastAPI REST API server for mini-google-v2.

Architecture note — two separate asyncio event loops:
  ┌──────────────────────────────────────────────────────────────┐
  │  Main Thread (uvicorn)                                       │
  │    asyncio event loop #1  ← FastAPI request handlers, SSE   │
  └───────────────────────────────┬──────────────────────────────┘
                                  │  thread-safe calls only
  ┌───────────────────────────────▼──────────────────────────────┐
  │  Background Daemon Thread (spawned by AsyncCrawler.start())  │
  │    asyncio event loop #2  ← BFS workers, HTTP fetches        │
  └──────────────────────────────────────────────────────────────┘

  The two loops share NO asyncio primitives.  All cross-boundary
  communication goes through:
    • threading.Event   — stop/pause/resume signals
    • threading.Lock    — stats snapshot (CrawlerStats)
    • Plain method calls — crawler.stop(), .pause(), .resume(), .is_active()

  The FastAPI layer never awaits anything from the crawler's loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from crawler.engine import AsyncCrawler
from storage.database import DB_PATH, FailedURLDB, QueueStateDB, SessionDB, init_db
from storage.index import InvertedIndex


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class IndexOptions(BaseModel):
    """Nested options block (PRD format)."""

    rate: float = 10.0
    max_workers: int = 10
    max_queue: int = 500
    timeout: float = 10.0
    same_domain: bool = True


class IndexRequest(BaseModel):
    """Body for POST /api/index.

    Accepts both the flat format (all fields at root) and the PRD's nested
    ``options`` block.  Flat fields take precedence; ``options`` is a
    convenience alias for callers that use the nested form.
    """

    url: str
    depth: int = 2
    max_workers: int = 10
    rate: float = 10.0
    max_queue: int = 500
    timeout: float = 10.0
    same_domain: bool = True
    options: Optional[IndexOptions] = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        parsed = urllib.parse.urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid URL — must be http/https with a host: {v!r}")
        return v

    @field_validator("depth")
    @classmethod
    def _validate_depth(cls, v: int) -> int:
        if v < 0 or v > 20:
            raise ValueError("depth must be between 0 and 20")
        return v

    def effective_options(self) -> dict:
        """Return the resolved crawl options, merging nested ``options`` if present."""
        if self.options:
            return {
                "rate": self.options.rate,
                "max_workers": self.options.max_workers,
                "max_queue": self.options.max_queue,
                "timeout": self.options.timeout,
                "same_domain": self.options.same_domain,
            }
        return {
            "rate": self.rate,
            "max_workers": self.max_workers,
            "max_queue": self.max_queue,
            "timeout": self.timeout,
            "same_domain": self.same_domain,
        }


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

class AppState:
    """Module-level singleton holding all long-lived server resources.

    Lifecycle:
      • Created at module import time (empty).
      • Populated during FastAPI ``startup`` event (after DB init).
      • Crawler slot populated on POST /api/index, cleared on reset.
    """

    def __init__(self) -> None:
        self.crawler: Optional[AsyncCrawler] = None
        self.index: Optional[InvertedIndex] = None
        self.session_db: Optional[SessionDB] = None
        self.failed_db: Optional[FailedURLDB] = None
        self.queue_db: Optional[QueueStateDB] = None
        self.db_path: str = DB_PATH

    # ------------------------------------------------------------------
    # Status snapshot — called from FastAPI handlers and SSE generator.
    # All reads are via thread-safe method calls; no asyncio involved.
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return a comprehensive, point-in-time system status dict.

        Fields guaranteed present:
          active, paused, origin, max_depth,
          pages_indexed, urls_queued, urls_failed, urls_skipped,
          urls_dropped, urls_visited, rate_per_sec, elapsed_sec,
          total_pages_in_db, total_words_in_db,
          sessions (last 5), recent_pages (last 10).
        """
        status: dict = {
            "active": False,
            "paused": False,
            "origin": "",
            "max_depth": 0,
            "pages_indexed": 0,
            "urls_queued": 0,
            "urls_failed": 0,
            "urls_skipped": 0,
            "urls_dropped": 0,
            "urls_visited": 0,
            "throttled": 0,
            "rate_per_sec": 0.0,
            "elapsed_sec": 0.0,
            "total_pages_in_db": 0,
            "total_words_in_db": 0,
            "sessions": [],
            "recent_pages": [],
        }

        # DB-level aggregate stats (thread-safe: own SQLite connection)
        if self.index is not None:
            try:
                db_stats = self.index.get_stats()
                status["total_pages_in_db"] = db_stats.get("pages", 0)
                status["total_words_in_db"] = db_stats.get("words", 0)
                status["recent_pages"] = self.index.recent_pages(10)
            except Exception:
                pass

        # Recent sessions (for UI history widget)
        if self.session_db is not None:
            try:
                status["sessions"] = self.session_db.list_sessions(limit=5)
            except Exception:
                pass

        # Live crawler stats (protected by CrawlerStats._lock)
        if self.crawler is not None:
            try:
                snap = self.crawler.stats.snapshot()
                for key in (
                    "active", "paused", "origin", "max_depth",
                    "pages_indexed", "urls_queued", "urls_failed",
                    "urls_skipped", "urls_dropped", "urls_visited",
                    "throttled", "rate_per_sec", "elapsed_sec",
                ):
                    if key in snap:
                        status[key] = snap[key]
            except Exception:
                pass

        return status


# Module-level singleton — created once at import time, populated at startup.
state = AppState()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(db_path: str = DB_PATH) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        db_path: Path to the SQLite database file.  Defaults to DB_PATH
                 (``data/mini_google.db``).

    Returns:
        Configured :class:`FastAPI` application ready for uvicorn.
    """
    state.db_path = db_path

    app = FastAPI(
        title="mini-google-v2",
        description="Async BFS web crawler with TF-IDF search — REST API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # -----------------------------------------------------------------------
    # Middleware
    # -----------------------------------------------------------------------

    # CORS: allow all origins (localhost-only service; safe in this context)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    @app.on_event("startup")
    async def _startup() -> None:
        """Initialise DB schema and storage helpers on server start."""
        init_db(state.db_path)
        state.index = InvertedIndex(db_path=state.db_path)
        state.session_db = SessionDB(state.db_path)
        state.failed_db = FailedURLDB(state.db_path)
        state.queue_db = QueueStateDB(state.db_path)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        """Gracefully stop any active crawl on server shutdown."""
        if state.crawler is not None and state.crawler.is_active():
            state.crawler.stop()

    # -----------------------------------------------------------------------
    # POST /api/index — start a crawl
    # -----------------------------------------------------------------------

    @app.post("/api/index", summary="Start a BFS crawl session")
    async def start_index(req: IndexRequest) -> JSONResponse:
        """Start a new crawl from *url* at BFS depth *depth*.

        Returns 409 if a crawl is already active.  The crawler runs in a
        background daemon thread; this endpoint returns immediately.
        """
        if state.crawler is not None and state.crawler.is_active():
            return JSONResponse(
                status_code=409,
                content={
                    "error": "Crawl already in progress",
                    "status": state.crawler.stats.snapshot(),
                },
            )

        if state.index is None:
            raise HTTPException(status_code=503, detail="Storage not initialised")

        opts = req.effective_options()

        crawler = AsyncCrawler(
            index=state.index,
            max_workers=opts["max_workers"],
            max_queue=opts["max_queue"],
            rate=opts["rate"],
            timeout=opts["timeout"],
            same_domain=opts["same_domain"],
            db_path=state.db_path,
        )
        state.crawler = crawler

        try:
            crawler.start(req.url, req.depth)
        except ValueError as exc:
            state.crawler = None
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            state.crawler = None
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Give the background thread a moment to create the DB session row
        # so we can return the session_id in the response.
        await asyncio.sleep(0.15)

        session_id: Optional[int] = None
        if state.session_db is not None:
            try:
                active_session = state.session_db.get_active_session()
                if active_session:
                    session_id = active_session["id"]
            except Exception:
                pass

        return JSONResponse(
            status_code=200,
            content={
                "status": "started",
                "message": "Crawl started",
                "session_id": session_id,
                "origin": req.url,
                "depth": req.depth,
            },
        )

    # -----------------------------------------------------------------------
    # POST /api/stop — stop the active crawl
    # -----------------------------------------------------------------------

    @app.post("/api/stop", summary="Stop the active crawl")
    async def stop_index() -> JSONResponse:
        """Signal the crawler to stop.  Queue state is persisted automatically
        by the crawler's ``_finalize()`` routine before the thread exits.
        """
        if state.crawler is None or not state.crawler.is_active():
            raise HTTPException(status_code=400, detail="No active crawl to stop")

        state.crawler.stop()
        return JSONResponse(
            content={"status": "stopped", "message": "Crawl stop signal sent"}
        )

    # -----------------------------------------------------------------------
    # POST /api/pause — toggle pause / resume
    # -----------------------------------------------------------------------

    @app.post("/api/pause", summary="Pause or resume the active crawl (toggle)")
    async def pause_toggle() -> JSONResponse:
        """Toggle the crawl between paused and running states."""
        if state.crawler is None or not state.crawler.is_active():
            raise HTTPException(status_code=400, detail="No active crawl")

        snap = state.crawler.stats.snapshot()
        if snap.get("paused", False):
            state.crawler.resume()
            return JSONResponse(content={"status": "resumed"})
        else:
            state.crawler.pause()
            return JSONResponse(content={"status": "paused"})

    # -----------------------------------------------------------------------
    # POST /api/resume — explicit resume (complement to /api/pause)
    # -----------------------------------------------------------------------

    @app.post("/api/resume", summary="Resume a paused crawl")
    async def resume_crawl() -> JSONResponse:
        """Resume a crawl that was previously paused."""
        if state.crawler is None or not state.crawler.is_active():
            raise HTTPException(status_code=400, detail="No active crawl")

        snap = state.crawler.stats.snapshot()
        if not snap.get("paused", False):
            raise HTTPException(status_code=400, detail="Crawl is not currently paused")

        state.crawler.resume()
        return JSONResponse(content={"status": "resumed", "message": "Crawl resumed"})

    # -----------------------------------------------------------------------
    # GET /api/search — full-text search
    # -----------------------------------------------------------------------

    @app.get("/api/search", summary="Search the inverted index")
    async def search(
        q: str = Query(..., description="Search query string"),
        limit: int = Query(default=20, ge=1, le=500, description="Max results"),
        scored: bool = Query(default=False, description="Include TF-IDF scores"),
    ) -> JSONResponse:
        """Query the TF-IDF inverted index.

        Works concurrently with an active crawl session because SQLite
        WAL mode allows readers and the writer to proceed in parallel.

        Set ``scored=true`` to include numeric relevance scores in results.
        """
        query = q.strip() if q else ""
        if not query:
            raise HTTPException(
                status_code=400,
                detail="Query parameter 'q' is required and must not be empty",
            )

        if state.index is None:
            raise HTTPException(status_code=503, detail="Index not ready")

        t0 = time.monotonic()
        try:
            if scored:
                raw: List = state.index.search_scored(query, limit=limit)
                results = [
                    {
                        "url": url,
                        "origin": origin,
                        "depth": depth,
                        "score": round(float(score), 4),
                        "rank": rank,
                    }
                    for rank, (url, origin, depth, score) in enumerate(raw, start=1)
                ]
            else:
                raw_plain: List = state.index.search(query, limit=limit)
                results = [
                    {
                        "url": url,
                        "origin": origin,
                        "depth": depth,
                        "score": None,
                        "rank": rank,
                    }
                    for rank, (url, origin, depth) in enumerate(raw_plain, start=1)
                ]
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        return JSONResponse(
            content={
                "query": query,
                "count": len(results),
                "results": results,
                "query_time_ms": elapsed_ms,
            }
        )

    # -----------------------------------------------------------------------
    # GET /api/status — full system status
    # -----------------------------------------------------------------------

    @app.get("/api/status", summary="Current crawler and index status")
    async def get_status() -> JSONResponse:
        """Return crawler stats plus DB aggregate counts.  Always 200."""
        return JSONResponse(content=state.get_status())

    # -----------------------------------------------------------------------
    # GET /api/events — Server-Sent Events stream
    # -----------------------------------------------------------------------

    @app.get("/api/events", summary="SSE stream of real-time stats")
    async def stream_events() -> StreamingResponse:
        """Push a ``stats`` event every 2 seconds to connected clients.

        Format::

            data: {"event": "stats", "active": true, "pages_indexed": 142, ...}

        Clients should use the browser ``EventSource`` API.  Reconnection
        is handled automatically by the browser.  The server catches
        ``asyncio.CancelledError`` on client disconnect so no resources
        are leaked.
        """

        async def _generate():
            try:
                while True:
                    payload = state.get_status()
                    payload["event"] = "stats"
                    payload["timestamp"] = time.time()
                    yield f"data: {json.dumps(payload, default=str)}\n\n"
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                # Client disconnected — exit cleanly
                pass
            except Exception:
                pass

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # -----------------------------------------------------------------------
    # GET /api/sessions — list past crawl sessions
    # -----------------------------------------------------------------------

    @app.get("/api/sessions", summary="List past crawl sessions")
    async def list_sessions(
        limit: int = Query(default=50, ge=1, le=200, description="Max sessions"),
    ) -> JSONResponse:
        """Return past crawl sessions (newest first) with failure counts."""
        if state.session_db is None:
            raise HTTPException(status_code=503, detail="Database not ready")

        try:
            sessions = state.session_db.list_sessions(limit=limit)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        if state.failed_db is not None:
            for session in sessions:
                try:
                    session["failed_count"] = state.failed_db.count_for_session(
                        session["id"]
                    )
                except Exception:
                    session["failed_count"] = 0

        return JSONResponse(content={"sessions": sessions, "count": len(sessions)})

    # -----------------------------------------------------------------------
    # GET /api/sessions/{session_id} — session detail
    # -----------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}", summary="Session detail with failed URLs")
    async def get_session(session_id: int) -> JSONResponse:
        """Return full detail for a single session including recent pages and
        failed URLs.
        """
        if state.session_db is None or state.index is None:
            raise HTTPException(status_code=503, detail="Database not ready")

        session = state.session_db.get_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=404, detail=f"Session {session_id} not found"
            )

        if state.failed_db is not None:
            try:
                session["failed_urls"] = state.failed_db.failures_for_session(
                    session_id, limit=100
                )
            except Exception:
                session["failed_urls"] = []

        try:
            session["recent_pages"] = state.index.pages_for_session(
                session_id, limit=50
            )
        except Exception:
            session["recent_pages"] = []

        return JSONResponse(content=session)

    # -----------------------------------------------------------------------
    # GET /api/pages/recent — recently indexed pages
    # -----------------------------------------------------------------------

    @app.get("/api/pages/recent", summary="Most recently indexed pages")
    async def recent_pages(
        limit: int = Query(default=20, ge=1, le=200),
    ) -> JSONResponse:
        """Return the *limit* most recently indexed pages from the DB."""
        if state.index is None:
            raise HTTPException(status_code=503, detail="Index not ready")

        try:
            pages = state.index.recent_pages(limit=limit)
            total = state.index.page_count()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return JSONResponse(content={"pages": pages, "total_pages": total})

    # -----------------------------------------------------------------------
    # DELETE /api/reset — wipe all indexed data
    # -----------------------------------------------------------------------

    @app.delete("/api/reset", summary="Reset — wipe all indexed data")
    async def reset_database() -> JSONResponse:
        """Delete all pages, word index, and visited records.

        Returns 409 if a crawl is currently active.  Session history and
        failed URL logs are preserved (only index data is wiped).
        """
        if state.crawler is not None and state.crawler.is_active():
            return JSONResponse(
                status_code=409,
                content={"error": "Cannot reset while crawl is active"},
            )

        if state.index is None:
            raise HTTPException(status_code=503, detail="Index not ready")

        try:
            state.index.reset()
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Reset failed: {exc}",
            ) from exc

        # Release the stale crawler reference so is_active() returns False
        state.crawler = None

        return JSONResponse(
            content={"status": "reset", "message": "Database reset complete"}
        )

    # -----------------------------------------------------------------------
    # GET / — serve SPA index.html
    # -----------------------------------------------------------------------

    _module_dir = os.path.dirname(os.path.abspath(__file__))
    _static_dir = os.path.normpath(os.path.join(_module_dir, "..", "static"))
    _index_html = os.path.join(_static_dir, "index.html")

    @app.get("/", include_in_schema=False)
    async def serve_root() -> HTMLResponse:
        """Serve the single-page application entry point."""
        if os.path.isfile(_index_html):
            try:
                with open(_index_html, "r", encoding="utf-8") as fh:
                    return HTMLResponse(content=fh.read())
            except OSError:
                pass
        return HTMLResponse(
            content=(
                "<html><body>"
                "<h1>mini-google-v2 API</h1>"
                "<p>UI not found (<code>static/index.html</code> missing).</p>"
                "<p>Swagger docs: <a href='/docs'>/docs</a></p>"
                "</body></html>"
            )
        )

    # Mount static assets AFTER all route definitions so API routes take
    # precedence in FastAPI's internal routing table.
    if os.path.isdir(_static_dir):
        app.mount(
            "/static",
            StaticFiles(directory=_static_dir),
            name="static",
        )

    return app


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    db_path: str = DB_PATH,
    reload: bool = False,
) -> None:
    """Start the uvicorn ASGI server (blocking).

    Args:
        host:     Bind address.  Default ``127.0.0.1`` (localhost only).
        port:     TCP port.  Default 8000.
        db_path:  SQLite database file path.
        reload:   Enable auto-reload (development only).
    """
    import uvicorn  # imported here so the module can be imported without uvicorn installed

    app = create_app(db_path=db_path)
    uvicorn.run(app, host=host, port=port, reload=reload)
