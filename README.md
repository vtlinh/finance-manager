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
MONARCH_EMAIL=your@email.com
MONARCH_PASSWORD=yourpassword
ANTHROPIC_API_KEY=your-anthropic-api-key
SPREADSHEET_ID=your-google-spreadsheet-id
```

### Monarch Money credentials

Use the same email and password you log in to [monarchmoney.com](https://monarchmoney.com) with. Monarch Money requires MFA — you will be prompted for a code on first run. The session is then cached in `.monarch_session` so you won't be asked again until it expires.

### Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **API Keys** → **Create Key**
3. Copy the key into your `.env`

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

3. Paste it as `SPREADSHEET_ID` in your `.env`

### 3d. Authorize on first run

The first time the script connects to Google Sheets, a browser window will open asking you to sign in and grant access. After that, the token is cached in `.gsheets_token.json` and future runs will not prompt again.

---

## 4. Run

### Option A — Web UI (recommended)

```bash
uv run server.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser. The setup wizard walks you through each configuration step (Monarch login, Anthropic key, Google OAuth, Spreadsheet ID) and then streams the run output live.

### Option B — Command line

```bash
uv run main.py
```

The script will:

1. Fetch every transactions from Monarch Money
2. Remove transfer pairs (e.g. credit card payments)
3. Categorize each merchant using Claude AI (cached in `.merchant_cache.json`)
4. Consolidate categories if there are more than 100 unique paths or 15 root categories
5. Detect spending anomalies month-over-month using Claude AI (cached in `.anomaly_cache.json`)
6. Print a spending summary to the terminal
7. Export per-year **Spending** and **Anomalies** tabs to your Google Sheet

---

## Cache files

| File | Purpose |
|---|---|
| `.merchant_cache.json` | Merchant → category path mappings (avoids redundant LLM calls) |
| `.anomaly_cache.json` | Month → anomaly notes (avoids redundant LLM calls) |
| `.monarch_session` | Saved Monarch Money login session |
| `.gsheets_token.json` | Saved Google OAuth token |

Delete any of these files to force a fresh fetch on the next run.
