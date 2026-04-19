# mini-google-v2

`mini-google-v2` is a localhost search engine prototype with two main operations:

- `index(origin, k)`: crawl from `origin` up to depth `k` with strict deduplication and back-pressure.
- `search(query)`: return relevant triples `(relevant_url, origin_url, depth)` from the indexed corpus.

This implementation is built with a **multi-agent development workflow** (documented in `multi_agent_workflow.md`) and focuses on language-native components for the core logic.

## Features

- Async BFS crawl engine (`asyncio`) with bounded queue back-pressure.
- Rate limiting with token-bucket logic.
- Never-crawl-twice behavior via persistent `visited` store.
- SQLite (WAL mode) for concurrent indexing and search on one machine.
- Real-time status and progress through API and SSE stream.
- Web UI (`/`) + CLI commands.
- Session tracking and queue snapshot persistence for interruption recovery.
- Structured JSONL agent logs in `logs/agent_log.jsonl`.

## Project Structure

- `crawler/`: crawler engine and HTML/text parsers.
- `storage/`: SQLite schema, session/visited stores, inverted index and scoring.
- `api/`: FastAPI server, REST endpoints, SSE event stream.
- `static/`: browser UI.
- `agents/`: agent descriptions/prompts/decisions per role.
- `logs/agent_log.jsonl`: machine-readable multi-agent execution log.
- `product_prd.md`: build-ready PRD for AI implementation.
- `recommendation.md`: production deployment recommendations.
- `multi_agent_workflow.md`: agent orchestration and evaluation details.

## Requirements

- Python 3.11+ recommended
- Pip

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

Start web app:

```bash
python main.py server --port 8080
```

Then open: [http://localhost:8080](http://localhost:8080)

## CLI Usage

Start crawl:

```bash
python main.py index https://example.com 2 --workers 10 --rate 10 --max-queue 500
```

Search:

```bash
python main.py search "example query" --limit 20
```

Status:

```bash
python main.py status
```

Reset index:

```bash
python main.py reset --yes
```

## API Summary

- `POST /api/index`
- `POST /api/stop`
- `POST /api/pause`
- `POST /api/resume`
- `GET /api/search?query=...&limit=...`
- `GET /api/status`
- `GET /api/events` (SSE)
- `GET /api/sessions`
- `GET /api/sessions/{session_id}`
- `GET /api/pages/recent`
- `DELETE /api/reset`

## Notes on Concurrent Search While Indexing

The system is designed so search can run while indexing is active:

- SQLite WAL mode allows readers during writes.
- Each thread uses its own connection (`threading.local`) to avoid contention bugs.
- Pages are committed incrementally, so new documents appear in search soon after indexing.

## Logging and Consistency

Agent process logging uses one JSON object per line in `logs/agent_log.jsonl` with common fields:

- `timestamp` (ISO 8601)
- `agent` (architect/storage/crawler/api/ui/qa/integration)
- `action` (decision, implementation_complete, test_result, etc.)
- optional payload (`files`, `notes`, `key_decisions`, `errors`)

This provides an auditable, replayable implementation trace for evaluation.
