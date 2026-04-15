"""Web UI for Finance Manager — setup wizard + streaming run."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, session, stream_with_context

load_dotenv()  # Load server-side env (ANTHROPIC_API_KEY, etc.)

BASE_DIR = Path(__file__).parent
MANAGER_DIR = BASE_DIR / "manager"

# ── Stable secret key (persists across server restarts) ────────────────────────

_SECRET_KEY_FILE = BASE_DIR / ".flask_secret"


def _get_secret_key() -> bytes:
    if _SECRET_KEY_FILE.exists():
        return _SECRET_KEY_FILE.read_bytes()
    key = os.urandom(32)
    _SECRET_KEY_FILE.write_bytes(key)
    return key


app = Flask(__name__)
app.secret_key = _get_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# Mutable shared state (single-user local tool, no locking needed)
_monarch_pending: dict = {}
_google_auth_status: dict = {"done": False, "error": None}


@app.before_request
def _make_session_permanent():
    session.permanent = True


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(str(BASE_DIR / "server.html"))


@app.route("/api/status")
def api_status():
    return jsonify({
        "monarch_email":   session.get("monarch_email", ""),
        "monarch_session": (MANAGER_DIR / ".monarch_session").exists(),
        "has_csv":         (MANAGER_DIR / ".monarch_transactions").exists(),
        "google_creds":    (MANAGER_DIR / "credentials.json").exists(),
        "google_token":    (MANAGER_DIR / ".gsheets_token.json").exists(),
        "spreadsheet_id":  session.get("spreadsheet_id", ""),
    })


@app.route("/api/monarch/login", methods=["POST"])
def monarch_login():
    from monarchmoney import MonarchMoney, RequireMFAException  # type: ignore

    data = request.json
    email    = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required."})

    async def _try():
        mm = MonarchMoney()
        try:
            await mm.login(email, password)
            mm.save_session(str(MANAGER_DIR / ".monarch_session"))
            return {"status": "ok"}
        except RequireMFAException:
            return {"status": "mfa_required"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    result = asyncio.run(_try())
    # Store pending credentials for MFA step or session
    if result["status"] in ("ok", "mfa_required"):
        _monarch_pending["email"]    = email
        _monarch_pending["password"] = password
    if result["status"] == "ok":
        session["monarch_email"]    = email
        session["monarch_password"] = password
    return jsonify(result)


@app.route("/api/monarch/mfa", methods=["POST"])
def monarch_mfa():
    from monarchmoney import MonarchMoney  # type: ignore

    code     = (request.json.get("code") or "").strip()
    email    = _monarch_pending.get("email")
    password = _monarch_pending.get("password")
    if not email or not code:
        return jsonify({"status": "error", "message": "No pending MFA session."})

    async def _submit():
        mm = MonarchMoney()
        try:
            await mm.multi_factor_authenticate(email, password, code)
            mm.save_session(str(MANAGER_DIR / ".monarch_session"))
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    result = asyncio.run(_submit())
    if result["status"] == "ok":
        session["monarch_email"]    = email
        session["monarch_password"] = password
        _monarch_pending.clear()
    return jsonify(result)


@app.route("/api/monarch/mode", methods=["POST"])
def monarch_mode():
    mode = (request.json.get("mode") or "").strip()
    if mode not in ("login", "csv"):
        return jsonify({"status": "error", "message": "mode must be 'login' or 'csv'."})
    session["monarch_mode"] = mode
    return jsonify({"status": "ok"})


@app.route("/api/transactions/upload", methods=["POST"])
def transactions_upload():
    """Accept a Monarch Money CSV export and store it as the CSV fallback."""
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file received."})
    request.files["file"].save(str(MANAGER_DIR / ".monarch_transactions"))
    return jsonify({"status": "ok"})


@app.route("/api/sheets/save", methods=["POST"])
def sheets_save():
    sid = (request.json.get("spreadsheet_id") or "").strip()
    if not sid:
        return jsonify({"status": "error", "message": "Spreadsheet ID is required."})
    session["spreadsheet_id"] = sid
    return jsonify({"status": "ok"})


@app.route("/api/sheets/create", methods=["POST"])
def sheets_create():
    """Create a new Google Sheet named 'Finance Manager' and return its ID."""
    try:
        import gspread  # type: ignore
        from manager.login import Google
        creds = Google.get_credentials()
        gc = gspread.authorize(creds)
        sh = gc.create("Finance Manager")
        session["spreadsheet_id"] = sh.id
        return jsonify({"status": "ok", "spreadsheet_id": sh.id})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)})


@app.route("/api/google/auth", methods=["POST"])
def google_auth():
    global _google_auth_status
    _google_auth_status = {"done": False, "error": None}

    if not (MANAGER_DIR / "credentials.json").exists():
        return jsonify({"status": "error",
                        "message": "credentials.json not found on server — cannot authorize Google Sheets."})

    def _run_oauth():
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
            flow = InstalledAppFlow.from_client_secrets_file(
                str(MANAGER_DIR / "credentials.json"),
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive.file",
                ],
            )
            creds = flow.run_local_server(port=0)
            (MANAGER_DIR / ".gsheets_token.json").write_text(creds.to_json())
            _google_auth_status["done"] = True
        except Exception as exc:
            _google_auth_status["error"] = str(exc)

    threading.Thread(target=_run_oauth, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/google/auth/status")
def google_auth_status():
    return jsonify(_google_auth_status)


@app.route("/api/run")
def api_run():
    # Capture session values before streaming begins
    env = os.environ.copy()
    if session.get("monarch_mode") == "csv":
        env["USE_CSV"] = "1"
    else:
        if session.get("monarch_email"):
            env["MONARCH_EMAIL"] = session["monarch_email"]
        if session.get("monarch_password"):
            env["MONARCH_PASSWORD"] = session["monarch_password"]
    if session.get("spreadsheet_id"):
        env["SPREADSHEET_ID"] = session["spreadsheet_id"]

    def _generate():
        proc = subprocess.Popen(
            [sys.executable, "-m", "manager.main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(BASE_DIR),
            env=env,
        )
        for line in iter(proc.stdout.readline, ""):
            yield f"data: {json.dumps({'type': 'line', 'text': line.rstrip()})}\n\n"
        proc.wait()
        sid = env.get("SPREADSHEET_ID", "")
        if proc.returncode == 0:
            url = f"https://docs.google.com/spreadsheets/d/{sid}"
            yield f"data: {json.dumps({'type': 'done', 'url': url})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Process exited with code {proc.returncode}'})}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def run() -> None:
    print("Finance Manager running at \033[1mhttp://localhost:5000\033[0m")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    run()
