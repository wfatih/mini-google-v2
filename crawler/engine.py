"""
Async BFS web crawler engine for mini-google-v2.

Concurrency model:
  - One asyncio event loop runs in a background daemon thread.
  - Up to max_workers asyncio tasks consume URLs from a bounded asyncio.Queue.
  - Each task offloads the blocking urllib HTTP fetch to a ThreadPoolExecutor
    via loop.run_in_executor(), keeping the event loop free.
  - A token-bucket rate limiter (thread-safe) controls dispatch rate.
  - Threading events coordinate stop/pause between the calling thread and
    the background event loop thread.
"""

import asyncio
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from crawler.parser import LinkParser, TextParser
from storage.database import DB_PATH, FailedURLDB, SessionDB, VisitedDB
from storage.index import InvertedIndex


# ---------------------------------------------------------------------------
# URL filtering constants
# ---------------------------------------------------------------------------

_SKIP_EXTENSIONS: frozenset = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".bz2",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".webm", ".ogg",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".json", ".xml", ".csv", ".yaml", ".yml", ".txt", ".map",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm",
})

_SKIP_PATH_PREFIXES: Tuple[str, ...] = (
    "/special:", "/talk:", "/user:", "/user_talk:",
    "/wikipedia:", "/help:", "/portal:", "/template:",
    "/category_talk:", "/file_talk:", "/mediawiki:",
    "/w/index.php",
)

_SKIP_QUERY_PATTERNS: Tuple[str, ...] = (
    "action=edit", "action=history", "action=raw",
    "oldid=", "diff=", "printable=yes",
)


def _should_skip_url(url: str) -> bool:
    """Return True for URLs that carry no indexable text content."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return True

    if parsed.scheme not in ("http", "https"):
        return True

    path_lower = parsed.path.lower().rstrip("/")
    if any(path_lower.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return True

    if any(path_lower.startswith(pfx) for pfx in _SKIP_PATH_PREFIXES):
        return True

    query_lower = parsed.query.lower()
    if any(pat in query_lower for pat in _SKIP_QUERY_PATTERNS):
        return True

    return False


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Token-bucket rate limiter.  Thread-safe via a threading.Lock so it can be
    used from both asyncio coroutines (try_acquire) and executor threads.

    The bucket starts full (capacity == rate).  Tokens replenish continuously
    at `rate` tokens/second.  Each acquire costs exactly 1 token.
    """

    def __init__(self, rate: float) -> None:
        self._rate = max(rate, 0.01)
        self._capacity = self._rate
        self._tokens: float = self._capacity
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def wait_and_acquire(self) -> None:
        """Blocking acquire for use inside executor threads (not on event loop)."""
        while not self.try_acquire():
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Stats container
# ---------------------------------------------------------------------------

class CrawlerStats:
    """
    Thread-safe accumulator for crawl metrics.  All mutations go through
    _set() / _inc() which hold the lock.  snapshot() returns a consistent
    point-in-time copy as a plain dict.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.urls_processed: int = 0    # successfully indexed pages
        self.urls_failed: int = 0        # fetch errors
        self.urls_skipped: int = 0       # dedup / domain / extension filter
        self.urls_dropped: int = 0       # queue overflow (QueueFull)
        self.queue_depth: int = 0
        self.throttled: int = 0          # rate-limit waits
        self.paused: bool = False
        self.active: bool = False
        self.start_time: Optional[float] = None
        self.finish_time: Optional[float] = None
        self._origin: str = ""
        self._max_depth: int = 0
        # Sliding window for instantaneous rate calculation (last 10 s)
        self._recent_timestamps: deque = deque()

    def _set(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def _inc(self, field: str, delta: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + delta)

    def _record_fetch(self) -> None:
        with self._lock:
            self._recent_timestamps.append(time.monotonic())

    def snapshot(self) -> Dict:
        with self._lock:
            now = time.monotonic()

            if self.start_time is None:
                elapsed = 0.0
            elif self.finish_time is not None:
                elapsed = self.finish_time - self.start_time
            else:
                elapsed = now - self.start_time

            # Evict timestamps older than 10 seconds
            cutoff = now - 10.0
            while self._recent_timestamps and self._recent_timestamps[0] < cutoff:
                self._recent_timestamps.popleft()
            rate = len(self._recent_timestamps) / 10.0

            return {
                "pages_indexed": self.urls_processed,
                "urls_queued": self.queue_depth,
                "urls_failed": self.urls_failed,
                "urls_skipped": self.urls_skipped,
                "urls_dropped": self.urls_dropped,
                "urls_visited": self.urls_processed + self.urls_failed,
                "throttled": self.throttled,
                "rate_per_sec": round(rate, 2),
                "elapsed_sec": round(elapsed, 1),
                "active": self.active,
                "paused": self.paused,
                "origin": self._origin,
                "max_depth": self._max_depth,
            }


# ---------------------------------------------------------------------------
# Async crawler
# ---------------------------------------------------------------------------

class AsyncCrawler:
    """
    BFS web crawler driven by an asyncio event loop in a background daemon
    thread.  Public methods (start / stop / pause / resume) are safe to call
    from any thread.
    """

    def __init__(
        self,
        index: InvertedIndex,
        max_workers: int = 10,
        max_queue: int = 500,
        rate: float = 10.0,
        timeout: float = 10.0,
        same_domain: bool = True,
        db_path: str = DB_PATH,
    ) -> None:
        self._index = index
        self._max_workers = max_workers
        self._max_queue = max_queue
        self._rate = rate
        self._timeout = timeout
        self._same_domain = same_domain
        self._db_path = db_path

        self._rate_limiter = _RateLimiter(rate)
        self._stats = CrawlerStats()

        # Threading primitives shared between calling thread and event loop thread
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()          # "not paused" = event is set

        # Set in _crawl coroutine; used by stop() for snapshot drain
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._session_id: Optional[int] = None
        self._thread: Optional[threading.Thread] = None

        # Reusable DB helper objects (thread-local connections inside)
        self._visited_db = VisitedDB(db_path)
        self._session_db = SessionDB(db_path)
        self._failed_db = FailedURLDB(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def stats(self) -> CrawlerStats:
        return self._stats

    def start(self, origin: str, max_depth: int) -> None:
        """
        Launch the crawl in a background daemon thread.  Non-blocking.
        Raises ValueError for an invalid origin URL.
        """
        parsed = urllib.parse.urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid URL: {origin!r}")

        self._stop_event.clear()
        self._done_event.clear()
        self._pause_event.set()
        self._stats._set(
            active=True,
            paused=False,
            start_time=time.monotonic(),
            finish_time=None,
            urls_processed=0,
            urls_failed=0,
            urls_skipped=0,
            urls_dropped=0,
            queue_depth=0,
            throttled=0,
        )
        self._stats._origin = origin
        self._stats._max_depth = max_depth

        self._thread = threading.Thread(
            target=self._run_event_loop,
            args=(origin, max_depth),
            daemon=True,
            name="crawler-event-loop",
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Signal the crawl to stop.  Non-blocking: returns immediately.
        Use wait() to block until shutdown is complete.
        The remaining queue is drained and snapshotted inside the event loop.
        """
        self._stop_event.set()
        self._pause_event.set()   # unblock paused workers so they can exit

    def pause(self) -> None:
        """Suspend URL dispatch without clearing the queue."""
        self._pause_event.clear()
        self._stats._set(paused=True)

    def resume(self) -> None:
        """Resume a paused crawl."""
        self._pause_event.set()
        self._stats._set(paused=False)

    def is_active(self) -> bool:
        return self._stats.active

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the crawl finishes.  Returns True if completed."""
        return self._done_event.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal: event loop thread entry point
    # ------------------------------------------------------------------

    def _run_event_loop(self, origin: str, max_depth: int) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._crawl(origin, max_depth))
        except Exception:
            pass
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._stats._set(active=False, finish_time=time.monotonic())
            self._done_event.set()

    # ------------------------------------------------------------------
    # Core crawl coroutine
    # ------------------------------------------------------------------

    async def _crawl(self, origin: str, max_depth: int) -> None:
        self._queue = asyncio.Queue(maxsize=self._max_queue)
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="crawler-fetch",
        )

        # Session setup and optional resume
        loop = asyncio.get_event_loop()
        incomplete = await loop.run_in_executor(
            None, self._session_db.find_incomplete_session, origin
        )

        if incomplete:
            self._session_id = incomplete["id"]
            snapshot = await loop.run_in_executor(
                None, self._session_db.load_queue_snapshot, self._session_id
            )
            seeded = 0
            for snap_url, snap_depth in snapshot:
                if snap_depth <= max_depth:
                    try:
                        self._queue.put_nowait((snap_url, origin, snap_depth, max_depth))
                        seeded += 1
                    except asyncio.QueueFull:
                        break
            if seeded == 0:
                # Snapshot was empty or all entries exceeded max_depth
                await self._queue.put((origin, origin, 0, max_depth))
        else:
            self._session_id = await loop.run_in_executor(
                None,
                self._session_db.create_session,
                origin,
                max_depth,
                self._same_domain,
            )
            await self._queue.put((origin, origin, 0, max_depth))

        self._stats._set(queue_depth=self._queue.qsize())

        # Spawn worker tasks
        workers = [
            asyncio.create_task(self._worker(), name=f"worker-{i}")
            for i in range(self._max_workers)
        ]

        # Race between natural completion and an external stop signal
        async def _poll_stop() -> None:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.05)

        join_task = asyncio.ensure_future(self._queue.join())
        stop_task = asyncio.ensure_future(_poll_stop())

        done, pending = await asyncio.wait(
            {join_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        status = "interrupted" if self._stop_event.is_set() else "completed"

        # Cancel all workers; they call task_done in their finally blocks
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        await self._finalize(status)
        self._executor.shutdown(wait=False)

    async def _finalize(self, status: str) -> None:
        """Drain remaining queue items into the snapshot, then close the session."""
        snapshot: List[Tuple[str, int]] = []
        while not self._queue.empty():
            try:
                url, _origin, depth, _max_depth = self._queue.get_nowait()
                snapshot.append((url, depth))
            except asyncio.QueueEmpty:
                break

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                self._session_db.save_queue_snapshot,
                self._session_id,
                snapshot,
            )
        except Exception:
            pass

        try:
            s = self._stats
            await loop.run_in_executor(
                None,
                self._session_db.finish_session,
                self._session_id,
                s.urls_processed,
                s.urls_processed + s.urls_failed,
                s.urls_failed,
                s.urls_skipped,
                status,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Worker coroutine
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        """
        Consume URLs from the queue indefinitely until cancelled.
        task_done() is called exactly once per get() on every code path.
        """
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return   # No item was dequeued, no task_done needed.

            # Track whether task_done was already called so the finally
            # block does not double-call it (Python always runs finally,
            # even when an except block issues an early return).
            _task_done_called = False
            try:
                if self._stop_event.is_set():
                    return   # finally will call task_done

                # Honour pause without burning CPU
                while not self._pause_event.is_set():
                    if self._stop_event.is_set():
                        break
                    await asyncio.sleep(0.1)

                if self._stop_event.is_set():
                    return   # finally will call task_done

                url, origin, depth, max_depth = item

                # Deduplication is serialised on the event loop thread —
                # no two workers can both see mark_visited return True for
                # the same URL because coroutines only interleave at await
                # points, and the run_in_executor await is per-worker.
                loop = asyncio.get_event_loop()
                is_new = await loop.run_in_executor(
                    None, self._visited_db.mark_visited, url
                )
                if not is_new:
                    self._stats._inc("urls_skipped")
                    self._stats._set(queue_depth=self._queue.qsize())
                    return   # finally will call task_done

                await self._process(url, origin, depth, max_depth)

            except asyncio.CancelledError:
                # Call task_done before returning so queue.join() does not hang.
                self._queue.task_done()
                _task_done_called = True
                return
            except Exception:
                pass   # swallow; task_done called in finally
            finally:
                if not _task_done_called:
                    self._queue.task_done()

    # ------------------------------------------------------------------
    # URL processor coroutine
    # ------------------------------------------------------------------

    async def _process(
        self,
        url: str,
        origin: str,
        depth: int,
        max_depth: int,
    ) -> None:
        if _should_skip_url(url):
            self._stats._inc("urls_skipped")
            return

        # Async rate limiting: spin without blocking the event loop
        while not self._rate_limiter.try_acquire():
            self._stats._inc("throttled")
            await asyncio.sleep(0.05)

        loop = asyncio.get_event_loop()
        try:
            html, error = await loop.run_in_executor(
                self._executor, self._fetch_sync, url
            )
        except Exception as exc:
            self._stats._inc("urls_failed")
            await loop.run_in_executor(
                None,
                self._failed_db.record_failure,
                url, origin, depth, str(exc), None,
            )
            return

        if error == "skip":
            self._stats._inc("urls_skipped")
            return

        if error is not None:
            self._stats._inc("urls_failed")
            http_status: Optional[int] = None
            if error.startswith("HTTP "):
                try:
                    http_status = int(error.split()[1])
                except (IndexError, ValueError):
                    pass
            await loop.run_in_executor(
                None,
                self._failed_db.record_failure,
                url, origin, depth, error, http_status,
            )
            return

        if not html:
            self._stats._inc("urls_skipped")
            return

        # Parse and index; these run on the event loop thread but are fast
        # (in-memory string operations + one SQLite write ≪ 50 ms).
        try:
            text_parser = TextParser()
            text_parser.feed(html)
            word_counts = text_parser.word_counts()

            if self._session_id is not None:
                await loop.run_in_executor(
                    None,
                    self._index.add_page,
                    url, origin, depth, word_counts, self._session_id,
                )

            self._stats._inc("urls_processed")
            self._stats._record_fetch()
            self._stats._set(queue_depth=self._queue.qsize())

            # Enqueue outgoing links only when depth budget remains
            if depth < max_depth:
                origin_netloc = urllib.parse.urlparse(origin).netloc
                link_parser = LinkParser(url)
                link_parser.feed(html)

                for link in link_parser.links:
                    if self._same_domain:
                        link_netloc = urllib.parse.urlparse(link).netloc
                        if link_netloc != origin_netloc:
                            self._stats._inc("urls_skipped")
                            continue
                    try:
                        self._queue.put_nowait((link, origin, depth + 1, max_depth))
                        self._stats._set(queue_depth=self._queue.qsize())
                    except asyncio.QueueFull:
                        self._stats._inc("urls_dropped")

        except Exception:
            pass

    # ------------------------------------------------------------------
    # Synchronous HTTP fetch (runs in ThreadPoolExecutor)
    # ------------------------------------------------------------------

    def _fetch_sync(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Blocking HTTP fetch.  Returns (html_str, None) on success,
        (None, "skip") for non-HTML responses, or (None, error_message)
        on failure.  Runs inside a ThreadPoolExecutor thread.
        """
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "MiniGoogleV2/1.0 (educational)",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                    "Accept-Language": "en-US,en;q=0.5",
                },
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                content_type: str = resp.headers.get("Content-Type", "") or ""
                ct_lower = content_type.lower()

                # Only index HTML/text content
                if "text/html" not in ct_lower and "text/plain" not in ct_lower:
                    return None, "skip"

                # Extract charset from Content-Type (e.g. "text/html; charset=utf-8")
                charset = "utf-8"
                if "charset=" in ct_lower:
                    raw_charset = content_type.split("charset=")[-1].split(";")[0].strip()
                    if raw_charset:
                        charset = raw_charset

                raw_bytes: bytes = resp.read()
                try:
                    html = raw_bytes.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    html = raw_bytes.decode("utf-8", errors="replace")

                return html, None

        except urllib.error.HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return None, str(exc.reason)
        except TimeoutError:
            return None, "timeout"
        except Exception as exc:
            return None, str(exc)
