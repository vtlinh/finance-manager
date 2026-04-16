# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                    # Install dependencies
uv run finance-server      # Start Flask web UI at http://localhost:5000
uv run finance-run         # Run analysis pipeline from CLI
cp .env.example .env       # Set up credentials
```

## Workflow

After completing any code change:
1. `/test` — write tests for any new or changed behaviour (do NOT run them locally; GitHub Actions runs them on push)
2. `/push` — commit and push to GitHub (CI will run tests automatically)
3. `/deploy` — deploy to Render once CI passes

Use `/finalize` to do all three in one go.

If running the server or pipeline produces an error, add a test reproducing the error case (unless one already exists) before pushing.

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

**Render service**: https://finance-manager-g4ao.onrender.com  
**Render dashboard**: https://dashboard.render.com/web/srv-d7gelfm47okc73fkall0  
**Render API key**: rnd_uuPdtff3XGvFk0pHjvLTAxaBfHoG  
**Render service ID**: srv-d7gelfm47okc73fkall0

After pushing to GitHub, if the changes include any user-visible UI or behavior changes, trigger a Render deploy:

```bash
curl -s -X POST "https://api.render.com/v1/services/srv-d7gelfm47okc73fkall0/deploys" \
  -H "Authorization: Bearer rnd_uuPdtff3XGvFk0pHjvLTAxaBfHoG" \
  -H "Content-Type: application/json" \
  -d '{"clearCache": "do_not_clear"}'
```

(Render also auto-deploys on every push, so this is mainly to ensure the deploy is triggered promptly for user-facing changes.)

### Key environment variables

```
ANTHROPIC_API_KEY
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   # cloud OAuth
FLASK_SECRET_KEY
USE_BATCH_LLM=1                           # enable Batches API
```

See `.env.example` for the full list.

## Change Log

After completing any task or making any change to the codebase, append a brief entry to the `## Change Log` section below in this format:

```
- YYYY-MM-DD: <short description of what was requested and what changed>
```

After pushing to GitHub, re-read this file and compact the Change Log: merge closely related entries, remove redundant detail, and keep each line under ~120 characters. The goal is a concise running history, not a verbose log.

### Entries

- 2026-04-16: Added Change Log section to CLAUDE.md; removed render.yaml and .debug from git tracking; tightened .gitignore
- 2026-04-16: Restructured manager/ into focused submodules (transactions/, sheets/, categorize.py, types.py); added /finalize, /structure, /test, /push, /deploy skills
- 2026-04-16: retry_on_quota decorator on gspread writes (silent 429 retry); Summary tab title/insights shifted to column B; anomaly print on separate lines; step-transition spinner fix
