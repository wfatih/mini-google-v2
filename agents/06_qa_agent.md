# Agent 06 — QA Agent

## Mission

Validate functional behavior and integration quality across crawler, storage, API, UI, and CLI.

## Scope

- Smoke tests for CLI commands.
- API/server startup verification.
- Search/index interaction checks.
- Detection of contract mismatches between modules.

## Test Strategy

1. **Syntax pass**
   - `python -m compileall mini-google-v2`
2. **CLI health**
   - `python main.py status`
   - `python main.py reset --yes`
3. **Integration smoke**
   - start short crawl
   - run search query
4. **Regression checks**
   - verify no runtime key errors
   - verify constructor and signatures align across modules

## Findings and Fix Requests

- Found constructor mismatch:
  - Caller used `InvertedIndex(db_path=...)`
  - Class lacked explicit matching constructor.
  - Fix: add `__init__(self, db_path)` to `storage/index.py`.

- Found stats key mismatch:
  - CLI expected `urls_processed`, `queue_depth`, `urls_dropped_backpressure`
  - Crawler snapshot exposed `pages_indexed`, `urls_queued`, `urls_dropped`.
  - Fix: normalize CLI with fallback mapping in `main.py`.

## Exit Criteria

- No syntax errors.
- CLI status command runs without exceptions.
- End-to-end commands execute without integration crashes.
- Documentation updated with known environment constraints.
