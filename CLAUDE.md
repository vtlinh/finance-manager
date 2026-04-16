# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                    # Install dependencies
uv run finance-server      # Start Flask web UI at http://localhost:5000
uv run finance-run         # Run analysis pipeline from CLI
cp .env.example .env       # Set up credentials
```

```bash
uv run pytest                          # Run all tests
uv run pytest tests/test_foo.py        # Run a single test file
```

Always write tests for new or changed code and run them before committing.

If running the server or pipeline produces an error, immediately add a test reproducing that error case (unless one already exists), fix it, and confirm the test passes before committing.

## Architecture

**Finance Manager** fetches transactions from Monarch Money (or CSV exports), categorizes merchants with Claude AI, detects spending anomalies, and exports summaries to Google Sheets.

### Pipeline flow (`manager/main.py`)

```
load_transactions → filter/group → categorize with LLM → detect anomalies → export to Sheets
```

### Key modules

| Module | Role |
|--------|------|
| `server.py` | Flask web UI: setup wizard, OAuth flows, SSE streaming of pipeline output |
| `server.html` | Single-page UI for the web interface |
| `manager/main.py` | CLI entry point; async orchestrator calling all pipeline stages |
| `manager/load_transactions.py` | Fetches from Monarch API (year-by-year, deduped by ID) or parses CSV |
| `manager/categorize_transactions.py` | Groups by (month, merchant), calls LLM for hierarchical category paths, consolidates taxonomy |
| `manager/anomaly.py` | Detects >30% month-over-month spending changes by top-level category |
| `manager/export_to_sheets.py` | Writes hierarchical category tree + anomaly notes to per-year Google Sheet tabs |
| `manager/llm.py` | Claude API wrapper with two modes: sequential (dev) or Batches API (prod, ~50% cheaper) |
| `manager/sheets_cache.py` | Stores LLM cache as JSON in hidden `_merchant_cache` / `_anomaly_cache` worksheet tabs |
| `manager/login.py` | Monarch Money and Google OAuth helpers |

### LLM integration

- All calls use `claude-opus-4-6` with structured tool_use for guaranteed JSON output
- `USE_BATCH_LLM=1`: enables Message Batches API (async, polls every 30s) — cheaper for prod
- Without that flag: sequential `llm_structured()` calls (immediate results, useful in dev)
- Categorization sends up to 1,000 merchants per call; results cached in Sheets
- If taxonomy grows beyond 100 unique paths or 15 root categories, a consolidation LLM call normalizes it

### Authentication

**Monarch Money**: Session cached in `.monarch_session` (CLI) or encrypted Flask cookie (web UI). Supports MFA prompts.

**Google**: CLI uses `credentials.json` + `.gsheets_token.json`. Web UI uses OAuth 2.0 redirect flow (cloud) or InstalledAppFlow (local); credentials stored only in encrypted Flask session cookie — never on disk.

**Anthropic**: `ANTHROPIC_API_KEY` env var.

### Web UI design

- The pipeline runs in a **subprocess** spawned by `/api/run`; output streams to the browser via Server-Sent Events
- All user credentials are encrypted in the Flask session cookie (Fernet); server is stateless
- `FLASK_SECRET_KEY` should be a stable hex string in cloud deployments (not regenerated on restart)

### Deployment

`render.yaml` configures Render.com: Python runtime, `uv sync --locked` build, `uv run finance-server` start command. Gunicorn runs with 1 worker / 4 threads / 300s timeout to support SSE streams. On Windows, falls back to Flask dev server.

### Key environment variables

```
ANTHROPIC_API_KEY
MONARCH_EMAIL / MONARCH_PASSWORD
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   # cloud OAuth
SPREADSHEET_ID
FLASK_SECRET_KEY
USE_BATCH_LLM=1                           # enable Batches API
```

See `.env.example` for the full list.
