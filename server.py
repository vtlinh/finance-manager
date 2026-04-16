"""Web UI for Finance Manager — setup wizard + streaming run."""
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_file, session, stream_with_context

load_dotenv()

BASE_DIR = Path(__file__).parent
MANAGER_DIR = BASE_DIR / "manager"

# ── Secret key ─────────────────────────────────────────────────────────────────

_SECRET_KEY_FILE = BASE_DIR / ".flask_secret"


def _get_secret_key() -> bytes:
    # Cloud deployments: set FLASK_SECRET_KEY to a stable hex string so sessions
    # survive restarts on ephemeral filesystems (e.g. Render free tier).
    # Generate once with: python -c "import secrets; print(secrets.token_hex(32))"
    key_hex = os.environ.get("FLASK_SECRET_KEY")
    if key_hex:
        return bytes.fromhex(key_hex)
    # Local dev fallback: persist to file so sessions survive server restarts
    if _SECRET_KEY_FILE.exists():
        return _SECRET_KEY_FILE.read_bytes()
    key = os.urandom(32)
    _SECRET_KEY_FILE.write_bytes(key)
    return key


_SECRET_KEY = _get_secret_key()

app = Flask(__name__)
app.secret_key = _SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


# ── Fernet encryption for sensitive session cookie values ──────────────────────

def _make_fernet() -> Fernet:
    """Derive a Fernet key from the Flask secret key."""
    key = base64.urlsafe_b64encode(hashlib.sha256(_SECRET_KEY).digest())
    return Fernet(key)


def _enc(plaintext: str) -> str:
    """Encrypt a string for storage in a session cookie."""
    if not plaintext:
        return ""
    return _make_fernet().encrypt(plaintext.encode()).decode()


def _dec(ciphertext: str) -> str:
    """Decrypt a session cookie value. Returns empty string on failure."""
    if not ciphertext:
        return ""
    try:
        return _make_fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):
        return ""


# ── Monarch session helper ─────────────────────────────────────────────────────

def _get_monarch_session_bytes(mm) -> str:
    """Serialize a MonarchMoney session to a base64 string without persisting to disk.

    mm.save_session writes binary (pickle) data, not text. We base64-encode the raw
    bytes so the result is safe ASCII with no embedded null characters.
    """
    with tempfile.NamedTemporaryFile(delete=False) as f:
        tmp_path = f.name
    try:
        mm.save_session(tmp_path)
        return base64.b64encode(Path(tmp_path).read_bytes()).decode("ascii")
    finally:
        os.unlink(tmp_path)


# ── Google OAuth helpers ───────────────────────────────────────────────────────

_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# In-memory holder for the local-dev OAuth result — not user data, just a
# transient status flag that lives only until the polling endpoint consumes it.
_local_oauth_result: dict = {"token_json": None, "error": None, "done": False}


def _callback_url() -> str:
    base = os.environ.get("APP_URL", request.host_url.rstrip("/"))
    return f"{base}/api/google/callback"


@app.before_request
def _make_session_permanent():
    session.permanent = True


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(str(BASE_DIR / "server.html"))


@app.route("/api/status")
def api_status():
    google_configured = bool(
        (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
        or (MANAGER_DIR / "credentials.json").exists()
    )
    return jsonify({
        "monarch_email":      session.get("monarch_email", ""),
        "monarch_session":    bool(session.get("monarch_session")),
        "has_csv":            bool(session.get("csv_path") and os.path.exists(session["csv_path"])),
        "google_configured":  google_configured,
        "google_token":       bool(session.get("google_token")),
        "spreadsheet_id":     session.get("spreadsheet_id", ""),
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
            return {"status": "ok", "mm": mm}
        except RequireMFAException:
            return {"status": "mfa_required", "mm": mm}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    result = asyncio.run(_try())
    mm = result.pop("mm", None)

    if result["status"] == "ok" and mm:
        session["monarch_session"] = _enc(_get_monarch_session_bytes(mm))
        session["monarch_email"] = email

    if result["status"] == "mfa_required":
        # Store MFA credentials in encrypted cookie — never touches disk
        session["monarch_mfa_email"] = email
        session["monarch_mfa_password"] = _enc(password)

    return jsonify(result)


@app.route("/api/monarch/mfa", methods=["POST"])
def monarch_mfa():
    from monarchmoney import MonarchMoney  # type: ignore

    code     = (request.json.get("code") or "").strip()
    email    = session.get("monarch_mfa_email")
    password = _dec(session.get("monarch_mfa_password", ""))

    if not email or not code:
        return jsonify({"status": "error", "message": "No pending MFA session."})

    async def _submit():
        mm = MonarchMoney()
        try:
            await mm.multi_factor_authenticate(email, password, code)
            return {"status": "ok", "mm": mm}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    result = asyncio.run(_submit())
    mm = result.pop("mm", None)

    if result["status"] == "ok" and mm:
        session["monarch_session"] = _enc(_get_monarch_session_bytes(mm))
        session["monarch_email"] = email
        session.pop("monarch_mfa_email", None)
        session.pop("monarch_mfa_password", None)

    return jsonify(result)


@app.route("/api/monarch/validate", methods=["POST"])
def monarch_validate():
    """Test the stored Monarch session. Clears it from the cookie if expired."""
    from monarchmoney import MonarchMoney  # type: ignore

    session_b64 = _dec(session.get("monarch_session", ""))
    if not session_b64:
        return jsonify({"status": "error", "message": "No session stored."})

    async def _test():
        mm = MonarchMoney()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(base64.b64decode(session_b64))
            tmp = f.name
        try:
            mm.load_session(tmp)
            await mm.get_accounts()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    result = asyncio.run(_test())
    if result["status"] != "ok":
        session.pop("monarch_session", None)
        session.pop("monarch_email", None)
    return jsonify(result)


@app.route("/api/monarch/mode", methods=["POST"])
def monarch_mode():
    mode = (request.json.get("mode") or "").strip()
    if mode not in ("login", "csv"):
        return jsonify({"status": "error", "message": "mode must be 'login' or 'csv'."})
    session["monarch_mode"] = mode
    return jsonify({"status": "ok"})


_CSV_REQUIRED_COLUMNS = {"Date", "Merchant", "Amount", "Category"}


@app.route("/api/transactions/upload", methods=["POST"])
def transactions_upload():
    """Accept a Monarch Money CSV export, validate it, and store it in a session-scoped temp file."""
    import csv as _csv

    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file received."})

    fd, csv_path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    request.files["file"].save(csv_path)

    error = None
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = _csv.DictReader(f)
            missing = _CSV_REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                error = f"Missing columns: {', '.join(sorted(missing))}"
            elif next(reader, None) is None:
                error = "The file contains no transactions."
    except Exception as exc:
        error = f"Could not read file: {exc}"

    if error:
        try:
            os.unlink(csv_path)
        except OSError:
            pass
        return jsonify({"status": "error", "message": error})

    # Remove previous upload for this session
    old_path = session.get("csv_path")
    if old_path:
        try:
            os.unlink(old_path)
        except OSError:
            pass
    session["csv_path"] = csv_path
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
    google_token = _dec(session.get("google_token", ""))
    if not google_token:
        return jsonify({"status": "error", "message": "Not authenticated with Google."})
    try:
        import gspread  # type: ignore
        from manager.login import Google
        creds = Google.get_credentials(token_json=google_token)
        gc = gspread.authorize(creds)
        sh = gc.create("Finance Manager")
        session["spreadsheet_id"] = sh.id
        # Persist refreshed token back to the cookie
        session["google_token"] = _enc(creds.to_json())
        return jsonify({"status": "ok", "spreadsheet_id": sh.id})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)})


@app.route("/api/google/auth", methods=["POST"])
def google_auth():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

    if client_id and client_secret:
        # Cloud deployment: web redirect flow — no browser opened on the server
        from google_auth_oauthlib.flow import Flow  # type: ignore
        flow = Flow.from_client_config(
            {"web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": [_callback_url()],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }},
            scopes=_GOOGLE_SCOPES,
            redirect_uri=_callback_url(),
        )
        import hashlib, secrets
        code_verifier = secrets.token_urlsafe(96)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        auth_url, state = flow.authorization_url(
            prompt="consent",
            access_type="offline",
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        session["google_oauth_state"] = state
        session["google_code_verifier"] = code_verifier
        return jsonify({"status": "redirect", "url": auth_url})

    # Local dev fallback: InstalledAppFlow opens a browser on the local machine
    if not (MANAGER_DIR / "credentials.json").exists():
        return jsonify({
            "status": "error",
            "message": (
                "Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars, "
                "or place credentials.json in the manager/ directory."
            ),
        })

    _local_oauth_result.update({"token_json": None, "error": None, "done": False})

    def _run_oauth():
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
            flow = InstalledAppFlow.from_client_secrets_file(
                str(MANAGER_DIR / "credentials.json"), scopes=_GOOGLE_SCOPES,
            )
            creds = flow.run_local_server(port=0)
            _local_oauth_result["token_json"] = creds.to_json()
            _local_oauth_result["done"] = True
        except Exception as exc:
            _local_oauth_result["error"] = str(exc)
            _local_oauth_result["done"] = True

    threading.Thread(target=_run_oauth, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/google/auth/status")
def google_auth_status():
    """Poll endpoint for the local-dev OAuth flow."""
    if not _local_oauth_result["done"]:
        return jsonify({"done": False, "error": None})
    token_json = _local_oauth_result.get("token_json")
    error = _local_oauth_result.get("error")
    if token_json:
        session["google_token"] = _enc(token_json)
    # Reset so the next auth attempt starts clean
    _local_oauth_result.update({"token_json": None, "error": None, "done": False})
    return jsonify({"done": True, "error": error})


@app.route("/api/google/callback")
def google_callback():
    """OAuth2 callback for cloud deployment redirect flow."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    state = session.get("google_oauth_state")

    if not state or not client_id or not client_secret:
        return "OAuth state or credentials missing.", 400

    from google_auth_oauthlib.flow import Flow  # type: ignore
    flow = Flow.from_client_config(
        {"web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [_callback_url()],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=_GOOGLE_SCOPES,
        state=state,
        redirect_uri=_callback_url(),
    )
    auth_response = request.url.replace("http://", "https://", 1)
    code_verifier = session.pop("google_code_verifier", None)
    flow.fetch_token(authorization_response=auth_response, code_verifier=code_verifier)
    creds = flow.credentials
    session["google_token"] = _enc(creds.to_json())
    session.pop("google_oauth_state", None)
    # If opened in a popup, signal the opener and close; otherwise redirect to main page.
    return """<!doctype html><html><head><title>Authorized</title></head><body>
<p>Authorization complete — you may close this window.</p>
<script>
if (window.opener) {
  window.opener.postMessage("google_auth_done", location.origin);
  window.close();
} else {
  location.href = "/";
}
</script></body></html>"""


@app.route("/api/run")
def api_run():
    # Collect credentials into a temp JSON file instead of env vars.
    # Windows subprocess rejects env var values that contain \x00 (embedded null),
    # and large token strings can trigger this unpredictably.
    config: dict[str, str] = {}
    if session.get("monarch_mode") == "csv":
        config["USE_CSV"] = "1"
        csv_path = session.get("csv_path", "")
        if csv_path:
            config["MONARCH_CSV_PATH"] = csv_path
    else:
        monarch_session = _dec(session.get("monarch_session", ""))
        if monarch_session:
            config["MONARCH_SESSION_JSON"] = monarch_session

    google_token = _dec(session.get("google_token", ""))
    if google_token:
        config["GOOGLE_TOKEN_JSON"] = google_token

    if session.get("spreadsheet_id"):
        config["SPREADSHEET_ID"] = session["spreadsheet_id"]

    config["USE_BATCH_LLM"] = "1"

    cfg_fd, cfg_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(cfg_fd, "w", encoding="utf-8") as cfg_f:
        json.dump(config, cfg_f)

    # Pass only safe short strings in the env — credentials stay in the file.
    env = {k: v for k, v in os.environ.items()
           if isinstance(k, str) and isinstance(v, str)
           and "\x00" not in k and "\x00" not in v}
    env["FINANCE_CONFIG"] = cfg_path
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    def _generate():
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "manager.main"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=str(BASE_DIR),
                env=env,
            )
        except Exception as exc:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
            yield f"data: {json.dumps({'type': 'error', 'text': f'Failed to start process: {exc}'})}\n\n"
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                text = line.rstrip("\n")
                if text.startswith("\r"):
                    yield f"data: {json.dumps({'type': 'update_last', 'text': text[1:]})}\n\n"
                elif text.strip():
                    yield f"data: {json.dumps({'type': 'line', 'text': text.rstrip()})}\n\n"
            proc.wait()
        except Exception as exc:
            proc.kill()
            yield f"data: {json.dumps({'type': 'error', 'text': f'Stream error: {exc}'})}\n\n"
            return
        finally:
            # Subprocess deletes cfg_path on startup; clean up here if it didn't.
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
        sid = config.get("SPREADSHEET_ID", "")
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
    port = int(os.environ.get("PORT", 5000))
    print(f"Finance Manager running at \033[1mhttp://localhost:{port}\033[0m")
    try:
        import gunicorn.app.base  # type: ignore

        class _StandaloneApp(gunicorn.app.base.BaseApplication):
            def load_config(self):
                self.cfg.set("bind", f"0.0.0.0:{port}")
                self.cfg.set("workers", 1)
                self.cfg.set("worker_class", "gthread")
                self.cfg.set("threads", 4)
                self.cfg.set("timeout", 300)  # long-running SSE streams

            def load(self):
                return app

        _StandaloneApp().run()
    except ImportError:
        # Gunicorn not available (e.g. Windows dev machine) — fall back to Flask dev server
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    run()
