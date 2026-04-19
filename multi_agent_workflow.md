# Multi-Agent Workflow

This project was delivered with a role-based multi-agent workflow. The runtime system itself is a normal Python application; the **development process** is where agent collaboration happened.

## Agent Roles

1. **Architect Agent**
   - Produced the end-to-end technical plan and PRD.
   - Defined interfaces, constraints, and API contracts.
   - Set non-negotiable requirements: stdlib-first crawler logic, bounded queue back-pressure, WAL-enabled concurrent search/indexing.

2. **Storage Agent**
   - Implemented SQLite schema and migrations in `storage/database.py`.
   - Implemented inverted index and relevance scoring in `storage/index.py`.
   - Ensured thread-safe concurrent reads/writes with thread-local SQLite connections.

3. **Crawler Agent**
   - Implemented async BFS crawler in `crawler/engine.py`.
   - Implemented text and link parsing in `crawler/parser.py`.
   - Implemented dedup, bounded frontier, rate limiting, pause/resume/stop, queue snapshot support.

4. **API Agent**
   - Implemented FastAPI endpoints and SSE in `api/server.py`.
   - Integrated crawler lifecycle controls and status/search APIs.
   - Exposed browseable API and static UI hosting.

5. **UI Agent**
   - Implemented dashboard-style UI in `static/index.html`.
   - Added indexing control panel, search panel, and live status widgets.
   - Consumed REST + SSE for near-real-time updates.

6. **QA Agent**
   - Performed smoke tests and integration checks.
   - Identified integration mismatches (constructor signature and stat-key mismatch).
   - Triggered fixes and re-validation.

7. **Integrator (Coordinator)**
   - Sequenced agents, resolved cross-module incompatibilities, and finalized deliverables.
   - Standardized logging discipline and documentation quality.

## Interaction Pattern

The workflow was iterative, with explicit handoffs:

- Architect -> Storage/Crawler/API/UI with interfaces and assumptions.
- Storage and Crawler implemented in parallel.
- API consumed crawler/storage interfaces and added control plane.
- UI consumed API contracts.
- QA tested integrated behavior and opened issues.
- Integrator applied fixes and reran checks.

No agent had final authority alone; the Integrator accepted/rejected outputs and reconciled conflicts.

## Prompt/Decision Discipline

Each agent prompt contained:

- Inputs (requirements + prior outputs)
- Scope boundaries (files owned by that agent)
- Constraints (stdlib-first core logic, no out-of-box crawler/search frameworks)
- Deliverables (code + agent markdown + log entries)
- Validation checklist

Decision examples:

- `asyncio + ThreadPoolExecutor` selected over full-threading for controlled concurrency.
- `urllib` selected over high-level crawl libraries for language-native constraint.
- `SQLite WAL + thread-local connections` selected for concurrent search while indexing.
- `SSE` selected over WebSockets for one-way realtime status streaming.

## Logging System

All agent actions were recorded in `logs/agent_log.jsonl`.

### Log Schema

Required fields:

- `timestamp`: ISO 8601 UTC timestamp
- `agent`: role name (`architect`, `storage`, `crawler`, `api`, `ui`, `qa`, `integration`)
- `action`: event type (`session_start`, `decision_made`, `implementation_complete`, `test_result`, `bug_fix`, `finalization`)

Optional context fields:

- `files`, `notes`, `key_decisions`, `errors`, `metrics`, `handoff_to`

This schema gives a consistent and auditable development trace.

## How Search Works During Active Indexing

Current design supports concurrent indexing and search by:

- committing page-level index writes incrementally,
- using SQLite WAL mode so readers are not blocked by active writer commits,
- isolating DB connections per thread.

For stronger production semantics, add committed index checkpoints and return checkpoint metadata with search responses (detailed in `recommendation.md`).

## Evaluation of Agent Outputs

Accepted outputs had to satisfy:

- Interface compatibility with existing modules.
- Functional completeness against task requirements.
- No direct dependency on full-featured crawl/search frameworks.
- Clear operational visibility (status, queue depth, throttling).
- Proper documentation and reproducibility.

Rejected/fixed outputs:

- `InvertedIndex` constructor mismatch discovered in CLI status test.
- Stats key mismatch between crawler snapshot and CLI renderer.

Both were fixed before final delivery.
