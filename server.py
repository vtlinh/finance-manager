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
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_file, session, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv(override=True)

# Local dev only: allow http:// on OAuth redirect URIs. Render sets APP_URL to
# the https:// URL, so its presence signals production where HTTPS is required.
if not os.environ.get("APP_URL"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

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

# Trust X-Forwarded-Proto from Render's proxy so request.url reports the real
# scheme (https on Render, http on localhost). Without this, Flask sees http
# behind Render's TLS terminator and OAuth redirect URIs get mangled.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

_run_proc: subprocess.Popen | None = None
_run_stopped: bool = False

# ── Persistent async event loop ────────────────────────────────────────────────
# asyncio.run() creates+destroys an event loop per call, which abruptly closes
# TCP connections and triggers 429s from the Monarch API. Instead we keep one
# loop alive for the lifetime of the process — exactly like the asyncio REPL.

_async_loop: asyncio.AbstractEventLoop | None = None
_async_loop_lock = threading.Lock()


def _get_async_loop() -> asyncio.AbstractEventLoop:
    global _async_loop
    with _async_loop_lock:
        if _async_loop is None or not _async_loop.is_running():
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, daemon=True, name="async-loop")
            t.start()
            _async_loop = loop
    return _async_loop


def run_async(coro):
    """Submit a coroutine to the persistent event loop and block until done."""
    future = asyncio.run_coroutine_threadsafe(coro, _get_async_loop())
    return future.result()


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


# ── Google Drive helpers ───────────────────────────────────────────────────────

def _untrash_spreadsheet(spreadsheet_id: str, google_token: str) -> bool:
    """If the spreadsheet is in Drive trash, restore it. Returns True if it was
    restored, False otherwise (including on any error — best-effort)."""
    try:
        from googleapiclient.discovery import build  # type: ignore
        from manager.login import Google
        creds = Google.get_credentials(token_json=google_token)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        meta = drive.files().get(fileId=spreadsheet_id, fields="trashed").execute()
        if meta.get("trashed"):
            drive.files().update(fileId=spreadsheet_id, body={"trashed": False}).execute()
            return True
    except Exception:
        pass
    return False


# ── Google OAuth helpers ───────────────────────────────────────────────────────

_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

def _callback_url() -> str:
    base = os.environ.get("APP_URL", request.host_url.rstrip("/"))
    return f"{base}/api/google/callback"


# ── Error store ────────────────────────────────────────────────────────────────

_error_log: deque = deque(maxlen=50)
# Derive a stable read key from the Flask secret so no extra env var is needed
_ERROR_KEY = hashlib.sha256(_SECRET_KEY).hexdigest()[:24]


def _capture_error(exc: Exception) -> None:
    _error_log.appendleft({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "path": request.path,
        "method": request.method,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    })


@app.errorhandler(Exception)
def _handle_unhandled(exc: Exception):
    _capture_error(exc)
    return jsonify({"error": str(exc)}), 500


@app.route("/api/errors")
def api_errors():
    if request.args.get("key") != _ERROR_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    since = request.args.get("since")
    errors = list(_error_log)
    if since:
        errors = [e for e in errors if e["timestamp"] > since]
    return jsonify({"key": _ERROR_KEY, "errors": errors, "endpoint": request.host_url.rstrip("/") + "/api/errors"})


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
    mfa      = (data.get("mfa") or "").strip() or None
    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required."})

    async def _try():
        mm = MonarchMoney()
        if mfa:
            # MFA code provided upfront — skip login() and authenticate directly
            try:
                await mm.multi_factor_authenticate(email, password, mfa)
                return {"status": "ok", "mm": mm}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        try:
            await mm.login(email, password, use_saved_session=False, save_session=False)
            return {"status": "ok", "mm": mm}
        except RequireMFAException:
            return {"status": "mfa_required", "mm": mm}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    result = run_async(_try())
    mm = result.pop("mm", None)

    if result["status"] == "ok" and mm:
        session["monarch_session"] = _enc(_get_monarch_session_bytes(mm))
        session["monarch_email"] = email

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

    result = run_async(_test())
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


@app.route("/api/sheets/check", methods=["POST"])
def sheets_check():
    """Verify the stored spreadsheet ID still exists; clear it only on a
    definitive not-found. Auth/network errors are treated as "assume exists"
    so a stale Google session doesn't make the user re-create their sheet."""
    sid = session.get("spreadsheet_id", "")
    if not sid:
        return jsonify({"status": "missing"})
    google_token = _dec(session.get("google_token", ""))
    if not google_token:
        return jsonify({"status": "auth_expired", "spreadsheet_id": sid})
    try:
        import gspread  # type: ignore
        from manager.login import Google
        creds = Google.get_credentials(token_json=google_token)
        gc = gspread.authorize(creds)
        gc.open_by_key(sid)
        return jsonify({"status": "ok", "spreadsheet_id": sid})
    except gspread.exceptions.SpreadsheetNotFound:
        session.pop("spreadsheet_id", None)
        return jsonify({"status": "not_found"})
    except Exception:
        # Token expired, network blip, etc — assume the sheet still exists.
        return jsonify({"status": "auth_expired", "spreadsheet_id": sid})


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


def _google_client_creds() -> tuple[str | None, str | None]:
    """Resolve client_id/client_secret from env vars, falling back to the 'web'
    section of manager/credentials.json for local development."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret
    creds_file = MANAGER_DIR / "credentials.json"
    if creds_file.exists():
        data = json.loads(creds_file.read_text())
        section = data.get("web") or data.get("installed") or {}
        return section.get("client_id"), section.get("client_secret")
    return None, None


@app.route("/api/google/auth", methods=["POST"])
def google_auth():
    """Start the OAuth web redirect flow. Works identically in local dev and on
    Render — client_id/client_secret come from env vars or credentials.json,
    and the callback URL is always <host>/api/google/callback."""
    client_id, client_secret = _google_client_creds()
    if not (client_id and client_secret):
        return jsonify({
            "status": "error",
            "message": (
                "Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars, "
                "or place credentials.json in the manager/ directory."
            ),
        })

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


@app.route("/api/google/callback")
def google_callback():
    """OAuth2 callback — used for both local dev and Render."""
    client_id, client_secret = _google_client_creds()
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
    code_verifier = session.pop("google_code_verifier", None)
    flow.fetch_token(authorization_response=request.url, code_verifier=code_verifier)
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

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        config["ANTHROPIC_API_KEY"] = anthropic_key

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
        global _run_proc, _run_stopped
        _run_stopped = False
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
            _run_proc = proc
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
            _run_proc = None
            # Subprocess deletes cfg_path on startup; clean up here if it didn't.
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
        sid = config.get("SPREADSHEET_ID", "")
        if _run_stopped:
            yield f"data: {json.dumps({'type': 'stopped'})}\n\n"
        elif proc.returncode == 0:
            # Final check: if the spreadsheet ended up in Drive trash (e.g. user
            # deleted it during the run), restore it so the "Open Spreadsheet"
            # link isn't broken. Best-effort — silent on failure.
            google_token = _dec(session.get("google_token", ""))
            if sid and google_token:
                _untrash_spreadsheet(sid, google_token)
            url = f"https://docs.google.com/spreadsheets/d/{sid}"
            yield f"data: {json.dumps({'type': 'done', 'url': url})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Process exited with code {proc.returncode}'})}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _run_proc, _run_stopped
    if _run_proc and _run_proc.poll() is None:
        _run_stopped = True
        _run_proc.terminate()
    return jsonify({"status": "ok"})


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
