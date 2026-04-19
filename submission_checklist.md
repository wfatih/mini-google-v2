# Submission Checklist

This checklist maps each assignment requirement to concrete files and implementation points in `mini-google-v2`.

## 1) Core Functional Requirements

- [x] **`index(origin, k)` implemented**
  - `crawler/engine.py`: async BFS crawl, origin seed, depth-limited traversal.
  - `api/server.py`: `POST /api/index`.
  - `main.py`: `index` CLI command.

- [x] **Never crawl same page twice**
  - `storage/database.py`: `visited` table.
  - `VisitedDB.mark_visited()` uses atomic `INSERT OR IGNORE`.
  - `crawler/engine.py`: dedup check before processing URL.

- [x] **Back pressure included**
  - `crawler/engine.py`: `asyncio.Queue(maxsize=...)`.
  - `crawler/engine.py`: `put_nowait()` with drop accounting when queue is full.
  - `crawler/engine.py`: token-bucket rate limiting.
  - UI/API status exposes queue depth and dropped URLs.

- [x] **`search(query)` implemented**
  - `storage/index.py`: `search()` and `search_scored()`.
  - Returns URL + origin + depth (and optional score/rank in API mode).
  - `api/server.py`: `GET /api/search`.
  - `main.py`: `search` CLI command.

- [x] **Search while indexing is active**
  - `storage/database.py`: SQLite WAL (`PRAGMA journal_mode=WAL`).
  - Thread-local DB connections for concurrent reader/writer behavior.
  - `api/server.py` documents concurrent search behavior.

- [x] **Simple UI/CLI for control and visibility**
  - CLI: `main.py` (`index`, `search`, `status`, `reset`).
  - Web UI: `static/index.html` served by FastAPI.
  - Realtime status: `GET /api/events` SSE stream.

- [x] **Resume after interruption (plus requirement)**
  - `storage/database.py`: queue/session persistence tables.
  - `crawler/engine.py`: loads incomplete session snapshot and resumes.

## 2) Multi-Agent Workflow Requirements

- [x] **Agents defined**
  - `agents/01_architect_agent.md`
  - `agents/02_crawler_agent.md`
  - `agents/03_storage_agent.md`
  - `agents/04_api_agent.md`
  - `agents/05_ui_agent.md`
  - `agents/06_qa_agent.md`

- [x] **Responsibilities assigned**
  - Explicitly separated by module ownership in each agent file.

- [x] **Agent interaction and communication described**
  - `multi_agent_workflow.md`: handoffs, conflict resolution, integration flow.

- [x] **Outputs managed and evaluated**
  - `agents/06_qa_agent.md`: validation strategy and issues found.
  - `logs/agent_log.jsonl`: auditable decision and implementation events.

## 3) Required Deliverables

- [x] **PRD**
  - `product_prd.md`

- [x] **Working codebase**
  - `crawler/`, `storage/`, `api/`, `static/`, `main.py`, `requirements.txt`

- [x] **Readme**
  - `readme.md`

- [x] **Recommendation (1–2 paragraphs)**
  - `recommendation.md`

- [x] **Multi-Agent Workflow explanation**
  - `multi_agent_workflow.md`

- [x] **Agent description files**
  - `agents/*.md`

## 4) Quick Verification Commands

Run from project root (`mini-google-v2`):

```bash
python -m compileall .
python main.py status
python main.py server --port 8080
```

Optional quick crawl/search:

```bash
python main.py index https://example.com 1 --workers 5 --rate 5 --max-queue 200
python main.py search "example" --limit 10
```
