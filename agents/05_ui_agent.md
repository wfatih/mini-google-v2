# Agent 05 — UI Agent

## Mission

Build a simple but practical localhost UI to:

- start indexing jobs,
- perform search queries,
- observe live crawler state (progress, queue depth, throttling/back-pressure).

## Inputs

- API contracts from Architect and API agents.
- Endpoints in `api/server.py`.
- Realtime stream `GET /api/events` (SSE).

## Owned Files

- `static/index.html`

## Design Decisions

- Vanilla HTML/CSS/JS single file for zero build-step and offline localhost use.
- EventSource (SSE) for live status without polling-heavy loops.
- Minimal form-based controls to keep behavior transparent for grading.

## Acceptance Checklist

- [x] Can start index with URL + depth.
- [x] Can search query and list triples.
- [x] Can display active/paused state, queue depth, failures, dropped URLs.
- [x] Updates refresh while crawl is running.
- [x] No framework dependency required.

## Handoff Notes

- UI relies on stable JSON keys from `/api/status` and `/api/search`.
- If API response format changes, this file is the first integration surface to update.
