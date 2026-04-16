# Finance Manager

Pulls transactions from Monarch Money, categorizes merchants with Claude AI, detects spending anomalies, and exports a summary to Google Sheets.

## Prerequisites

- Python 3.11+
- A [Monarch Money](https://monarchmoney.com) account
- An [Anthropic](https://console.anthropic.com) API key
- A Google account

---

## 1. Install dependencies

```bash
uv sync
```

---

## 2. Configure environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and set:

```
ANTHROPIC_API_KEY=your-anthropic-api-key
```

### Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **API Keys** → **Create Key**
3. Copy the key into your `.env`

### Monarch Money credentials

Enter your Monarch Money email and password when prompted by the CLI, or through the web UI setup wizard. Monarch Money requires MFA — you will be prompted for a code on first run. The session is then cached so you won't be asked again until it expires.

---

## 3. Set up Google Sheets

### 3a. Create a Google Cloud project and enable the Sheets API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or select an existing one)
3. Go to **APIs & Services → Library**, search for **Google Sheets API**, and click **Enable**

### 3b. Create OAuth 2.0 credentials

1. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. If prompted, configure the consent screen: choose **External**, enter any app name, and save
3. For application type, select **Desktop app** and click **Create**
4. Click **Download JSON** and save the file as `credentials.json` in this folder

### 3c. Create a Google Sheet and get its ID

1. Go to [sheets.google.com](https://sheets.google.com) and create a new blank spreadsheet
2. Copy the spreadsheet ID from the URL — it is the long string between `/d/` and `/edit`:

   ```
   https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/edit
                                          ↑ this part ↑
   ```

3. Enter it when prompted by the web UI setup wizard or the CLI

### 3d. Authorize on first run

The first time the script connects to Google Sheets, a browser window will open asking you to sign in and grant access. After that, the token is cached in `.gsheets_token.json` and future runs will not prompt again.

---

## 4. Run

### Option A — Web UI (recommended)

```bash
uv run finance-server
```

Open [http://localhost:5000](http://localhost:5000) in your browser. The setup wizard has two steps:

1. **Monarch Money** — sign in (session is validated before proceeding), or upload a CSV export instead
2. **Spreadsheet** — enter an existing Google Sheet ID or create a new one

Clicking **Run** starts the analysis immediately and streams output live.

### Option B — Command line

```bash
uv run finance-run
```

The script will:

1. Fetch all transactions from Monarch Money (or read from an offline CSV)
2. Remove transfer pairs (e.g. credit card payments)
3. Categorize merchants using Claude AI in batches of up to 1000 per call
4. Consolidate categories if there are more than 100 unique paths or 15 root categories
5. Detect spending anomalies month-over-month using Claude AI
6. Export per-year **Spending** and **Summary** tabs to your Google Sheet

---

## Cache files

LLM results (merchant categories and monthly anomalies) are cached in hidden worksheet tabs (`_merchant_cache`, `_anomaly_cache`) inside the target Google Sheet, so they are per-user and require no server-side storage.

For CLI use, two local files are created the first time you run:

| File | Purpose |
|---|---|
| `manager/.monarch_session` | Saved Monarch Money login session |
| `manager/.gsheets_token.json` | Saved Google OAuth token |

Delete either file to force re-authentication on the next run.
