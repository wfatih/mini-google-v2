#!/usr/bin/env python3
"""
main.py — CLI entry point for mini-google-v2.

Commands
--------
  python main.py server [--port 8080]          Start the web UI (recommended)
  python main.py index  <url> <depth>          Start a crawl (CLI mode)
  python main.py search <query>                Search the index
  python main.py status                        Print index statistics
  python main.py reset                         Wipe the index and visited set
"""

import argparse
import os
import sys
import time

from storage.database import DB_PATH
from storage.index import InvertedIndex


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_index() -> InvertedIndex:
    return InvertedIndex(db_path=DB_PATH)


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _fmt(n: int) -> str:
    return f"{n:,}"


def _render_dashboard(s: dict, pages: int, words: int, max_q: int):
    status = "● ACTIVE" if s["active"] else "■  IDLE  "
    pause_tag = "  [PAUSED]" if s.get("paused") else ""
    throttle = "YES ⚠" if s["throttled"] else "no "
    qd = s.get("queue_depth", s.get("urls_queued", 0))
    processed = s.get("urls_processed", s.get("pages_indexed", 0))
    dropped = s.get("urls_dropped_backpressure", s.get("urls_dropped", 0))
    qbar_width = 36
    filled = int(qbar_width * min(1.0, qd / max(1, max_q)))
    qbar = "█" * filled + "░" * (qbar_width - filled)
    print("╔══════════════════════════════════════════════════════╗")
    print("║        mini-google v2  —  Crawler Dashboard         ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Status           : {status}{pause_tag:<24}║")
    print(f"║  Elapsed          : {s['elapsed_s']:<30.1f} s║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  URLs processed   : {_fmt(processed):<32}║")
    print(f"║  URLs failed      : {_fmt(s['urls_failed']):<32}║")
    print(f"║  URLs skipped     : {_fmt(s['urls_skipped']):<32}║")
    print(f"║  Dropped (BP)     : {_fmt(dropped):<32}║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Queue            : {qd:<5} / {max_q:<5}                     ║")
    print(f"║  Queue bar        : [{qbar}] ║")
    print(f"║  Throttled        : {throttle:<32}║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Pages indexed    : {_fmt(pages):<32}║")
    print(f"║  Unique words     : {_fmt(words):<32}║")
    print("╚══════════════════════════════════════════════════════╝")
    print("\nPress Ctrl+C to stop.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_server(args):
    """Start the web UI server."""
    from api.server import create_app, run_server
    print(f"\nmini-google v2  —  Web UI starting on http://localhost:{args.port}")
    print("Press Ctrl+C to stop.\n")
    run_server(host="localhost", port=args.port)


def cmd_index(args):
    """CLI crawl with live dashboard."""
    from crawler.engine import AsyncCrawler

    idx = _make_index()
    crawler = AsyncCrawler(
        index=idx,
        max_workers=args.workers,
        max_queue=args.max_queue,
        rate=args.rate,
        timeout=args.timeout,
        same_domain=not args.all_domains,
        db_path=DB_PATH,
    )

    print(f"\n[index] origin={args.url}  depth={args.depth}  "
          f"workers={args.workers}  rate={args.rate} req/s  "
          f"max_queue={args.max_queue}\n")

    crawler.start(args.url, args.depth)

    if args.dashboard:
        try:
            while crawler.is_active():
                _clear()
                _render_dashboard(
                    crawler.stats.snapshot(),
                    idx.page_count(),
                    idx.word_count(),
                    args.max_queue,
                )
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[index] Interrupted — saving state…")
            crawler.stop()
    else:
        try:
            while crawler.is_active():
                s = crawler.stats.snapshot()
                processed = s.get("urls_processed", s.get("pages_indexed", 0))
                queued = s.get("queue_depth", s.get("urls_queued", 0))
                dropped = s.get("urls_dropped_backpressure", s.get("urls_dropped", 0))
                print(
                    f"\r  processed={processed:5d}  "
                    f"queued={queued:4d}  "
                    f"indexed={idx.page_count():5d}  "
                    f"failed={s['urls_failed']:4d}  "
                    f"skipped={s['urls_skipped']:4d}  "
                    f"dropped={dropped:4d}  "
                    f"{'[throttled]' if s['throttled'] else '           '}",
                    end="", flush=True,
                )
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[index] Interrupted — saving state…")
            crawler.stop()

    crawler.wait(timeout=10)
    s = crawler.stats.snapshot()
    processed = s.get("urls_processed", s.get("pages_indexed", 0))
    dropped = s.get("urls_dropped_backpressure", s.get("urls_dropped", 0))
    print(f"\n\n[done]  processed={_fmt(processed)}  "
          f"failed={_fmt(s['urls_failed'])}  "
          f"dropped={_fmt(dropped)}  "
          f"indexed={_fmt(idx.page_count())}")


def cmd_search(args):
    """Search and print results."""
    idx = _make_index()
    results = idx.search_scored(args.query, limit=args.limit)
    if not results:
        print("No results found.")
        return
    limit = args.limit
    print(f"\nResults for '{args.query}'  ({len(results)} total, "
          f"showing {min(limit, len(results))}):\n")
    for i, row in enumerate(results[:limit], 1):
        url, origin, depth, score = row if len(row) == 4 else (*row, 0)
        print(f"  {i:3d}. {url}")
        print(f"       origin={origin}  depth={depth}  score={score}")
    print()


def cmd_status(args):
    """Print index statistics."""
    from storage.database import VisitedDB, SessionDB
    idx = _make_index()
    visited = VisitedDB(path=DB_PATH)
    sessions_db = SessionDB(path=DB_PATH)

    print(f"\n[status] Indexed pages  : {_fmt(idx.page_count())}")
    print(f"[status] Unique words   : {_fmt(idx.word_count())}")
    print(f"[status] Visited URLs   : {_fmt(visited.count())}")
    print(f"[status] Database       : {os.path.abspath(DB_PATH)}")

    recent = idx.recent_pages(5)
    if recent:
        print("\n[status] Recently indexed:")
        for p in recent:
            print(f"           depth={p['depth']}  {p['url']}")

    sessions = sessions_db.list_sessions(5)
    if sessions:
        print("\n[status] Recent crawl sessions:")
        for s in sessions:
            fin = "running" if s["status"] == "running" else f"done ({s.get('pages_indexed', 0)} pages)"
            print(f"           #{s['id']}  {s['origin'][:60]}  depth={s['depth']}  {fin}")
    print()


def cmd_reset(args):
    """Wipe the index."""
    if not args.yes:
        confirm = input("Reset all index data? This cannot be undone. [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return
    idx = _make_index()
    idx.reset()
    print("[reset] Index, visited URLs, and queue state cleared.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-google-v2",
        description="mini-google v2 — Async Web Crawler & Search Engine",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # server
    ps = sub.add_parser("server", help="Start the web UI (default: http://localhost:8080)")
    ps.add_argument("--port", type=int, default=8080)

    # index
    pi = sub.add_parser("index", help="Crawl from a URL to depth k (CLI mode)")
    pi.add_argument("url", help="Origin URL to start crawling from")
    pi.add_argument("depth", type=int, help="Max hop depth (0 = origin page only)")
    pi.add_argument("--workers", type=int, default=10, help="Concurrent fetch workers (default: 10)")
    pi.add_argument("--rate", type=float, default=10.0, help="Max requests/second (default: 10)")
    pi.add_argument("--max-queue", type=int, default=500, help="Max queue size for back-pressure (default: 500)")
    pi.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds (default: 10)")
    pi.add_argument("--dashboard", action="store_true", help="Show live ASCII dashboard")
    pi.add_argument("--all-domains", action="store_true", help="Follow links to any domain (default: same domain only)")

    # search
    psr = sub.add_parser("search", help="Search the index")
    psr.add_argument("query", help="Search query string")
    psr.add_argument("--limit", type=int, default=20, help="Max results to show (default: 20)")

    # status
    sub.add_parser("status", help="Print index statistics")

    # reset
    pr = sub.add_parser("reset", help="Wipe the index and all crawl data")
    pr.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "server": cmd_server,
        "index":  cmd_index,
        "search": cmd_search,
        "status": cmd_status,
        "reset":  cmd_reset,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
